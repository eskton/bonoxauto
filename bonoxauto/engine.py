"""Движок покупок: проверки и лимиты, dry-run, оформление заказа, ожидание оплаты.

Главные правила безопасности:
* каждый запуск (задача + время) обрабатывается не больше одного раза — см. reserve_purchase;
* заказ отправляется в магазин ровно один раз, без автоматических повторов;
* если непонятно, прошёл ли заказ, статус UNKNOWN: деньги считаются потраченными,
  а пользователь получает просьбу проверить сайт;
* покупки выполняются строго по очереди, чтобы лимиты бюджета нельзя было обойти гонкой.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo

from . import texts
from .models import WATCHED_STATUSES, PurchaseStatus, Task, utcnow
from .shop.base import (
    GameAccount,
    Offer,
    Order,
    OrderOutcomeUnknown,
    OrderRequest,
    OrderStatus,
    PaymentInfo,
    ShopClient,
    ShopError,
    TemporaryShopError,
)
from .storage import Repository

log = logging.getLogger(__name__)
T = TypeVar("T")


class Notifier(Protocol):
    async def send(
        self, text: str, *, links: Sequence[tuple[str, str]] = (), silent: bool = False
    ) -> None: ...


_ORDER_TO_PURCHASE = {
    OrderStatus.AWAITING_PAYMENT: PurchaseStatus.AWAITING_PAYMENT,
    OrderStatus.PROCESSING: PurchaseStatus.PROCESSING,
    OrderStatus.COMPLETED: PurchaseStatus.COMPLETED,
    OrderStatus.FAILED: PurchaseStatus.FAILED,
    OrderStatus.CANCELLED: PurchaseStatus.FAILED,
}


@dataclass
class Outcome:
    status: PurchaseStatus
    reason: str | None = None
    offer: Offer | None = None
    order: Order | None = None


def period_start(now: datetime, tz: ZoneInfo, period: str) -> datetime:
    """Начало текущих суток ('day') или месяца ('month') в часовом поясе tz."""
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.replace(day=1) if period == "month" else start


class PurchaseEngine:
    def __init__(
        self,
        repo: Repository,
        shop: ShopClient,
        notifier: Notifier,
        *,
        tz: ZoneInfo,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        payment_timeout: timedelta = timedelta(minutes=30),
        poll_interval: float = 20.0,
        read_attempts: int = 3,
        retry_delay: float = 3.0,
    ) -> None:
        self.repo = repo
        self.shop = shop
        self.notifier = notifier
        self.tz = tz
        self.clock = clock
        self.sleep = sleep
        self.payment_timeout = payment_timeout
        self.poll_interval = poll_interval
        self.read_attempts = read_attempts
        self.retry_delay = retry_delay
        self._lock = asyncio.Lock()
        self._jobs: set[asyncio.Task[Any]] = set()
        self._watchers: dict[int, asyncio.Task[None]] = {}

    # ---- запуск в фоне ---------------------------------------------------

    def run_in_background(
        self, task_id: int, slot: datetime, *, manual: bool = False
    ) -> asyncio.Task[int | None]:
        return self._spawn(self.run_task(task_id, slot, manual=manual))

    def missed_in_background(self, task: Task, slot: datetime) -> asyncio.Task[None]:
        return self._spawn(self.record_missed(task, slot))

    def _spawn(self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        job = asyncio.create_task(coro)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        return job

    async def drain(self) -> None:
        """Дождаться завершения текущих покупок (не наблюдения за оплатой)."""
        # Смотрим на done(), а не на пустоту множества: колбэки удаления выполняются
        # циклом событий позже, а gather() из уже завершённых задач не уступает управление.
        while pending := [job for job in self._jobs if not job.done()]:
            await asyncio.wait(pending)

    async def wait_watchers(self) -> None:
        """Дождаться завершения наблюдения за оплатой (для тестов и отладки)."""
        while pending := [job for job in self._watchers.values() if not job.done()]:
            await asyncio.wait(pending)

    async def shutdown(self, grace_seconds: float = 15.0) -> None:
        """Дать текущим покупкам завершиться, затем остановить всё фоновое."""
        if running := [job for job in self._jobs if not job.done()]:
            await asyncio.wait(running, timeout=grace_seconds)
        pending = [job for job in (*self._jobs, *self._watchers.values()) if not job.done()]
        for job in pending:
            job.cancel()
        if pending:
            await asyncio.wait(pending)

    # ---- покупка ---------------------------------------------------------

    async def run_task(self, task_id: int, slot: datetime, *, manual: bool = False) -> int | None:
        """Выполнить запуск задачи. Возвращает id записи в истории или None, если запуск
        уже обрабатывался (или задача удалена)."""
        task = await self.repo.get_task(task_id)
        if task is None:
            log.warning("Задача #%s не найдена — запуск пропущен", task_id)
            return None
        async with self._lock:
            purchase_id = await self.repo.reserve_purchase(
                task, slot, manual=manual, now=self.clock()
            )
            if purchase_id is None:
                log.info("Запуск задачи #%s на %s уже обработан", task_id, slot)
                return None
            outcome = await self._attempt(task, purchase_id, manual=manual)
            await self._save(purchase_id, outcome)
        log.info("Задача #%s: %s %s", task_id, outcome.status, outcome.reason or "")
        await self._report(task, outcome, manual=manual)
        if outcome.status in WATCHED_STATUSES and outcome.order is not None:
            deadline = self._deadline(outcome.order.payment)
            self._watch(purchase_id, task.title, outcome.order.id, outcome.status, deadline)
        return purchase_id

    async def _attempt(self, task: Task, purchase_id: int, *, manual: bool) -> Outcome:
        try:
            prepared = await self._prepare(task, manual=manual)
        except Exception as exc:
            log.exception("Сбой при подготовке покупки по задаче #%s", task.id)
            return Outcome(PurchaseStatus.FAILED, f"внутренняя ошибка: {exc!r}")
        if isinstance(prepared, Outcome):
            return prepared
        if await self.repo.is_dry_run():
            return Outcome(PurchaseStatus.DRY_RUN, offer=prepared)
        try:
            return await self._submit(task, purchase_id, prepared)
        except Exception as exc:
            log.exception("Сбой при оформлении заказа по задаче #%s", task.id)
            return Outcome(
                PurchaseStatus.UNKNOWN, f"сбой во время оформления: {exc!r}", offer=prepared
            )

    async def _prepare(self, task: Task, *, manual: bool) -> Offer | Outcome:
        if not manual and await self.repo.is_paused():
            return Outcome(PurchaseStatus.SKIPPED, "все автопокупки на паузе")
        try:
            product = await self._read(lambda: self.shop.get_product(task.product_ref))
        except ShopError as exc:
            return Outcome(PurchaseStatus.FAILED, f"не удалось получить товар: {exc}")
        offer = product.offer(task.offer_id)
        if offer is None:
            return Outcome(
                PurchaseStatus.SKIPPED, f"пакет «{task.offer_title}» больше не продаётся"
            )
        if not offer.available:
            return Outcome(PurchaseStatus.SKIPPED, "нет в наличии", offer=offer)
        if offer.currency != task.currency:
            return Outcome(
                PurchaseStatus.SKIPPED,
                f"цена теперь в {offer.currency}, а лимит задан в {task.currency}",
                offer=offer,
            )
        if offer.price > task.max_price:
            return Outcome(
                PurchaseStatus.SKIPPED,
                f"цена {texts.money(offer.price, offer.currency)} выше лимита "
                f"{texts.money(task.max_price, task.currency)}",
                offer=offer,
            )
        problem = await self._budget_problem(offer.price, offer.currency)
        if problem:
            return Outcome(PurchaseStatus.SKIPPED, problem, offer=offer)
        return offer

    async def _submit(self, task: Task, purchase_id: int, offer: Offer) -> Outcome:
        # Сначала помечаем «оформляется»: если бот упадёт посреди запроса, после
        # перезапуска эта покупка станет UNKNOWN, а не будет повторена.
        await self.repo.update_purchase(
            purchase_id,
            now=self.clock(),
            status=PurchaseStatus.ORDERING,
            price=offer.price,
            currency=offer.currency,
        )
        request = OrderRequest(
            product_ref=task.product_ref,
            offer_id=offer.id,
            account=GameAccount(task.player_id, task.server_id),
            max_total=task.max_price,
            currency=task.currency,
            payment_method=task.payment_method,
            idempotency_key=f"bonoxauto-{purchase_id}",
        )
        try:
            order = await self.shop.place_order(request)
        except OrderOutcomeUnknown as exc:
            return Outcome(
                PurchaseStatus.UNKNOWN, str(exc) or "нет ответа от магазина", offer=offer
            )
        except TemporaryShopError as exc:
            # Временная ошибка на запросе с деньгами — заказ мог успеть создаться.
            return Outcome(
                PurchaseStatus.UNKNOWN, f"временная ошибка при оформлении: {exc}", offer=offer
            )
        except ShopError as exc:
            return Outcome(PurchaseStatus.FAILED, str(exc) or "магазин отклонил заказ", offer=offer)
        status = _ORDER_TO_PURCHASE[order.status]
        reason = (
            (order.message or "магазин отклонил заказ") if status is PurchaseStatus.FAILED else None
        )
        return Outcome(status, reason, offer=offer, order=order)

    async def _budget_problem(self, price: Decimal, currency: str) -> str | None:
        limits = await self.repo.get_limits()
        now = self.clock()
        for period, label in (("day", "дневной"), ("month", "месячный")):
            limit = limits.get((period, currency))
            if limit is None:
                continue
            spent = await self.repo.spent_since(currency, period_start(now, self.tz, period))
            if spent + price > limit:
                return (
                    f"{label} лимит: потрачено {texts.money(spent, currency)} "
                    f"из {texts.money(limit, currency)}"
                )
        return None

    async def spent(self, currency: str) -> tuple[Decimal, Decimal]:
        """Потрачено за сегодня и за текущий месяц."""
        now = self.clock()
        day = await self.repo.spent_since(currency, period_start(now, self.tz, "day"))
        month = await self.repo.spent_since(currency, period_start(now, self.tz, "month"))
        return day, month

    async def _read(self, call: Callable[[], Awaitable[T]]) -> T:
        """Чтение из магазина с повторами при временных ошибках."""
        attempt = 1
        while True:
            try:
                return await call()
            except TemporaryShopError as exc:
                if attempt >= self.read_attempts:
                    raise
                log.warning("Временная ошибка магазина: %s — повтор %s", exc, attempt)
                await self.sleep(self.retry_delay * attempt)
                attempt += 1

    async def _save(self, purchase_id: int, outcome: Outcome) -> None:
        fields: dict[str, Any] = {"status": outcome.status, "reason": outcome.reason}
        if outcome.offer is not None:
            fields["price"] = outcome.offer.price
            fields["currency"] = outcome.offer.currency
        order = outcome.order
        if order is not None:
            fields["order_id"] = order.id
            if order.total is not None:
                fields["price"] = order.total
                fields["currency"] = order.currency or fields.get("currency")
            if order.payment is not None:
                fields["payment"] = order.payment.to_dict()
        await self.repo.update_purchase(purchase_id, now=self.clock(), **fields)

    async def _report(self, task: Task, outcome: Outcome, *, manual: bool) -> None:
        price: Decimal | None = outcome.offer.price if outcome.offer else None
        currency: str | None = outcome.offer.currency if outcome.offer else None
        order = outcome.order
        if order is not None and order.total is not None:
            price, currency = order.total, order.currency or currency
        text = texts.purchase_report(
            title=task.title,
            account=texts.account_label(task.player_id, task.server_id, task.nickname),
            status=outcome.status,
            reason=outcome.reason,
            price=price,
            currency=currency,
            limit=task.max_price,
            order=order,
            manual=manual,
            tz=self.tz,
        )
        links: list[tuple[str, str]] = []
        if order is not None and order.payment is not None and order.payment.url:
            links.append(("💳 Оплатить", order.payment.url))
        elif order is not None and order.url:
            links.append(("🧾 Заказ на сайте", order.url))
        await self._notify(text, links=links, silent=outcome.status is PurchaseStatus.DRY_RUN)

    async def _notify(
        self, text: str, *, links: Sequence[tuple[str, str]] = (), silent: bool = False
    ) -> None:
        try:
            await self.notifier.send(text, links=links, silent=silent)
        except Exception:
            log.exception("Не удалось отправить уведомление")

    # ---- пропущенные запуски --------------------------------------------

    async def record_missed(self, task: Task, slot: datetime) -> None:
        now = self.clock()
        reason = f"бот был недоступен (опоздание {texts.duration(now - slot)})"
        async with self._lock:
            purchase_id = await self.repo.reserve_purchase(task, slot, manual=False, now=now)
            if purchase_id is None:
                return
            await self.repo.update_purchase(
                purchase_id, now=now, status=PurchaseStatus.MISSED, reason=reason
            )
        await self._notify(texts.missed_report(task.title, slot, reason, ZoneInfo(task.timezone)))

    # ---- ожидание оплаты / зачисления -----------------------------------

    def _deadline(self, payment: PaymentInfo | None) -> datetime:
        if payment is not None and payment.expires_at is not None:
            return payment.expires_at
        return self.clock() + self.payment_timeout

    def _watch(
        self,
        purchase_id: int,
        title: str,
        order_id: str,
        status: PurchaseStatus,
        deadline: datetime,
    ) -> None:
        if purchase_id in self._watchers:
            return
        watcher = asyncio.create_task(
            self._watch_order(purchase_id, title, order_id, status, deadline)
        )
        self._watchers[purchase_id] = watcher
        watcher.add_done_callback(lambda _: self._watchers.pop(purchase_id, None))

    async def _watch_order(
        self,
        purchase_id: int,
        title: str,
        order_id: str,
        status: PurchaseStatus,
        deadline: datetime,
    ) -> None:
        while True:
            await self.sleep(self.poll_interval)
            try:
                order = await self.shop.get_order(order_id)
            except Exception as exc:
                log.warning("Не удалось проверить заказ %s: %s", order_id, exc)
            else:
                new_status = _ORDER_TO_PURCHASE[order.status]
                if new_status is not status:
                    reason = None
                    if new_status is PurchaseStatus.FAILED:
                        reason = order.message or "магазин отменил заказ"
                    await self.repo.update_purchase(
                        purchase_id, now=self.clock(), status=new_status, reason=reason
                    )
                    await self._notify(texts.order_update(title, order_id, new_status, reason))
                    if new_status not in WATCHED_STATUSES:
                        return
                    if status is PurchaseStatus.AWAITING_PAYMENT:
                        # Оплата прошла — теперь ждём зачисления.
                        deadline = max(deadline, self.clock() + self.payment_timeout)
                    status = new_status
            if self.clock() >= deadline:
                if status is PurchaseStatus.AWAITING_PAYMENT:
                    final, reason = PurchaseStatus.EXPIRED, "оплата не поступила вовремя"
                else:
                    final, reason = (
                        PurchaseStatus.UNKNOWN,
                        "заказ оплачен, но зачисление не подтвердилось",
                    )
                await self.repo.update_purchase(
                    purchase_id, now=self.clock(), status=final, reason=reason
                )
                await self._notify(texts.order_update(title, order_id, final, reason))
                return

    # ---- восстановление после перезапуска -------------------------------

    async def recover(self) -> None:
        """Разобрать покупки, прерванные перезапуском, и продолжить ожидание оплаты.

        Вызывать при старте, до запуска планировщика.
        """
        now = self.clock()
        interrupted = await self.repo.purchases_by_status(
            [PurchaseStatus.PENDING, PurchaseStatus.ORDERING]
        )
        for purchase in interrupted:
            if purchase.status is PurchaseStatus.PENDING:
                status = PurchaseStatus.FAILED
                reason = "прервано перезапуском бота (заказ не отправлялся)"
            else:
                status = PurchaseStatus.UNKNOWN
                reason = "бот перезапустился во время оформления заказа"
            await self.repo.update_purchase(purchase.id, now=now, status=status, reason=reason)
            await self._notify(
                texts.order_update(purchase.task_name, purchase.order_id, status, reason)
            )
        for purchase in await self.repo.purchases_by_status(WATCHED_STATUSES):
            if not purchase.order_id:
                continue
            payment = PaymentInfo.from_dict(purchase.payment) if purchase.payment else None
            if payment is not None and payment.expires_at is not None:
                deadline = payment.expires_at
            else:
                deadline = purchase.updated_at + self.payment_timeout
            self._watch(
                purchase.id,
                purchase.task_name,
                purchase.order_id,
                purchase.status,
                max(deadline, now),
            )
