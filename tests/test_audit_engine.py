"""Local adversarial checks for scheduling, worker lifetime, and candidate fallback."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.browser.manager import BrowserManager, BrowserSession
from src.browser.session import ProfileLock
from src.core.engine import Engine
from src.core.exceptions import (
    CandidateUnavailable,
    HumanRequired,
    RetryableError,
    SelectorNotFound,
)
from src.core.models import Platform
from src.runtime import Runtime
from src.storage.database import Database
from tests.test_engine import QuietNotifier, make_engine, wait_state


@pytest.mark.parametrize(
    "failure",
    [
        RetryableError("offline retry"),
        HumanRequired("offline login"),
        RetryableError("offline 429", retry_after=120),
    ],
)
async def test_preparation_failure_cannot_skip_sale_deadline(tmp_path, monkeypatch, failure):
    engine, adapters, database = make_engine(tmp_path, max_checks=1)
    adapter = adapters[Platform.APPLE]
    current = datetime.now(UTC)
    sale = current + timedelta(minutes=5)
    engine.config = engine.config.model_copy(
        update={"sale": engine.config.sale.model_copy(update={"time": sale})}
    )
    cart_times = []
    waits = []
    original_open, original_cart = adapter.open_product, adapter.add_to_cart
    first = True

    async def advance(stop, seconds):
        nonlocal current
        waits.append(seconds)
        current += timedelta(seconds=seconds)
        await asyncio.sleep(0)
        return not stop.is_set()

    async def fail_first(product_id, url):
        nonlocal first
        if first:
            first = False
            raise failure
        await original_open(product_id, url)

    async def cart(quantity):
        cart_times.append(current)
        await original_cart(quantity)

    engine._now = lambda: current
    engine._waiter = advance
    monkeypatch.setattr("src.core.engine.wait_or_stop", advance)
    adapter.open_product = fail_first
    adapter.add_to_cart = cart
    task = asyncio.create_task(engine.run())
    try:
        if isinstance(failure, HumanRequired):
            await wait_state(engine, "WAITING_HUMAN")
            await engine.resume(Platform.APPLE)
        await asyncio.wait_for(task, timeout=2)
        assert cart_times and cart_times[0] >= sale
        if getattr(failure, "retry_after", 0):
            assert waits[0] >= failure.retry_after
        assert adapter.calls.count("submit_order") == 1
    finally:
        await engine.stop()
        await asyncio.gather(task, return_exceptions=True)
        database.close()


def tracked_browser_session(manager, platform, close):
    """Real local profile lock; the context is the only fault-injection boundary."""
    lock = ProfileLock(manager.profiles_dir / platform.value)
    lock.acquire()
    closed = asyncio.Event()
    context = SimpleNamespace(close=AsyncMock(side_effect=close))
    page = SimpleNamespace(is_closed=closed.is_set)
    session = BrowserSession(context, page, lock, closed)
    manager._sessions[platform] = session
    return session


@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_browser_close_fault_retains_contexts_and_profile_locks_for_retry(tmp_path, failure):
    manager = BrowserManager(tmp_path / "profiles")
    entered = asyncio.Event()

    async def fail_close():
        entered.set()
        if failure == "error":
            raise OSError("offline context close error")
        await asyncio.Future()

    first = tracked_browser_session(manager, Platform.APPLE, fail_close)
    second = tracked_browser_session(manager, Platform.JD, lambda: None)
    manager._playwright = SimpleNamespace(stop=AsyncMock())
    task = asyncio.create_task(manager.close())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if failure == "cancel":
            task.cancel()
        (result,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError if failure == "cancel" else OSError)
        blocked = []
        for platform in (Platform.APPLE, Platform.JD):
            challenger = ProfileLock(manager.profiles_dir / platform.value)
            try:
                challenger.acquire()
            except HumanRequired:
                blocked.append(True)
            else:
                blocked.append(False)
                challenger.release()
        assert set(manager._sessions) == {Platform.APPLE, Platform.JD} and blocked == [
            True,
            True,
        ], f"retained={list(manager._sessions)}; profile_locks_held={blocked}"
        assert manager._playwright.stop.await_count == 0
        first.context.close = AsyncMock(side_effect=first.closed.set)
        second.context.close = AsyncMock(side_effect=second.closed.set)
        await manager.close()
        assert manager._sessions == {} and manager._playwright is None
        assert first.lock._file is None and second.lock._file is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        first.closed.set()
        second.closed.set()
        first.lock.release()
        second.lock.release()
        await manager.close()


@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_runtime_close_fault_waits_for_context_and_closes_database_only_after_success(
    tmp_path, failure
):
    original, _, database = make_engine(tmp_path)
    database.close()
    original.config._root = tmp_path
    runtime = Runtime(original.config)
    entered, release = asyncio.Event(), asyncio.Event()
    attempted = False

    async def close_context():
        nonlocal attempted
        entered.set()
        if failure == "error" and not attempted:
            attempted = True
            raise OSError("offline context close error")
        await release.wait()
        session.closed.set()

    session = tracked_browser_session(runtime.manager, Platform.APPLE, close_context)
    runtime.manager._playwright = SimpleNamespace(stop=AsyncMock())
    task = asyncio.create_task(runtime.close())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        if failure == "cancel":
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
            assert not task.done() and not runtime._closed
        else:
            (result,) = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(result, OSError)
            assert not runtime._closed
        assert runtime.database.guard_status() is None
        assert runtime.manager._sessions[Platform.APPLE] is session
        assert session.lock._file is not None
        release.set()
        if failure == "error":
            await runtime.close()  # Failed closure remains retryable.
        else:
            await asyncio.gather(task, return_exceptions=True)
        assert runtime._closed and session.closed.is_set()
        assert runtime.manager._sessions == {} and runtime.manager._playwright is None
        assert session.lock._file is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        session.closed.set()
        session.lock.release()
        await runtime.close()


@pytest.mark.parametrize("immediate,dry_run", [(False, True), (True, True), (True, False)])
async def test_injected_clock_keeps_scheduled_dry_run_and_explicit_immediate_distinct(
    tmp_path, immediate, dry_run
):
    original, adapters, database = make_engine(tmp_path, dry_run=dry_run, max_checks=1)
    start = current = datetime.now(UTC)
    sale = start + timedelta(minutes=5)
    config = original.config.model_copy(
        update={"sale": original.config.sale.model_copy(update={"time": sale})}
    )

    async def advance(stop, seconds):
        nonlocal current
        current += timedelta(seconds=seconds)
        await asyncio.sleep(0)
        return not stop.is_set()

    engine = Engine(
        config, adapters, database, QuietNotifier(), now=lambda: current, waiter=advance
    )
    try:
        await engine.run(immediate=immediate)
        assert current == (start if immediate else sale)
        mode = ("immediate" if immediate else "scheduled") + ("_dry_run" if dry_run else "_live")
        assert engine.snapshot()["execution_mode"] == mode
        assert any("execution_mode=" + mode in row["message"] for row in database.recent("events"))
        assert adapters[Platform.APPLE].calls.count("submit_order") == (0 if dry_run else 1)
    finally:
        await engine.stop()
        database.close()


async def test_sale_permission_is_rechecked_after_human_pause_before_selection(tmp_path):
    original, adapters, database = make_engine(tmp_path, max_checks=1)
    current = datetime.now(UTC)
    sale = current + timedelta(minutes=5)
    config = original.config.model_copy(
        update={"sale": original.config.sale.model_copy(update={"time": sale})}
    )

    async def advance(stop, seconds):
        nonlocal current
        current += timedelta(seconds=seconds)
        await asyncio.sleep(0)
        return not stop.is_set()

    engine = Engine(
        config, adapters, database, QuietNotifier(), now=lambda: current, waiter=advance
    )
    adapter = adapters[Platform.APPLE]
    login, select = adapter.login_status, adapter.select_sku
    paused = False
    selections = []

    async def login_before_selection():
        nonlocal current, paused
        if engine._status[Platform.APPLE].state == "SELECTING_SKU" and not paused:
            paused = True
            # Model the clock moving back while the user handles an expired login.
            current = sale - timedelta(seconds=30)
            raise HumanRequired("offline expired login before selection")
        return await login()

    async def select_at_deadline(sku):
        selections.append(current)
        await select(sku)

    adapter.login_status, adapter.select_sku = login_before_selection, select_at_deadline
    task = asyncio.create_task(engine.run())
    try:
        await wait_state(engine, "WAITING_HUMAN")
        assert not selections and current < sale
        await engine.resume(Platform.APPLE)
        await asyncio.wait_for(task, timeout=2)
        assert selections == [sale]
        assert adapter.calls.count("add_to_cart") == adapter.calls.count("submit_order") == 1
    finally:
        await engine.stop()
        await asyncio.gather(task, return_exceptions=True)
        database.close()


async def test_worker_audit_failure_cancels_sibling_submission_before_runtime_releases_it(
    tmp_path, monkeypatch
):
    original_engine, adapters, database = make_engine(
        tmp_path, platforms=[Platform.APPLE, Platform.JD]
    )
    database.close()
    config = original_engine.config
    config._root = tmp_path
    runtime = Runtime(config)
    runtime.adapters = adapters
    apple, jd = adapters[Platform.APPLE], adapters[Platform.JD]
    submitted, cancelled, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    live_tasks = []
    original_open = apple.open_product
    first = True

    async def fail_after_sibling_submits(product_id, url):
        nonlocal first
        if first:
            first = False
            await submitted.wait()
            raise RuntimeError("offline worker audit trigger")
        await original_open(product_id, url)

    async def hanging_submit():
        jd.calls.append("submit_order")
        submitted.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await finish_cleanup.wait()
            raise

    original_record = runtime.database.record_event

    def failing_audit(run_id, event):
        if event["platform"] == "apple" and submitted.is_set():
            raise OSError("offline audit storage unavailable")
        return original_record(run_id, event)

    apple.open_product = fail_after_sibling_submits
    jd.submit_order = hanging_submit
    monkeypatch.setattr(runtime.database, "record_event", failing_audit)
    try:
        await runtime.start(immediate=True)
        await asyncio.wait_for(submitted.wait(), timeout=2)
        live_tasks = list(runtime.engine._tasks)
        await asyncio.wait_for(cancelled.wait(), timeout=0.3)
        assert runtime.engine.running and not runtime.task.done()
        assert runtime.engine._tasks == live_tasks
        finish_cleanup.set()
        await asyncio.gather(runtime.task, return_exceptions=True)
        assert all(task.done() for task in live_tasks)
        assert not runtime.engine.running and runtime.engine._tasks == []
        assert runtime.database.guard_status()["status"] in {"UNKNOWN", "SUBMITTING"}
        assert jd.calls.count("submit_order") == 1
    finally:
        finish_cleanup.set()
        for task in live_tasks:
            task.cancel()
        await asyncio.gather(*live_tasks, return_exceptions=True)
        await runtime.close()


async def test_finish_run_failure_still_clears_joined_worker_references_and_preserves_success(
    tmp_path, monkeypatch
):
    engine, adapters, database = make_engine(tmp_path)
    original_finish = database.finish_run

    def fail_finish(*args):
        raise OSError("offline finish_run failure")

    monkeypatch.setattr(database, "finish_run", fail_finish)
    try:
        with pytest.raises(OSError, match="finish_run"):
            await engine.run(immediate=True)
        assert not engine.running
        assert engine._tasks == []
        assert engine.snapshot()["platforms"]["apple"]["state"] == "SUCCESS"
        assert database.guard_status()["status"] == "SUCCESS"
    finally:
        monkeypatch.setattr(database, "finish_run", original_finish)
        await engine.stop()
        database.close()


@pytest.mark.parametrize("control", ["parent_cancel", "stop_twice", "close"])
async def test_runtime_never_releases_active_submission_during_cancel_stop_or_close(
    tmp_path, control
):
    original, adapters, database = make_engine(tmp_path)
    database.close()
    original.config._root = tmp_path
    runtime = Runtime(original.config)
    runtime.adapters = adapters
    adapter = adapters[Platform.APPLE]
    submitted, cancelling, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    controller_tasks = []

    async def submit():
        adapter.calls.append("submit_order")
        submitted.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelling.set()
            await finish.wait()
            raise

    async def close_browser():
        assert not runtime.engine.running and not runtime.engine._tasks
        assert runtime.task.done()

    adapter.submit_order = submit
    runtime.manager.close = AsyncMock(side_effect=close_browser)
    try:
        await runtime.start(immediate=True)
        await asyncio.wait_for(submitted.wait(), timeout=2)
        workers = list(runtime.engine._tasks)
        if control == "parent_cancel":
            runtime.task.cancel()
        elif control == "stop_twice":
            controller_tasks = [asyncio.create_task(runtime.stop()) for _ in range(2)]
        else:
            controller_tasks = [asyncio.create_task(runtime.close())]
        await asyncio.wait_for(cancelling.wait(), timeout=2)
        assert runtime.engine.running and not runtime.task.done()
        assert runtime.engine._tasks == workers
        if control == "close":
            controller_tasks[0].cancel()  # Closing must finish cleanup despite caller cancellation.
        finish.set()
        await asyncio.gather(runtime.task, *controller_tasks, return_exceptions=True)
        assert all(worker.done() for worker in workers)
        assert not runtime.engine.running and runtime.engine._tasks == []
        assert adapter.calls.count("submit_order") == 1
        stored = Database(original.config.paths.database)
        try:
            assert stored.guard_status()["status"] in {"UNKNOWN", "SUBMITTING"}
            restarted = Engine(original.config, adapters, stored, QuietNotifier())
            prior_calls = adapter.calls.copy()
            await restarted.run(immediate=True)
            assert adapter.calls == prior_calls
        finally:
            stored.close()
    finally:
        finish.set()
        await asyncio.gather(*controller_tasks, return_exceptions=True)
        await runtime.close()


@pytest.mark.parametrize("failure", ["success_audit", "submit_log"])
async def test_confirmed_success_is_not_downgraded_by_audit_or_logging_failure(
    tmp_path, monkeypatch, failure
):
    engine, adapters, database = make_engine(tmp_path)
    record = database.record_event

    def fail_success_event(run_id, event):
        if event["new_state"] == "SUCCESS":
            raise OSError("offline success audit failure")
        return record(run_id, event)

    logger = logging.getLogger("apple")
    info = logger.info

    def fail_submit_log(message, *args, **kwargs):
        if "action=%s" in message and len(args) > 2 and args[2] == "submit_order":
            raise OSError("offline log failure")
        return info(message, *args, **kwargs)

    if failure == "success_audit":
        monkeypatch.setattr(database, "record_event", fail_success_event)
    else:
        monkeypatch.setattr(logger, "info", fail_submit_log)
    try:
        result = await asyncio.gather(engine.run(immediate=True), return_exceptions=True)
        if failure == "success_audit":
            assert isinstance(result[0], OSError)
        assert database.guard_status()["status"] == "SUCCESS"
        assert engine.snapshot()["platforms"]["apple"]["state"] == "SUCCESS"
        assert not engine.running and engine._tasks == []
        calls = adapters[Platform.APPLE].calls.copy()
        await engine.run(immediate=True)
        assert adapters[Platform.APPLE].calls == calls
    finally:
        await engine.stop()
        database.close()


@pytest.mark.parametrize(
    "failure_stage", ["select", "precart", "all_unavailable", "selector", "cart", "submit"]
)
async def test_candidate_fallback_is_allowed_only_for_confirmed_selection_failure(
    tmp_path, failure_stage
):
    engine, adapters, database = make_engine(tmp_path, max_checks=3)
    engine.config = engine.config.model_copy(
        update={
            "monitor": engine.config.monitor.model_copy(update={"normal_interval": 10, "jitter": 0})
        }
    )
    adapter = adapters[Platform.APPLE]
    first = adapter.sku
    second = first.model_copy(update={"id": "second-candidate", "capacity": "256GB"})
    selected = []
    current = datetime.now(UTC)
    excluded_seen = []
    adapter.excluded_sku_ids = set()

    async def stock():
        await adapter.tick("check_stock")
        excluded_seen.append(set(adapter.excluded_sku_ids))
        # Match Apple's production first-match contract, not a fake returning
        # every alternative for Engine to consume in the same stock check.
        candidates = [first] if failure_stage == "all_unavailable" else [first, second]
        return next(([sku] for sku in candidates if sku.id not in adapter.excluded_sku_ids), [])

    async def advance(stop, seconds):
        nonlocal current
        current += timedelta(seconds=seconds)
        await asyncio.sleep(0)
        return not stop.is_set()

    async def select(sku):
        selected.append(sku.id)
        if sku.id == first.id:
            if failure_stage == "select" or (
                failure_stage == "all_unavailable" and selected.count(first.id) == 1
            ):
                raise CandidateUnavailable("offline first candidate no longer available")
            if failure_stage == "selector":
                raise SelectorNotFound("offline unknown selector is not missing stock")
        adapter.sku = sku

    adapter.check_stock, adapter.select_sku = stock, select
    engine._now, engine._waiter = lambda: current, advance
    if failure_stage == "precart":
        actual_cart = adapter.add_to_cart

        async def precheck_then_cart(quantity):
            if adapter.sku.id == first.id:
                raise CandidateUnavailable("offline stock disappeared before first click")
            await actual_cart(quantity)

        adapter.add_to_cart = precheck_then_cart
    if failure_stage == "cart":
        adapter.cart_error = CandidateUnavailable("wrongly classified after cart click")
    if failure_stage == "submit":
        adapter.submit_error = CandidateUnavailable("wrongly classified after submit click")
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        if failure_stage in {"selector", "cart"}:
            await wait_state(engine, "WAITING_HUMAN")
            await engine.stop()
        await asyncio.wait_for(task, timeout=2)
        if failure_stage in {"select", "precart", "all_unavailable"}:
            expected_next = first.id if failure_stage == "all_unavailable" else second.id
            assert selected == [first.id, expected_next]
            assert excluded_seen[1] == {first.id}
            if failure_stage == "all_unavailable":
                assert excluded_seen[2] == set()
            assert database.guard_status()["status"] == "SUCCESS"
        else:
            assert selected == [first.id]
        assert adapter.calls.count("add_to_cart") == (0 if failure_stage == "selector" else 1)
        assert adapter.calls.count("submit_order") == (
            1 if failure_stage in {"select", "precart", "all_unavailable", "submit"} else 0
        )
        if failure_stage == "submit":
            assert database.guard_status()["status"] == "UNKNOWN"
    finally:
        await engine.stop()
        await asyncio.gather(task, return_exceptions=True)
        database.close()
