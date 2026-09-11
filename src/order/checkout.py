"""The final purchase boundary: every field must be positively verified."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.core.exceptions import HumanRequired
from src.core.models import (
    SKU,
    FinancingOffer,
    FinancingState,
    OrderReview,
    Platform,
    SaleMode,
    StockState,
)

QUOTE_MAX_AGE_SECONDS = 30


class OrderVerificationError(HumanRequired):
    """A checkout requires human correction; never retry it automatically."""


def _fresh(observed: datetime | None, now: datetime) -> bool:
    return bool(
        observed is not None
        and observed.tzinfo is not None
        and observed.utcoffset() is not None
        and 0 <= (now - observed).total_seconds() <= QUOTE_MAX_AGE_SECONDS
    )


def financing_state(
    offer: FinancingOffer | None, total: Decimal, *, now: datetime | None = None
) -> FinancingState:
    """Derive zero-cost eligibility from the selected offer, never its banner."""
    if offer is None:
        return FinancingState.UNKNOWN
    if (
        offer.state == FinancingState.INELIGIBLE
        or (offer.terms is not None and offer.terms != 24)
        or (offer.interest is not None and offer.interest != 0)
        or (offer.service_fee is not None and offer.service_fee != 0)
        or (offer.principal is not None and offer.principal != total)
        or (offer.total_repayment is not None and offer.total_repayment != total)
    ):
        return FinancingState.INELIGIBLE
    if (
        offer.state == FinancingState.ELIGIBLE
        and offer.selected
        and offer.provider.strip()
        and offer.terms == 24
        and offer.interest == 0
        and offer.service_fee == 0
        and offer.principal == total
        and offer.total_repayment == total
        and _fresh(offer.verified_at, now or datetime.now(UTC))
    ):
        return FinancingState.ELIGIBLE
    return FinancingState.UNKNOWN


def _verify_marketplace(review: OrderReview, sku: SKU, target: Any, policy: Any) -> None:
    if target is None or not target.seller_ids or not target.region.strip():
        raise OrderVerificationError("Marketplace target requires approved sellers and region")
    if not sku.seller_id or sku.seller_id not in target.seller_ids:
        raise OrderVerificationError("Selected seller has not been approved")
    if review.seller_id != sku.seller_id:
        raise OrderVerificationError("Checkout seller differs from selected product")
    if not sku.platform_item_id or review.platform_item_id != sku.platform_item_id:
        raise OrderVerificationError("Checkout platform item identity is unknown or changed")
    if sku.region != target.region or review.region != target.region:
        raise OrderVerificationError("Checkout region differs from the approved region")
    if (
        sku.stock_state != StockState.AVAILABLE
        or review.stock_state != StockState.AVAILABLE
        or sku.sale_mode != SaleMode.NORMAL
        or review.sale_mode != SaleMode.NORMAL
    ):
        raise OrderVerificationError("Only confirmed in-stock normal-sale products are supported")
    now = datetime.now(UTC)
    # A fresh checkout quote revalidates stock/region after manual login without re-adding.
    if not _fresh(review.observed_at, now):
        raise OrderVerificationError("Marketplace quote is missing or expired; recheck the page")
    amounts = (review.items_subtotal, review.discount, review.shipping, review.fees)
    if any(amount is None for amount in amounts):
        raise OrderVerificationError(
            "Checkout requires explicit subtotal, discount, shipping and fees"
        )
    if review.items_subtotal != review.unit_price * review.quantity:
        raise OrderVerificationError("Checkout merchandise subtotal does not match the quantity")
    if review.discount > review.items_subtotal:
        raise OrderVerificationError("Checkout discount exceeds the merchandise subtotal")
    if review.shipping > target.max_shipping or review.fees > target.max_fees:
        raise OrderVerificationError("Checkout shipping or fees exceed the approved limits")
    if (
        review.total_price
        != review.items_subtotal - review.discount + review.shipping + review.fees
    ):
        raise OrderVerificationError(
            "Checkout payable amount does not match the verified breakdown"
        )
    if policy is None:
        raise OrderVerificationError("Marketplace checkout requires an explicit payment policy")
    if policy.payment_method == "installments":
        state = financing_state(review.financing, review.total_price, now=now)
        if state == FinancingState.INELIGIBLE:
            raise OrderVerificationError(
                "The installment offer does not meet 24-term zero-cost terms"
            )
        if state != FinancingState.ELIGIBLE and not policy.allow_post_order_financing_check:
            raise OrderVerificationError("Installment terms are unknown; human review is required")


def verify_checkout(
    review: OrderReview,
    sku: SKU,
    preferences: Any,
    *,
    target: Any = None,
    order_policy: Any = None,
) -> None:
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
    if sku.platform == Platform.APPLE:
        if review.total_price != review.unit_price * review.quantity:
            raise OrderVerificationError("Checkout total includes an unexpected amount")
    else:
        _verify_marketplace(review, sku, target, order_policy)
    max_total = getattr(preferences, "max_total", None)
    limit = max_total if max_total is not None else preferences.max_price * preferences.quantity
    if review.total_price > limit:
        raise OrderVerificationError("Checkout total exceeds the permitted total")
    if not review.address_present:
        raise OrderVerificationError("Checkout address requires confirmation")
    if review.verification_present:
        raise OrderVerificationError("Checkout verification requires human action")
    if not review.checkout_valid or review.line_items != 1:
        raise OrderVerificationError("Checkout must contain exactly one verified product line")
