from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bonoxauto.storage import Repository
from tests.fakes import FakeClock


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repository]:
    repository = await Repository.open(tmp_path / "bot.db")
    yield repository
    await repository.close()


@pytest.fixture
def clock() -> FakeClock:
    # 03.11.2026 12:00 UTC, вторник
    return FakeClock(datetime(2026, 11, 3, 12, 0, tzinfo=UTC))
