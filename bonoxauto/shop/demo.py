"""Демо-магазин: тестовый каталог в памяти, чтобы опробовать бота без реальных денег."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from ..models import utcnow
from .base import (
    GameAccount,
    InvalidAccountError,
    Offer,
    Order,
    OrderRequest,
    OrderStatus,
    PaymentInfo,
    PaymentMethod,
    Product,
    ShopClient,
    ShopError,
)

_PAYMENTS = (
    PaymentMethod("balance", "Баланс (демо, сразу)"),
    PaymentMethod("pix", "Pix (демо: «оплатится» сам через минуту)"),
)


class DemoShop(ShopClient):
    name = "demo"
    is_demo = True
    product_hint = (
        "Сейчас подключён <b>демо-магазин</b> — тестовый каталог, реальных покупок нет.\n"
        "Отправьте <code>free-fire</code> или <code>mobile-legends</code>."
    )

    def __init__(
        self,
        *,
        pix_paid_after: timedelta = timedelta(minutes=1),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._pix_paid_after = pix_paid_after
        self._clock = clock
        self._orders: dict[str, tuple[Order, datetime]] = {}
        self._counter = 0
        self.products: dict[str, Product] = {
            "free-fire": Product(
                ref="demo:free-fire",
                title="Free Fire (демо)",
                offers=(
                    Offer("ff-100", "100 diamantes", Decimal("4.49"), "BRL"),
                    Offer("ff-310", "310 diamantes", Decimal("12.99"), "BRL"),
                    Offer("ff-weekly", "Assinatura semanal", Decimal("8.90"), "BRL"),
                ),
                payment_methods=_PAYMENTS,
            ),
            "mobile-legends": Product(
                ref="demo:mobile-legends",
                title="Mobile Legends (демо)",
                offers=(
                    Offer("ml-86", "86 diamonds", Decimal("7.90"), "BRL"),
                    Offer("ml-weekly", "Weekly Diamond Pass", Decimal("9.90"), "BRL"),
                ),
                requires_server_id=True,
                payment_methods=_PAYMENTS,
            ),
        }

    def describe(self) -> str:
        return "демо-магазин (тестовый каталог, реальных покупок нет)"

    async def get_product(self, ref: str) -> Product:
        key = ref.strip().lower().rstrip("/").rsplit("/", 1)[-1].removeprefix("demo:")
        product = self.products.get(key)
        if product is None:
            raise ShopError(
                f"В демо-каталоге нет «{ref.strip()}». Доступно: {', '.join(self.products)}."
            )
        return product

    async def check_account(self, product: Product, account: GameAccount) -> str | None:
        if not account.player_id.isdigit():
            raise InvalidAccountError("Player ID должен состоять из цифр.")
        return f"DemoPlayer{account.player_id[-4:]}"

    async def place_order(self, request: OrderRequest) -> Order:
        product = await self.get_product(request.product_ref)
        offer = product.offer(request.offer_id)
        if offer is None or not offer.available:
            raise ShopError("пакет недоступен")
        if offer.price > request.max_total:
            raise ShopError("итог к оплате выше лимита")
        self._counter += 1
        order_id = f"DEMO-{self._counter:04d}"
        now = self._clock()
        if request.payment_method == "pix":
            order = Order(
                order_id,
                OrderStatus.AWAITING_PAYMENT,
                offer.price,
                offer.currency,
                payment=PaymentInfo(
                    method="Pix",
                    code=f"00020126580014BR.GOV.BCB.PIX-DEMO-{order_id}",
                    expires_at=now + timedelta(minutes=30),
                ),
            )
        else:
            order = Order(order_id, OrderStatus.COMPLETED, offer.price, offer.currency)
        self._orders[order_id] = (order, now)
        return order

    async def get_order(self, order_id: str) -> Order:
        try:
            order, created = self._orders[order_id]
        except KeyError:
            raise ShopError(f"заказ {order_id} не найден") from None
        if (
            order.status is OrderStatus.AWAITING_PAYMENT
            and self._clock() - created >= self._pix_paid_after
        ):
            order = replace(order, status=OrderStatus.COMPLETED)
            self._orders[order_id] = (order, created)
        return order
