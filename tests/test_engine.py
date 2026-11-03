from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from bonoxauto.engine import PurchaseEngine
from bonoxauto.models import PurchaseStatus, Task
from bonoxauto.shop.base import (
    Order,
    OrderOutcomeUnknown,
    OrderStatus,
    PaymentInfo,
    ShopError,
    TemporaryShopError,
)
from bonoxauto.storage import Repository
from tests.fakes import FakeClock, FakeSleep, RecordingNotifier, ScriptedShop, make_task


@dataclass
class Env:
    engine: PurchaseEngine
    repo: Repository
    shop: ScriptedShop
    notifier: RecordingNotifier
    clock: FakeClock
    sleep: FakeSleep
    task: Task

    async def run(self, *, minutes_later: int = 0, manual: bool = False) -> PurchaseStatus:
        assert self.task.id is not None
        purchase_id = await self.engine.run_task(
            self.task.id, self.clock.now + timedelta(minutes=minutes_later), manual=manual
        )
        assert purchase_id is not None
        purchase = await self.repo.get_purchase(purchase_id)
        assert purchase is not None
        return purchase.status


@pytest.fixture
async def env(repo: Repository, clock: FakeClock) -> AsyncIterator[Env]:
    shop = ScriptedShop()
    notifier = RecordingNotifier()
    sleep = FakeSleep(clock)
    engine = PurchaseEngine(
        repo,
        shop,
        notifier,
        tz=ZoneInfo("UTC"),
        clock=clock,
        sleep=sleep,
        payment_timeout=timedelta(minutes=5),
        poll_interval=10,
        retry_delay=1,
    )
    await repo.init_defaults(dry_run=False)
    task = await repo.add_task(make_task())
    yield Env(engine, repo, shop, notifier, clock, sleep, task)
    await engine.shutdown()


async def test_live_purchase(env: Env) -> None:
    assert await env.run() is PurchaseStatus.COMPLETED
    [request] = env.shop.placed
    assert request.max_total == Decimal("5.00")
    assert request.account.player_id == "123456789"
    assert request.payment_method == "balance"
    assert request.idempotency_key.startswith("bonoxauto-")
    [purchase] = await env.repo.recent_purchases()
    assert purchase.order_id == "ORD-1"
    assert purchase.price == Decimal("4.49")
    assert "Куплено" in env.notifier.last
    assert "ORD-1" in env.notifier.last


async def test_dry_run_never_orders(env: Env) -> None:
    await env.repo.set_dry_run(True)
    assert await env.run() is PurchaseStatus.DRY_RUN
    assert env.shop.placed == []
    assert "Dry-run" in env.notifier.last
    assert "4,49" in env.notifier.last


async def test_price_above_limit_is_skipped(env: Env) -> None:
    env.shop.set_offer(price=Decimal("5.01"))
    assert await env.run() is PurchaseStatus.SKIPPED
    assert env.shop.placed == []
    assert "выше лимита" in env.notifier.last


async def test_unavailable_or_missing_offer_is_skipped(env: Env) -> None:
    env.shop.set_offer(available=False)
    assert await env.run() is PurchaseStatus.SKIPPED
    env.shop.set_offer(id="other", available=True)
    assert await env.run(minutes_later=1) is PurchaseStatus.SKIPPED
    assert "больше не продаётся" in env.notifier.last
    assert env.shop.placed == []


async def test_daily_budget(env: Env) -> None:
    await env.repo.set_limit("day", "BRL", Decimal("8.00"))
    assert await env.run() is PurchaseStatus.COMPLETED
    assert await env.run(minutes_later=1) is PurchaseStatus.SKIPPED
    assert "дневной лимит" in env.notifier.last
    assert len(env.shop.placed) == 1
    # на следующий день бюджет снова доступен
    env.clock.advance(days=1)
    assert await env.run() is PurchaseStatus.COMPLETED


async def test_monthly_budget_counts_unknown_outcomes(env: Env) -> None:
    await env.repo.set_limit("month", "BRL", Decimal("5.00"))
    env.shop.order_result = OrderOutcomeUnknown("таймаут")
    assert await env.run() is PurchaseStatus.UNKNOWN
    env.shop.order_result = Order("ORD-2", OrderStatus.COMPLETED, Decimal("4.49"), "BRL")
    assert await env.run(minutes_later=1) is PurchaseStatus.SKIPPED
    assert "месячный лимит" in env.notifier.last


async def test_same_slot_is_processed_once(env: Env) -> None:
    assert env.task.id is not None
    slot = env.clock.now
    assert await env.engine.run_task(env.task.id, slot) is not None
    assert await env.engine.run_task(env.task.id, slot) is None
    assert len(env.shop.placed) == 1


async def test_unknown_outcome_is_not_retried(env: Env) -> None:
    env.shop.order_result = OrderOutcomeUnknown("нет ответа за 30 с")
    assert await env.run() is PurchaseStatus.UNKNOWN
    assert len(env.shop.placed) == 1
    assert "проверьте историю заказов" in env.notifier.last


