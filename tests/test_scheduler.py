import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import src.core.scheduler as scheduler_module
from src.core.clock import diagnostics
from src.core.scheduler import STAGES, Scheduler, backoff


def test_backoff_bounds_and_retry_after_minimum():
    assert [backoff(i, 0.5, 3, jitter=0) for i in range(5)] == [0.5, 1, 2, 3, 3]
    assert backoff(10**6, 0.5, 30) <= 30
    assert backoff(900, 1e300, 30) <= 30
    assert backoff(0, 0.01, 30, jitter=0) == 0.3
    assert backoff(0, 0.5, 30, retry_after=120) == 120
    assert all(0.3 <= backoff(i, 0.3, 5, jitter=0.5) <= 5 for i in range(30))


@pytest.mark.parametrize("changes", [
    {"attempt": -1}, {"base": 0}, {"maximum": 0.1}, {"jitter": 1.1},
    {"retry_after": -1}, {"base": float("inf")},
])
def test_invalid_backoff_rejected(changes):
    parameters = dict(attempt=0, base=1, maximum=10)
    parameters.update(changes)
    with pytest.raises(ValueError):
        backoff(**parameters)


async def test_scheduler_fake_clock_uses_short_waits(monkeypatch):
    start = datetime(2026, 9, 12, 12, tzinfo=UTC)
    current = start
    waits = []

    class FakeClock:
        @staticmethod
        def now(zone):
            return current

    async def advance_time(awaitable, timeout):  # noqa: ASYNC109 - match asyncio.wait_for
        nonlocal current
        awaitable.close()
        waits.append(timeout)
        current += timedelta(seconds=timeout)
        raise TimeoutError

    monkeypatch.setattr(scheduler_module, "datetime", FakeClock)
    monkeypatch.setattr(scheduler_module.asyncio, "wait_for", advance_time)
    scheduler = Scheduler(start + timedelta(seconds=7), asyncio.Event())
    assert await scheduler.wait_until(-4.5)
    assert waits == [1.0, 1.0, 0.5]
    assert [offset for _, offset in STAGES] == [-600, -180, -30, -5, 0]


async def test_scheduler_stops_without_waiting_for_target():
    stop = asyncio.Event()
    scheduler = Scheduler(datetime.now(UTC) + timedelta(hours=1), stop)
    task = asyncio.create_task(scheduler.wait_until())
    await asyncio.sleep(0)
    stop.set()
    assert await asyncio.wait_for(task, timeout=0.5) is False
    assert await Scheduler(None, stop).wait_until() is False
    assert await Scheduler(None, asyncio.Event()).wait_until() is True


def test_clock_reports_offset_without_claiming_external_synchronization():
    target = datetime.now(UTC) + timedelta(hours=1)
    result = diagnostics(target)
    assert result["timezone"] == "Asia/Shanghai"
    assert result["utc_offset_seconds"] == 28800
    assert 3590 < result["time_until_target_seconds"] <= 3600
    assert result["external_clock_skew_seconds"] is None
    with pytest.raises(ValueError):
        Scheduler(datetime(2026, 9, 12), asyncio.Event())
    with pytest.raises(ValueError):
        diagnostics(datetime(2026, 9, 12))
