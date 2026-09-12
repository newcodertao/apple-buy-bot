"""Unrestricted variants still require a concrete, unchanged product and budget."""
# ruff: noqa: F811 -- reuse the existing local browser fixture.

from decimal import Decimal

import pytest
from test_apple_live_flow import apple_page  # noqa: F401
from test_audit_apple import PRODUCT

from src.core.config import AppConfig, ProductPreferences
from src.core.exceptions import HumanRequired, SelectorNotFound
from src.core.models import OrderReview, Platform
from src.order.checkout import OrderVerificationError, verify_checkout
from src.order.plan import draft_plan
from src.order.priority import rank_skus, score_sku
from src.platforms.marketplace import parse_product


def test_any_observed_variant_keeps_model_budget_and_checkout_identity():
    config = AppConfig.model_validate(
        {
            "products": {
                "test": {
                    "model": "iPhone 18 Pro Max",
                    "capacity_priority": [],
                    "color_priority": [],
                    "platforms": {"apple": {"url": "https://www.apple.com.cn/shop/test"}},
                }
            }
        }
    )
    prefs = config.preferences_for("test")
    data = dict(
        item_id="123", title="iPhone 18 Pro Max 2TB 琥珀色", model="iPhone 18 Pro Max",
        capacity="2TB", color="琥珀色", price="14999", stock_state="AVAILABLE",
        sale_mode="NORMAL", seller_id="fixture-shop", region="fixture-region", buy_enabled=True,
    )
    sku = parse_product(data, platform=Platform.JD, product_id="test", preferences=prefs)
    assert score_sku(sku, prefs) == (0, 0, 0)
    other = sku.model_copy(update={"id": "other", "capacity": "256GB", "color": "白色"})
    assert rank_skus([sku, other], prefs) == [sku, other]
    for field in ("capacity", "color", "model"):
        with pytest.raises(SelectorNotFound):
            parse_product(
                {**data, field: " "}, platform=Platform.JD, product_id="test", preferences=prefs
            )
    with pytest.raises(SelectorNotFound):
        parse_product(
            {**data, "model": "iPhone 17"}, platform=Platform.JD,
            product_id="test", preferences=prefs,
        )

    sku = sku.model_copy(update={"platform": Platform.APPLE})
    plan = draft_plan(config).approved()
    assert plan.products[0].capacities == plan.products[0].colors == []
    plan.require(sku, 1, config.order.payment_method, config.order.installment_bank)
    review = OrderReview(
        platform=sku.platform, product_id=sku.product_id, sku_id=sku.id,
        model=sku.model, capacity=sku.capacity, color=sku.color,
        unit_price=sku.price, total_price=sku.price, quantity=1,
        address_present=True, address_confirmed=True, address_fingerprint="fixture-address",
        market_verified=True, market_evidence="fixture-market", checkout_valid=True, line_items=1,
    )
    verify_checkout(review, sku, prefs)
    # Unrestricted choice never means unknown specs or changes after selection are acceptable.
    with pytest.raises(OrderVerificationError):
        verify_checkout(review.model_copy(update={"color": "白色"}), sku, prefs)
    for changes in (
        {"capacity": " "}, {"color": ""}, {"model": "iPhone 17"}, {"price": Decimal("15001")},
    ):
        invalid = sku.model_copy(update=changes)
        assert score_sku(invalid, prefs) is None
        with pytest.raises(HumanRequired):
            plan.require(invalid, 1, config.order.payment_method, config.order.installment_bank)
        with pytest.raises(OrderVerificationError):
            verify_checkout(review, invalid, prefs)


@pytest.mark.browser
async def test_apple_any_variant_uses_visible_choices_and_stops_at_first_match(apple_page):
    adapter, page = apple_page
    adapter.preferences["test"] = ProductPreferences(
        model_priority=["iPhone 18"], capacity_priority=[], color_priority=[]
    )
    hidden = (
        '<input name="dimensionColor" id="hidden">'
        '<label for="hidden" hidden>不存在颜色</label>'
    )
    await page.set_content(hidden + PRODUCT.replace("iPhone 17", "iPhone 18"))
    first = (await adapter.get_skus())[0]
    assert (first.model, first.capacity, first.color) == ("iPhone 18", "256GB", "黑色")
    assert await page.locator("#black").is_checked()
    assert not await page.locator("#white").is_checked()
    adapter.excluded_sku_ids = {first.id}
    assert [sku.color for sku in await adapter.get_skus()] == ["白色"]
    assert not adapter._cart_attempted and not adapter._submit_attempted
