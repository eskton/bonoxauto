"""Доступ к боту — только для владельцев из ADMIN_IDS."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

log = logging.getLogger(__name__)

DENIED = "⛔ Это личный бот, доступ закрыт."


class AdminOnly(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self._admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None and user.id in self._admin_ids:
            return await handler(event, data)
        if user is not None:
            log.warning("Отказ в доступе пользователю %s (@%s)", user.id, user.username)
        if isinstance(event, Update):
            if event.message is not None:
                await event.message.answer(DENIED)
            elif event.callback_query is not None:
                await event.callback_query.answer(DENIED, show_alert=True)
        return None
