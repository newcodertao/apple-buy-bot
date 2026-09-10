"""The final purchase boundary: every field must be positively verified."""

from typing import Any

from src.core.exceptions import HumanRequired
from src.core.models import SKU, OrderReview


class OrderVerificationError(HumanRequired):
    """A checkout requires human correction; never retry it automatically."""


def verify_checkout(review: OrderReview, sku: SKU, preferences: Any) -> None:
    if (
        sku.model not in preferences.model_priority
        or sku.capacity not in preferences.capacity_priority
        or sku.color not in preferences.color_priority
    ):
        raise OrderVerificationError("Selected SKU is outside the approved preferences")
    expected = {
        "platform": sku.platform,
        "product_id": sku.product_id,
        "sku_id": sku.id,
        "model": sku.model,
        "capacity": sku.capacity,
        "color": sku.color,
        "currency": "CNY",
        "quantity": preferences.quantity,
    }
    for field, value in expected.items():
        if getattr(review, field) != value:
            raise OrderVerificationError(f"Checkout {field} does not match selected product")
    if sku.currency != "CNY" or not sku.available:
        raise OrderVerificationError("Selected product currency or availability is invalid")
    if review.unit_price != sku.price or review.unit_price > preferences.max_price:
        raise OrderVerificationError("Checkout unit price differs or exceeds the price limit")
    if review.total_price != review.unit_price * review.quantity:
        raise OrderVerificationError("Checkout total includes an unexpected amount")
    if review.total_price > preferences.max_price * preferences.quantity:
        raise OrderVerificationError("Checkout total exceeds the permitted total")
    if not review.address_present:
        raise OrderVerificationError("Checkout address requires confirmation")
    if review.verification_present:
        raise OrderVerificationError("Checkout verification requires human action")
    if not review.checkout_valid or review.line_items != 1:
        raise OrderVerificationError("Checkout must contain exactly one verified product line")
