"""CLI lifecycle and manual reconciliation checks without real browser commerce."""

import asyncio

import pytest

import src.main as cli
from src.browser.session import ProfileLock
from src.core.config import AppConfig
from src.core.exceptions import HumanRequired
from src.core.models import Platform
from src.storage.database import Database


class ConsoleRuntime:
    def __init__(self, active=False):
        self.active = active
        self.task = None
        self.closed = False
        self.manager = self
        self.worker_started = asyncio.Event()
        self.hold_observed = asyncio.Event()
        self.resume_calls = 0

    async def start(self, platforms, immediate=False, dry_run=None):
        async def worker():
            self.worker_started.set()
            if self.active:
                await asyncio.Future()

        self.task = asyncio.create_task(worker())

    def snapshot(self):
        return {
            "running": bool(self.task and not self.task.done()),
            "platforms": {"apple": {"state": "MONITORING" if self.active else "WAITING_HUMAN"}},
            "order_guard": {"owner": "test-owner", "status": "UNKNOWN"},
        }

    def current_page(self, platform):
        self.hold_observed.set()
        return self if platform == Platform.APPLE and not self.closed else None

    async def stop(self):
        if self.task and not self.task.done():
            self.task.cancel()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)

    async def resume(self, platform=None):
        self.resume_calls += 1

    async def close(self):
        await self.stop()
        self.closed = True


def configure_console(monkeypatch, tmp_path, runtime):
    config = AppConfig()
    config._root = tmp_path
    queue = asyncio.Queue()
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "console_queue", lambda: queue)
    monkeypatch.setattr("src.runtime.Runtime", lambda settings: runtime)
    args = cli.parser().parse_args(["run", "--now"])
    return args, queue


@pytest.mark.parametrize("exit_action", ["stop", "browser_close"])
async def test_completed_unknown_worker_keeps_browser_open_until_user_exits(
    tmp_path, monkeypatch, exit_action,
):
    runtime = ConsoleRuntime()
    args, queue = configure_console(monkeypatch, tmp_path, runtime)
    task = asyncio.create_task(cli.async_main(args))
    try:
        await asyncio.wait_for(runtime.hold_observed.wait(), timeout=1)
        assert runtime.task.done(), "The ambiguous worker must already have finished"
        assert not task.done(), "The CLI must hold the browser even after its worker finishes"
        assert not runtime.closed
        if exit_action == "stop":
            queue.put_nowait("stop")
        else:
            runtime.closed = True
        assert await asyncio.wait_for(task, timeout=2) == 0
        assert runtime.closed
        assert runtime.resume_calls == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_console_stop_during_active_worker_exits_without_cancelled_error(
    tmp_path, monkeypatch,
):
    runtime = ConsoleRuntime(active=True)
    args, queue = configure_console(monkeypatch, tmp_path, runtime)
    task = asyncio.create_task(cli.async_main(args))
    try:
        await asyncio.wait_for(runtime.worker_started.wait(), timeout=1)
        queue.put_nowait("stop")
        assert await asyncio.wait_for(task, timeout=1) == 0
        assert runtime.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def reconciliation_setup(tmp_path, monkeypatch, status):
    config = AppConfig()
    config._root = tmp_path
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    database = Database(config.paths.database)
    database.initialize()
    assert database.claim_order("test-owner")
    database.mark_submission("test-owner", "SUBMITTING")
    database.mark_submission("test-owner", status)
    args = cli.parser().parse_args(["reconcile", "test-owner", "--confirmed-no-order"])
    return config, database, args


@pytest.mark.parametrize("locked_platform", list(Platform))
async def test_reconcile_requires_every_browser_profile_to_be_closed(
    tmp_path, monkeypatch, locked_platform,
):
    config, database, args = reconciliation_setup(tmp_path, monkeypatch, "UNKNOWN")
    in_use = ProfileLock(config.paths.profiles / locked_platform.value)
    in_use.acquire()
    try:
        with pytest.raises(HumanRequired, match="already in use"):
            await cli.async_main(args)
        assert database.guard_status()["status"] == "UNKNOWN"
        # A partial acquisition failure must release the profiles it acquired first.
        if locked_platform != Platform.APPLE:
            probe = ProfileLock(config.paths.profiles / Platform.APPLE.value)
            probe.acquire()
            probe.release()
        in_use.release()
        assert await cli.async_main(args) == 0
        assert database.guard_status() is None
    finally:
        in_use.release()
        database.close()


async def test_reconcile_never_clears_a_successful_order(tmp_path, monkeypatch, capsys):
    _, database, args = reconciliation_setup(tmp_path, monkeypatch, "SUCCESS")
    try:
        assert await cli.async_main(args) == 0
        assert database.guard_status()["status"] == "SUCCESS"
        assert '"reconciled": false' in capsys.readouterr().out
    finally:
        database.close()
