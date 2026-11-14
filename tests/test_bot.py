"""Сквозные сценарии бота: апдейты подаются в диспетчер, запросы к Telegram перехватываются."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bonoxauto.bot import build_dispatcher
from bonoxauto.bot.callbacks import TaskCb
from bonoxauto.config import Settings
from bonoxauto.engine import PurchaseEngine
from bonoxauto.models import PurchaseStatus
from bonoxauto.scheduler import Scheduler
from bonoxauto.shop.demo import DemoShop
from bonoxauto.storage import Repository
from tests.fakes import RecordingNotifier

ADMIN = 1001
STRANGER = 2002


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(1000)

    async def close(self) -> None:
        pass

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура aiogram
    ) -> Any:
        self.requests.append(method)
        if isinstance(method, SendMessage | EditMessageText):
            chat = Chat(id=int(method.chat_id or ADMIN), type="private")
            return Message(
                message_id=next(self._ids), date=datetime.now(UTC), chat=chat, text=method.text
            ).as_(bot)
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.session = MockSession()
        self.bot = Bot(
            "42:TEST", session=self.session, default=DefaultBotProperties(parse_mode="HTML")
        )
        self._ids = itertools.count(1)

    async def start(self) -> None:
        self.config = Settings(
            _env_file=None,  # type: ignore[call-arg]
            bot_token="42:TEST",
            admin_ids=str(ADMIN),
            timezone="Europe/Moscow",
            db_path=self.tmp_path / "bot.db",
        )
        self.repo = await Repository.open(self.config.db_path)
        await self.repo.init_defaults(dry_run=True)
        self.shop = DemoShop()
        self.notifier = RecordingNotifier()
        self.engine = PurchaseEngine(self.repo, self.shop, self.notifier, tz=self.config.tz)
        self.scheduler = Scheduler(self.repo, self.engine, grace=timedelta(minutes=15))
        self.dp = build_dispatcher(
            config=self.config,
            repo=self.repo,
            shop=self.shop,
            engine=self.engine,
            scheduler=self.scheduler,
        )

    async def stop(self) -> None:
        await self.engine.shutdown()
        await self.repo.close()

    def _user(self, user_id: int) -> User:
        return User(id=user_id, is_bot=False, first_name="Test")

    async def _feed(self, update: Update) -> list[TelegramMethod[Any]]:
        before = len(self.session.requests)
        await self.dp.feed_update(self.bot, update)
        return self.session.requests[before:]

    async def send(self, text: str, user_id: int = ADMIN) -> list[TelegramMethod[Any]]:
        n = next(self._ids)
        message = Message(
            message_id=n,
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=self._user(user_id),
            text=text,
        )
        return await self._feed(Update(update_id=n, message=message))

    async def click(self, data: str, user_id: int = ADMIN) -> list[TelegramMethod[Any]]:
        n = next(self._ids)
        message = Message(
            message_id=1, date=datetime.now(UTC), chat=Chat(id=user_id, type="private"), text="…"
        )
        query = CallbackQuery(
            id=str(n), from_user=self._user(user_id), chat_instance="ci", data=data, message=message
        )
        return await self._feed(Update(update_id=n, callback_query=query))

    def buttons(self) -> dict[str, str]:
        """Кнопки последнего сообщения с клавиатурой: {текст: callback_data}."""
        for request in reversed(self.session.requests):
            markup = getattr(request, "reply_markup", None)
            if markup is not None and getattr(markup, "inline_keyboard", None):
                return {
                    button.text: button.callback_data or ""
                    for row in markup.inline_keyboard
                    for button in row
                }
        raise AssertionError("нет сообщений с кнопками")

    def button(self, prefix: str) -> str:
        for text, data in self.buttons().items():
            if text.startswith(prefix):
                return data
        raise AssertionError(f"нет кнопки «{prefix}…» среди {list(self.buttons())}")

    def last_text(self) -> str:
        for request in reversed(self.session.requests):
            if isinstance(request, SendMessage | EditMessageText):
                return request.text
        raise AssertionError("нет сообщений")


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = Harness(tmp_path)
    await harness.start()
    yield harness
    await harness.stop()


async def _create_task(
    h: Harness, schedule: str = "ежедневно 12:00", *, double_click: bool = False
) -> None:
    await h.send("/add")
    await h.send("free-fire")
    await h.click(h.button("100 diamantes"))
    await h.send("123456789")
    assert "DemoPlayer6789" in h.last_text()
    await h.click(h.button("✅ Да"))
    await h.click(h.button("4,94"))  # +10%
    await h.send(schedule)
    await h.click(h.button("Баланс"))
    assert "Проверьте задачу" in h.last_text()
    create = h.button("✅ Создать")
    if double_click:
        await asyncio.gather(h.click(create), h.click(create))
    else:
        await h.click(create)


async def test_stranger_is_rejected(h: Harness) -> None:
    requests = await h.send("/tasks", user_id=STRANGER)
    assert len(requests) == 1
    assert isinstance(requests[0], SendMessage)
    assert "доступ закрыт" in requests[0].text
    requests = await h.click("t:list:0", user_id=STRANGER)
    assert isinstance(requests[0], AnswerCallbackQuery)


async def test_help(h: Harness) -> None:
    await h.send("/start")
    assert "/add" in h.last_text()
    assert "dry-run" in h.last_text()


async def test_add_task_wizard(h: Harness) -> None:
    await _create_task(h)
    [task] = await h.repo.list_tasks()
    assert task.product_ref == "demo:free-fire"
    assert task.offer_id == "ff-100"
    assert task.player_id == "123456789"
    assert task.nickname == "DemoPlayer6789"
    assert task.max_price == Decimal("4.94")
    assert task.currency == "BRL"
    assert task.schedule == "daily 12:00"
    assert task.timezone == "Europe/Moscow"
    assert task.payment_method == "balance"
    assert task.enabled and task.next_run_at is not None
    assert task.next_run_at > datetime.now(UTC)
    assert "Задача создана" in h.last_text()


async def test_wizard_validates_input(h: Harness) -> None:
    await h.send("/add")
    await h.send("genshin")
    assert "нет «genshin»" in h.last_text()
    await h.send("mobile-legends")
    await h.click(h.button("86 diamonds"))
    await h.send("123456789")
    assert "Server ID" in h.last_text()
    await h.send("123456789 (1234)")
    await h.click(h.button("✅ Да"))
    await h.send("бесплатно")
    assert "числом" in h.last_text()
    await h.send("8,50")
    await h.send("когда-нибудь")
    assert "Не понял" in h.last_text()
    await h.send("разово 01.01.2020 12:00")
    assert "уже прошло" in h.last_text()
    await h.send("/cancel")
    assert h.last_text() == "Отменено."
    assert await h.repo.list_tasks() == []


async def test_task_card_actions(h: Harness) -> None:
    await _create_task(h)
    [task] = await h.repo.list_tasks()

    await h.send("/tasks")
    await h.click(h.button("✅ #1"))
    assert "#1" in h.last_text() and "ежедневно в 12:00" in h.last_text()

    await h.click(h.button("⏸ Пауза"))
    paused = await h.repo.get_task(task.id or 0)
    assert paused is not None and not paused.enabled and paused.next_run_at is None

    await h.click(h.button("▶️ Включить"))
    resumed = await h.repo.get_task(task.id or 0)
    assert resumed is not None and resumed.enabled and resumed.next_run_at is not None

    await h.click(h.button("🕒 Расписание"))
    await h.send("пн, чт 18:30")
    edited = await h.repo.get_task(task.id or 0)
    assert edited is not None and edited.schedule == "weekly mon,thu 18:30"

    await h.click(h.button("💰 Лимит"))
    await h.send("6")
    edited = await h.repo.get_task(task.id or 0)
    assert edited is not None and edited.max_price == Decimal("6.00")

    await h.click(h.button("🗑 Удалить"))
    await h.click(h.button("🗑 Да"))
    assert await h.repo.list_tasks() == []
    assert "Задач пока нет" in h.last_text()


async def test_buy_now_in_dry_run(h: Harness) -> None:
    await _create_task(h)
    await h.send("/tasks")
    await h.click(h.button("✅ #1"))
    await h.click(h.button("🛒 Купить сейчас"))
    await h.engine.drain()
    [purchase] = await h.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.DRY_RUN
    assert purchase.manual is True
    assert "Dry-run" in h.notifier.last

    await h.send("/history")
    assert "Free Fire (демо) · 100 diamantes" in h.last_text()


async def test_live_mode_needs_confirmation_and_buys(h: Harness) -> None:
    await _create_task(h)
    await h.send("/mode")
    await h.click(h.button("💸 Включить"))
    assert await h.repo.is_dry_run() is True  # ещё не подтвердили
    await h.click(h.button("✅ Да"))
    assert await h.repo.is_dry_run() is False

    await h.send("/tasks")
    await h.click(h.button("✅ #1"))
    await h.click(h.button("🛒 Купить сейчас"))
    assert "Купить прямо сейчас" in h.last_text()  # в боевом режиме — подтверждение
    await h.click(h.button("✅ Да, купить"))
    await h.engine.drain()
    [purchase] = await h.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.COMPLETED
    assert purchase.order_id == "DEMO-0001"
    assert "Куплено" in h.notifier.last


async def test_limits_and_pause(h: Harness) -> None:
    await h.send("/limit день BRL 50")
    await h.send("/limit month brl 300,5")
    assert await h.repo.get_limits() == {
        ("day", "BRL"): Decimal("50.00"),
        ("month", "BRL"): Decimal("300.50"),
    }
    await h.send("/limit день BRL off")
    assert ("day", "BRL") not in await h.repo.get_limits()
    await h.send("/limit год BRL 1")
    assert "Задать" in h.last_text()

    await h.send("/limits")
    assert "300,50" in h.last_text()

    await h.send("/pause")
    assert await h.repo.is_paused() is True
    await h.send("/resume")
    assert await h.repo.is_paused() is False

    await h.send("/status")
    assert "Режим" in h.last_text()


async def test_double_click_create_makes_one_task(h: Harness) -> None:
    await _create_task(h, double_click=True)
    assert len(await h.repo.list_tasks()) == 1


async def test_double_click_buy_makes_one_order(h: Harness) -> None:
    await _create_task(h)
    await h.repo.set_dry_run(False)
    await h.send("/tasks")
    await h.click(h.button("✅ #1"))
    await h.click(h.button("🛒 Купить сейчас"))
    confirm = h.button("✅ Да, купить")
    await asyncio.gather(h.click(confirm), h.click(confirm))
    await h.click(confirm)  # и ещё раз позже — та же кнопка
    await h.engine.drain()
    [purchase] = await h.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.COMPLETED
    assert purchase.order_id == "DEMO-0001"


async def test_stale_buy_confirmation_is_refused(h: Harness) -> None:
    await _create_task(h)
    await h.repo.set_dry_run(False)
    [task] = await h.repo.list_tasks()
    old = int((datetime.now(UTC) - timedelta(minutes=11)).timestamp() * 1000)
    requests = await h.click(TaskCb(action="buy_ok", task_id=task.id or 0, stamp=old).pack())
    await h.engine.drain()
    assert await h.repo.recent_purchases() == []
    answers = [r for r in requests if isinstance(r, AnswerCallbackQuery)]
    assert answers and "устарело" in (answers[0].text or "")
