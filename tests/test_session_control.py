"""Session controls preserve the run and use the same program profile."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_engine import FakeAdapter

from src.core.config import ProductTarget, load_config
from src.core.engine import Engine
from src.core.exceptions import ConfigurationError
from src.core.models import CartState, LoginStatus, OrderResult, Platform
from src.main import initialize
from src.runtime import Runtime


@pytest.fixture
async def runtime(tmp_path):
    path = tmp_path / "config/config.yaml"
    initialize(path)
    config = load_config(path)
    # These controls exercise the existing program-owned profile, not tab pairing.
    config = config.model_copy(update={"app": config.app.model_copy(update={"browser": "chrome"})})
    instance = Runtime(config)
    try:
        yield instance
    finally:
        await instance.close()


async def test_busy_login_controls_do_not_navigate(runtime, monkeypatch):
    open_visible = AsyncMock()
    monkeypatch.setattr(runtime.manager, "open_visible", open_visible)
    runtime.task = asyncio.create_task(asyncio.Event().wait())
    for operation in (runtime.open_login, runtime.check_login):
        with pytest.raises(ConfigurationError, match="运行中"):
            await operation()
    open_visible.assert_not_called()


async def test_manual_login_and_status_never_claim_success_from_opening(runtime, monkeypatch):
    page = SimpleNamespace(url="https://www.apple.com.cn/shop/buy-iphone/local-fixture")
    open_visible = AsyncMock(return_value=page)
    monkeypatch.setattr(runtime.manager, "open_visible", open_visible)
    adapter = runtime.adapters[Platform.APPLE]
    navigate = AsyncMock()
    monkeypatch.setattr(adapter, "_navigate", navigate)
    await runtime.open_login()
    open_visible.assert_awaited_once_with(Platform.APPLE)
    assert navigate.await_args.args[0] is page
    assert runtime.snapshot()["platforms"]["apple"]["login"] == "UNKNOWN"
    monkeypatch.setattr(runtime.manager, "current_page", lambda _: page)
    monkeypatch.setattr(adapter, "login_status", AsyncMock(return_value=LoginStatus.REQUIRED))
    assert (await runtime.check_login())["login"] == "REQUIRED"
    assert runtime.snapshot()["platforms"]["apple"]["login"] == "REQUIRED"
    assert runtime.database.guard_status() is None


async def test_saving_marketplace_target_preserves_purchase_policy_and_legacy_guard(runtime):
    original_app = runtime.config.app
    original_order = runtime.config.order
    source = runtime.config._source_path
    assert runtime.database.claim_order("legacy-run:apple")
    runtime.database.mark_submission("legacy-run:apple", "SUBMITTING")
    runtime.database.mark_submission("legacy-run:apple", "SUCCESS")
    guard = runtime.database.guard_status()
    target = ProductTarget(
        url="https://item.jd.com/fixture.html",
        seller_ids=["fixture-seller"],
        region="fixture-region",
        max_shipping=5,
    )
    product_id = next(iter(runtime.config.products))
    await runtime.save_target(Platform.JD, product_id, target)
    saved = load_config(source)
    assert saved.products[product_id].platforms[Platform.JD] == target
    assert saved.app == original_app and saved.order == original_order
    assert runtime.config._source_path == source
    assert runtime.database.guard_status() == guard
    assert not source.with_suffix(source.suffix + ".tmp").exists()


async def test_finished_tmall_order_still_blocks_taobao_navigation(runtime, monkeypatch):
    assert runtime.database.claim_order("legacy-run:tmall")
    runtime.database.mark_submission("legacy-run:tmall", "SUBMITTING")
    runtime.database.mark_submission("legacy-run:tmall", "UNKNOWN")
    runtime.task = asyncio.create_task(asyncio.sleep(0))
    await runtime.task
    open_visible = AsyncMock()
    monkeypatch.setattr(runtime.manager, "open_visible", open_visible)
    adapter = runtime.adapters[Platform.TAOBAO]
    open_product, login_status, read_order = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(adapter, "open_product", open_product)
    monkeypatch.setattr(adapter, "login_status", login_status)
    monkeypatch.setattr(adapter, "read_order_status", read_order)
    product_id = next(iter(runtime.config.products))
    for operation, args in (
        (runtime.open_login, (Platform.TAOBAO,)),
        (runtime.check_login, (Platform.TAOBAO,)),
        (runtime.check_product, (Platform.TAOBAO, product_id)),
        (runtime.save_target, (Platform.TAOBAO, product_id, ProductTarget())),
        (runtime.read_order, (Platform.TAOBAO,)),
    ):
        with pytest.raises(ConfigurationError):
            await operation(*args)
    for method in (open_visible, open_product, login_status, read_order):
        method.assert_not_called()
    assert runtime.database.guard_status()["status"] == "UNKNOWN"


async def test_finish_monitoring_task_keeps_session_and_allows_next_start(runtime, monkeypatch):
    product_id = next(iter(runtime.config.products))
    await runtime.save_target(
        Platform.APPLE,
        product_id,
        ProductTarget(url="https://www.apple.com.cn/shop/buy-iphone/fixture"),
    )
    adapter = FakeAdapter(Platform.APPLE)
    runtime.adapters[Platform.APPLE] = adapter
    entered = asyncio.Event()

    async def monitor_until_stopped():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "check_stock", monitor_until_stopped)
    monkeypatch.setattr(Engine, "_notify", AsyncMock())
    close_page = AsyncMock()
    monkeypatch.setattr(runtime.manager, "close", close_page)
    for _ in range(2):
        entered.clear()
        await runtime.start([Platform.APPLE], immediate=True, dry_run=True)
        await asyncio.wait_for(entered.wait(), 2)
        task = runtime.task
        result = await runtime.finish_task()
        assert result["ended"] and result["can_start_new_task"]
        assert task.done() and not runtime.engine._tasks and not runtime.engine.running
        assert runtime.adapters[Platform.APPLE] is adapter
    assert len(runtime.database.recent("runs")) == 2
    assert runtime.database.guard_status() is None
    assert "add_to_cart" not in adapter.calls and "submit_order" not in adapter.calls
    assert "close" not in adapter.calls
    close_page.assert_not_awaited()


@pytest.mark.parametrize("protection", ["SUCCESS", "UNKNOWN", "SUBMITTING", "CART_UNKNOWN"])
async def test_finish_task_preserves_order_records_and_transaction_protection(
    runtime, monkeypatch, protection
):
    adapter = runtime.adapters[Platform.APPLE]
    adapter._cart_attempted = True
    adapter._cart_state = CartState.ATTEMPTED_UNKNOWN
    adapter._submit_attempted = protection != "CART_UNKNOWN"
    run_id = runtime.database.create_run(None, "fixture")
    if protection != "CART_UNKNOWN":
        owner = run_id + ":apple"
        assert runtime.database.claim_order(owner)
        runtime.database.mark_submission(owner, "SUBMITTING")
        if protection != "SUBMITTING":
            runtime.database.mark_submission(owner, protection)
        runtime.database.record_order(
            run_id,
            FakeAdapter(Platform.APPLE).sku,
            protection,
            result=OrderResult(
                status="SUCCESS" if protection == "SUCCESS" else "UNKNOWN",
                order_id="fixture-unpaid",
                payment_state="UNPAID",
            ),
        )
    original_guard = runtime.database.guard_status()
    original_orders = runtime.database.recent("orders")
    close_page = AsyncMock()
    monkeypatch.setattr(runtime.manager, "close", close_page)
    for _ in range(2):
        result = await runtime.finish_task()
        assert result["ended"] and not result["can_start_new_task"]
        assert result["retained_protection"]["apple"]["cart_state"] == "ATTEMPTED_UNKNOWN"
        assert runtime.database.guard_status() == original_guard
        assert runtime.database.recent("orders") == original_orders
        assert runtime.adapters[Platform.APPLE] is adapter
        assert adapter._cart_attempted and adapter.cart_state == CartState.ATTEMPTED_UNKNOWN
        assert adapter._submit_attempted == (protection != "CART_UNKNOWN")
        with pytest.raises(ConfigurationError):
            await runtime.start([Platform.APPLE], immediate=True, dry_run=True)
    close_page.assert_not_awaited()
