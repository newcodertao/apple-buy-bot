"""Offline regressions from Chrome observations; these are not live acceptance."""

import asyncio
from decimal import Decimal

import pytest
from test_engine import make_engine, wait_state

from src.browser.manager import BrowserManager
from src.core.config import ProductPreferences
from src.core.exceptions import HumanRequired, SelectorNotFound
from src.core.models import SKU, LoginStatus, Platform, State
from src.platforms.apple_cn.adapter import AppleCNAdapter
from src.platforms.apple_cn.parser import money, parse_skus, verify_installment_offer
from src.platforms.inspection import InspectionAdapter


@pytest.fixture
def product_snapshot():
    return {
        "models": [
            {
                "label": "iPhone 18 Pro Max\n6.9 英寸",
                "value": "6_9inch",
                "checked": True,
                "disabled": False,
            }
        ],
        "colors": [{"label": "黑色", "value": "black", "checked": True, "disabled": False}],
        "capacities": [
            {"label": "512GB\nRMB 12,999", "value": "512gb", "checked": True, "disabled": False}
        ],
        "summary": ["iPhone 18 Pro Max 512GB 黑色"],
        "price": ["RMB 12,999"],
        "delivery": ["预计送达日期:\n暂未发售"],
        "pickup": ["目前暂不提供 Apple Store 零售店取货服务"],
        "add": [],
        "next": [{"text": "继续", "enabled": False}],
        "no_trade_in": True,
        "care": ["不加 AppleCare+ 服务计划"],
    }


def parse(data):
    return parse_skus(data, product_id="pro-max", model="iPhone 18 Pro Max")[0]


def test_preorder_is_a_real_unavailable_sku_not_unknown(product_snapshot):
    sku = parse(product_snapshot)
    assert not sku.available
    assert sku.price == Decimal("12999")
    assert sku.capacity == "512GB" and sku.color == "黑色"
    assert "暂未发售" in sku.delivery


def test_delivery_available_even_if_pickup_is_unavailable(product_snapshot):
    product_snapshot.update(
        delivery=["3-5 个工作日\n免费送货"],
        next=[],
        add=[{"text": "添加到购物袋", "enabled": True}],
    )
    assert parse(product_snapshot).available


@pytest.mark.parametrize(
    "changes",
    [
        {"summary": ["iPhone 18 Pro 512GB 黑色"]},
        {"price": ["RMB 542/月 (24 期)"]},
        {"price": ["RMB 12999", "RMB 12999"]},
        {"next": []},
        {"no_trade_in": False},
        {"care": ["AppleCare+ 服务计划"]},
        {"colors": []},
    ],
)
def test_incomplete_or_mixed_snapshot_stops(product_snapshot, changes):
    product_snapshot.update(changes)
    with pytest.raises(SelectorNotFound):
        parse(product_snapshot)


@pytest.mark.parametrize(
    "text",
    [
        "RMB 542/月",
        "USD 999",
        "RMB 0",
        "RMB 12,99",
        "RMB 12999 起",
        "RMB 12999 或 RMB 542/月",
        "NaN",
    ],
)
def test_price_rejects_installments_ambiguous_currency_and_malformed_numbers(text):
    with pytest.raises(SelectorNotFound):
        money(text)


def test_24_month_zero_apr_offer_needs_explicit_fees_and_repayment_schedule():
    with pytest.raises(SelectorNotFound):
        verify_installment_offer("24 期\n0% 年化利率\nRMB 284/月\n总计 RMB 6,799", Decimal("6799"))


@pytest.mark.parametrize(
    "label",
    [
        "12 期0% 年化利率RMB 567/月总计 RMB 6,799",
        "24 期3% 年化利率RMB 284/月总计 RMB 6,799",
        "24 期0% 年化利率RMB 300/月总计 RMB 7,199",
        "24 期RMB 284/月总计 RMB 6,799",
        "24 期0% 年化利率RMB 284/月",
    ],
)
def test_no_silent_fallback_to_interest_or_other_terms(label):
    with pytest.raises(SelectorNotFound):
        verify_installment_offer(label, Decimal("6799"))


def test_disabled_purchase_button_does_not_invent_delivery(product_snapshot):
    product_snapshot["delivery"] = []
    sku = parse(product_snapshot)
    assert not sku.available and "未加载" in sku.delivery


