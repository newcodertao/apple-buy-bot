"""Apple purchase-boundary regressions; all fixtures are local, never live orders."""
# ruff: noqa: F811 -- pytest fixtures imported from the existing local Apple suite.

from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_apple_live_flow import REVIEW, apple_page, product_snapshot  # noqa: F401

from src.core.config import ProductPreferences
from src.core.exceptions import (
    CandidateUnavailable,
    ConfigurationError,
    HumanRequired,
    SelectorNotFound,
)
from src.core.models import FinancingState
from src.platforms.apple_cn.adapter import AppleCNAdapter
from src.platforms.apple_cn.parser import parse_skus, verify_installment_offer

PLAN = (
    "中国建设银行 24期 本金RMB6799 总还款额RMB6799 "
    "利息RMB0 手续费RMB0 前23期每期RMB283.29 末期RMB283.33"
)


def test_zero_apr_does_not_prove_zero_fees_or_a_valid_payment_schedule():
    with pytest.raises(SelectorNotFound):
        verify_installment_offer("24期0%年化利率RMB1/月总计RMB6799", Decimal("6799"))


def test_complete_cent_schedule_and_last_payment_are_required():
    terms = verify_installment_offer(PLAN, Decimal("6799"), bank="中国建设银行")
    assert len(terms.payments) == 24 and sum(terms.payments) == Decimal("6799")
    assert terms.payments[-1] == Decimal("283.33")
    full = PLAN.split("前23期")[0] + " ".join(
        f"第{i}期RMB{amount}" for i, amount in enumerate(terms.payments, 1)
    )
    assert verify_installment_offer(full, Decimal("6799"), bank="中国建设银行") == terms
    no_tail = PLAN.replace("6799", "6720").replace("283.29", "280").replace("283.33", "280")
    assert set(
        verify_installment_offer(no_tail, Decimal("6720"), bank="中国建设银行").payments
    ) == {Decimal("280")}
    for text in (
        PLAN.replace("手续费RMB0", ""),
        PLAN.replace("手续费RMB0", "手续费RMB1"),
        PLAN.replace("手续费RMB0", "手续费RMB0.001"),
        PLAN.replace("末期RMB283.33", "末期RMB283.32"),
        PLAN.replace("24期", "12期"),
        PLAN.replace("本金RMB6799", "本金RMB6798"),
        PLAN.replace("中国建设银行", "其他银行"),
        PLAN + "RMB1/月",
    ):
        with pytest.raises(SelectorNotFound):
            verify_installment_offer(text, Decimal("6799"), bank="中国建设银行")


def test_enabled_continue_is_not_a_verified_purchase_path(product_snapshot):
    product_snapshot["next"][0]["enabled"] = True
    product_snapshot["delivery"] = ["3-5 个工作日"]
    with pytest.raises(SelectorNotFound, match="NOT RUN"):
        parse_skus(product_snapshot, product_id="pro-max", model="iPhone 18 Pro Max")


@pytest.mark.browser
async def test_current_installment_review_is_read_again_before_submission(apple_page):
    adapter, page = apple_page
    adapter.payment_method = "installments"
    adapter._approved_installment = verify_installment_offer(
        PLAN, Decimal("6799"), bank="中国建设银行"
    )
    html = REVIEW.replace('<img alt="微信支付">', '<img alt="中国建设银行">' + PLAN)
    await page.set_content(html)
    await adapter.confirm_address()
    await adapter.confirm_market()
    assert (await adapter._read_review()).financing.state == FinancingState.ELIGIBLE
    # A previous approval and its time cannot hide a now nonzero fee.
    await page.set_content(html.replace("手续费RMB0", "手续费RMB1"))
    with pytest.raises(SelectorNotFound):
        await adapter._read_review()
    await page.set_content(html.replace("中国建设银行", "其他银行"))
    with pytest.raises(HumanRequired):
        await adapter._read_review()
    assert await page.locator("body").get_attribute("data-submits") is None


@pytest.mark.browser
async def test_address_and_market_confirmation_bind_the_current_item(apple_page):
    adapter, page = apple_page
    await page.set_content(REVIEW)
    initial = await adapter._read_review()
    assert initial.address_present and not initial.address_confirmed
    assert not initial.market_verified
    address = await adapter.confirm_address()
    market = await adapter.confirm_market()
    assert "测试地址" not in str(address) and len(address["address_fingerprint"]) == 64
    assert market["market_evidence"].startswith("MANUAL_CN:")
    assert (await adapter._read_review()).address_confirmed
    await page.set_content(REVIEW.replace("测试地址", "另一个地址"))
    assert not (await adapter._read_review()).address_confirmed
    extra = REVIEW + '<span data-autom="form-field-street2">测试单元一</span>'
    await page.set_content(extra)
    await adapter.confirm_address()
    await page.set_content(extra.replace("测试单元一", "测试单元二"))
    assert not (await adapter._read_review()).address_confirmed
    await page.set_content(REVIEW.replace("</li>", "<span>TEST123ZP/A</span></li>"))
    assert not (await adapter._read_review()).market_verified
    with pytest.raises(HumanRequired):
        await adapter.confirm_market()


