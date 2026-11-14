"""Запуск: python -m bonoxauto"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import timedelta
from html import escape

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramUnauthorizedError
from pydantic import ValidationError

from .bot import BOT_COMMANDS, TelegramNotifier, build_dispatcher
from .config import Settings
from .engine import PurchaseEngine
from .scheduler import Scheduler
from .shop import create_shop
from .shop.base import ShopError
from .storage import Repository

log = logging.getLogger("bonoxauto")


async def main(config: Settings) -> None:
    repo = await Repository.open(config.db_path)
    await repo.init_defaults(dry_run=config.dry_run)
    shop = create_shop(config)
    bot = Bot(
        config.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    notifier = TelegramNotifier(bot, sorted(config.admin_ids))
    engine = PurchaseEngine(
        repo,
        shop,
        notifier,
        tz=config.tz,
        payment_timeout=timedelta(minutes=config.payment_timeout_minutes),
        poll_interval=config.payment_poll_seconds,
    )
    scheduler = Scheduler(repo, engine, grace=timedelta(minutes=config.misfire_grace_minutes))
    dp = build_dispatcher(config=config, repo=repo, shop=shop, engine=engine, scheduler=scheduler)
    scheduler_task: asyncio.Task[None] | None = None
    try:
        me = await bot.get_me()  # заодно проверяем токен и связь с Telegram
        log.info("Telegram: @%s", me.username)
        try:
            await shop.start()
        except ShopError as exc:
            log.error("Магазин: %s", exc)
            await notifier.send(f"⚠️ Магазин не готов: {escape(str(exc))}")
        await engine.recover()
        try:
            await bot.set_my_commands(BOT_COMMANDS)
        except TelegramAPIError as exc:
            log.warning("Не удалось установить меню команд: %s", exc)
        tasks = await repo.list_tasks()
        active = sum(1 for t in tasks if t.enabled and t.next_run_at is not None)
        mode = "🧪 dry-run" if await repo.is_dry_run() else "💸 реальные покупки"
        await notifier.send(
            f"🤖 bonoxauto запущен\nМагазин: {escape(shop.describe())}\nРежим: {mode}\n"
            f"Активных задач: {active}",
            silent=True,
        )
        scheduler_task = asyncio.create_task(scheduler.run())
        log.info("Бот запущен: магазин %s, задач %s", shop.name, len(tasks))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
        await engine.shutdown()
        await shop.close()
        await repo.close()
        await bot.session.close()


def run() -> None:
    try:
        config = Settings()  # type: ignore[call-arg]  # значения берутся из окружения/.env
    except ValidationError as exc:
        print("Ошибка настроек — проверьте файл .env (образец: .env.example):", file=sys.stderr)
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]).upper()
            print(f"  {field}: {error['msg']}", file=sys.stderr)
        raise SystemExit(2) from None
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main(config))
    except KeyboardInterrupt:
        pass
    except TelegramUnauthorizedError:
        print("Telegram не принял BOT_TOKEN — проверьте токен от @BotFather.", file=sys.stderr)
        raise SystemExit(2) from None
    except TelegramNetworkError as exc:
        print(f"Нет связи с Telegram (api.telegram.org): {exc.message}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
