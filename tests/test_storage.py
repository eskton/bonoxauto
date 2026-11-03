from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bonoxauto.models import PurchaseStatus
from bonoxauto.storage import Repository
from tests.fakes import make_task

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


async def test_task_roundtrip(repo: Repository) -> None:
    task = await repo.add_task(
        make_task(server_id="1234", nickname="Nick", next_run_at=NOW, max_price=Decimal("4.99"))
    )
    assert task.id is not None
    loaded = await repo.get_task(task.id)
    assert loaded == task
    assert loaded is not None and loaded.max_price == Decimal("4.99")

    loaded.enabled = False
    loaded.schedule = "weekly mon 10:00"
    await repo.update_task(loaded)
    assert await repo.get_task(task.id) == loaded

    assert [t.id for t in await repo.list_tasks()] == [task.id]
    assert await repo.delete_task(task.id) is True
    assert await repo.get_task(task.id) is None
    assert await repo.delete_task(task.id) is False


async def test_due_tasks_and_earliest(repo: Repository) -> None:
    due = await repo.add_task(make_task(next_run_at=NOW - timedelta(minutes=1)))
    later = await repo.add_task(make_task(next_run_at=NOW + timedelta(hours=1)))
    await repo.add_task(make_task(next_run_at=NOW - timedelta(hours=1), enabled=False))

    assert [t.id for t in await repo.due_tasks(NOW)] == [due.id]
    assert await repo.earliest_next_run() == due.next_run_at

    assert due.id is not None and later.id is not None
    await repo.set_next_run(due.id, None, enabled=False)
    assert await repo.due_tasks(NOW) == []
    assert await repo.earliest_next_run() == later.next_run_at


async def test_reserve_purchase_is_idempotent(repo: Repository) -> None:
    task = await repo.add_task(make_task())
    first = await repo.reserve_purchase(task, NOW, manual=False, now=NOW)
    second = await repo.reserve_purchase(task, NOW, manual=False, now=NOW)
    other_slot = await repo.reserve_purchase(task, NOW + timedelta(days=1), manual=False, now=NOW)
    assert first is not None
    assert second is None
    assert other_slot is not None and other_slot != first

    purchase = await repo.get_purchase(first)
    assert purchase is not None
    assert purchase.status is PurchaseStatus.PENDING
    assert purchase.task_name == "Free Fire · 100 diamantes"
    assert purchase.slot == NOW


async def test_update_purchase_fields(repo: Repository) -> None:
    task = await repo.add_task(make_task())
    purchase_id = await repo.reserve_purchase(task, NOW, manual=True, now=NOW)
    assert purchase_id is not None
    await repo.update_purchase(
        purchase_id,
        now=NOW + timedelta(seconds=5),
        status=PurchaseStatus.AWAITING_PAYMENT,
        price=Decimal("4.49"),
        currency="BRL",
        order_id="ORD-1",
        payment={"method": "pix", "code": "000201"},
    )
    purchase = await repo.get_purchase(purchase_id)
    assert purchase is not None
    assert purchase.status is PurchaseStatus.AWAITING_PAYMENT
    assert purchase.price == Decimal("4.49")
    assert purchase.payment == {"method": "pix", "code": "000201"}
    assert purchase.manual is True
    assert purchase.updated_at == NOW + timedelta(seconds=5)
    assert [p.id for p in await repo.purchases_by_status([PurchaseStatus.AWAITING_PAYMENT])] == [
        purchase_id
    ]


async def test_spent_since_counts_only_money_statuses(repo: Repository) -> None:
    task = await repo.add_task(make_task())
    cases = [
        (PurchaseStatus.COMPLETED, "4.49", "BRL", NOW),
        (PurchaseStatus.UNKNOWN, "1.00", "BRL", NOW),
        (PurchaseStatus.AWAITING_PAYMENT, "2.00", "BRL", NOW),
        (PurchaseStatus.DRY_RUN, "100", "BRL", NOW),
        (PurchaseStatus.SKIPPED, "100", "BRL", NOW),
        (PurchaseStatus.FAILED, "100", "BRL", NOW),
        (PurchaseStatus.EXPIRED, "100", "BRL", NOW),
        (PurchaseStatus.COMPLETED, "100", "MXN", NOW),
        (PurchaseStatus.COMPLETED, "100", "BRL", NOW - timedelta(days=2)),
    ]
    for index, (status, price, currency, created) in enumerate(cases):
        purchase_id = await repo.reserve_purchase(
            task, NOW + timedelta(minutes=index), manual=False, now=created
        )
        assert purchase_id is not None
        await repo.update_purchase(
            purchase_id, now=created, status=status, price=Decimal(price), currency=currency
        )
    assert await repo.spent_since("BRL", NOW - timedelta(hours=1)) == Decimal("7.49")
    assert await repo.spent_since("MXN", NOW - timedelta(hours=1)) == Decimal("100")
    assert await repo.spent_since("ARS", NOW - timedelta(hours=1)) == Decimal("0")


async def test_settings_and_limits(repo: Repository) -> None:
    assert await repo.is_dry_run() is True  # безопасное значение по умолчанию
    await repo.init_defaults(dry_run=False)
    assert await repo.is_dry_run() is False
    await repo.init_defaults(dry_run=True)  # повторная инициализация не перетирает выбор
    assert await repo.is_dry_run() is False

    assert await repo.is_paused() is False
    await repo.set_paused(True)
    assert await repo.is_paused() is True

    await repo.set_limit("day", "brl", Decimal("50"))
    await repo.set_limit("month", "BRL", Decimal("300.50"))
    assert await repo.get_limits() == {
        ("day", "BRL"): Decimal("50"),
        ("month", "BRL"): Decimal("300.50"),
    }
    await repo.set_limit("day", "BRL", None)
    assert await repo.get_limits() == {("month", "BRL"): Decimal("300.50")}


async def test_reopen_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "bot.db"
    first = await Repository.open(path)
    task = await first.add_task(make_task())
    await first.close()

    second = await Repository.open(path)  # миграции не применяются повторно
    assert await second.get_task(task.id or 0) == replace(task)
    await second.close()
