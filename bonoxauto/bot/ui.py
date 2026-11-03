"""Общие элементы интерфейса: показ экранов, клавиатуры задач, фильтры ввода."""

from __future__ import annotations

from aiogram import Bot, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import texts
from ..models import Task, utcnow
from ..storage import Repository
from .callbacks import TaskCb, WizardCb

#: Обычный текст, а не команда — для шагов ввода.
TEXT_INPUT = F.text & ~F.text.startswith("/")


async def show(
    callback: CallbackQuery, bot: Bot, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Заменить сообщение с кнопками новым экраном (или прислать новое, если нельзя)."""
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(texts.clip(text), reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    await bot.send_message(callback.from_user.id, texts.clip(text), reply_markup=markup)


def card_keyboard(task: Task) -> InlineKeyboardMarkup:
    assert task.id is not None
    builder = InlineKeyboardBuilder()
    if task.enabled:
        builder.button(text="⏸ Пауза", callback_data=TaskCb(action="pause", task_id=task.id))
    else:
        builder.button(text="▶️ Включить", callback_data=TaskCb(action="resume", task_id=task.id))
    builder.button(text="🛒 Купить сейчас", callback_data=TaskCb(action="buy", task_id=task.id))
    builder.button(text="🕒 Расписание", callback_data=TaskCb(action="edit_sched", task_id=task.id))
    builder.button(text="💰 Лимит цены", callback_data=TaskCb(action="edit_price", task_id=task.id))
    builder.button(text="🗑 Удалить", callback_data=TaskCb(action="del", task_id=task.id))
    builder.button(text="« Все задачи", callback_data=TaskCb(action="list"))
    builder.adjust(2, 2, 2)
    return builder.as_markup()


async def task_list_view(repo: Repository) -> tuple[str, InlineKeyboardMarkup]:
    tasks = await repo.list_tasks()
    now = utcnow()
    builder = InlineKeyboardBuilder()
    for task in tasks:
        assert task.id is not None
        builder.button(
            text=texts.task_button(task, now), callback_data=TaskCb(action="open", task_id=task.id)
        )
    builder.button(text="➕ Новая задача", callback_data=WizardCb(action="start"))
    builder.adjust(1)
    if tasks:
        text = f"📋 <b>Задачи ({len(tasks)})</b>\nНажмите на задачу, чтобы управлять ею."
    else:
        text = "📋 Задач пока нет. Создайте первую — /add"
    return text, builder.as_markup()
