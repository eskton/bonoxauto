from datetime import timedelta
from decimal import Decimal

import pytest

from bonoxauto.shop.base import (
    GameAccount,
    InvalidAccountError,
    OrderRequest,
    OrderStatus,
    PaymentInfo,
    ShopError,
)
from bonoxauto.shop.bonoxs import BonoxsShop, parse_product_url
from bonoxauto.shop.demo import DemoShop
from tests.fakes import FakeClock


def _request(**overrides: object) -> OrderRequest:
    values: dict[str, object] = {
        "product_ref": "demo:free-fire",
        "offer_id": "ff-100",
        "account": GameAccount("123456789"),
        "max_total": Decimal("5"),
        "currency": "BRL",
        "payment_method": "balance",
        "idempotency_key": "k1",
    }
    values.update(overrides)
    return OrderRequest(**values)  # type: ignore[arg-type]


async def test_demo_product_lookup() -> None:
    shop = DemoShop()
    for ref in ("free-fire", "demo:free-fire", "https://bonoxs.com/br/free-fire/", " Free-Fire "):
        assert (await shop.get_product(ref)).ref == "demo:free-fire"
    assert (await shop.get_product("mobile-legends")).requires_server_id is True
    with pytest.raises(ShopError):
        await shop.get_product("genshin")


async def test_demo_account_check() -> None:
    shop = DemoShop()
    product = await shop.get_product("free-fire")
    assert await shop.check_account(product, GameAccount("123456789")) == "DemoPlayer6789"
    with pytest.raises(InvalidAccountError):
        await shop.check_account(product, GameAccount("abc"))


async def test_demo_orders(clock: FakeClock) -> None:
    shop = DemoShop(clock=clock, pix_paid_after=timedelta(minutes=1))
    instant = await shop.place_order(_request())
    assert instant.status is OrderStatus.COMPLETED
    assert instant.total == Decimal("4.49")

    pix = await shop.place_order(_request(payment_method="pix"))
    assert pix.status is OrderStatus.AWAITING_PAYMENT
    assert pix.payment is not None and pix.payment.code
    assert (await shop.get_order(pix.id)).status is OrderStatus.AWAITING_PAYMENT
    clock.advance(minutes=1)
    assert (await shop.get_order(pix.id)).status is OrderStatus.COMPLETED

    with pytest.raises(ShopError):
        await shop.place_order(_request(max_total=Decimal("4.00")))
    with pytest.raises(ShopError):
        await shop.get_order("nope")


def test_payment_info_roundtrip(clock: FakeClock) -> None:
    info = PaymentInfo(method="Pix", code="000201", url="https://pay", expires_at=clock.now)
    assert PaymentInfo.from_dict(info.to_dict()) == info


def test_bonoxs_url_parsing() -> None:
    assert parse_product_url("https://bonoxs.com/br/free-fire") == ("br", "free-fire")
    assert parse_product_url("https://www.bonoxs.com/AR/mobile-legends-br/?x=1") == (
        "ar",
        "mobile-legends-br",
    )
    for bad in ("https://example.com/br/free-fire", "bonoxs.com/br/free-fire", "free-fire"):
        with pytest.raises(ShopError):
            parse_product_url(bad)


async def test_bonoxs_stub_reports_not_ready() -> None:
    shop = BonoxsShop(email=None, password=None)
    with pytest.raises(ShopError, match="не подключён"):
        await shop.get_product("https://bonoxs.com/br/free-fire")
    with pytest.raises(ShopError, match="Нужна ссылка"):
        await shop.get_product("free-fire")
