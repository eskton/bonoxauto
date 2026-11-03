"""Отправка уведомлений о покупках владельцам бота."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .. import texts

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot: Bot, chat_ids: Iterable[int]) -> None:
        self._bot = bot
        self._chat_ids = tuple(chat_ids)

    async def send(
        self, text: str, *, links: Sequence[tuple[str, str]] = (), silent: bool = False
    ) -> None:
        markup = None
        if links:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=title, url=url)] for title, url in links
                ]
            )
        for chat_id in self._chat_ids:
            try:
                await self._bot.send_message(
                    chat_id, texts.clip(text), reply_markup=markup, disable_notification=silent
                )
            except TelegramAPIError as exc:
                # Чаще всего владелец ещё не нажал /start у бота или нет связи с Telegram.
                log.warning("Не удалось отправить уведомление в чат %s: %s", chat_id, exc)
            except Exception:
                log.exception("Не удалось отправить уведомление в чат %s", chat_id)
