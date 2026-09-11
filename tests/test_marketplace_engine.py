import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.browser.groups import profile_platform, session_key, unique_profile_platforms
from src.browser.manager import BrowserManager
from src.core.config import AppConfig
from src.core.engine import Engine, PurchaseTaken
from src.core.exceptions import HumanRequired
from src.core.models import OrderResult, Platform, SaleMode, StockState
from src.storage.database import Database
from tests.test_browser import LocalFixtureAdapter, local_site  # noqa: F401
from tests.test_engine import FakeAdapter, QuietNotifier, wait_state


def alibaba_engine(tmp_path, *, max_checks=1):
    config = AppConfig.model_validate(
        {
            "app": {"dry_run": False},
            "product": {"model_priority": ["Fixture Phone"]},
            "products": {
                "fixture": {
                    "model": "Fixture Phone",
                    # Deliberately reverse config order: Tmall must still go first.
                    "platforms": {
                        "taobao": {
                            "url": "https://item.taobao.com/item.htm?id=123",
                            "seller_ids": ["fixture-seller"],
                            "region": "fixture-region",
                        },
                        "tmall": {
                            "url": "https://detail.tmall.com/item.htm?id=456",
                            "seller_ids": ["fixture-seller"],
                            "region": "fixture-region",
                        },
                    },
                }
            },
            "monitor": {"max_checks": max_checks, "jitter": 0},
            "order": {"auto_submit": True, "allow_post_order_financing_check": True},
        }
    )
    adapters = {p: FakeAdapter(p) for p in (Platform.TAOBAO, Platform.TMALL)}
    for adapter in adapters.values():
        adapter.sku = adapter.sku.model_copy(
            update={
                "seller_id": "fixture-seller",
                "platform_item_id": adapter.platform.value + "-item",
                "region": "fixture-region",
                "observed_at": datetime.now(UTC),
                "stock_state": StockState.AVAILABLE,
                "sale_mode": SaleMode.NORMAL,
            }
        )
        adapter.review_updates = {
            "seller_id": adapter.sku.seller_id,
            "platform_item_id": adapter.sku.platform_item_id,
            "region": adapter.sku.region,
            "observed_at": datetime.now(UTC),
            "sale_mode": SaleMode.NORMAL,
            "stock_state": StockState.AVAILABLE,
            "items_subtotal": adapter.sku.price,
            "discount": 0,
            "shipping": 0,
            "fees": 0,
        }
    database = Database(tmp_path / "marketplace.db")
    return Engine(config, adapters, database, QuietNotifier()), adapters, database


async def test_taobao_and_tmall_open_and_close_one_existing_profile(tmp_path):
    page = SimpleNamespace(is_closed=lambda: False)
    context = SimpleNamespace(
        pages=[page],
        set_default_timeout=Mock(),
        set_default_navigation_timeout=Mock(),
        on=Mock(),
        close=AsyncMock(),
    )
    launch = AsyncMock(return_value=context)
    manager = BrowserManager(tmp_path / "profiles")
    manager._playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock()
    )
    first, second = await asyncio.gather(
        manager.open(Platform.TAOBAO), manager.open(Platform.TMALL)
    )
    assert first is second is manager.current_page(Platform.TAOBAO)
    assert launch.await_count == 1
    assert launch.await_args.kwargs["user_data_dir"] == str(tmp_path / "profiles" / "tmall")
    assert profile_platform(Platform.TAOBAO) == Platform.TMALL
    assert session_key(Platform.TAOBAO) == session_key(Platform.TMALL) == "alibaba"
    assert unique_profile_platforms() == (Platform.APPLE, Platform.JD, Platform.TMALL)
    await manager.close(Platform.TAOBAO)
    assert context.close.await_count == 1
    assert manager.current_page(Platform.TMALL) is None


