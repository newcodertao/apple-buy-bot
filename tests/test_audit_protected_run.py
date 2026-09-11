"""Program-profile acceptance on synthetic pages; no live commerce or account access.

The real AppleCNAdapter and Engine run unchanged. Every page request is fulfilled
locally and the isolated context is offline, including URLs shaped like Apple CN.
"""

import asyncio
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest
from test_apple_live_flow import REVIEW

from src.browser.manager import BrowserManager
from src.core.config import AppConfig
from src.core.engine import Engine
from src.core.models import Platform
from src.platforms.apple_cn.adapter import AppleCNAdapter
from src.storage.database import Database

PRODUCT_URL = "https://www.apple.com.cn/shop/buy-iphone/iphone-17"
ACCOUNT_NAV = '<nav id="globalnav"><a href="/shop/signOut">退出登录</a></nav>'
PRODUCT = """
<main id="root">
<input type="radio" name="dimensionColor" id="black" value="black" checked>
<label for="black">黑色</label>
<input type="radio" name="dimensionCapacity" id="capacity" value="256gb" checked>
<label for="capacity">256GB</label>
<input type="radio" id="noTradeIn" value="noTradeIn" checked>
<label for="noTradeIn">不折抵</label>
<input type="radio" name="applecare-options" id="care" value="on" checked>
<label for="care">不加 AppleCare+ 服务计划</label>
<h1 data-autom="summary-productName">iPhone 17 256GB 黑色</h1>
<div data-autom="full-price">RMB 6,799</div>
<div data-autom="dudeInfo">3-5 个工作日 免费送货</div>
<button id="globalnav-menubutton-link-bag" aria-expanded="false"
  onclick="this.setAttribute('aria-expanded',this.getAttribute('aria-expanded')!=='true');
    document.querySelector('#empty').hidden=this.getAttribute('aria-expanded')!=='true'">
  购物袋</button><h2 id="empty" hidden>你的购物袋是空的。</h2>
<button data-autom="add-to-cart" onclick="recordFixtureAction('add');location.href='/shop/attach'">
添加到购物袋</button></main>
"""
ATTACH = """<button data-autom="proceed"
onclick="recordFixtureAction('view_bag');location.href='/shop/bag'">查看购物袋</button>"""
BAG = """
<main id="bag-content"><ol data-autom="bag-items"><li>
<a data-autom="bag-item-name">iPhone 17 256GB 黑色</a>
<select data-autom="item-quantity-dropdown"><option selected>1</option><option>2</option></select>
<div data-autom="Monthly_price">RMB 6,799</div></li></ol>
<div data-autom="bagtotalvalue">RMB 6,799</div><div data-autom="bag-error-message"></div>
<button id="shoppingCart.actions.checkout"
onclick="recordFixtureAction('checkout');location.href='/shop/checkout'">结账</button></main>
"""
CHECKOUT = (
    REVIEW.replace("document.body.dataset.submits='1'", "recordFixtureAction('submit')")
    + '<button id="payment" onclick="recordFixtureAction(\'payment\')">立即支付</button>'
)


def configuration(tmp_path):
    config = AppConfig.model_validate(
        {
            "app": {"dry_run": True, "headless": True},
            "product": {
                "model_priority": ["iPhone 17"],
                "capacity_priority": ["256GB"],
                "color_priority": ["黑色"],
                "quantity": 1,
                "max_price": 6799,
                "max_total": 6799,
            },
            # auto_submit intentionally enabled: dry_run must still dominate it.
            "order": {"auto_submit": True, "payment_method": "wechat"},
            "monitor": {"max_checks": 1, "max_retries": 0, "jitter": 0},
            "products": {
                "test": {
                    "model": "iPhone 17",
                    "platforms": {"apple": {"url": PRODUCT_URL}},
                }
            },
        }
    )
    config._root = tmp_path
    return config


async def wait_for_review_pause(engine, task):
    while not task.done():
        if engine.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN":
            return
        await engine._guard_changed.wait()
    raise AssertionError("Protected run ended before the explicit address confirmation")


