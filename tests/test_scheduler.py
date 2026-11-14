import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bonoxauto.engine import PurchaseEngine
from bonoxauto.models import PurchaseStatus
from bonoxauto.scheduler import Scheduler
from bonoxauto.storage import Repository
from tests.fakes import FakeClock, FakeSleep, RecordingNotifier, ScriptedShop, make_task


@dataclass
class Env:
    scheduler: Scheduler
    engine: PurchaseEngine
    repo: Repository
    shop: ScriptedShop
    notifier: RecordingNotifier
    clock: FakeClock

    async def tick(self) -> None:
        jobs = await self.scheduler.tick()
        await asyncio.gather(*jobs)


@pytest.fixture
async def env(repo: Repository, clock: FakeClock) -> AsyncIterator[Env]:
    shop = ScriptedShop()
    notifier = RecordingNotifier()
    engine = PurchaseEngine(
        repo, shop, notifier, tz=ZoneInfo("UTC"), clock=clock, sleep=FakeSleep(clock)
    )
    scheduler = Scheduler(repo, engine, grace=timedelta(minutes=15), clock=clock, max_sleep=5)
    await repo.init_defaults(dry_run=False)
    yield Env(scheduler, engine, repo, shop, notifier, clock)
    await engine.shutdown()


async def test_due_task_runs_and_moves_to_next_slot(env: Env) -> None:
    task = await env.repo.add_task(make_task(next_run_at=env.clock.now))
    env.clock.advance(seconds=1)
    await env.tick()

    [purchase] = await env.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.COMPLETED
    assert purchase.slot == datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    reloaded = await env.repo.get_task(task.id or 0)
    assert reloaded is not None
    assert reloaded.next_run_at == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)

    await env.tick()  # повторный тик ничего не покупает
    assert len(env.shop.placed) == 1


async def test_not_yet_due(env: Env) -> None:
    await env.repo.add_task(make_task(next_run_at=env.clock.now + timedelta(minutes=1)))
    await env.tick()
    assert await env.repo.recent_purchases() == []


async def test_late_start_beyond_grace_is_missed(env: Env) -> None:
    task = await env.repo.add_task(make_task(next_run_at=env.clock.now - timedelta(hours=3)))
    await env.tick()
    [purchase] = await env.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.MISSED
    assert env.shop.placed == []
    reloaded = await env.repo.get_task(task.id or 0)
    assert reloaded is not None
    assert reloaded.next_run_at == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


async def test_late_start_within_grace_still_buys(env: Env) -> None:
    await env.repo.add_task(make_task(next_run_at=env.clock.now - timedelta(minutes=10)))
    await env.tick()
    [purchase] = await env.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.COMPLETED


async def test_once_task_finishes(env: Env) -> None:
    task = await env.repo.add_task(
        make_task(schedule="once 2026-11-03 12:00", next_run_at=env.clock.now)
    )
    await env.tick()
    reloaded = await env.repo.get_task(task.id or 0)
    assert reloaded is not None
    assert reloaded.enabled is False
    assert reloaded.next_run_at is None
    [purchase] = await env.repo.recent_purchases()
    assert purchase.status is PurchaseStatus.COMPLETED


async def test_global_pause_and_resume(env: Env) -> None:
    task = await env.repo.add_task(make_task(next_run_at=env.clock.now))
    await env.repo.set_paused(True)
    await env.tick()
    assert await env.repo.recent_purchases() == []

    # после паузы пропущенные слоты не догоняются
    env.clock.advance(hours=1)
    await env.scheduler.reschedule_all()
    await env.repo.set_paused(False)
    await env.tick()
    assert await env.repo.recent_purchases() == []
    reloaded = await env.repo.get_task(task.id or 0)
    assert reloaded is not None
    assert reloaded.next_run_at == datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


async def test_reschedule_disables_past_once_task(env: Env) -> None:
    task = await env.repo.add_task(make_task(schedule="once 2026-11-01 12:00"))
    task = await env.scheduler.reschedule(task)
    assert task.enabled is False
    assert task.next_run_at is None


async def test_poke_wakes_sleep(env: Env) -> None:
    env.scheduler.max_sleep = 30
    sleeper = asyncio.create_task(env.scheduler._sleep(0))
    await asyncio.sleep(0.01)
    env.scheduler.poke()
    await asyncio.wait_for(sleeper, 1)
