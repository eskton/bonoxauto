"""Тестовые заглушки: управляемые часы, магазин и уведомления."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from bonoxauto.models import Task
from bonoxauto.shop.base import (
    Offer,
    Order,
    OrderRequest,
    OrderStatus,
    PaymentMethod,
    Product,
    ShopClient,
)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeSleep:
    """Не ждёт по-настоящему: сдвигает часы и отдаёт управление циклу событий."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds=seconds)
        await asyncio.sleep(0)


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.links: list[Sequence[tuple[str, str]]] = []

    async def send(
        self, text: str, *, links: Sequence[tuple[str, str]] = (), silent: bool = False
    ) -> None:
        self.messages.append(text)
        self.links.append(links)

    @property
    def last(self) -> str:
        return self.messages[-1]


class ScriptedShop(ShopClient):
    """Магазин, поведение которого задаёт тест."""

    name = "scripted"

    def __init__(self) -> None:
        self.product = Product(
            ref="test:free-fire",
            title="Free Fire",
            offers=(Offer("ff-100", "100 diamantes", Decimal("4.49"), "BRL"),),
            payment_methods=(PaymentMethod("balance", "Баланс"),),
        )
        self.product_errors: list[Exception] = []
        self.order_result: Order | Exception = Order(
            "ORD-1", OrderStatus.COMPLETED, Decimal("4.49"), "BRL"
        )
        self.order_updates: list[Order | Exception] = []
        self.placed: list[OrderRequest] = []
        self.get_order_calls = 0

    def set_offer(self, **changes: Any) -> None:
        offer = replace(self.product.offers[0], **changes)
        self.product = replace(self.product, offers=(offer,))

    async def get_product(self, ref: str) -> Product:
        if self.product_errors:
            raise self.product_errors.pop(0)
        return self.product

    async def place_order(self, request: OrderRequest) -> Order:
        self.placed.append(request)
        if isinstance(self.order_result, Exception):
            raise self.order_result
        return self.order_result

    async def get_order(self, order_id: str) -> Order:
        self.get_order_calls += 1
        item = self.order_updates.pop(0) if len(self.order_updates) > 1 else self.order_updates[0]
        if isinstance(item, Exception):
            raise item
        return item


def make_task(**overrides: Any) -> Task:
    values: dict[str, Any] = {
        "product_ref": "test:free-fire",
        "product_title": "Free Fire",
        "offer_id": "ff-100",
        "offer_title": "100 diamantes",
        "player_id": "123456789",
        "schedule": "daily 12:00",
        "timezone": "UTC",
        "max_price": Decimal("5.00"),
        "currency": "BRL",
        "payment_method": "balance",
        "payment_title": "Баланс",
    }
    values.update(overrides)
    return Task(**values)
