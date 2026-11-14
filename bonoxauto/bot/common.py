"""Общие команды: справка, статус, история, режим, лимиты, пауза."""

from __future__ import annotations

import re
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import texts
from ..config import Settings
from ..engine import PurchaseEngine
from ..scheduler import Scheduler
from ..shop.base import ShopClient
from ..storage import Repository
from .callbacks import ModeCb
from .registry import Handlers
from .ui import show

router = Handlers("common")

_PERIODS = {"day": "day", "день": "day", "сутки": "day", "month": "month", "месяц": "month"}
_OFF_WORDS = {"off", "нет", "снять", "-"}
LIMIT_USAGE = (
    "Задать: <code>/limit день BRL 50</code> или <code>/limit месяц BRL 300</code>\n"
    "Снять: <code>/limit день BRL off</code>"
)


def _mode_line(dry_run: bool) -> str:
    return "🧪 dry-run — без реальных покупок" if dry_run else "💸 реальные покупки"


async def _budget_lines(repo: Repository, engine: PurchaseEngine) -> list[str]:
    limits = await repo.get_limits()
    currencies = sorted(
        {currency for _, currency in limits} | {t.currency for t in await repo.list_tasks()}
    )
    lines = []
    for currency in currencies:
        day, month = await engine.spent(currency)
        day_limit = limits.get(("day", currency))
        month_limit = limits.get(("month", currency))
        day_text = texts.money(day, currency)
        if day_limit is not None:
            day_text += f" из {texts.money(day_limit, currency)}"
        month_text = texts.money(month, currency)
        if month_limit is not None:
            month_text += f" из {texts.money(month_limit, currency)}"
        lines.append(f"• сегодня {day_text}; за месяц {month_text}")
    return lines


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message, repo: Repository, shop: ShopClient) -> None:
    await message.answer(
        "🤖 <b>bonoxauto</b> — автопокупки на bonoxs.com по расписанию\n\n"
        "/add — новая задача: товар, аккаунт, лимит цены, расписание\n"
        "/tasks — задачи: пауза, «купить сейчас», изменить, удалить\n"
        "/history — последние покупки\n"
        "/status — режим, бюджет, ближайшие запуски\n"
        "/mode — dry-run или реальные покупки\n"
        "/limits — дневной и месячный бюджет\n"
        "/pause, /resume — остановить или возобновить все автопокупки\n"
        "/cancel — отменить ввод\n\n"
        f"Магазин: {escape(shop.describe())}\n"
        f"Режим: {_mode_line(await repo.is_dry_run())}"
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено.")


@router.message(Command("status"))
async def cmd_status(
    message: Message, repo: Repository, shop: ShopClient, engine: PurchaseEngine
) -> None:
    tasks = await repo.list_tasks()
    active = sorted(
        (t for t in tasks if t.enabled and t.next_run_at is not None),
        key=lambda t: (t.next_run_at, t.id),
    )
    paused = await repo.is_paused()
    lines = [
        "🤖 <b>Статус</b>",
        f"Магазин: {escape(shop.describe())}",
        f"Режим: {_mode_line(await repo.is_dry_run())}",
        "Автопокупки: " + ("⏸ на паузе — /resume" if paused else "▶️ включены"),
        f"Задачи: активных {len(active)}, выключенных {len(tasks) - len(active)}",
    ]
    if active:
        lines += ["", "<b>Ближайшие запуски:</b>"]
        for task in active[:5]:
            assert task.next_run_at is not None
            when = texts.fmt_run(task.next_run_at, ZoneInfo(task.timezone))
            lines.append(f"• {when} — #{task.id} {escape(task.title)}")
    budget = await _budget_lines(repo, engine)
    if budget:
        lines += ["", "<b>Расходы:</b>", *budget]
    await message.answer("\n".join(lines))


@router.message(Command("history"))
async def cmd_history(
    message: Message, command: CommandObject, repo: Repository, config: Settings
) -> None:
    limit = 15
    if command.args and command.args.strip().isdigit():
        limit = max(1, min(50, int(command.args.strip())))
    purchases = await repo.recent_purchases(limit)
    if not purchases:
        await message.answer("🧾 История пуста.")
        return
    lines = ["🧾 <b>Последние запуски</b>"]
    lines += [texts.history_line(purchase, config.tz) for purchase in purchases]
    await message.answer(texts.clip("\n".join(lines)))


# ---- режим -----------------------------------------------------------------


def _mode_view(dry_run: bool, shop: ShopClient) -> tuple[str, InlineKeyboardMarkup]:
    builder = InlineKeyboardBuilder()
    if dry_run:
        text = (
            "🧪 <b>Режим: dry-run</b>\n"
            "По расписанию бот проверяет цену и лимиты, но ничего не покупает — только пишет, "
            "что купил бы."
        )
        builder.button(text="💸 Включить реальные покупки", callback_data=ModeCb(action="live"))
    else:
        text = (
            "💸 <b>Режим: реальные покупки</b>\n"
            "По расписанию бот оформляет настоящие заказы в пределах лимитов цены и бюджета."
        )
        builder.button(text="🧪 Вернуть dry-run", callback_data=ModeCb(action="dry"))
    if shop.is_demo:
        text += "\n\nℹ️ Подключён демо-магазин — заказы в любом случае тестовые."
    return text, builder.as_markup()


@router.message(Command("mode"))
async def cmd_mode(message: Message, repo: Repository, shop: ShopClient) -> None:
    text, markup = _mode_view(await repo.is_dry_run(), shop)
    await message.answer(text, reply_markup=markup)


@router.callback_query(ModeCb.filter(F.action == "live"))
async def cb_mode_live(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, покупать по-настоящему", callback_data=ModeCb(action="live_ok"))
    builder.button(text="✖️ Отмена", callback_data=ModeCb(action="show"))
    builder.adjust(1)
    await show(
        callback,
        bot,
        "⚠️ <b>Включить реальные покупки?</b>\n"
        "Бот начнёт тратить деньги по расписанию — в пределах лимита цены каждой задачи "
        "и бюджета (/limits).",
        builder.as_markup(),
    )


@router.callback_query(ModeCb.filter(F.action.in_({"live_ok", "dry", "show"})))
async def cb_mode_set(
    callback: CallbackQuery, callback_data: ModeCb, repo: Repository, shop: ShopClient, bot: Bot
) -> None:
    if callback_data.action == "live_ok":
        await repo.set_dry_run(False)
        await callback.answer("Реальные покупки включены")
    elif callback_data.action == "dry":
        await repo.set_dry_run(True)
        await callback.answer("Dry-run включён")
    else:
        await callback.answer()
    text, markup = _mode_view(await repo.is_dry_run(), shop)
    await show(callback, bot, text, markup)


# ---- лимиты бюджета -----------------------------------------------------------


@router.message(Command("limits"))
async def cmd_limits(message: Message, repo: Repository, engine: PurchaseEngine) -> None:
    limits = await repo.get_limits()
    lines = ["💰 <b>Бюджет</b>"]
    if limits:
        for (period, currency), amount in sorted(limits.items()):
            label = "в день" if period == "day" else "в месяц"
            lines.append(f"• {label}: {texts.money(amount, currency)}")
    else:
        lines.append("Лимиты не заданы — ограничивает только лимит цены в каждой задаче.")
    budget = await _budget_lines(repo, engine)
    if budget:
        lines += ["", "<b>Расходы:</b>", *budget]
    lines += ["", LIMIT_USAGE]
    await message.answer("\n".join(lines))


@router.message(Command("limit"))
async def cmd_limit(message: Message, command: CommandObject, repo: Repository) -> None:
    args = (command.args or "").split()
    if len(args) != 3 or args[0].lower() not in _PERIODS:
        await message.answer(LIMIT_USAGE)
        return
    period = _PERIODS[args[0].lower()]
    currency = args[1].upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        await message.answer("❌ Валюта — три буквы, например BRL.\n\n" + LIMIT_USAGE)
        return
    amount: Decimal | None = None
    if args[2].lower() not in _OFF_WORDS:
        try:
            amount = texts.parse_amount(args[2], allow_zero=True)
        except ValueError as exc:
            await message.answer(f"❌ {exc}")
            return
    await repo.set_limit(period, currency, amount)
    label = "Дневной" if period == "day" else "Месячный"
    if amount is None:
        await message.answer(f"✅ {label} лимит для {currency} снят.")
    else:
        await message.answer(f"✅ {label} лимит: {texts.money(amount, currency)}")


# ---- пауза -----------------------------------------------------------------


@router.message(Command("pause"))
async def cmd_pause(message: Message, repo: Repository, scheduler: Scheduler) -> None:
    await repo.set_paused(True)
    scheduler.poke()
    await message.answer(
        "⏸ Все автопокупки остановлены. Запуски, которые придутся на паузу, будут пропущены.\n"
        "/resume — возобновить"
    )


@router.message(Command("resume"))
async def cmd_resume(message: Message, repo: Repository, scheduler: Scheduler) -> None:
    if not await repo.is_paused():
        await message.answer("Автопокупки и так включены.")
        return
    # Сначала пересчитываем расписание, потом снимаем паузу — чтобы не «догонять»
    # запуски, пришедшиеся на паузу.
    await scheduler.reschedule_all()
    await repo.set_paused(False)
    scheduler.poke()
    text = "▶️ Автопокупки возобновлены."
    active = [t for t in await repo.list_tasks() if t.enabled and t.next_run_at is not None]
    if active:
        first = min(active, key=lambda t: (t.next_run_at, t.id))
        assert first.next_run_at is not None
        when = texts.fmt_run(first.next_run_at, ZoneInfo(first.timezone))
        text += f"\nБлижайший запуск: {when} — #{first.id} {escape(first.title)}"
    await message.answer(text)
