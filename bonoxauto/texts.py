"""Форматирование сообщений бота (HTML-разметка Telegram)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo

from .models import Purchase, PurchaseStatus, Task
from .schedule import WEEKDAY_SHORT_RU, parse_schedule
from .shop.base import Order, PaymentInfo

NBSP = " "
MESSAGE_LIMIT = 4096
CENT = Decimal("0.01")

STATUS_ICON = {
    PurchaseStatus.PENDING: "⏳",
    PurchaseStatus.ORDERING: "⏳",
    PurchaseStatus.AWAITING_PAYMENT: "💳",
    PurchaseStatus.PROCESSING: "🚚",
    PurchaseStatus.COMPLETED: "✅",
    PurchaseStatus.DRY_RUN: "🧪",
    PurchaseStatus.SKIPPED: "⏭",
    PurchaseStatus.MISSED: "⏰",
    PurchaseStatus.FAILED: "❌",
    PurchaseStatus.EXPIRED: "⌛",
    PurchaseStatus.UNKNOWN: "❓",
}

STATUS_TITLE = {
    PurchaseStatus.PENDING: "В процессе",
    PurchaseStatus.ORDERING: "Оформляется",
    PurchaseStatus.AWAITING_PAYMENT: "Заказ создан — нужна оплата",
    PurchaseStatus.PROCESSING: "Оплачено, ждём зачисления",
    PurchaseStatus.COMPLETED: "Куплено",
    PurchaseStatus.DRY_RUN: "Dry-run: купил бы",
    PurchaseStatus.SKIPPED: "Пропущено",
    PurchaseStatus.MISSED: "Пропущен запуск",
    PurchaseStatus.FAILED: "Не куплено",
    PurchaseStatus.EXPIRED: "Оплата не поступила",
    PurchaseStatus.UNKNOWN: "Результат неизвестен",
}

SCHEDULE_EXAMPLES = (
    "Примеры (нажмите, чтобы скопировать):\n"
    "<code>ежедневно 12:00</code>\n"
    "<code>ежедневно 09:00, 21:00</code>\n"
    "<code>пн, чт 18:30</code>\n"
    "<code>будни 10:00</code>\n"
    "<code>ежемесячно 1 10:00</code>\n"
    "<code>разово 10.11.2026 12:00</code>"
)

UNKNOWN_HINT = (
    "⚠️ Заказ мог пройти — проверьте историю заказов на сайте. "
    "Повторно по этому запуску бот покупать не будет."
)


# ---- деньги, время ---------------------------------------------------------


def money(amount: Decimal | None, currency: str | None = None) -> str:
    if amount is None:
        return "—"
    text = f"{amount:,.2f}".replace(",", NBSP).replace(".", ",")
    return f"{text}{NBSP}{currency}" if currency else text


def parse_amount(text: str, *, allow_zero: bool = False) -> Decimal:
    cleaned = text.strip().replace(NBSP, "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Нужна сумма числом, например 4,99") from None
    if not value.is_finite() or value < 0 or (value == 0 and not allow_zero):
        raise ValueError("Сумма должна быть больше нуля")
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def raise_by(amount: Decimal, percent: int) -> Decimal:
    return (amount * (100 + percent) / 100).quantize(CENT, rounding=ROUND_UP)


def fmt_dt(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def fmt_short(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m %H:%M")


def fmt_run(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return f"{WEEKDAY_SHORT_RU[local.weekday()]} {local:%d.%m %H:%M}"


def duration(delta: timedelta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} ч")
    if mins or not parts:
        parts.append(f"{mins} мин")
    return " ".join(parts[:2])


def clip(text: str, limit: int = MESSAGE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 2)
    return text[: cut if cut > 0 else limit - 2] + "\n…"


# ---- задачи -----------------------------------------------------------------


def account_label(player_id: str, server_id: str | None, nickname: str | None) -> str:
    ident = f"{player_id} ({server_id})" if server_id else player_id
    return f"{escape(ident)} — {escape(nickname)}" if nickname else escape(ident)


def task_state(task: Task, now: datetime) -> str:
    if task.enabled and task.next_run_at is not None:
        return "✅ активна"
    schedule = parse_schedule(task.schedule)
    if schedule.kind == "once" and schedule.next_after(now, ZoneInfo(task.timezone)) is None:
        return "⏹ завершена"
    return "⏸ на паузе"


def task_summary(task: Task) -> str:
    schedule = parse_schedule(task.schedule)
    lines = [
        f"Товар: {escape(task.title)}",
        f"Аккаунт: {account_label(task.player_id, task.server_id, task.nickname)}",
        f"Расписание: {schedule.describe()} ({escape(task.timezone)})",
        f"Лимит цены: {money(task.max_price, task.currency)}",
    ]
    if task.payment_title:
        lines.append(f"Оплата: {escape(task.payment_title)}")
    return "\n".join(lines)


def task_card(task: Task, now: datetime) -> str:
    lines = [f"<b>#{task.id}</b>", task_summary(task), f"Статус: {task_state(task, now)}"]
    if task.enabled and task.next_run_at is not None:
        lines.append(f"Следующий запуск: {fmt_run(task.next_run_at, ZoneInfo(task.timezone))}")
    return "\n".join(lines)


def task_button(task: Task, now: datetime) -> str:
    icon = "✅" if task.enabled and task.next_run_at is not None else "⏸"
    label = f"{icon} #{task.id} {task.offer_title} — {parse_schedule(task.schedule).describe()}"
    return label if len(label) <= 60 else label[:59] + "…"


# ---- покупки ----------------------------------------------------------------


def payment_lines(payment: PaymentInfo, tz: ZoneInfo) -> list[str]:
    lines = [f"Способ оплаты: {escape(payment.method)}"] if payment.method else []
    if payment.code:
        lines.append("Код для оплаты (нажмите, чтобы скопировать):")
        lines.append(f"<code>{escape(payment.code)}</code>")
    if payment.expires_at:
        lines.append(f"Оплатить до {fmt_short(payment.expires_at, tz)}")
    return lines


def purchase_report(
    *,
    title: str,
    account: str,
    status: PurchaseStatus,
    reason: str | None,
    price: Decimal | None,
    currency: str | None,
    limit: Decimal,
    order: Order | None,
    manual: bool,
    tz: ZoneInfo,
) -> str:
    """Итог покупки. `account` — уже готовый HTML (см. account_label)."""
    head = f"{STATUS_ICON[status]} <b>{STATUS_TITLE[status]}</b>"
    if manual:
        head += " (вручную)"
    lines = [head, escape(title), f"Аккаунт: {account}"]
    if price is not None:
        line = f"Цена: {money(price, currency)}"
        if status in (PurchaseStatus.DRY_RUN, PurchaseStatus.SKIPPED):
            line += f" (лимит {money(limit, currency)})"
        lines.append(line)
    if order is not None:
        lines.append(f"Заказ: <code>{escape(order.id)}</code>")
    if reason:
        lines.append(f"Причина: {escape(reason)}")
    if status is PurchaseStatus.AWAITING_PAYMENT and order is not None and order.payment:
        lines.extend(payment_lines(order.payment, tz))
    if status is PurchaseStatus.UNKNOWN:
        lines.append(UNKNOWN_HINT)
    if status is PurchaseStatus.DRY_RUN:
        lines.append("Реальной покупки не было. Включить реальные покупки: /mode")
    return "\n".join(lines)


def order_update(
    title: str, order_id: str | None, status: PurchaseStatus, reason: str | None = None
) -> str:
    lines = [f"{STATUS_ICON[status]} <b>{STATUS_TITLE[status]}</b>", escape(title)]
    if order_id:
        lines.append(f"Заказ: <code>{escape(order_id)}</code>")
    if reason:
        lines.append(f"Причина: {escape(reason)}")
    if status is PurchaseStatus.UNKNOWN:
        lines.append(UNKNOWN_HINT)
    return "\n".join(lines)


def missed_report(title: str, slot: datetime, reason: str, tz: ZoneInfo) -> str:
    return "\n".join(
        [
            f"{STATUS_ICON[PurchaseStatus.MISSED]} <b>{STATUS_TITLE[PurchaseStatus.MISSED]}</b>",
            escape(title),
            f"По расписанию: {fmt_dt(slot, tz)}",
            f"Причина: {escape(reason)}",
        ]
    )


def history_line(purchase: Purchase, tz: ZoneInfo) -> str:
    icon = STATUS_ICON[purchase.status]
    parts = [f"{fmt_short(purchase.slot, tz)} {icon} {escape(purchase.task_name)}"]
    if purchase.price is not None:
        parts.append(money(purchase.price, purchase.currency))
    if purchase.order_id:
        parts.append(f"<code>{escape(purchase.order_id)}</code>")
    line = " — ".join(parts)
    if purchase.manual:
        line += " (вручную)"
    if purchase.reason and purchase.status not in (
        PurchaseStatus.COMPLETED,
        PurchaseStatus.DRY_RUN,
    ):
        line += f"\n      <i>{escape(purchase.reason)}</i>"
    return line