@pytest.mark.browser
async def test_real_apple_adapter_and_engine_reach_protected_review(tmp_path):
    config = configuration(tmp_path)
    manager = BrowserManager(config.paths.profiles, headless=True)
    database = Database(config.paths.database)
    adapter = AppleCNAdapter(
        manager,
        config.paths.screenshots,
        {"test": config.preferences_for("test")},
        allow_submit=True,
        payment_method="wechat",
    )
    engine = Engine(config, {Platform.APPLE: adapter}, database)
    actions, requests, unexpected = [], [], []
    task = None
    try:
        page = await manager.open(Platform.APPLE)
        await page.context.set_offline(True)
        await page.context.expose_function(
            "recordFixtureAction", lambda action: actions.append(action)
        )
        pages = {
            "/shop/buy-iphone/iphone-17": PRODUCT,
            "/shop/attach": ATTACH,
            "/shop/bag": BAG,
            "/shop/checkout": CHECKOUT,
        }

        async def fixture_route(route):
            parsed = urlsplit(route.request.url)
            requests.append(parsed.path)
            if parsed.hostname == "www.apple.com.cn" and parsed.path in pages:
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="<!doctype html>" + ACCOUNT_NAV + pages[parsed.path],
                )
            else:
                unexpected.append(parsed.path)
                await route.abort()

        await page.context.route("**/*", fixture_route)
        # A local human confirms the actual selected item before the protected run.
        # This is the real confirmation method, not a patched fingerprint/guard.
        await adapter.open_product("test", PRODUCT_URL)
        sku = (await adapter.get_skus())[0]
        await adapter.select_sku(sku)
        assert (await adapter.confirm_market())["market_verified"]
        task = asyncio.create_task(engine.run(immediate=True))
        await asyncio.wait_for(wait_for_review_pause(engine, task), timeout=20)
        assert urlsplit(page.url).path == "/shop/checkout", engine.snapshot()
        assert actions == ["add", "view_bag", "checkout"]
        assert database.guard_status() is None
        assert (await adapter.confirm_address())["address_confirmed"]
        assert (await adapter.confirm_market())["market_verified"]
        await engine.resume(Platform.APPLE)
        result = await asyncio.wait_for(task, timeout=10)
        assert result["platforms"]["apple"]["state"] == "READY_TO_SUBMIT"
        assert result["dry_run"] is True
        assert database.guard_status()["status"] == "CLAIMED"
        assert database.recent("orders")[0]["status"] == "READY_TO_SUBMIT"
        assert actions.count("submit") == actions.count("payment") == 0
        assert not adapter._submit_attempted
        assert actions.count("add") == actions.count("checkout") == 1
        assert {"/shop/bag", "/shop/checkout"} <= set(requests)
        assert not unexpected
        assert not page.is_closed()  # The payment/review page remains available to the user.
        assert config.paths.database.is_relative_to(tmp_path)
        assert config.paths.profiles.is_relative_to(tmp_path)
    finally:
        await engine.stop()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        database.close()


@pytest.mark.parametrize("status", ["SUCCESS", "UNKNOWN", "SUBMITTING"])
async def test_existing_guard_keeps_real_adapter_business_actions_at_zero(
    tmp_path, monkeypatch, status
):
    config = configuration(tmp_path)
    manager = BrowserManager(config.paths.profiles, headless=True)
    database = Database(config.paths.database)
    adapter = AppleCNAdapter(
        manager,
        config.paths.screenshots,
        {"test": config.preferences_for("test")},
        allow_submit=True,
        payment_method="wechat",
    )
    engine = Engine(config, {Platform.APPLE: adapter}, database)
    spies = []
    for name in (
        "open_product",
        "get_skus",
        "check_stock",
        "select_sku",
        "add_to_cart",
        "goto_checkout",
        "verify_order",
        "submit_order",
        "login_status",
        "detect_verification",
        "capture",
    ):
        spy = AsyncMock(wraps=getattr(adapter, name))
        monkeypatch.setattr(adapter, name, spy)
        spies.append(spy)
    try:
        assert database.claim_order("fixture-prior:apple")
        database.mark_submission("fixture-prior:apple", "SUBMITTING")
        if status != "SUBMITTING":
            database.mark_submission("fixture-prior:apple", status)
        original = database.guard_status()
        result = await engine.run(immediate=True)
        assert result["platforms"]["apple"]["state"] == "WAITING_HUMAN"
        assert database.guard_status() == original
        assert database.recent("orders") == []
        assert database.recent("stock_checks") == []
        for spy in spies:
            spy.assert_not_called()
        assert manager.current_page(Platform.APPLE) is None
    finally:
        await engine.stop()
        await manager.close()
        database.close()