def test_enabled_button_without_quotes_only_confirms_bag_eligibility(product_snapshot):
    product_snapshot.update(delivery=[], next=[], add=[{"text": "添加到购物袋", "enabled": True}])
    sku = parse(product_snapshot)
    assert sku.available and "待结账确认" in sku.delivery


@pytest.mark.browser
async def test_installment_bank_and_zero_rate_selection(apple_page):
    adapter, page = apple_page
    adapter.payment_method = "installments"
    await page.set_content("""<input type="radio" id="bank"
      data-autom="checkout-billingOptions-installments0000882476">
      <label for="bank"><img alt="中国建设银行"></label>
      <input type="radio" id="term" data-autom="installments0000882476-24">
      <label for="term">24 期0% 年化利率RMB 284/月总计 RMB 6,799</label>""")
    with pytest.raises(SelectorNotFound, match="完整还款计划"):
        await adapter._select_installments()
    assert await page.locator("#bank").is_checked()
    assert await page.locator("#term").is_checked()
    assert adapter._approved_installment is None
    await page.set_content(
        REVIEW.replace("微信支付", "中国建设银行").replace(
            '<img alt="中国建设银行">',
            '<img alt="中国建设银行">分期付款方案：24 个月，每月约 RMB 284',
        )
    )
    with pytest.raises(HumanRequired, match="重新核对"):
        await adapter._read_review()


@pytest.mark.browser
async def test_unverified_installment_review_cannot_submit(apple_page):
    adapter, page = apple_page
    adapter.payment_method = "installments"
    await page.set_content(REVIEW.replace("微信支付", "中国建设银行"))
    with pytest.raises(HumanRequired, match="重新核对"):
        await adapter._read_review()


@pytest.mark.browser
async def test_delivery_failure_stops_before_any_bag_action(apple_page, monkeypatch):
    adapter, page = apple_page
    await page.set_content(
        '<button data-autom="add-to-cart" onclick="document.body.dataset.adds=1">'
        "添加到购物袋</button>"
    )

    async def missing_quotes():
        return {"delivery": []}

    monkeypatch.setattr(adapter, "_snapshot", missing_quotes)

    async def confirmed_market():
        return "test-market"

    monkeypatch.setattr(adapter, "_market_fingerprint", confirmed_market)
    adapter._confirmed_market = "test-market"
    with pytest.raises(HumanRequired, match="配送信息未加载"):
        await adapter.add_to_cart(1)
    assert not adapter._cart_attempted
    assert await page.locator("body").get_attribute("data-adds") is None


class FixtureApple(AppleCNAdapter):
    allowed_hosts = ("127.0.0.1",)
    require_https = False

    def _validate_platform_url(self, url):
        InspectionAdapter._validate_platform_url(self, url)

    def _require_cn_store(self):
        # Local fixture only; production enforces the exact CN transaction hosts.
        pass


@pytest.fixture
async def apple_page(tmp_path):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    page = await manager.open(Platform.APPLE)
    await page.route(
        "http://127.0.0.1/**",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<!doctype html><body></body>"
        ),
    )
    await page.goto("http://127.0.0.1/shop/checkout")
    prefs = ProductPreferences(
        model_priority=["iPhone 17"], capacity_priority=["256GB"], color_priority=["黑色"]
    )
    adapter = FixtureApple(
        manager, tmp_path / "screenshots", {"test": prefs}, payment_method="wechat"
    )
    adapter.product_id = "test"
    adapter.selected = SKU(
        id="iphone17/256gb/black",
        platform=Platform.APPLE,
        product_id="test",
        model="iPhone 17",
        capacity="256GB",
        color="黑色",
        price=6799,
        available=True,
    )
    try:
        yield adapter, page
    finally:
        await manager.close()


REVIEW = """<main><h1>准备下单了吗？</h1>
<ol data-autom="bag-items"><li data-autom="bag-item-1">
<h2 class="rs-iteminfo-title">iPhone 17 256GB 黑色</h2>
<div class="rs-quantity-text">数量 <span>1</span></div>
<div data-autom="Monthly_price">RMB 6,799</div></li></ol>
<div data-autom="bagtotalvalue">RMB 6,799</div>
<div class="rs-review-billing"><div class="rs-review-billing-details">
<img alt="微信支付"></div></div>
<span data-autom="form-field-lastName">测试</span>
<span data-autom="form-field-firstName">测试</span>
<span data-autom="form-field-state">测试省</span>
<span data-autom="form-field-city">测试市</span>
<span data-autom="form-field-district">测试区</span>
<span data-autom="form-field-street">测试地址</span>
<span data-autom="form-field-countryCode">中国大陆</span>
<span data-autom="form-field-emailAddress">a***@example.test</span>
<span data-autom="form-field-fullDaytimePhone">1**********</span>
<button data-autom="continue-button-placeOrder" onclick="document.body.dataset.submits='1'">
立即下单</button></main>"""