def test_auth_domain_does_not_authorize_a_foreign_store(tmp_path):
    page = SimpleNamespace(url="https://www.apple.com/shop/buy-iphone/iphone-17")
    manager = SimpleNamespace(current_page=lambda platform: page)
    adapter = AppleCNAdapter(manager, tmp_path)
    with pytest.raises(HumanRequired, match="中国大陆"):
        adapter._require_cn_store()
    page.url = "https://secure10.www.apple.com.cn/shop/checkout"
    adapter._require_cn_store()
    for host in ("account.apple.com", "appleid.apple.com", "idmsa.apple.com"):
        page.url = f"https://{host}/auth"
        adapter._validate_platform_url(page.url)
        with pytest.raises(HumanRequired):
            adapter._require_cn_store()
    for host in (
        "unknown.apple.com",
        "account.apple.com.cn",
        "secure10.www.apple.com.cn.evil.test",
    ):
        with pytest.raises(ConfigurationError):
            adapter._validate_platform_url(f"https://{host}/shop/checkout")


@pytest.mark.browser
async def test_unavailable_candidate_is_distinct_from_missing_page_structure(apple_page):
    adapter, page = apple_page
    await page.set_content(
        '<input name="dimensionColor" id="black" value="black" disabled>'
        '<label for="black">黑色</label>'
    )
    with pytest.raises(CandidateUnavailable):
        await adapter._choose("color", "黑色")
    with pytest.raises(CandidateUnavailable):
        await adapter._choose("color", "白色")
    with pytest.raises(SelectorNotFound):
        await adapter._choose("capacity", "256GB")


PRODUCT = """<input type="radio" name="dimensionColor" id="black" value="black" checked
 onchange="document.querySelector('[data-autom=summary-productName]')
 .innerText='iPhone 17 256GB 黑色'">
<label for="black">黑色</label>
<input type="radio" name="dimensionColor" id="white" value="white"
 onchange="document.querySelector('[data-autom=summary-productName]')
 .innerText='iPhone 17 256GB 白色'">
<label for="white">白色</label>
<input type="radio" name="dimensionCapacity" id="capacity" value="256gb" checked>
<label for="capacity">256GB</label>
<input type="radio" id="noTradeIn" value="noTradeIn" checked><label for="noTradeIn">不折抵</label>
<input type="radio" name="applecare-options" id="care" checked>
<label for="care">不加 AppleCare+ 服务计划</label>
<div data-autom="summary-productName">iPhone 17 256GB 黑色</div>
<div data-autom="full-price">RMB 6799</div><div data-autom="dudeInfo">3-5 个工作日</div>
<button data-autom="add-to-cart">添加到购物袋</button>"""


@pytest.mark.browser
async def test_get_skus_returns_verified_fallbacks_and_empty_candidates(apple_page):
    adapter, page = apple_page
    adapter.preferences["test"] = ProductPreferences(
        model_priority=["iPhone 17"],
        capacity_priority=["256GB"],
        color_priority=["不存在的颜色", "黑色", "白色"],
    )
    await page.set_content(PRODUCT)
    skus = await adapter.get_skus()
    assert [sku.color for sku in skus] == ["黑色"]
    assert await page.locator("#black").is_checked()
    adapter.excluded_sku_ids = {skus[0].id}
    fallback = await adapter.check_stock()
    assert [sku.color for sku in fallback] == ["白色"]
    adapter.excluded_sku_ids.add(fallback[0].id)
    assert await adapter.check_stock() == []
    adapter.excluded_sku_ids.clear()
    adapter.preferences["test"] = adapter.preferences["test"].model_copy(
        update={"quantity": 2, "max_total": Decimal("12000")}
    )
    await page.set_content(
        PRODUCT.replace(
            "iPhone 17 256GB 白色'\"",
            "iPhone 17 256GB 白色';"
            "document.querySelector('[data-autom=full-price]').innerText='RMB 6000'\"",
        )
    )
    assert [sku.color for sku in await adapter.get_skus()] == ["白色"]
    adapter.preferences["test"] = adapter.preferences["test"].model_copy(
        update={"color_priority": ["不存在的颜色"]}
    )
    assert await adapter.get_skus() == []
    assert not adapter._cart_attempted and not adapter._submit_attempted


@pytest.mark.browser
async def test_select_sku_refreshes_candidates_only_before_cart_attempt(apple_page):
    adapter, page = apple_page
    original = adapter.selected
    await page.set_content(PRODUCT.replace("RMB 6799", "RMB 6800"))
    with pytest.raises(CandidateUnavailable):
        await adapter.select_sku(original)
    await page.set_content(
        PRODUCT.replace('data-autom="add-to-cart"', 'data-autom="add-to-cart" disabled')
    )
    with pytest.raises(CandidateUnavailable):
        await adapter.select_sku(original)
    adapter._cart_attempted = True
    await page.set_content(
        PRODUCT.replace('id="black" value="black" checked', 'id="black" value="black"')
    )
    with pytest.raises(HumanRequired):
        await adapter.select_sku(original)
    assert not await page.locator("#black").is_checked()
