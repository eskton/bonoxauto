"""Telegram-интерфейс бота (aiogram 3)."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from ..config import Settings
from ..engine import PurchaseEngine
from ..scheduler import Scheduler
from ..shop.base import ShopClient
from ..storage import Repository
from . import common, tasks, wizard
from .access import AdminOnly
from .notifier import TelegramNotifier

__all__ = ["BOT_COMMANDS", "TelegramNotifier", "build_dispatcher"]

BOT_COMMANDS = [
    BotCommand(command="tasks", description="Задачи автопокупки"),
    BotCommand(command="add", description="Новая задача"),
    BotCommand(command="history", description="История покупок"),
    BotCommand(command="status", description="Режим, расходы, ближайшие запуски"),
    BotCommand(command="mode", description="Dry-run или реальные покупки"),
    BotCommand(command="limits", description="Лимиты бюджета"),
    BotCommand(command="pause", description="Остановить все автопокупки"),
    BotCommand(command="resume", description="Возобновить автопокупки"),
    BotCommand(command="cancel", description="Отменить ввод"),
    BotCommand(command="help", description="Справка"),
]


def build_dispatcher(
    *,
    config: Settings,
    repo: Repository,
    shop: ShopClient,
    engine: PurchaseEngine,
    scheduler: Scheduler,
) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(AdminOnly(config.admin_ids))
    # Объекты, которые получают обработчики по имени параметра.
    dp["config"] = config
    dp["repo"] = repo
    dp["shop"] = shop
    dp["engine"] = engine
    dp["scheduler"] = scheduler
    # common первым: /cancel и другие команды работают на любом шаге мастера.
    dp.include_routers(common.router.build(), tasks.router.build(), wizard.router.build())
    return dp
