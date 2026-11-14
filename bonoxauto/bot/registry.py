"""Набор обработчиков, из которого собирается новый aiogram.Router.

Router, созданный на уровне модуля, можно подключить только к одному диспетчеру,
а тестам нужен новый диспетчер на каждый тест. Поэтому модули регистрируют обработчики
здесь (теми же декораторами, что и у Router), а build() создаёт свежий Router.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from aiogram import Router

HandlerT = TypeVar("HandlerT", bound=Callable[..., Any])


class Handlers:
    def __init__(self, name: str) -> None:
        self.name = name
        self._entries: list[tuple[str, tuple[Any, ...], Callable[..., Any]]] = []

    def message(self, *filters: Any) -> Callable[[HandlerT], HandlerT]:
        return self._add("message", filters)

    def callback_query(self, *filters: Any) -> Callable[[HandlerT], HandlerT]:
        return self._add("callback_query", filters)

    def _add(self, kind: str, filters: tuple[Any, ...]) -> Callable[[HandlerT], HandlerT]:
        def decorator(handler: HandlerT) -> HandlerT:
            self._entries.append((kind, filters, handler))
            return handler

        return decorator

    def build(self) -> Router:
        router = Router(name=self.name)
        for kind, filters, handler in self._entries:
            getattr(router, kind).register(handler, *filters)
        return router
