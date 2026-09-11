from decimal import Decimal

import pytest

from src.core.config import ProductPreferences
from src.core.models import SKU, OrderReview, Platform
from src.order.checkout import OrderVerificationError, verify_checkout


@pytest.fixture
def checkout():
    sku = SKU(
        id="fixture-sku",
        platform=Platform.APPLE,
        product_id="fixture",
        model="Fixture Phone",
        capacity="512GB",
        color="黑色",
        price=100,
        available=True,
    )
    review = OrderReview(
        platform=sku.platform,
        product_id=sku.product_id,
        sku_id=sku.id,
        model=sku.model,
        capacity=sku.capacity,
        color=sku.color,
        unit_price=sku.price,
        total_price=sku.price,
        quantity=1,
        address_present=True,
        address_fingerprint="fixture-address",
        address_confirmed=True,
        market_evidence="fixture-mainland-version",
        market_verified=True,
        checkout_valid=True,
        line_items=1,
    )
    prefs = ProductPreferences(model_priority=[sku.model], max_price=150)
    return sku, review, prefs


def test_complete_verified_checkout_passes(checkout):
    sku, review, prefs = checkout
    verify_checkout(review, sku, prefs)


@pytest.mark.parametrize(
    "changes",
    [
        {"sku_id": "different"},
        {"model": "Wrong Model"},
        {"capacity": "256GB"},
        {"color": "银色"},
        {"platform": Platform.JD},
        {"product_id": "other"},
        {"currency": "USD"},
        {"quantity": 2},
        {"unit_price": Decimal("151")},
        {"unit_price": Decimal("99")},
        {"total_price": Decimal("110")},
        {"address_present": False},
        {"verification_present": True},
        {"checkout_valid": False},
        {"line_items": 2},
        {"line_items": 0},
    ],
)
def test_every_order_guard_blocks_incorrect_review(checkout, changes):
    sku, review, prefs = checkout
    with pytest.raises(OrderVerificationError):
        verify_checkout(review.model_copy(update=changes), sku, prefs)


def test_total_price_and_quantity_for_multiple_units(checkout):
    sku, review, prefs = checkout
    prefs = prefs.model_copy(update={"quantity": 2})
    review = review.model_copy(update={"quantity": 2, "total_price": Decimal("200")})
    verify_checkout(review, sku, prefs)
    with pytest.raises(OrderVerificationError):
        verify_checkout(review.model_copy(update={"total_price": Decimal("201")}), sku, prefs)


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "USD"},
        {"available": False},
        {"model": "Unapproved"},
        {"capacity": "128GB"},
        {"color": "Unapproved"},
    ],
)
def test_selected_sku_must_remain_valid(checkout, changes):
    sku, review, prefs = checkout
    with pytest.raises(OrderVerificationError):
        verify_checkout(review, sku.model_copy(update=changes), prefs)
