"""Адаптер bonoxs.com — ЗАГОТОВКА.

Из среды разработки сайт был недоступен, поэтому реальные запросы (вход, страница игры
с пакетами, проверка Player ID, оформление заказа, статус заказа) ещё не написаны.
Методы ниже отвечают понятной ошибкой, а бот при этом продолжает работать.
Что нужно для подключения — см. README, раздел «Подключение bonoxs.com».
"""

from __future__ import annotations

import re

from .base import Order, OrderRequest, Product, ShopClient, ShopError

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?bonoxs\.com/(?P<region>[a-z]{2})/(?P<slug>[a-z0-9][a-z0-9-]*)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

NOT_READY = (
    "Адаптер bonoxs.com ещё не подключён к сайту — реальные покупки пока недоступны. "
    "Используйте SHOP=demo, чтобы опробовать бота (подробности в README)."
)


def parse_product_url(url: str) -> tuple[str, str]:
    """https://bonoxs.com/br/free-fire → ('br', 'free-fire')."""
    match = _PRODUCT_URL_RE.match(url.strip())
    if not match:
        raise ShopError("Нужна ссылка на страницу игры, например https://bonoxs.com/br/free-fire")
    return match["region"].lower(), match["slug"].lower()


class BonoxsShop(ShopClient):
    name = "bonoxs"
    product_hint = (
        "Отправьте ссылку на страницу игры на bonoxs.com, "
        "например <code>https://bonoxs.com/br/free-fire</code>."
    )

    def __init__(self, *, email: str | None, password: str | None) -> None:
        self._email = email
        self._password = password

    def describe(self) -> str:
        return "bonoxs.com — ⚠️ адаптер ещё не подключён к сайту"

    async def get_product(self, ref: str) -> Product:
        parse_product_url(ref)
        raise ShopError(NOT_READY)

    async def place_order(self, request: OrderRequest) -> Order:
        raise ShopError(NOT_READY)

    async def get_order(self, order_id: str) -> Order:
        raise ShopError(NOT_READY)
