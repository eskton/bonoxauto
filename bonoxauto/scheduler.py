"""Планировщик: следит за временем следующего запуска задач и запускает покупки."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .engine import PurchaseEngine
from .models import Task, utcnow
from .schedule import parse_schedule
from .storage import Repository

log = logging.getLogger(__name__)


def next_run(task: Task, after: datetime) -> datetime | None:
    return parse_schedule(task.schedule).next_after(after, ZoneInfo(task.timezone))


class Scheduler:
    """Время следующего запуска каждой задачи хранится в БД (tasks.next_run_at).

    Перед запуском покупки next_run_at сдвигается на следующий слот, поэтому падение
    посреди покупки не приводит к её повтору. Если бот был выключен и проснулся позже,
    чем через `grace` после запланированного времени, запуск помечается пропущенным.
    """

    def __init__(
        self,
        repo: Repository,
        engine: PurchaseEngine,
        *,
        grace: timedelta,
        clock: Callable[[], datetime] = utcnow,
        max_sleep: float = 30.0,
    ) -> None:
        self.repo = repo
        self.engine = engine
        self.grace = grace
        self.clock = clock
        self.max_sleep = max_sleep
        self._wakeup = asyncio.Event()

    def poke(self) -> None:
        """Разбудить планировщик (задачи изменились)."""
        self._wakeup.set()

    async def run(self) -> None:
        log.info("Планировщик запущен")
        while True:
            floor = 0.0
            try:
                await self.tick()
            except Exception:
                log.exception("Ошибка в планировщике")
                floor = 5.0
            await self._sleep(floor)

    async def tick(self) -> list[asyncio.Task[Any]]:
        """Запустить всё, что пора. Возвращает запущенные фоновые задачи."""
        if await self.repo.is_paused():
            return []
        now = self.clock()
        started: list[asyncio.Task[Any]] = []
        for task in await self.repo.due_tasks(now):
            assert task.id is not None and task.next_run_at is not None
            slot = task.next_run_at
            try:
                upcoming = next_run(task, now)
            except Exception:
                log.exception("Не удалось рассчитать расписание задачи #%s", task.id)
                upcoming = None
            await self.repo.set_next_run(
                task.id, upcoming, enabled=False if upcoming is None else None
            )
            if now - slot > self.grace:
                log.warning(
                    "Задача #%s: запуск %s пропущен (опоздание %s)", task.id, slot, now - slot
                )
                started.append(self.engine.missed_in_background(task, slot))
            else:
                started.append(self.engine.run_in_background(task.id, slot))
        return started

    async def reschedule(self, task: Task) -> Task:
        """Пересчитать следующий запуск от текущего момента и сохранить задачу.

        Если запусков больше не будет (разовая задача в прошлом) — задача выключается.
        """
        if task.enabled:
            task.next_run_at = next_run(task, self.clock())
            if task.next_run_at is None:
                task.enabled = False
        else:
            task.next_run_at = None
        await self.repo.update_task(task)
        self.poke()
        return task

    async def reschedule_all(self) -> None:
        for task in await self.repo.list_tasks():
            if task.enabled:
                await self.reschedule(task)

    async def _sleep(self, floor: float) -> None:
        timeout = self.max_sleep
        if not await self.repo.is_paused():
            upcoming = await self.repo.earliest_next_run()
            if upcoming is not None:
                timeout = min(timeout, (upcoming - self.clock()).total_seconds())
        timeout = max(timeout, floor, 0.05)
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout)
        except TimeoutError:
            pass
        self._wakeup.clear()
