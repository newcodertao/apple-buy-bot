"""Small contract checks for money, financing and the existing durable guard."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.config import OrderSettings, ProductPreferences, ProductTarget
from src.core.models import (
    SKU,
    FinancingOffer,
    FinancingState,
    OrderResult,
    OrderReview,
    Platform,
    SaleMode,
    StockState,
)
from src.order.checkout import OrderVerificationError, financing_state, verify_checkout
from src.order.priority import score_sku
from src.storage.database import Database


def purchase():
    now = datetime.now(UTC)
    sku = SKU(
        id="fixture-variant",
        platform=Platform.JD,
        product_id="fixture",
        platform_item_id="fixture-item",
        model="Fixture Phone",
        capacity="512GB",
        color="黑色",
        price=100,
        available=True,
        seller_id="approved-seller",
        region="region-id",
        observed_at=now,
        stock_state=StockState.AVAILABLE,
        sale_mode=SaleMode.NORMAL,
    )
    offer = FinancingOffer(
        provider="Fixture installments",
        terms=24,
        interest=0,
        service_fee=0,
        total_repayment=95,
        principal=95,
        verified_at=now,
        state=FinancingState.ELIGIBLE,
        selected=True,
    )
    review = OrderReview(
        platform=sku.platform,
        product_id=sku.product_id,
        sku_id=sku.id,
        platform_item_id=sku.platform_item_id,
        model=sku.model,
        capacity=sku.capacity,
        color=sku.color,
        seller_id=sku.seller_id,
        region=sku.region,
        observed_at=now,
        sale_mode=SaleMode.NORMAL,
        unit_price=100,
        stock_state=StockState.AVAILABLE,
        quantity=1,
        items_subtotal=100,
        discount=10,
        shipping=5,
        fees=0,
        total_price=95,
        address_present=True,
        address_fingerprint="fixture-address",
        address_confirmed=True,
        market_evidence="fixture-mainland-version",
        market_verified=True,
        checkout_valid=True,
        line_items=1,
        financing=offer,
    )
    prefs = ProductPreferences(model_priority=[sku.model], max_price=100, max_total=95)
    target = ProductTarget(seller_ids=[sku.seller_id], region=sku.region, max_shipping=5)
    return sku, review, prefs, target


def test_marketplace_accepts_verified_discount_and_blocks_changed_identity_or_unknown_fees():
    sku, review, prefs, target = purchase()
    policy = OrderSettings()
    verify_checkout(review, sku, prefs, target=target, order_policy=policy)
    # A current checkout revalidates an older selection after a manual login pause.
    older_selection = sku.model_copy(
        update={"observed_at": datetime.now(UTC) - timedelta(minutes=5)}
    )
    verify_checkout(review, older_selection, prefs, target=target, order_policy=policy)
    for change in (
        {"seller_id": "other-seller"},
        {"region": "different-region"},
        {"platform_item_id": "other-item"},
        {"fees": None},
        {"stock_state": StockState.UNKNOWN},
        {"shipping": Decimal("6"), "total_price": Decimal("96")},
        {"observed_at": datetime.now(UTC) - timedelta(seconds=31)},
        {"total_price": Decimal("94")},
    ):
        with pytest.raises(OrderVerificationError):
            verify_checkout(
                review.model_copy(update=change), sku, prefs, target=target, order_policy=policy
            )
    with pytest.raises(OrderVerificationError):
        verify_checkout(review, sku, prefs)


def test_deferred_financing_never_overrides_known_bad_terms():
    sku, review, prefs, target = purchase()
    policy = OrderSettings(allow_post_order_financing_check=True)
    unknown = review.model_copy(update={"financing": None})
    with pytest.raises(OrderVerificationError):
        verify_checkout(unknown, sku, prefs, target=target, order_policy=OrderSettings())
    verify_checkout(unknown, sku, prefs, target=target, order_policy=policy)
    for change in (
        {"terms": 12},
        {"interest": Decimal("1")},
        {"service_fee": Decimal("1")},
        {"total_repayment": Decimal("96")},
    ):
        offer = review.financing.model_copy(update=change)
        assert financing_state(offer, review.total_price) == FinancingState.INELIGIBLE
        with pytest.raises(OrderVerificationError):
            verify_checkout(
                review.model_copy(update={"financing": offer}),
                sku,
                prefs,
                target=target,
                order_policy=policy,
            )
    expired = review.financing.model_copy(
        update={"verified_at": datetime.now(UTC) - timedelta(seconds=31)}
    )
    assert financing_state(expired, review.total_price) == FinancingState.UNKNOWN


def test_unknown_marketplace_stock_is_never_a_purchase_candidate():
    sku, _, prefs, _ = purchase()
    assert score_sku(sku, prefs) is not None
    assert score_sku(sku.model_copy(update={"stock_state": StockState.UNKNOWN}), prefs) is None
    assert score_sku(sku.model_copy(update={"sale_mode": SaleMode.PREORDER}), prefs) is None


def test_upgrade_backs_up_old_database_and_preserves_success_guard(tmp_path):
    path = tmp_path / "database.db"
    with sqlite3.connect(path) as old:
        old.executescript("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                platform TEXT NOT NULL, product TEXT NOT NULL, sku TEXT NOT NULL,
                price TEXT NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, message TEXT NOT NULL
            );
            CREATE TABLE order_guard (
                singleton INTEGER PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO order_guard VALUES (1, 'legacy-owner', 'SUCCESS', 'before', 'before');
        """)
    database = Database(path)
    try:
        database.initialize()
        database.initialize()
        backups = list(tmp_path.glob("database.db.pre-marketplace-*.bak"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute("SELECT status FROM order_guard").fetchone()[0] == "SUCCESS"
            assert "order_id" not in {row[1] for row in backup.execute("PRAGMA table_info(orders)")}
        assert database.guard_status()["status"] == "SUCCESS"
        assert not database.claim_order("new-owner")
        sku, review, _, _ = purchase()
        run_id = database.create_run(None, "fixture")
        database.record_order(
            run_id,
            sku,
            "SUCCESS",
            review=review,
            result=OrderResult(
                status="SUCCESS",
                order_id="fixture-order",
                payment_state="UNPAID",
                financing_state="ELIGIBLE",
            ),
        )
        record = database.recent("orders")[0]
        assert (record["order_id"], record["total_price"], record["quantity"]) == (
            "fixture-order",
            "95",
            1,
        )
        assert record["payment_state"] == "UNPAID"
        assert record["session_group"] == "jd"
        assert not database.update_order_status(
            "fixture-order", Platform.TAOBAO, payment_state="UNPAID", financing_state="UNKNOWN"
        )
        assert database.update_order_status(
            "fixture-order", Platform.JD, payment_state="UNPAID", financing_state="INELIGIBLE"
        )
        assert database.recent("orders")[0]["financing_state"] == "INELIGIBLE"
        assert database.guard_status()["status"] == "SUCCESS"
    finally:
        database.close()
