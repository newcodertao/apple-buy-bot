"""Local cart/login/console fault injection; no browser commerce or saved profiles."""

import asyncio

import pytest

import src.main as cli
from src.core.exceptions import HumanRequired
from src.core.models import CartState, LoginStatus, Platform, State
from src.runtime import Runtime
from tests.test_engine import FakeAdapter, QuietNotifier, make_engine


class CartAdapter(FakeAdapter):
    public_states = {State.PREPARING, State.WAITING, State.MONITORING, State.ADDING_CART}

    def __init__(self):
        super().__init__(Platform.APPLE)
        self.cart_state = CartState.NOT_ATTEMPTED
        self.interruption = None
        self.clicks = 0
        self.bag_matches = True
        self.env_login_succeeds = False

    async def add_to_cart(self, quantity):
        await self.tick("add_to_cart")
        if self.interruption == "before":
            self.interruption = None
            raise HumanRequired("Fixture requires confirmation before the first cart click")
        self.cart_state = CartState.ATTEMPTED_UNKNOWN
        self.clicks += 1
        if self.interruption == "after":
            raise HumanRequired("Fixture cart response was interrupted")
        if self.interruption != "silent":
            self.cart_state = CartState.CART_VERIFIED

    async def verify_cart(self, quantity):
        await self.tick("verify_cart")
        if not self.bag_matches:
            raise HumanRequired("Fixture bag does not contain the expected item and quantity")
        self.cart_state = CartState.CART_VERIFIED

    async def try_login_from_env(self):
        await self.tick("try_login_from_env")
        if self.env_login_succeeds:
            self.login = LoginStatus.AUTHENTICATED
        return {"status": "COMPLETE" if self.env_login_succeeds else "NOT_RUN"}


class Events(QuietNotifier):
    def __init__(self):
        self.events = []

    async def notify(self, event, message, platform=None):
        self.events.append(event)


def setup_engine(tmp_path):
    engine, _, database = make_engine(tmp_path, dry_run=True)
    adapter = CartAdapter()
    engine.adapters[Platform.APPLE] = adapter
    engine.notifier = Events()
    return engine, adapter, database


async def settled(task, predicate):
    async with asyncio.timeout(2):
        # Bounded fault-injection probe also catches a task ending without its expected event.
        while not predicate() and not task.done():  # noqa: ASYNC110
            await asyncio.sleep(0)
    assert predicate(), "The run finished before reaching the required safe pause"
    # Let incident capture/notification complete, so resume cannot precede event.clear().
    for _ in range(5):
        await asyncio.sleep(0)


async def cleanup(engine, task, database):
    await engine.stop()
    await asyncio.gather(task, return_exceptions=True)
    database.close()


@pytest.mark.parametrize("interruption", ["before", "after"])
async def test_cart_resume_requires_verified_bag_without_replaying_click(tmp_path, interruption):
    engine, adapter, database = setup_engine(tmp_path)
    adapter.interruption = interruption
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        await settled(
            task, lambda: engine.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN"
        )
        assert "cart" not in engine.notifier.events
        await engine.resume()
        await asyncio.wait_for(task, 2)
        assert adapter.cart_state == CartState.CART_VERIFIED
        assert adapter.clicks == 1
        assert adapter.calls.count("add_to_cart") == (2 if interruption == "before" else 1)
        assert adapter.calls.count("verify_cart") == (0 if interruption == "before" else 1)
        assert adapter.calls.index("goto_checkout") > adapter.calls.index("add_to_cart")
    finally:
        await cleanup(engine, task, database)


@pytest.mark.parametrize("interruption", ["after", "silent"])
async def test_unknown_cart_stays_paused_until_readonly_verification_passes(tmp_path, interruption):
    engine, adapter, database = setup_engine(tmp_path)
    adapter.interruption = interruption
    adapter.bag_matches = False
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        await settled(
            task, lambda: engine.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN"
        )
        if interruption == "after":
            await engine.resume()
            await settled(task, lambda: "verify_cart" in adapter.calls)
            await settled(
                task, lambda: engine.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN"
            )
        assert adapter.cart_state == CartState.ATTEMPTED_UNKNOWN
        assert adapter.clicks == 1
        assert "goto_checkout" not in adapter.calls
        assert "cart" not in engine.notifier.events
        adapter.bag_matches = True
        await engine.resume()
        await asyncio.wait_for(task, 2)
        assert adapter.cart_state == CartState.CART_VERIFIED
        assert adapter.calls.count("add_to_cart") == 1
        assert adapter.calls.count("goto_checkout") == 1
    finally:
        await cleanup(engine, task, database)


@pytest.mark.parametrize("login", [LoginStatus.UNKNOWN, LoginStatus.REQUIRED])
async def test_public_preparation_checks_login_before_stock_and_preserves_location(tmp_path, login):
    engine, adapter, database = setup_engine(tmp_path)
    adapter.login = login
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        await settled(
            task, lambda: engine.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN"
        )
        assert "check_stock" not in adapter.calls
        opened = adapter.calls.count("open_product")
        await engine.resume()
        for _ in range(20):
            await asyncio.sleep(0)
        assert adapter.calls.count("open_product") == opened
        assert adapter.calls.count("try_login_from_env") == (login == LoginStatus.REQUIRED)
        adapter.login = LoginStatus.AUTHENTICATED
        await engine.resume()
        await asyncio.wait_for(task, 2)
        assert adapter.clicks == 1
    finally:
        await cleanup(engine, task, database)


