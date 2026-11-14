"""Список задач и управление задачей: пауза, «купить сейчас», изменение, удаление."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import Bot, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import texts
from ..engine import PurchaseEngine
from ..models import Task, utcnow
from ..schedule import ScheduleError, parse_schedule
from ..scheduler import Scheduler
from ..storage import Repository
from .callbacks import TaskCb
from .registry import Handlers
from .ui import TEXT_INPUT, card_keyboard, show, task_list_view

router = Handlers("tasks")

#: Сколько действует кнопка подтверждения «купить сейчас».
CONFIRM_TTL = timedelta(minutes=10)


class EditTask(StatesGroup):
    schedule = State()
    max_price = State()


async def _load(callback: CallbackQuery, callback_data: TaskCb, repo: Repository) -> Task | None:
    task = await repo.get_task(callback_data.task_id)
    if task is None:
        await callback.answer("Задача не найдена — возможно, уже удалена", show_alert=True)
    return task


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, repo: Repository) -> None:
    text, markup = await task_list_view(repo)
    await message.answer(text, reply_markup=markup)


@router.callback_query(TaskCb.filter(F.action == "list"))
async def cb_list(callback: CallbackQuery, repo: Repository, bot: Bot) -> None:
    await callback.answer()
    text, markup = await task_list_view(repo)
    await show(callback, bot, text, markup)


@router.callback_query(TaskCb.filter(F.action == "open"))
async def cb_open(
    callback: CallbackQuery, callback_data: TaskCb, repo: Repository, bot: Bot
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    await callback.answer()
    await show(callback, bot, texts.task_card(task, utcnow()), card_keyboard(task))


@router.callback_query(TaskCb.filter(F.action == "pause"))
async def cb_pause(
    callback: CallbackQuery,
    callback_data: TaskCb,
    repo: Repository,
    scheduler: Scheduler,
    bot: Bot,
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    task.enabled = False
    task = await scheduler.reschedule(task)
    await callback.answer("Задача на паузе")
    await show(callback, bot, texts.task_card(task, utcnow()), card_keyboard(task))


@router.callback_query(TaskCb.filter(F.action == "resume"))
async def cb_resume(
    callback: CallbackQuery,
    callback_data: TaskCb,
    repo: Repository,
    scheduler: Scheduler,
    bot: Bot,
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    task.enabled = True
    task = await scheduler.reschedule(task)
    if task.enabled:
        await callback.answer("Задача включена")
    else:
        await callback.answer(
            "Время разовой покупки уже прошло — задайте новое расписание", show_alert=True
        )
    await show(callback, bot, texts.task_card(task, utcnow()), card_keyboard(task))


@router.callback_query(TaskCb.filter(F.action == "buy"))
async def cb_buy(
    callback: CallbackQuery,
    callback_data: TaskCb,
    repo: Repository,
    engine: PurchaseEngine,
    bot: Bot,
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    assert task.id is not None
    if await repo.is_dry_run():
        engine.run_in_background(task.id, utcnow(), manual=True)
        await callback.answer("🧪 Проверяю (dry-run)… Результат придёт сообщением.")
        return
    await callback.answer()
    stamp = int(utcnow().timestamp() * 1000)
    if stamp % 60_000 == 0:
        stamp += 1  # не совпадать с запусками по расписанию (они ровно в начале минуты)
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Да, купить", callback_data=TaskCb(action="buy_ok", task_id=task.id, stamp=stamp)
    )
    builder.button(text="✖️ Нет", callback_data=TaskCb(action="open", task_id=task.id))
    builder.adjust(2)
    await show(
        callback,
        bot,
        f"{texts.task_card(task, utcnow())}\n\n⚠️ <b>Купить прямо сейчас</b> "
        f"не дороже {texts.money(task.max_price, task.currency)}?",
        builder.as_markup(),
    )


@router.callback_query(TaskCb.filter(F.action == "buy_ok"))
async def cb_buy_ok(
    callback: CallbackQuery,
    callback_data: TaskCb,
    repo: Repository,
    engine: PurchaseEngine,
    bot: Bot,
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    assert task.id is not None
    # Слот покупки = момент показа подтверждения. Повторное нажатие той же кнопки попадёт
    # в тот же слот и будет отброшено (см. Repository.reserve_purchase).
    shown_at = datetime.fromtimestamp(callback_data.stamp / 1000, UTC)
    if callback_data.stamp <= 0 or utcnow() - shown_at > CONFIRM_TTL:
        await callback.answer(
            "Подтверждение устарело — нажмите «Купить сейчас» ещё раз.", show_alert=True
        )
        await show(callback, bot, texts.task_card(task, utcnow()), card_keyboard(task))
        return
    engine.run_in_background(task.id, shown_at, manual=True)
    await callback.answer("⏳ Покупаю… Результат придёт сообщением.")
    await show(callback, bot, texts.task_card(task, utcnow()), card_keyboard(task))


@router.callback_query(TaskCb.filter(F.action == "del"))
async def cb_delete(
    callback: CallbackQuery, callback_data: TaskCb, repo: Repository, bot: Bot
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    assert task.id is not None
    await callback.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить", callback_data=TaskCb(action="del_ok", task_id=task.id))
    builder.button(text="✖️ Нет", callback_data=TaskCb(action="open", task_id=task.id))
    builder.adjust(2)
    await show(
        callback,
        bot,
        f"🗑 Удалить задачу <b>#{task.id} {escape(task.title)}</b>?\nИстория покупок сохранится.",
        builder.as_markup(),
    )


@router.callback_query(TaskCb.filter(F.action == "del_ok"))
async def cb_delete_ok(
    callback: CallbackQuery,
    callback_data: TaskCb,
    repo: Repository,
    scheduler: Scheduler,
    bot: Bot,
) -> None:
    await repo.delete_task(callback_data.task_id)
    scheduler.poke()
    await callback.answer("Задача удалена")
    text, markup = await task_list_view(repo)
    await show(callback, bot, text, markup)


# ---- изменение расписания и лимита цены --------------------------------------


@router.callback_query(TaskCb.filter(F.action == "edit_sched"))
async def cb_edit_schedule(
    callback: CallbackQuery, callback_data: TaskCb, state: FSMContext, repo: Repository, bot: Bot
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    await state.set_state(EditTask.schedule)
    await state.update_data(task_id=task.id)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        f"🕒 Новое расписание для <b>#{task.id}</b> "
        f"(сейчас: {parse_schedule(task.schedule).describe()}, "
        f"часовой пояс {escape(task.timezone)}).\n\n{texts.SCHEDULE_EXAMPLES}\n\n/cancel — отмена",
    )


@router.message(EditTask.schedule, TEXT_INPUT)
async def on_edit_schedule(
    message: Message, state: FSMContext, repo: Repository, scheduler: Scheduler
) -> None:
    task = await repo.get_task((await state.get_data())["task_id"])
    if task is None:
        await state.clear()
        await message.answer("Задача уже удалена.")
        return
    try:
        schedule = parse_schedule(message.text or "")
    except ScheduleError as exc:
        await message.answer(f"❌ {escape(str(exc))}\n\n{texts.SCHEDULE_EXAMPLES}")
        return
    task.schedule = schedule.to_spec()
    task.enabled = True
    task = await scheduler.reschedule(task)
    if not task.enabled:
        await message.answer("❌ Это время уже прошло — укажите время в будущем.")
        return
    await state.clear()
    await message.answer(
        f"✅ Расписание обновлено\n\n{texts.task_card(task, utcnow())}",
        reply_markup=card_keyboard(task),
    )


@router.callback_query(TaskCb.filter(F.action == "edit_price"))
async def cb_edit_price(
    callback: CallbackQuery, callback_data: TaskCb, state: FSMContext, repo: Repository, bot: Bot
) -> None:
    task = await _load(callback, callback_data, repo)
    if task is None:
        return
    await state.set_state(EditTask.max_price)
    await state.update_data(task_id=task.id)
    await callback.answer()
    await bot.send_message(
        callback.from_user.id,
        f"💰 Новый лимит цены для <b>#{task.id}</b> в {escape(task.currency)} "
        f"(сейчас: {texts.money(task.max_price, task.currency)}). Отправьте сумму.\n\n"
        "/cancel — отмена",
    )


@router.message(EditTask.max_price, TEXT_INPUT)
async def on_edit_price(message: Message, state: FSMContext, repo: Repository) -> None:
    task = await repo.get_task((await state.get_data())["task_id"])
    if task is None:
        await state.clear()
        await message.answer("Задача уже удалена.")
        return
    try:
        task.max_price = texts.parse_amount(message.text or "")
    except ValueError as exc:
        await message.answer(f"❌ {exc}")
        return
    await repo.update_task(task)
    await state.clear()
    await message.answer(
        f"✅ Лимит цены обновлён\n\n{texts.task_card(task, utcnow())}",
        reply_markup=card_keyboard(task),
    )
