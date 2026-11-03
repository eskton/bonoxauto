"""Модели данных: задачи автопокупки и записи истории покупок."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


class PurchaseStatus(StrEnum):
    PENDING = "pending"  # идут проверки перед покупкой
    ORDERING = "ordering"  # заказ отправляется в магазин
    AWAITING_PAYMENT = "awaiting_payment"  # заказ создан, ждём оплату (Pix/boleto)
    PROCESSING = "processing"  # оплачен, ждём зачисления
    COMPLETED = "completed"  # куплено
    DRY_RUN = "dry_run"  # тестовый режим: только проверили, что купили бы
    SKIPPED = "skipped"  # пропущено: цена выше лимита, бюджет, нет в наличии, пауза
    MISSED = "missed"  # бот был выключен в момент запуска
    FAILED = "failed"  # не куплено, деньги не списаны
    EXPIRED = "expired"  # заказ создан, но оплата не поступила вовремя
    UNKNOWN = "unknown"  # непонятно, прошёл ли заказ — нужно проверить вручную


#: Деньги потрачены или могли быть потрачены — эти покупки учитываются в лимитах бюджета.
SPENDING_STATUSES = frozenset(
    {
        PurchaseStatus.ORDERING,
        PurchaseStatus.AWAITING_PAYMENT,
        PurchaseStatus.PROCESSING,
        PurchaseStatus.COMPLETED,
        PurchaseStatus.UNKNOWN,
    }
)

#: Заказ создан, но ещё не завершён — бот следит за ним.
WATCHED_STATUSES = frozenset({PurchaseStatus.AWAITING_PAYMENT, PurchaseStatus.PROCESSING})


@dataclass
class Task:
    """Задача: что купить, на какой аккаунт, по какому расписанию и не дороже чего."""

    product_ref: str
    product_title: str
    offer_id: str
    offer_title: str
    player_id: str
    schedule: str  # каноническая запись расписания, см. schedule.Schedule.to_spec()
    timezone: str
    max_price: Decimal
    currency: str
    server_id: str | None = None
    nickname: str | None = None
    payment_method: str | None = None
    payment_title: str | None = None
    enabled: bool = True
    next_run_at: datetime | None = None
    created_at: datetime | None = None
    id: int | None = None

    @property
    def title(self) -> str:
        return f"{self.product_title} · {self.offer_title}"


@dataclass
class Purchase:
    """Один запуск задачи (по расписанию или вручную) и его результат."""

    id: int
    task_id: int
    task_name: str
    slot: datetime
    manual: bool
    status: PurchaseStatus
    created_at: datetime
    updated_at: datetime
    reason: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    order_id: str | None = None
    payment: dict[str, Any] | None = None