@pytest.mark.browser
async def test_review_uses_rendered_masked_address_and_full_price(apple_page):
    adapter, page = apple_page
    await page.set_content(REVIEW)
    await adapter.confirm_address()
    await adapter.confirm_market()
    review = await adapter.verify_order()
    assert review.address_present and review.checkout_valid
    assert review.quantity == 1 and review.unit_price == Decimal("6799")
    assert await page.locator("body").get_attribute("data-submits") is None
    with pytest.raises(HumanRequired, match="最终提交关闭"):
        await adapter.submit_order()
    assert await page.locator("body").get_attribute("data-submits") is None


@pytest.mark.browser
@pytest.mark.parametrize(
    "old,new", [("微信支付", "中国建设银行"), ("iPhone 17 256GB 黑色", "iPhone 17 512GB 黑色")]
)
async def test_review_rejects_financing_or_wrong_sku(apple_page, old, new):
    adapter, page = apple_page
    await page.set_content(REVIEW.replace(old, new))
    with pytest.raises(HumanRequired):
        await adapter.verify_order()


@pytest.mark.browser
async def test_submission_once_and_receipt_without_payment(apple_page):
    adapter, page = apple_page
    adapter._allow_submit = True
    await page.route(
        "**/thankyou",
        lambda route: route.fulfill(
            content_type="text/html; charset=utf-8",
            body="""
      <h1 class="rs-qr-header">请使用你的微信扫描此二维码进行付款。</h1>
      <a class="rs-qr-ordernumber">订单 #W123456789</a>""",
        ),
    )
    await page.set_content(
        REVIEW.replace(
            "document.body.dataset.submits='1'", "location.href='/shop/checkout/thankyou'"
        )
    )
    await adapter.confirm_address()
    await adapter.confirm_market()
    result = await adapter.submit_order()
    assert result.status == "SUCCESS" and result.order_id == "W123456789"
    assert result.payment_state == "UNPAID"
    assert (await adapter.read_order_status("W999999999")).status == "UNKNOWN"
    adapter._last_order_id = None
    adapter.selected = None  # A restarted adapter only has the persisted order reference.
    assert (await adapter.read_order_status()).status == "UNKNOWN"
    restored = await adapter.read_order_status("W123456789")
    assert restored.status == "SUCCESS" and restored.payment_state == "UNPAID"
    assert (await adapter.submit_order()).status == "UNKNOWN"


@pytest.mark.browser
async def test_login_requires_real_signout_evidence(apple_page):
    adapter, page = apple_page
    await page.set_content('<nav id="globalnav"><a href="/shop/signOut">退出登录</a></nav>')
    assert await adapter.login_status() == LoginStatus.AUTHENTICATED
    await page.goto("http://127.0.0.1/shop/signIn")
    await page.set_content('<label for="account">电子邮件或电话号码</label><input id="account">')
    assert await adapter.login_status() == LoginStatus.REQUIRED
    assert (await adapter.detect_verification()).reason == "login"


async def test_preparation_requires_login_before_public_monitoring(tmp_path):
    engine, adapters, db = make_engine(tmp_path, dry_run=True, max_checks=1)
    adapter = adapters[Platform.APPLE]
    adapter.public_states = AppleCNAdapter.public_states
    adapter.login = LoginStatus.UNKNOWN
    adapter.sku = adapter.sku.model_copy(update={"available": False})
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        await wait_state(engine, "WAITING_HUMAN")
        assert "check_stock" not in adapter.calls
        adapter.login = LoginStatus.AUTHENTICATED
        await engine.resume()
        await asyncio.wait_for(task, timeout=2)
        assert "check_stock" in adapter.calls
        assert "add_to_cart" not in adapter.calls
        assert not engine._login_required(Platform.APPLE, State.MONITORING)
        assert engine._login_required(Platform.APPLE, State.VERIFYING)
        assert db.guard_status() is None
    finally:
        await engine.stop()
        await asyncio.gather(task, return_exceptions=True)
        db.close()
