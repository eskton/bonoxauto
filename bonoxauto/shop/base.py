"""Интерфейс магазина. Бот работает с любым магазином, реализующим ShopClient."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ShopError(Exception):
    """Операция не удалась. Для place_order это значит: заказ точно НЕ создан."""


class TemporaryShopError(ShopError):
    """Временная проблема (сеть, 5xx, лимит запросов) — чтение можно повторить."""


class ShopAuthError(ShopError):
    """Не удалось войти в аккаунт магазина."""


class InvalidAccountError(ShopError):
    """Игровой аккаунт (Player ID / Server ID) не найден."""


class OrderOutcomeUnknown(Exception):
    """Заказ мог создаться, а мог и нет (таймаут или обрыв связи после отправки).

    Такой заказ нельзя повторять автоматически — только проверить вручную на сайте.
    """


@dataclass(frozen=True)
class Offer:
    """Пакет на странице товара: например, «100 diamantes»."""

    id: str
    title: str
    price: Decimal
    currency: str
    available: bool = True


@dataclass(frozen=True)
class PaymentMethod:
    id: str
    title: str


@dataclass(frozen=True)
class Product:
    ref: str
    title: str
    offers: tuple[Offer, ...]
    requires_server_id: bool = False
    payment_methods: tuple[PaymentMethod, ...] = ()

    def offer(self, offer_id: str) -> Offer | None:
        return next((offer for offer in self.offers if offer.id == offer_id), None)


@dataclass(frozen=True)
class GameAccount:
    player_id: str
    server_id: str | None = None


@dataclass(frozen=True)
class OrderRequest:
    product_ref: str
    offer_id: str
    account: GameAccount
    max_total: Decimal  # итог к оплате не может быть больше — иначе заказ не оформлять
    currency: str
    payment_method: str | None
    idempotency_key: str  # уникален для каждой покупки бота


class OrderStatus(StrEnum):
    AWAITING_PAYMENT = "awaiting_payment"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PaymentInfo:
    """Как оплатить созданный заказ (например, Pix «copia e cola» или ссылка)."""

    method: str
    code: str | None = None
    url: str | None = None
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "code": self.code,
            "url": self.url,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaymentInfo:
        expires = data.get("expires_at")
        return cls(
            method=data.get("method") or "",
            code=data.get("code"),
            url=data.get("url"),
            expires_at=datetime.fromisoformat(expires) if expires else None,
        )


@dataclass(frozen=True)
class Order:
    id: str
    status: OrderStatus
    total: Decimal | None = None
    currency: str | None = None
    payment: PaymentInfo | None = None
    url: str | None = None
    message: str | None = None  # пояснение магазина, например причина отказа


class ShopClient(ABC):
    name: str = "shop"
    is_demo: bool = False
    #: Подсказка для мастера /add: что отправить боту, чтобы выбрать товар (HTML).
    product_hint: str = "Отправьте ссылку на товар."

    async def start(self) -> None:  # noqa: B027 — необязательный хук
        """Подготовка: вход в аккаунт, прогрев сессии. Вызывается один раз при запуске бота."""

    async def close(self) -> None:  # noqa: B027 — необязательный хук
        """Освободить ресурсы (HTTP-сессии и т.п.)."""

    def describe(self) -> str:
        """Короткое описание для /status."""
        return self.name

    @abstractmethod
    async def get_product(self, ref: str) -> Product:
        """Товар с актуальными ценами пакетов. ref — ссылка на страницу или идентификатор."""

    async def check_account(self, product: Product, account: GameAccount) -> str | None:
        """Проверить игровой аккаунт.

        Вернуть ник, если магазин умеет его показывать, иначе None.
        Если аккаунта не существует — InvalidAccountError.
        """
        return None

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """Оформить заказ. Бот вызывает это ровно один раз на покупку, без повторов.

        * заказ точно не создан → ShopError с понятной причиной;
        * неясно, создан ли заказ (таймаут, обрыв после отправки) → OrderOutcomeUnknown;
        * итог к оплате больше request.max_total → не оформлять, ShopError.
        """

    @abstractmethod
    async def get_order(self, order_id: str) -> Order:
        """Текущее состояние заказа — для ожидания оплаты и зачисления."""
