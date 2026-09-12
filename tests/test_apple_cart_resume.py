"""Local Apple cart recovery: a skipped click differs from an uncertain click."""
# ruff: noqa: F811 -- reuse the local Apple browser fixture.

from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout
from test_apple_live_flow import apple_page  # noqa: F401

from src.core.exceptions import CandidateUnavailable, HumanRequired
from src.core.models import CartState, LoginStatus

SNAPSHOT = {
    "models": [],
    "colors": [{"label": "黑色", "value": "black", "checked": True, "disabled": False}],
    "capacities": [{"label": "256GB", "value": "256gb", "checked": True, "disabled": False}],
    "summary": ["iPhone 17 256GB 黑色"],
    "price": ["RMB 6799"],
    "delivery": ["3-5 个工作日"],
    "pickup": [],
    "next": [],
    "add": [{"text": "添加到购物袋", "enabled": True}],
    "no_trade_in": True,
    "care": ["不加 AppleCare+ 服务计划"],
}

BAG = """<div id="bag-content"><ul data-autom="bag-items"><li>
<a data-autom="bag-item-name">iPhone 17 256GB 黑色</a>
<select data-autom="item-quantity-dropdown"><option>1</option><option>2</option></select>
<span data-autom="Monthly_price">RMB 6799</span></li></ul>
<span data-autom="bagtotalvalue">RMB 6799</span>
<div data-autom="bag-error-message"></div></div>"""

PRODUCT = """<div data-autom="summary-productName">iPhone 17 256GB 黑色</div>
<button id="globalnav-menubutton-link-bag" aria-expanded="false"
 onclick="const open=this.getAttribute('aria-expanded')!=='true';
 this.setAttribute('aria-expanded',String(open));document.querySelector('h1').hidden=!open">购物袋</button>
<h1 hidden>你的购物袋是空的。</h1>
<button data-autom="add-to-cart" onclick="document.body.dataset.adds='1';
 document.querySelector('[data-autom=proceed]').hidden=false">添加到购物袋</button>
<button data-autom="proceed" hidden onclick="document.querySelector('#bag-content').hidden=false;
 this.hidden=true">查看购物袋</button>""" + BAG.replace(
    'id="bag-content"', 'id="bag-content" hidden'
)


@pytest.mark.browser
async def test_confirmation_before_add_resumes_first_click_then_verifies(apple_page, monkeypatch):
    adapter, page = apple_page
    await page.set_content(PRODUCT)

    async def snapshot():
        return SNAPSHOT

    monkeypatch.setattr(adapter, "_snapshot", snapshot)
    with pytest.raises(HumanRequired, match="国行"):
        await adapter.add_to_cart(1)
    assert not adapter._cart_attempted
    assert adapter.cart_state == CartState.NOT_ATTEMPTED
    await adapter.confirm_market()
    # Definite loss before the first click is safe to hand back to candidate search.
    for changes in ({"price": ["RMB 6800"]}, {"add": [{"enabled": False}]}):
        async def changed_snapshot(changes=changes):
            return {**SNAPSHOT, **changes}

        monkeypatch.setattr(adapter, "_snapshot", changed_snapshot)
        with pytest.raises(CandidateUnavailable):
            await adapter.add_to_cart(1)
        assert adapter.cart_state == CartState.NOT_ATTEMPTED
        assert await page.locator("body").get_attribute("data-adds") is None
    monkeypatch.setattr(adapter, "_snapshot", snapshot)
    await adapter.add_to_cart(1)
    assert adapter.cart_state == CartState.CART_VERIFIED
    assert await page.locator("body").get_attribute("data-adds") == "1"


