"""Audit R05: real checkout/config modules, synthetic evidence only."""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from test_audit_protected_run import configuration
from test_marketplace_contracts import purchase

from src.core.config import OrderSettings, validate_platform_url
from src.core.models import Platform
from src.order.checkout import OrderVerificationError, verify_checkout
from src.web.app import create_app


def test_saved_address_presence_is_not_this_run_address_approval():
    sku, review, prefs, target = purchase()
    review = review.model_copy(
        update={
            "market_verified": True,
            "market_evidence": "fixture-cn",
            "address_confirmed": False,
            "address_fingerprint": "",
        }
    )
    with pytest.raises(OrderVerificationError, match="address"):
        verify_checkout(review, sku, prefs, target=target, order_policy=OrderSettings())


def test_approved_marketplace_seller_and_delivery_region_are_not_version_proof():
    sku, review, prefs, target = purchase()
    review = review.model_copy(
        update={
            "address_confirmed": True,
            "address_fingerprint": "fixture-address",
            "market_verified": False,
            "market_evidence": "",
        }
    )
    with pytest.raises(OrderVerificationError, match="version"):
        verify_checkout(review, sku, prefs, target=target, order_policy=OrderSettings())


def test_apple_foreign_storefront_cannot_be_a_mainland_product_target():
    for url in (
        "https://www.apple.com/shop/buy-iphone/iphone-17",
        "https://www.apple.com/hk/shop/buy-iphone/iphone-17",
    ):
        with pytest.raises(ValueError):
            validate_platform_url(Platform.APPLE, url)
    assert validate_platform_url(
        Platform.APPLE, "https://www.apple.com.cn/shop/buy-iphone/iphone-17"
    )


def test_local_confirmation_requires_explicit_flag_and_cannot_enable_submission(tmp_path):
    app = create_app(configuration(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        runtime = app.state.runtime
        reader = AsyncMock(
            return_value={"address_fingerprint": "fixture", "address_confirmed": True}
        )
        runtime.adapters[Platform.APPLE].confirm_address = reader
        before = (runtime.config.app, runtime.config.order, runtime.database.guard_status())
        headers = {"X-Apple-Bot-Control": "local"}
        plan = client.get("/purchase-plan").json()
        assert (
            client.post(
                "/approve-plan",
                headers=headers,
                json={"digest": plan["digest"], "confirmed": True},
            ).status_code
            == 200
        )
        assert client.post("/confirm-address", json={"confirmed": True}).status_code == 403
        for body in ({}, {"confirmed": False}, {"confirmed": True, "auto_submit": True}):
            assert client.post("/confirm-address", headers=headers, json=body).status_code == 422
        assert not reader.called
        assert (
            client.post("/confirm-address", headers=headers, json={"confirmed": True}).status_code
            == 200
        )
        assert reader.await_count == 1
        assert (runtime.config.app, runtime.config.order, runtime.database.guard_status()) == before
