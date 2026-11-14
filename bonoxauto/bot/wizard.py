"""Мастер создания задачи: /add → товар → пакет → аккаунт → лимит цены → расписание → оплата."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from html import escape
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import texts
from ..config import Settings
from ..models import Task, utcnow
from ..schedule import ScheduleError, parse_schedule
from ..scheduler import Scheduler, next_run
from ..shop.base import (
    GameAccount,
    InvalidAccountError,
    Offer,
    PaymentMethod,
    Product,
    ShopClient,
    ShopError,
)
from ..storage import Repository
from .callbacks import WizardCb
from .registry import Handlers
from .ui import TEXT_INPUT, card_keyboard, show

log = logging.getLogger(__name__)
router = Handlers("wizard")

#: Черновики, по которым задача уже создана, — защита от двойного нажатия «Создать».
_created_drafts: set[str] = set()


class AddTask(StatesGroup):
    product = State()
    offer = State()
    account = State()
    account_confirm = State()
    max_price = State()
    schedule = State()
    payment = State()
    confirm = State()


# ---- сериализация в FSM (только простые типы) -------------------------------


def _product_to_data(product: Product) -> dict[str, Any]:
    return {
        "ref": product.ref,
        "title": product.title,
        "requires_server_id": product.requires_server_id,
        "offers": [
            {
                "id": o.id,
                "title": o.title,
                "price": str(o.price),
                "currency": o.currency,
                "available": o.available,
            }
            for o in product.offers
        ],
        "payment_methods": [{"id": m.id, "title": m.title} for m in product.payment_methods],
    }


def _product_from_data(data: dict[str, Any]) -> Product:
    return Product(
        ref=data["ref"],
        title=data["title"],
        requires_server_id=data["requires_server_id"],
        offers=tuple(
            Offer(o["id"], o["title"], Decimal(o["price"]), o["currency"], o["available"])
            for o in data["offers"]
        ),
        payment_methods=tuple(PaymentMethod(m["id"], m["title"]) for m in data["payment_methods"]),
    )


def parse_account(text: str, requires_server_id: bool) -> GameAccount:
    parts = [p for p in re.split(r"[\s,;:()/|]+", text.strip()) if p]
    if any(len(p) > 64 for p in parts):
        raise ValueError("Слишком длинный ID.")
    if requires_server_id:
        if len(parts) != 2:
            raise ValueError("Нужно два значения через пробел: Player ID и Server ID.")
        return GameAccount(parts[0], parts[1])
    if len(parts) != 1:
        raise ValueError("Нужен только Player ID — одно значение без пробелов.")
    return GameAccount(parts[0])


async def _draft(state: FSMContext) -> tuple[dict[str, Any], Product, Offer]:
    data = await state.get_data()
    product = _product_from_data(data["product"])
    return data, product, product.offers[data["offer"]]


def _task_from_draft(data: dict[str, Any], timezone: str) -> Task:
    product = _product_from_data(data["product"])
    offer = product.offers[data["offer"]]
    payment: PaymentMethod | None = None
    if data.get("payment") is not None:
        payment = product.payment_methods[data["payment"]]
    return Task(
        product_ref=product.ref,
        product_title=product.title,
        offer_id=offer.id,
        offer_title=offer.title,
        player_id=data["player_id"],
        server_id=data.get("server_id"),
        nickname=data.get("nickname"),
        schedule=data["schedule"],
        timezone=timezone,
        max_price=Decimal(data["max_price"]),
        currency=offer.currency,
        payment_method=payment.id if payment else None,
        payment_title=payment.title if payment else None,
    )


# ---- шаги ------------------------------------------------------------------


async def start_wizard(bot: Bot, chat_id: int, state: FSMContext, shop: ShopClient) -> None:
    await state.clear()
    await state.set_state(AddTask.product)
    await state.update_data(draft=uuid4().hex)
    await bot.send_message(
        chat_id,
        f"🛒 <b>Новая задача</b>\n\n<b>1. Товар.</b> {shop.product_hint}\n\n/cancel — отменить",
    )


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext, shop: ShopClient, bot: Bot) -> None:
    await start_wizard(bot, message.chat.id, state, shop)


@router.callback_query(WizardCb.filter(F.action == "start"))
async def cb_add(callback: CallbackQuery, state: FSMContext, shop: ShopClient, bot: Bot) -> None:
    await callback.answer()
    await start_wizard(bot, callback.from_user.id, state, shop)


@router.callback_query(WizardCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await show(callback, bot, "Создание задачи отменено.")


@router.message(AddTask.product, TEXT_INPUT)
async def on_product(message: Message, state: FSMContext, shop: ShopClient) -> None:
    ref = (message.text or "").strip()
    try:
        product = await shop.get_product(ref)
    except ShopError as exc:
        await message.answer(f"❌ {escape(str(exc))}\n\nОтправьте другую ссылку или /cancel.")
        return
    except Exception:
        log.exception("Не удалось загрузить товар %r", ref)
        await message.answer("❌ Не получилось открыть товар. Попробуйте ещё раз или /cancel.")
        return
    if not product.offers:
        await message.answer("❌ На странице нет пакетов для покупки. Отправьте другую ссылку.")
        return
    await state.update_data(product=_product_to_data(product))
    await state.set_state(AddTask.offer)
    builder = InlineKeyboardBuilder()
    for index, offer in enumerate(product.offers):
        label = f"{offer.title} — {texts.money(offer.price, offer.currency)}"
        if not offer.available:
            label += " (нет в наличии)"
        builder.button(text=label, callback_data=WizardCb(action="offer", value=str(index)))
    builder.button(text="✖️ Отмена", callback_data=WizardCb(action="cancel"))
    builder.adjust(1)
    await message.answer(
        f"<b>{escape(product.title)}</b>\n\n<b>2. Пакет.</b> Что покупать?",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(AddTask.offer, WizardCb.filter(F.action == "offer"))
async def on_offer(
    callback: CallbackQuery, callback_data: WizardCb, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    product = _product_from_data(data["product"])
    try:
        index = int(callback_data.value)
        offer = product.offers[index]
    except (ValueError, IndexError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(offer=index)
    await state.set_state(AddTask.account)
    await callback.answer()
    if product.requires_server_id:
        hint = (
            "Отправьте <b>Player ID</b> и <b>Server ID</b> через пробел, "
            "например <code>123456789 1234</code>."
        )
    else:
        hint = "Отправьте <b>Player ID</b> — аккаунт, на который зачислять покупку."
    await show(
        callback,
        bot,
        f"<b>{escape(product.title)}</b> · {escape(offer.title)} — "
        f"{texts.money(offer.price, offer.currency)}\n\n<b>3. Аккаунт.</b> {hint}",
    )


@router.message(AddTask.account, TEXT_INPUT)
async def on_account(message: Message, state: FSMContext, shop: ShopClient, bot: Bot) -> None:
    data = await state.get_data()
    product = _product_from_data(data["product"])
    try:
        account = parse_account(message.text or "", product.requires_server_id)
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    note = ""
    try:
        nickname = await shop.check_account(product, account)
    except InvalidAccountError as exc:
        await message.answer(f"❌ {escape(str(exc))}\nПроверьте ID и отправьте ещё раз.")
        return
    except ShopError as exc:
        nickname = None
        note = f"⚠️ Не удалось проверить аккаунт: {escape(str(exc))}\n\n"
    await state.update_data(
        player_id=account.player_id, server_id=account.server_id, nickname=nickname
    )
    if nickname:
        await state.set_state(AddTask.account_confirm)
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Да, верно", callback_data=WizardCb(action="account_ok"))
        builder.button(text="✏️ Ввести заново", callback_data=WizardCb(action="account_retry"))
        builder.adjust(2)
        await message.answer(
            f"Нашёл аккаунт: <b>{escape(nickname)}</b>. Покупки будут зачисляться на него. Верно?",
            reply_markup=builder.as_markup(),
        )
        return
    await _ask_max_price(bot, message.chat.id, state, note)


@router.callback_query(AddTask.account_confirm, WizardCb.filter(F.action == "account_ok"))
async def on_account_ok(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await callback.answer()
    await show(callback, bot, f"✅ Аккаунт: <b>{escape(data.get('nickname') or '')}</b>")
    await _ask_max_price(bot, callback.from_user.id, state)


@router.callback_query(AddTask.account_confirm, WizardCb.filter(F.action == "account_retry"))
async def on_account_retry(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await state.set_state(AddTask.account)
    await callback.answer()
    await show(callback, bot, "Хорошо, отправьте ID ещё раз.")


async def _ask_max_price(bot: Bot, chat_id: int, state: FSMContext, prefix: str = "") -> None:
    _, _, offer = await _draft(state)
    await state.set_state(AddTask.max_price)
    builder = InlineKeyboardBuilder()
    for percent, label in ((0, "как сейчас"), (10, "+10%"), (25, "+25%")):
        value = texts.raise_by(offer.price, percent)
        builder.button(
            text=f"{texts.money(value, offer.currency)} ({label})",
            callback_data=WizardCb(action="price", value=str(value)),
        )
    builder.adjust(1)
    await bot.send_message(
        chat_id,
        f"{prefix}<b>4. Лимит цены.</b> Сейчас пакет стоит "
        f"{texts.money(offer.price, offer.currency)}.\n"
        "Если в момент покупки цена окажется выше лимита, бот пропустит покупку и напишет вам.\n"
        "Выберите вариант или отправьте свою сумму:",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(AddTask.max_price, WizardCb.filter(F.action == "price"))
async def on_price_button(
    callback: CallbackQuery, callback_data: WizardCb, state: FSMContext, bot: Bot, config: Settings
) -> None:
    await callback.answer()
    value = texts.parse_amount(callback_data.value)
    await state.update_data(max_price=str(value))
    _, _, offer = await _draft(state)
    await show(callback, bot, f"✅ Лимит цены: {texts.money(value, offer.currency)}")
    await _ask_schedule(bot, callback.from_user.id, state, config)


@router.message(AddTask.max_price, TEXT_INPUT)
async def on_price_text(message: Message, state: FSMContext, bot: Bot, config: Settings) -> None:
    try:
        value = texts.parse_amount(message.text or "")
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    await state.update_data(max_price=str(value))
    await _ask_schedule(bot, message.chat.id, state, config)


async def _ask_schedule(bot: Bot, chat_id: int, state: FSMContext, config: Settings) -> None:
    await state.set_state(AddTask.schedule)
    await bot.send_message(
        chat_id,
        f"<b>5. Расписание.</b> Когда покупать? Время — по часовому поясу "
        f"<b>{escape(config.timezone)}</b>.\n\n{texts.SCHEDULE_EXAMPLES}",
    )


@router.message(AddTask.schedule, TEXT_INPUT)
async def on_schedule(
    message: Message, state: FSMContext, bot: Bot, repo: Repository, config: Settings
) -> None:
    try:
        schedule = parse_schedule(message.text or "")
    except ScheduleError as exc:
        await message.answer(f"❌ {escape(str(exc))}\n\n{texts.SCHEDULE_EXAMPLES}")
        return
    if schedule.next_after(utcnow(), config.tz) is None:
        await message.answer("❌ Это время уже прошло — укажите время в будущем.")
        return
    await state.update_data(schedule=schedule.to_spec())
    _, product, _ = await _draft(state)
    methods = product.payment_methods
    if len(methods) > 1:
        await state.set_state(AddTask.payment)
        builder = InlineKeyboardBuilder()
        for index, method in enumerate(methods):
            builder.button(
                text=method.title, callback_data=WizardCb(action="pay", value=str(index))
            )
        builder.adjust(1)
        await message.answer("<b>6. Оплата.</b> Чем платить?", reply_markup=builder.as_markup())
        return
    await state.update_data(payment=0 if methods else None)
    await _ask_confirm(bot, message.chat.id, state, repo, config)


@router.callback_query(AddTask.payment, WizardCb.filter(F.action == "pay"))
async def on_payment(
    callback: CallbackQuery,
    callback_data: WizardCb,
    state: FSMContext,
    bot: Bot,
    repo: Repository,
    config: Settings,
) -> None:
    _, product, _ = await _draft(state)
    try:
        index = int(callback_data.value)
        method = product.payment_methods[index]
    except (ValueError, IndexError):
        await callback.answer("Кнопка устарела", show_alert=True)
        return
    await state.update_data(payment=index)
    await callback.answer()
    await show(callback, bot, f"✅ Оплата: {escape(method.title)}")
    await _ask_confirm(bot, callback.from_user.id, state, repo, config)


async def _ask_confirm(
    bot: Bot, chat_id: int, state: FSMContext, repo: Repository, config: Settings
) -> None:
    data = await state.get_data()
    task = _task_from_draft(data, config.timezone)
    tz = ZoneInfo(task.timezone)
    runs = parse_schedule(task.schedule).upcoming(utcnow(), tz, 3)
    lines = [
        "<b>Проверьте задачу:</b>",
        texts.task_summary(task),
        "Ближайшие запуски: " + ", ".join(texts.fmt_run(run, tz) for run in runs),
    ]
    if await repo.is_dry_run():
        lines.append(
            "\n🧪 Сейчас включён dry-run: по расписанию бот будет проверять цену и лимиты "
            "и писать, что купил бы, но ничего не купит. Реальные покупки — /mode"
        )
    await state.set_state(AddTask.confirm)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Создать", callback_data=WizardCb(action="create"))
    builder.button(text="✖️ Отмена", callback_data=WizardCb(action="cancel"))
    builder.adjust(2)
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=builder.as_markup())


@router.callback_query(AddTask.confirm, WizardCb.filter(F.action == "create"))
async def on_create(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    repo: Repository,
    scheduler: Scheduler,
    config: Settings,
) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    if not draft or draft in _created_drafts:
        await callback.answer()  # повторное нажатие «Создать»
        return
    task = _task_from_draft(data, config.timezone)
    task.next_run_at = next_run(task, utcnow())
    if task.next_run_at is None:
        await state.set_state(AddTask.schedule)
        await callback.answer()
        await show(callback, bot, "❌ Время из расписания уже прошло. Отправьте новое расписание.")
        return
    # Отмечаем черновик до первого await: двойное нажатие не создаст задачу-дубль
    # (а значит, и двойные покупки по расписанию).
    _created_drafts.add(draft)
    await state.clear()
    task = await repo.add_task(task)
    scheduler.poke()
    await callback.answer("Задача создана")
    log.info("Создана задача #%s: %s", task.id, task.title)
    await show(
        callback,
        bot,
        f"✅ <b>Задача создана</b>\n\n{texts.task_card(task, utcnow())}",
        card_keyboard(task),
    )
