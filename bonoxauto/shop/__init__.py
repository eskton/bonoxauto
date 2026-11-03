"""Магазины: общий интерфейс и реализации."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import ShopClient
from .bonoxs import BonoxsShop
from .demo import DemoShop

if TYPE_CHECKING:
    from ..config import Settings

__all__ = ["BonoxsShop", "DemoShop", "ShopClient", "create_shop"]


def create_shop(config: Settings) -> ShopClient:
    if config.shop == "bonoxs":
        password = config.bonoxs_password.get_secret_value() if config.bonoxs_password else None
        return BonoxsShop(email=config.bonoxs_email, password=password)
    return DemoShop()
