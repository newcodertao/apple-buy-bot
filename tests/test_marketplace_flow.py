"""Synthetic local DOM exercises shared code only; it is not live-site acceptance."""

import json

import pytest

from src.browser.manager import BrowserManager
from src.core.config import OrderSettings, ProductPreferences, ProductTarget
from src.core.exceptions import HumanRequired
from src.core.models import LoginStatus, Platform
from src.platforms.marketplace import MarketplaceAdapter
from tests.test_browser import local_site  # noqa: F401

pytestmark = pytest.mark.browser


def field(key, value):
    return f'<span data-fixture="{key}">{value}</span>'


def fixture_html(*, review_seller="fixture-seller"):
    financing = (
        '<div data-fixture="financing_selected">'
        + "".join(
            field(key, value)
            for key, value in {
                "financing_provider": "Fixture Installments",
                "financing_terms": "24 期",
                "financing_interest": "0",
                "financing_fee": "0",
                "financing_principal": "100",
                "financing_total": "100",
            }.items()
        )
        + "</div>"
    )
    stages = {
        "product": "".join(
            field(key, value)
            for key, value in {
                "authenticated": "Account ready",
                "product_name": "Fixture Phone 512GB 黑色",
                "product_price": "100",
                "selected_model": "Fixture Phone",
                "selected_capacity": "512GB",
                "selected_color": "黑色",
                "seller_id": "fixture-seller",
                "shop_name": "Fixture Store",
                "region": "fixture-region",
                "stock": "有货",
                "sale_mode": "普通销售",
            }.items()
        )
        + """
            <button data-fixture="open_cart" onclick="show('empty')">Open cart</button>
            <button data-fixture="add_to_cart" onclick="count('adds');show('added')">Add</button>
        """,
        "empty": '<p data-fixture="cart_empty">Cart empty</p>',
        "added": """
            <p data-fixture="cart_added">Added</p>
            <button data-fixture="view_cart" onclick="show('cart')">View cart</button>
        """,
        "cart": '<div data-fixture="cart_items">'
        + "".join(
            field(key, value)
            for key, value in {
                "cart_name": "Fixture Phone 512GB 黑色",
                "cart_seller_id": "fixture-seller",
                "cart_item_id": "123",
                "cart_price": "100",
            }.items()
        )
        + """
            <input data-fixture="cart_quantity" value="1">
            <input data-fixture="cart_selected" type="checkbox" checked>
            </div><span data-fixture="cart_total">100</span>
            <button data-fixture="checkout" onclick="show('checkout')">Checkout</button>
        """,
        "checkout": '<div data-fixture="review_items">'
        + "".join(
            field(key, value)
            for key, value in {
                "review_name": "Fixture Phone 512GB 黑色",
                "review_item_id": "123",
                "review_seller_id": review_seller,
                "review_shop": "Fixture Store",
                "review_quantity": "数量 1",
                "review_unit_price": "100",
            }.items()
        )
        + "</div>"
        + "".join(
            field(key, value)
            for key, value in {
                "review_region": "fixture-region",
                "review_stock": "有货",
                "review_total": "100",
                "review_subtotal": "100",
                "review_discount": "0",
                "review_shipping": "0",
                "review_fees": "0",
                "review_address_identity": "合成测试地址甲，不是真实地址",
            }.items()
        )
        + financing
        + """
            <input data-fixture="review_address_selected" type="radio" checked>
            <button data-fixture="submit_order" onclick="count('submits');show('receipt')">
                Create unpaid fixture order
            </button>
        """,
        "receipt": "".join(
            field(key, value)
            for key, value in {
                "receipt_order_id": "FIXTURE-123",
                "receipt_status": "等待付款",
                "receipt_item_id": "123",
                "receipt_seller_id": "fixture-seller",
                "receipt_quantity": "1",
                "receipt_total": "100",
            }.items()
        )
        + financing,
    }
    return """<!doctype html><meta charset="utf-8"><title>Local marketplace fixture</title>
        <body></body><script>
        const stages = STAGES;
        function show(stage) { document.body.innerHTML = stages[stage]; }
        function count(key) {
            sessionStorage.setItem(key, String(Number(sessionStorage.getItem(key) || 0) + 1));
        }
        show('product');
        </script>""".replace("STAGES", json.dumps(stages, ensure_ascii=False))