async def test_shared_page_rotates_serially_with_per_channel_preparation_and_budget(
    tmp_path, monkeypatch
):
    engine, adapters, database = alibaba_engine(tmp_path, max_checks=2)
    trace, delays = [], []
    active = 0

    async def tick(platform, action):
        nonlocal active
        active += 1
        assert active == 1
        trace.append((platform, action))
        await asyncio.sleep(0)
        active -= 1

    async def no_delay(stop, seconds):
        delays.append(seconds)
        return not stop.is_set()

    for platform, adapter in adapters.items():
        adapter.sku = adapter.sku.model_copy(update={"available": False})
        adapter.tick = lambda action, p=platform: tick(p, action)
    monkeypatch.setattr("src.core.engine.wait_or_stop", no_delay)
    result = await engine.run(immediate=True)
    checks = [p for p, action in trace if action == "check_stock"]
    assert checks == [Platform.TMALL, Platform.TAOBAO] * 2
    assert delays == [10, 10, 10]
    events = list(reversed(database.recent("events")))
    for platform in adapters:
        first = next(event for event in events if event["platform"] == platform.value)
        assert (first["old_state"], first["new_state"]) == ("IDLE", "PREPARING")
        assert result["platforms"][platform]["state"] == "FAILED"
    assert engine.session_hold(Platform.TAOBAO) is None


async def test_human_pause_and_finished_unknown_keep_shared_page_and_global_order_guard(tmp_path):
    engine, adapters, database = alibaba_engine(tmp_path)
    tmall, taobao = adapters[Platform.TMALL], adapters[Platform.TAOBAO]
    tmall.challenge = True
    tmall.submit_result = OrderResult(status="UNKNOWN")
    task = asyncio.create_task(engine.run(immediate=True))
    await wait_state(engine, "WAITING_HUMAN", Platform.TMALL)
    assert engine.session_hold(Platform.TAOBAO) == {
        "active_channel": "tmall",
        "reason": "WAITING_HUMAN",
    }
    assert taobao.calls == []
    tmall.challenge = False
    await engine.resume(Platform.TAOBAO)  # Route a group resume to its actual active channel.
    await asyncio.wait_for(task, timeout=2)
    assert tmall.calls.count("add_to_cart") == tmall.calls.count("submit_order") == 1
    assert taobao.calls == []
    assert engine.session_hold(Platform.TAOBAO) == {"active_channel": "tmall", "reason": "UNKNOWN"}
    with pytest.raises(PurchaseTaken):
        await engine._call(Platform.TAOBAO, "add_to_cart", 1)
    replacement = Engine(engine.config, adapters, database, QuietNotifier())
    assert replacement.session_hold(Platform.TAOBAO) == engine.session_hold(Platform.TAOBAO)
    await replacement.stop()
    assert replacement.session_hold(Platform.TAOBAO)["reason"] == "UNKNOWN"


@pytest.mark.browser
async def test_action_guard_blocks_redirect_and_allows_only_declared_readonly_popup(
    tmp_path,
    local_site,  # noqa: F811 - imported shared pytest fixture
):
    manager = BrowserManager(tmp_path / "guard-profiles", headless=True)
    adapter = LocalFixtureAdapter(manager, tmp_path / "screenshots")
    try:
        page = await manager.open(Platform.APPLE)
        await adapter._navigate(page, local_site)
        await page.set_content(f'<a href="{local_site}" target="_blank">Shop</a>')
        async with adapter.navigation_guard(
            page, allowed_hosts=("127.0.0.1",), allowed_popup_hosts=("127.0.0.1",)
        ):
            async with page.expect_popup() as opened:
                await page.get_by_text("Shop", exact=True).click()
            popup = await opened.value
            await popup.wait_for_load_state("domcontentloaded")
        assert manager.current_page(Platform.APPLE) is page
        assert popup.url == local_site + "/"
        await popup.close()
        await page.set_content(f'<a href="{local_site}/redirect">Continue</a>')
        with pytest.raises(HumanRequired, match="approved platform"):
            async with adapter.navigation_guard(page, allowed_hosts=("127.0.0.1",)):
                await page.get_by_text("Continue", exact=True).click()
    finally:
        await manager.close()