async def test_optional_normal_login_is_checked_again_and_challenge_still_pauses(tmp_path):
    engine, adapter, database = setup_engine(tmp_path)
    adapter.login = LoginStatus.REQUIRED
    adapter.env_login_succeeds = True
    original_login = adapter.try_login_from_env

    async def login_then_challenge():
        result = await original_login()
        adapter.challenge = True
        return result

    adapter.try_login_from_env = login_then_challenge
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        await settled(
            task, lambda: engine.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN"
        )
        assert adapter.calls.count("try_login_from_env") == 1
        assert adapter.calls.count("login_status") >= 2
        assert "check_stock" not in adapter.calls
        adapter.challenge = False
        await engine.resume()
        await asyncio.wait_for(task, 2)
        assert adapter.calls.count("try_login_from_env") == 1
    finally:
        await cleanup(engine, task, database)


def local_runtime(tmp_path, monkeypatch):
    engine, adapter, database = setup_engine(tmp_path)
    config = engine.config
    config._root = tmp_path
    database.close()
    monkeypatch.setattr("src.runtime.setup_logging", lambda path: None)
    monkeypatch.setattr(Runtime, "_build_adapters", lambda self: {Platform.APPLE: adapter})
    return Runtime(config), adapter


async def test_runtime_each_run_checks_expired_login_before_any_cart_action(tmp_path, monkeypatch):
    runtime, adapter = local_runtime(tmp_path, monkeypatch)
    adapter.login = LoginStatus.REQUIRED
    try:
        for run_number in (1, 2):
            await runtime.start([Platform.APPLE], immediate=True, dry_run=True)
            await settled(
                runtime.task,
                lambda: runtime.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN",
            )
            assert adapter.calls.count("try_login_from_env") == run_number
            assert "check_stock" not in adapter.calls
            await runtime.stop()
        assert adapter.clicks == 0
        assert runtime.database.guard_status() is None
    finally:
        await runtime.close()


async def test_console_parent_failure_preserves_runtime_page_until_stop(
    tmp_path, monkeypatch, capsys
):
    runtime, adapter = local_runtime(tmp_path, monkeypatch)
    queue = asyncio.Queue()
    monkeypatch.setattr(cli, "console_queue", lambda: queue)
    monkeypatch.setattr(cli, "load_config", lambda path: runtime.config)
    monkeypatch.setattr("src.runtime.Runtime", lambda config: runtime)
    page = object()
    monkeypatch.setattr(
        runtime.manager, "current_page", lambda platform: None if runtime._closed else page
    )

    def finish_failure(*args):
        raise OSError("fixture private data must not be printed")

    monkeypatch.setattr(runtime.database, "finish_run", finish_failure)
    task = asyncio.create_task(cli.async_main(cli.parser().parse_args(["run", "--now"])))
    try:
        async with asyncio.timeout(2):
            while runtime.task is None or not runtime.task.done():  # noqa: ASYNC110
                await asyncio.sleep(0)
        for _ in range(20):
            await asyncio.sleep(0)
        assert not runtime._closed, "A failed parent must retain the browser evidence"
        assert not task.done()
        assert runtime.database.guard_status()["status"] == "CLAIMED"
        queue.put_nowait("stop")
        assert await asyncio.wait_for(task, 2) == 2
        assert runtime._closed
        captured = capsys.readouterr()
        assert "OSError" in captured.out + captured.err
        assert "fixture private data" not in captured.out + captured.err
        assert adapter.clicks == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()


async def test_console_plan_approval_requires_the_displayed_digest(tmp_path, monkeypatch):
    runtime, adapter = local_runtime(tmp_path, monkeypatch)
    adapter.login = LoginStatus.REQUIRED
    queue = asyncio.Queue()
    monkeypatch.setattr(cli, "console_queue", lambda: queue)
    approved = asyncio.Event()
    original_approve = runtime.approve_plan

    async def observe_approval(digest):
        try:
            return await original_approve(digest)
        finally:
            approved.set()

    monkeypatch.setattr(runtime, "approve_plan", observe_approval)
    task = asyncio.create_task(cli.run_console(runtime, [Platform.APPLE], True, True))
    try:
        await settled(
            task,
            lambda: runtime.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN",
        )
        queue.put_nowait("approve-plan stale-digest")
        await asyncio.wait_for(approved.wait(), 2)
        assert not runtime.plan_snapshot()["approved"]
        approved.clear()
        queue.put_nowait("plan")
        queue.put_nowait("approve-plan " + runtime.plan_snapshot()["digest"])
        await asyncio.wait_for(approved.wait(), 2)
        assert runtime.plan_snapshot()["approved"]
        assert adapter.clicks == 0
        queue.put_nowait("stop")
        assert await asyncio.wait_for(task, 2) == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