class FixtureMarketplaceAdapter(MarketplaceAdapter):
    platform = Platform.JD
    allowed_hosts = ("127.0.0.1",)
    require_https = False
    selectors = {
        key: f'[data-fixture="{key}"]'
        for key in (
            "authenticated product_name product_price selected_model selected_capacity "
            "selected_color seller_id shop_name region stock sale_mode open_cart cart_empty "
            "add_to_cart cart_added view_cart cart_items cart_name cart_seller_id cart_item_id "
            "cart_price cart_quantity cart_selected cart_total checkout review_items review_name "
            "review_item_id review_seller_id review_shop review_quantity review_unit_price "
            "review_region review_stock review_total review_subtotal review_discount "
            "review_shipping review_fees review_address_selected review_address_identity "
            "submit_order financing_selected "
            "financing_provider financing_terms financing_interest financing_fee "
            "financing_principal financing_total receipt_order_id receipt_status receipt_item_id "
            "receipt_seller_id receipt_quantity receipt_total"
        ).split()
    }

    async def product_snapshot(self):
        snapshot = await super().product_snapshot()
        snapshot["stock_state"] = "AVAILABLE" if snapshot["stock_text"] == "有货" else "UNKNOWN"
        snapshot["sale_mode"] = (
            "NORMAL" if await self._text("sale_mode") == "普通销售" else "UNKNOWN"
        )
        return snapshot

    async def _capture(self, action, state=""):
        return None  # Diagnostics already have separate local coverage.


@pytest.mark.parametrize("review_seller", ["fixture-seller", "changed-seller"])
async def test_marketplace_local_cart_checkout_receipt_or_mismatch_without_replay(
    tmp_path,
    local_site,  # noqa: F811 - imported shared pytest fixture
    review_seller,
):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    url = local_site + "/123.html"
    preferences = ProductPreferences(
        model_priority=["Fixture Phone"],
        capacity_priority=["512GB"],
        color_priority=["黑色"],
        max_price=100,
        max_total=100,
    )
    adapter = FixtureMarketplaceAdapter(
        manager,
        tmp_path / "screenshots",
        {"fixture": preferences},
        targets={
            "fixture": ProductTarget(
                url=url, seller_ids=["fixture-seller"], region="fixture-region"
            )
        },
        order_policy=OrderSettings(auto_submit=True),
        allow_submit=True,
    )
    try:
        page = await manager.open(Platform.JD)
        await page.route(
            "**/123.html",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=fixture_html(review_seller=review_seller),
            ),
        )
        await adapter.open_product("fixture", url)
        assert await adapter.login_status() == LoginStatus.AUTHENTICATED
        (sku,) = await adapter.check_stock()
        await adapter.select_sku(sku)
        await adapter.add_to_cart(1)
        await adapter.add_to_cart(1)  # Existing matching cart is read, never added again.
        assert await page.evaluate("sessionStorage.getItem('adds')") == "1"
        await adapter.goto_checkout()
        if review_seller != "fixture-seller":
            with pytest.raises(HumanRequired, match="seller"):
                await adapter.verify_order()
            with pytest.raises(HumanRequired, match="seller"):
                await adapter.submit_order()
            assert await page.evaluate("sessionStorage.getItem('submits')") is None
        else:
            await adapter.confirm_address()
            await adapter.confirm_market()
            review = await adapter.verify_order()
            assert review.financing.terms == 24
            assert review.financing.interest == review.financing.service_fee == 0
            result = await adapter.submit_order()
            assert (result.status, result.payment_state, result.financing_state) == (
                "SUCCESS",
                "UNPAID",
                "ELIGIBLE",
            )
            assert await adapter.read_order_status(result.order_id) == result
            assert (await adapter.read_order_status("WRONG-ORDER")).status == "UNKNOWN"
            assert (await adapter.submit_order()).status == "UNKNOWN"
            assert await page.evaluate("sessionStorage.getItem('submits')") == "1"
        assert await page.evaluate("sessionStorage.getItem('adds')") == "1"
    finally:
        await manager.close()
