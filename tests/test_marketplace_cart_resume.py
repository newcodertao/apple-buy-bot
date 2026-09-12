"""Real shared adapter and Engine on offline pages; never live marketplace acceptance."""

import asyncio

import pytest

from src.browser.manager import BrowserManager
from src.core.exceptions import HumanRequired
from src.core.models import CartState, Platform
from tests.test_cart_runtime_engine import Events
from tests.test_engine import make_engine
from tests.test_marketplace_flow import FixtureMarketplaceAdapter, field, fixture_html


async def wait_pause(engine, task, platform, reason):
    async with asyncio.timeout(15):
        while True:
            status = engine.snapshot()["platforms"][platform]
            if status["state"] == "WAITING_HUMAN" and reason in status["result"]:
                # Let pause notification complete before issuing the user resume.
                await asyncio.sleep(0.02)
                return
            if task.done():
                await task
                raise AssertionError(f"Run ended before expected pause: {status}")
            await asyncio.sleep(0.01)


@pytest.mark.browser
@pytest.mark.parametrize("platform", [Platform.JD, Platform.TAOBAO, Platform.TMALL])
@pytest.mark.parametrize("interruption", ["before", "after"])
async def test_marketplace_engine_recovers_cart_once(
    tmp_path, monkeypatch, platform, interruption
):
    engine, _, database = make_engine(tmp_path, platforms=[platform], dry_run=True, max_checks=1)
    url = {
        Platform.JD: "https://item.jd.com/123.html",
        Platform.TAOBAO: "https://item.taobao.com/item.htm?id=123",
        Platform.TMALL: "https://detail.tmall.com/item.htm?id=123",
    }[platform]
    targets = engine.config.products["fixture"].platforms
    target = targets[platform].model_copy(update={"url": url})
    targets[platform] = target
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    adapter = FixtureMarketplaceAdapter(
        manager,
        tmp_path / "screenshots",
        {"fixture": engine.config.preferences_for("fixture")},
        targets={"fixture": target},
        order_policy=engine.config.order,
    )
    adapter.platform = platform
    adapter.allowed_hosts = (url.split("/")[2],)
    engine.adapters[platform] = adapter
    engine.notifier = Events()
    original_click = adapter._click
    interrupted = False

    async def interrupt_add(key):
        nonlocal interrupted
        if key == "add_to_cart" and not interrupted:
            interrupted = True
            if interruption == "after":
                await original_click(key)
            raise HumanRequired("fixture interrupted " + interruption)
        await original_click(key)

    monkeypatch.setattr(adapter, "_click", interrupt_add)
    task = None
    try:
        page = await manager.open(platform)
        await page.context.set_offline(True)
        body = fixture_html().replace(
            "document.body.innerHTML = stages[stage];",
            "document.body.innerHTML = stages[stage]; "
            "if (!document.querySelector('[data-fixture=authenticated]')) "
            f"document.body.insertAdjacentHTML('beforeend', {field('authenticated', 'Ready')!r});",
        )
        await page.context.route(
            "**/*",
            lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=body),
        )
        task = asyncio.create_task(engine.run(immediate=True))
        await wait_pause(engine, task, platform, "fixture interrupted")
        assert "cart" not in engine.notifier.events
        assert adapter.cart_state == (
            CartState.NOT_ATTEMPTED if interruption == "before" else CartState.ATTEMPTED_UNKNOWN
        )
        assert await page.evaluate("sessionStorage.getItem('adds')") == (
            None if interruption == "before" else "1"
        )
        if interruption == "after":
            await page.evaluate("stages.cart = stages.cart.replace('value=\"1\"', 'value=\"2\"')")
        await engine.resume(platform)
        if interruption == "after":
            await wait_pause(engine, task, platform, "购物车")
            assert adapter.cart_state == CartState.ATTEMPTED_UNKNOWN
            assert "cart" not in engine.notifier.events and "checkout" not in engine.notifier.events
            assert await page.evaluate("sessionStorage.getItem('adds')") == "1"
            await page.locator('[data-fixture="cart_quantity"]').fill("1")
            await engine.resume(platform)
        await wait_pause(engine, task, platform, "address")
        assert adapter.cart_state == CartState.CART_VERIFIED
        assert engine.notifier.events.count("cart") == 1
        assert await page.locator('[data-fixture="submit_order"]').is_visible()
        assert await page.evaluate("sessionStorage.getItem('adds')") == "1"
        assert await page.evaluate("sessionStorage.getItem('submits')") is None
        assert database.guard_status() is None

        # Rechecking a now-mismatched cart invalidates proof and never adjusts it.
        await page.evaluate("show('cart')")
        await page.locator('[data-fixture="cart_quantity"]').fill("2")
        with pytest.raises(HumanRequired):
            await adapter.verify_cart(1)
        assert adapter.cart_state == CartState.ATTEMPTED_UNKNOWN
        assert await page.evaluate("sessionStorage.getItem('adds')") == "1"
        assert await page.locator('[data-fixture="cart_quantity"]').input_value() == "2"
    finally:
        await engine.stop()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        database.close()
