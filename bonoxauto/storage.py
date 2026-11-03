"""Хранилище SQLite: задачи, история покупок, настройки бота."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from .models import SPENDING_STATUSES, Purchase, PurchaseStatus, Task, utcnow

_MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE tasks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        product_ref    TEXT    NOT NULL,
        product_title  TEXT    NOT NULL,
        offer_id       TEXT    NOT NULL,
        offer_title    TEXT    NOT NULL,
        player_id      TEXT    NOT NULL,
        server_id      TEXT,
        nickname       TEXT,
        schedule       TEXT    NOT NULL,
        timezone       TEXT    NOT NULL,
        max_price      TEXT    NOT NULL,
        currency       TEXT    NOT NULL,
        payment_method TEXT,
        payment_title  TEXT,
        enabled        INTEGER NOT NULL DEFAULT 1,
        next_run_at    TEXT,
        created_at     TEXT    NOT NULL
    );
    CREATE INDEX tasks_due ON tasks (enabled, next_run_at);

    CREATE TABLE purchases (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id    INTEGER NOT NULL,
        task_name  TEXT    NOT NULL,
        slot       TEXT    NOT NULL,
        manual     INTEGER NOT NULL DEFAULT 0,
        status     TEXT    NOT NULL,
        reason     TEXT,
        price      TEXT,
        currency   TEXT,
        order_id   TEXT,
        payment    TEXT,
        created_at TEXT    NOT NULL,
        updated_at TEXT    NOT NULL,
        UNIQUE (task_id, slot)
    );
    CREATE INDEX purchases_status ON purchases (status);
    CREATE INDEX purchases_created ON purchases (created_at);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
)

_TASK_FIELDS = (
    "product_ref",
    "product_title",
    "offer_id",
    "offer_title",
    "player_id",
    "server_id",
    "nickname",
    "schedule",
    "timezone",
    "max_price",
    "currency",
    "payment_method",
    "payment_title",
    "enabled",
    "next_run_at",
    "created_at",
)
_PURCHASE_UPDATABLE = frozenset({"status", "reason", "price", "currency", "order_id", "payment"})
_LIMIT_PERIODS = ("day", "month")


def _to_db(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("ожидается datetime с часовым поясом")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _from_db(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Repository:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def open(cls, path: str | Path) -> Repository:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        repo = cls(db)
        await repo._migrate()
        return repo

    async def close(self) -> None:
        await self._db.close()

    async def _migrate(self) -> None:
        async with self._db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        version = row[0] if row else 0
        for number, script in enumerate(_MIGRATIONS, start=1):
            if version < number:
                await self._db.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                )

    # ---- задачи ----------------------------------------------------------

    async def add_task(self, task: Task) -> Task:
        if task.created_at is None:
            task = replace(task, created_at=utcnow())
        columns = ", ".join(_TASK_FIELDS)
        marks = ", ".join("?" for _ in _TASK_FIELDS)
        cursor = await self._db.execute(
            f"INSERT INTO tasks ({columns}) VALUES ({marks})", _task_values(task)
        )
        await self._db.commit()
        return replace(task, id=cursor.lastrowid)

    async def update_task(self, task: Task) -> None:
        if task.id is None:
            raise ValueError("у задачи нет id")
        assignments = ", ".join(f"{name} = ?" for name in _TASK_FIELDS)
        await self._db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?", (*_task_values(task), task.id)
        )
        await self._db.commit()

    async def get_task(self, task_id: int) -> Task | None:
        async with self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cursor:
            row = await cursor.fetchone()
        return _task_from_row(row) if row else None

    async def list_tasks(self) -> list[Task]:
        async with self._db.execute("SELECT * FROM tasks ORDER BY id") as cursor:
            rows = await cursor.fetchall()
        return [_task_from_row(row) for row in rows]

    async def delete_task(self, task_id: int) -> bool:
        cursor = await self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await self._db.commit()
        return cursor.rowcount > 0

    async def set_next_run(
        self, task_id: int, next_run_at: datetime | None, *, enabled: bool | None = None
    ) -> None:
        if enabled is None:
            await self._db.execute(
                "UPDATE tasks SET next_run_at = ? WHERE id = ?", (_to_db(next_run_at), task_id)
            )
        else:
            await self._db.execute(
                "UPDATE tasks SET next_run_at = ?, enabled = ? WHERE id = ?",
                (_to_db(next_run_at), int(enabled), task_id),
            )
        await self._db.commit()

    async def due_tasks(self, now: datetime) -> list[Task]:
        async with self._db.execute(
            "SELECT * FROM tasks WHERE enabled = 1 AND next_run_at IS NOT NULL"
            " AND next_run_at <= ? ORDER BY next_run_at, id",
            (_to_db(now),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_task_from_row(row) for row in rows]

    async def earliest_next_run(self) -> datetime | None:
        async with self._db.execute(
            "SELECT MIN(next_run_at) FROM tasks WHERE enabled = 1 AND next_run_at IS NOT NULL"
        ) as cursor:
            row = await cursor.fetchone()
        return _from_db(row[0]) if row else None

    # ---- покупки ---------------------------------------------------------

    async def reserve_purchase(
        self, task: Task, slot: datetime, *, manual: bool, now: datetime
    ) -> int | None:
        """Занять запуск (задача + время). Вернёт None, если этот запуск уже обрабатывался.

        Уникальность пары (task_id, slot) — главная защита от двойной покупки
        при перезапусках и гонках.
        """
        cursor = await self._db.execute(
            "INSERT OR IGNORE INTO purchases"
            " (task_id, task_name, slot, manual, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task.id,
                task.title,
                _to_db(slot),
                int(manual),
                PurchaseStatus.PENDING.value,
                _to_db(now),
                _to_db(now),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid if cursor.rowcount == 1 else None

    async def update_purchase(self, purchase_id: int, *, now: datetime, **fields: Any) -> None:
        unknown = set(fields) - _PURCHASE_UPDATABLE
        if unknown:
            raise TypeError(f"неизвестные поля покупки: {sorted(unknown)}")
        values: list[Any] = []
        for key, value in fields.items():
            if value is not None:
                if key == "status":
                    value = PurchaseStatus(value).value
                elif key == "price":
                    value = str(value)
                elif key == "payment":
                    value = json.dumps(value, ensure_ascii=False)
            values.append(value)
        assignments = ", ".join([*(f"{key} = ?" for key in fields), "updated_at = ?"])
        await self._db.execute(
            f"UPDATE purchases SET {assignments} WHERE id = ?",
            (*values, _to_db(now), purchase_id),
        )
        await self._db.commit()

    async def get_purchase(self, purchase_id: int) -> Purchase | None:
        async with self._db.execute(
            "SELECT * FROM purchases WHERE id = ?", (purchase_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _purchase_from_row(row) if row else None

    async def recent_purchases(self, limit: int = 15) -> list[Purchase]:
        async with self._db.execute(
            "SELECT * FROM purchases ORDER BY id DESC LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
        return [_purchase_from_row(row) for row in rows]

    async def purchases_by_status(self, statuses: Iterable[PurchaseStatus]) -> list[Purchase]:
        values = sorted(PurchaseStatus(s).value for s in statuses)
        marks = ", ".join("?" for _ in values)
        async with self._db.execute(
            f"SELECT * FROM purchases WHERE status IN ({marks}) ORDER BY id", values
        ) as cursor:
            rows = await cursor.fetchall()
        return [_purchase_from_row(row) for row in rows]

    async def spent_since(self, currency: str, since: datetime) -> Decimal:
        """Сколько потрачено (или могло быть потрачено) в валюте начиная с момента `since`."""
        statuses = sorted(s.value for s in SPENDING_STATUSES)
        marks = ", ".join("?" for _ in statuses)
        async with self._db.execute(
            "SELECT price FROM purchases WHERE currency = ? AND created_at >= ?"
            f" AND price IS NOT NULL AND status IN ({marks})",
            (currency, _to_db(since), *statuses),
        ) as cursor:
            rows = await cursor.fetchall()
        return sum((Decimal(row["price"]) for row in rows), Decimal("0"))

    # ---- настройки -------------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._db.commit()

    async def delete_setting(self, key: str) -> None:
        await self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await self._db.commit()

    async def init_defaults(self, *, dry_run: bool) -> None:
        """Значения при самом первом запуске; дальше они меняются только из бота."""
        await self._db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES ('dry_run', ?)",
            ("1" if dry_run else "0",),
        )
        await self._db.commit()

    async def is_dry_run(self) -> bool:
        # Нет записи — считаем, что dry-run включён: безопасное значение по умолчанию.
        return await self.get_setting("dry_run") != "0"

    async def set_dry_run(self, enabled: bool) -> None:
        await self.set_setting("dry_run", "1" if enabled else "0")

    async def is_paused(self) -> bool:
        return await self.get_setting("paused") == "1"

    async def set_paused(self, paused: bool) -> None:
        await self.set_setting("paused", "1" if paused else "0")

    async def get_limits(self) -> dict[tuple[str, str], Decimal]:
        """Лимиты бюджета: {(период, валюта): сумма}, период — 'day' или 'month'."""
        async with self._db.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'limit:%'"
        ) as cursor:
            rows = await cursor.fetchall()
        limits: dict[tuple[str, str], Decimal] = {}
        for row in rows:
            _, period, currency = row["key"].split(":", 2)
            limits[(period, currency)] = Decimal(row["value"])
        return limits

    async def set_limit(self, period: str, currency: str, amount: Decimal | None) -> None:
        if period not in _LIMIT_PERIODS:
            raise ValueError(f"неизвестный период {period!r}")
        key = f"limit:{period}:{currency.upper()}"
        if amount is None:
            await self.delete_setting(key)
        else:
            await self.set_setting(key, str(amount))


def _task_values(task: Task) -> tuple[Any, ...]:
    return (
        task.product_ref,
        task.product_title,
        task.offer_id,
        task.offer_title,
        task.player_id,
        task.server_id,
        task.nickname,
        task.schedule,
        task.timezone,
        str(task.max_price),
        task.currency,
        task.payment_method,
        task.payment_title,
        int(task.enabled),
        _to_db(task.next_run_at),
        _to_db(task.created_at),
    )


def _task_from_row(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        product_ref=row["product_ref"],
        product_title=row["product_title"],
        offer_id=row["offer_id"],
        offer_title=row["offer_title"],
        player_id=row["player_id"],
        server_id=row["server_id"],
        nickname=row["nickname"],
        schedule=row["schedule"],
        timezone=row["timezone"],
        max_price=Decimal(row["max_price"]),
        currency=row["currency"],
        payment_method=row["payment_method"],
        payment_title=row["payment_title"],
        enabled=bool(row["enabled"]),
        next_run_at=_from_db(row["next_run_at"]),
        created_at=_from_db(row["created_at"]),
    )


def _purchase_from_row(row: aiosqlite.Row) -> Purchase:
    created_at = _from_db(row["created_at"])
    updated_at = _from_db(row["updated_at"])
    slot = _from_db(row["slot"])
    assert created_at is not None and updated_at is not None and slot is not None
    return Purchase(
        id=row["id"],
        task_id=row["task_id"],
        task_name=row["task_name"],
        slot=slot,
        manual=bool(row["manual"]),
        status=PurchaseStatus(row["status"]),
        created_at=created_at,
        updated_at=updated_at,
        reason=row["reason"],
        price=Decimal(row["price"]) if row["price"] is not None else None,
        currency=row["currency"],
        order_id=row["order_id"],
        payment=json.loads(row["payment"]) if row["payment"] else None,
    )
