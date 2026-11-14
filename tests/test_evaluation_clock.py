import asyncio
from datetime import UTC, datetime

import pytest

from app.common.clock import at_reference_time, reference_now, utcnow
from scripts.agent_snapshot import restore_snapshot


async def test_business_clock_is_task_local_and_operational_time_keeps_running():
    frozen = datetime(2020, 1, 1, tzinfo=UTC)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def evaluate():
        with at_reference_time(frozen):
            entered.set()
            await release.wait()
            assert reference_now() == frozen
            assert utcnow().year >= 2026

    task = asyncio.create_task(evaluate())
    await entered.wait()
    assert reference_now().year >= 2026
    release.set()
    await task
    assert reference_now().year >= 2026
    with pytest.raises(ValueError, match="timezone"):
        with at_reference_time(datetime(2020, 1, 1)):
            pass


async def test_snapshot_guard_rejects_normal_database_before_opening_session():
    def forbidden_session():
        pytest.fail("Normal database must never be touched by snapshot restore")

    with pytest.raises(ValueError, match="dedicated"):
        await restore_snapshot(
            forbidden_session, "postgresql+asyncpg://localhost/bashculture", [{}]
        )