@pytest.mark.browser
async def test_unknown_add_resumes_by_reading_bag_without_another_add(apple_page, monkeypatch):
    adapter, page = apple_page
    await page.set_content(
        PRODUCT.replace("document.querySelector('[data-autom=proceed]').hidden=false", "void 0")
    )

    async def snapshot():
        return SNAPSHOT

    monkeypatch.setattr(adapter, "_snapshot", snapshot)
    await adapter.confirm_market()
    original_locator = adapter._locator

    async def missing_bag_response(**_kwargs):
        raise PlaywrightTimeout("injected lost response after the real add click")

    # Inject only the missing response, not a 100ms timeout for all real clicks.
    monkeypatch.setattr(
        adapter,
        "_locator",
        lambda key: (
            SimpleNamespace(wait_for=missing_bag_response)
            if key == "view_bag"
            else original_locator(key)
        ),
    )
    with pytest.raises(HumanRequired, match="不会重复加购"):
        await adapter.add_to_cart(1)
    assert adapter.cart_state == CartState.ATTEMPTED_UNKNOWN
    assert await page.locator("body").get_attribute("data-adds") == "1"
    monkeypatch.setattr(adapter, "_locator", original_locator)
    await page.set_content(
        BAG + '<button data-autom="add-to-cart" '
        'onclick="document.body.dataset.adds=1">添加到购物袋</button>'
    )
    await adapter.verify_cart(1)
    assert adapter.cart_state == CartState.CART_VERIFIED
    assert await page.locator("body").get_attribute("data-adds") is None
    with pytest.raises(HumanRequired):
        await adapter.verify_cart(2)
    assert adapter.cart_state == CartState.ATTEMPTED_UNKNOWN
    assert await page.locator('[data-autom="item-quantity-dropdown"]').input_value() == "1"


@pytest.mark.browser
async def test_bag_mismatch_never_becomes_verified(apple_page):
    adapter, page = apple_page
    adapter._cart_attempted = True
    await page.set_content(BAG.replace("RMB 6799", "RMB 6800"))
    with pytest.raises(HumanRequired):
        await adapter.verify_cart(1)
    assert adapter.cart_state == CartState.ATTEMPTED_UNKNOWN


@pytest.mark.browser
async def test_login_distinguishes_loading_ready_and_error_without_reading_values(
    apple_page, monkeypatch
):
    adapter, page = apple_page
    await page.goto("http://127.0.0.1/shop/signIn")
    assert (await adapter.login_status_detail())["component"] == "LOADING"
    assert await adapter.login_status() == LoginStatus.UNKNOWN
    assert (await adapter.detect_verification()).reason == "login_loading"
    await page.set_content(
        '<label for="account">电子邮件或电话号码</label>'
        '<input id="account" value="TEST_PRIVATE_ACCOUNT">'
    )
    detail = await adapter.login_status_detail()
    assert detail["component"] == "READY" and detail["status"] == "REQUIRED"
    assert "TEST_PRIVATE_ACCOUNT" not in str(detail)
    # The real account widget is an iframe. This route is a local HTML fixture
    # and prevents any request to the external authentication service.
    await page.route(
        "https://idmsa.apple.com/**",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body='<label for="account">电子邮件或电话号码</label><input id="account">',
        ),
    )
    await page.set_content('<iframe src="https://idmsa.apple.com/local-fixture"></iframe>')
    assert (await adapter.login_status_detail())["component"] == "READY"
    await page.set_content(
        '<div id="signin-container"><div role="alert">TEST_PRIVATE_ERROR</div></div>'
    )
    detail = await adapter.login_status_detail()
    assert detail["component"] == "ERROR" and detail["status"] == "UNKNOWN"
    assert "TEST_PRIVATE_ERROR" not in str(detail)
    monkeypatch.setenv("APPLE_BUY_BOT_USERNAME", "TEST_ONLY_USERNAME")
    monkeypatch.setenv("APPLE_BUY_BOT_PASSWORD", "TEST_ONLY_PASSWORD")
    result = await adapter.try_login_from_env()
    assert result["status"] == "NOT_RUN"
    assert "TEST_ONLY_" not in str(result)