async def test_temporary_error_while_ordering_is_unknown(env: Env) -> None:
    env.shop.order_result = TemporaryShopError("502")
    assert await env.run() is PurchaseStatus.UNKNOWN
    assert len(env.shop.placed) == 1


async def test_rejected_order_is_failed(env: Env) -> None:
    env.shop.order_result = ShopError("недостаточно средств")
    assert await env.run() is PurchaseStatus.FAILED
    assert "недостаточно средств" in env.notifier.last


async def test_read_errors_are_retried(env: Env) -> None:
    env.shop.product_errors = [TemporaryShopError("502"), TemporaryShopError("503")]
    assert await env.run() is PurchaseStatus.COMPLETED
    assert env.sleep.calls == [1, 2]


async def test_read_errors_give_up(env: Env) -> None:
    env.shop.product_errors = [TemporaryShopError("502")] * 3
    assert await env.run() is PurchaseStatus.FAILED
    assert env.shop.placed == []


async def test_pause_skips_scheduled_but_not_manual(env: Env) -> None:
    await env.repo.set_paused(True)
    assert await env.run() is PurchaseStatus.SKIPPED
    assert await env.run(minutes_later=1, manual=True) is PurchaseStatus.COMPLETED


async def test_waits_for_payment_then_completes(env: Env) -> None:
    payment = PaymentInfo(method="Pix", code="PIX-CODE-1", url="https://pay.example/1")
    env.shop.order_result = Order(
        "ORD-9", OrderStatus.AWAITING_PAYMENT, Decimal("4.49"), "BRL", payment=payment
    )
    env.shop.order_updates = [
        Order("ORD-9", OrderStatus.AWAITING_PAYMENT),
        ShopError("сайт недоступен"),
        Order("ORD-9", OrderStatus.PROCESSING),
        Order("ORD-9", OrderStatus.COMPLETED),
    ]
    assert env.task.id is not None
    await env.engine.run_task(env.task.id, env.clock.now)
    # Первое сообщение — итог оформления (наблюдение за оплатой стартует после него).
    report = env.notifier.messages[0]
    assert "нужна оплата" in report
    assert "PIX-CODE-1" in report
    assert env.notifier.links[0] == [("💳 Оплатить", "https://pay.example/1")]

    await env.engine.wait_watchers()
    [purchase] = await env.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.COMPLETED
    assert len(env.notifier.messages) == 3
    assert "Оплачено" in env.notifier.messages[1]
    assert "Куплено" in env.notifier.messages[2]


async def test_payment_timeout_expires(env: Env) -> None:
    env.shop.order_result = Order(
        "ORD-5", OrderStatus.AWAITING_PAYMENT, Decimal("4.49"), "BRL", payment=PaymentInfo("Pix")
    )
    env.shop.order_updates = [Order("ORD-5", OrderStatus.AWAITING_PAYMENT)]
    assert env.task.id is not None
    await env.engine.run_task(env.task.id, env.clock.now)
    await env.engine.wait_watchers()
    [purchase] = await env.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.EXPIRED
    assert env.shop.get_order_calls == 30  # 5 минут / 10 секунд
    assert "Оплата не поступила" in env.notifier.last
    # просроченный заказ не занимает бюджет
    assert await env.repo.spent_since("BRL", env.clock.now - timedelta(days=1)) == 0


async def test_recover_after_restart(env: Env) -> None:
    now = env.clock.now
    pending = await env.repo.reserve_purchase(env.task, now, manual=False, now=now)
    ordering = await env.repo.reserve_purchase(
        env.task, now + timedelta(minutes=1), manual=False, now=now
    )
    waiting = await env.repo.reserve_purchase(
        env.task, now + timedelta(minutes=2), manual=False, now=now
    )
    assert pending and ordering and waiting
    await env.repo.update_purchase(ordering, now=now, status=PurchaseStatus.ORDERING)
    await env.repo.update_purchase(
        waiting, now=now, status=PurchaseStatus.AWAITING_PAYMENT, order_id="ORD-7"
    )
    env.shop.order_updates = [Order("ORD-7", OrderStatus.COMPLETED)]

    await env.engine.recover()
    await env.engine.wait_watchers()

    statuses = {p.id: p.status for p in await env.repo.recent_purchases()}
    assert statuses == {
        pending: PurchaseStatus.FAILED,
        ordering: PurchaseStatus.UNKNOWN,
        waiting: PurchaseStatus.COMPLETED,
    }


async def test_record_missed(env: Env) -> None:
    slot = env.clock.now - timedelta(hours=2, minutes=5)
    await env.engine.record_missed(env.task, slot)
    await env.engine.record_missed(env.task, slot)  # повторно не записывается
    [purchase] = await env.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.MISSED
    assert purchase.reason == "бот был недоступен (опоздание 2 ч 5 мин)"
    assert len(env.notifier.messages) == 1
    assert env.shop.placed == []


async def test_deleted_task_is_ignored(env: Env) -> None:
    assert env.task.id is not None
    await env.repo.delete_task(env.task.id)
    assert await env.engine.run_task(env.task.id, env.clock.now) is None
    assert env.shop.placed == []
