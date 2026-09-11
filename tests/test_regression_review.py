"""Regressions from independent review; all adapters are local test doubles."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from src.core.config import AppConfig
from src.core.engine import Engine
from src.core.exceptions import ConfigurationError
from src.core.models import (
    SKU,
    FinancingOffer,
    FinancingState,
    LoginStatus,
    OrderResult,
    OrderReview,
    Platform,
    SaleMode,
    StockState,
    Verification,
)
from src.runtime import Runtime
from src.storage.database import Database


class ReviewAdapter:
    def __init__(self, platform=Platform.APPLE, needs_page=False, reject_first=False):
        self.platform = platform
        self.needs_page = needs_page
        self.reject_first = reject_first
        self.opened = False
        self.login_checked = asyncio.Event()
        self.submits = 0
        self.checks = 0
        self.sku = SKU(
            id="test-sku",
            platform=platform,
            product_id="iphone",
            model="iPhone 18 Pro Max",
            capacity="512GB",
            color="黑色",
            price=Decimal("10000"),
            available=True,
            seller_id="fixture-seller",
            platform_item_id="fixture-item",
            region="fixture-region",
            observed_at=datetime.now(UTC),
            stock_state=StockState.AVAILABLE,
            sale_mode=SaleMode.NORMAL,
        )

    async def login_status(self):
        self.login_checked.set()
        if self.needs_page and not self.opened:
            return LoginStatus.UNKNOWN
        return LoginStatus.AUTHENTICATED

    async def detect_verification(self):
        return Verification()

    async def open_product(self, product_id, url):
        self.opened = True

    async def check_stock(self):
        self.checks += 1
        return [] if self.reject_first and self.checks > 1 else [self.sku]

    async def select_sku(self, sku):
        return None

    async def add_to_cart(self, quantity):
        return None

    async def goto_checkout(self):
        return None

    async def verify_order(self):
        return OrderReview(
            platform=self.platform,
            product_id=self.sku.product_id,
            sku_id=self.sku.id,
            model=self.sku.model,
            capacity=self.sku.capacity,
            color=self.sku.color,
            unit_price=self.sku.price,
            total_price=self.sku.price,
            quantity=1,
            address_present=True,
            address_fingerprint="fixture-address",
            address_confirmed=True,
            market_evidence="fixture-mainland-version",
            market_verified=True,
            checkout_valid=True,
            line_items=1,
            seller_id=self.sku.seller_id,
            platform_item_id=self.sku.platform_item_id,
            region=self.sku.region,
            observed_at=datetime.now(UTC),
            sale_mode=SaleMode.NORMAL,
            stock_state=StockState.AVAILABLE,
            items_subtotal=self.sku.price,
            discount=0,
            shipping=0,
            fees=0,
            financing=FinancingOffer(
                provider="Fixture installments",
                terms=24,
                interest=0,
                service_fee=0,
                principal=self.sku.price,
                total_repayment=self.sku.price,
                verified_at=datetime.now(UTC),
                selected=True,
                state=FinancingState.ELIGIBLE,
            ),
        )

    async def submit_order(self):
        self.submits += 1
        if self.reject_first:
            # Let the other platform observe SUBMITTING before confirmed rejection.
            await asyncio.sleep(0)
            return OrderResult(status="REJECTED")
        return OrderResult(status="SUCCESS", order_id="local-test-order")

    async def capture(self, action, state=""):
        return None


def configuration(tmp_path, *, dry_run=False, platforms=(Platform.APPLE,)):
    config = AppConfig.model_validate(
        {
            "app": {"dry_run": dry_run},
            "order": {"auto_submit": True},
            "monitor": {"max_checks": 2, "max_retries": 1},
            "products": {
                "iphone": {
                    "model": "iPhone 18 Pro Max",
                    "platforms": {
                        platform.value: {
                            "url": {
                                Platform.APPLE: "https://www.apple.com.cn/shop/buy-iphone/fixture",
                                Platform.JD: "https://jd.com/test",
                                Platform.TMALL: "https://detail.tmall.com/item.htm?id=fixture",
                                Platform.TAOBAO: "https://item.taobao.com/item.htm?id=fixture",
                            }[platform],
                            "seller_ids": ["fixture-seller"],
                            "region": "fixture-region",
                        }
                        for platform in platforms
                    },
                }
            },
        }
    )
    config._root = tmp_path
    return config


async def test_configured_dry_run_cannot_be_disabled_by_call_override(tmp_path):
    adapter = ReviewAdapter()
    database = Database(tmp_path / "audit.db")
    try:
        engine = Engine(configuration(tmp_path, dry_run=True), {Platform.APPLE: adapter}, database)
        await engine.run(immediate=True, dry_run=False)
        assert adapter.submits == 0
        assert engine.snapshot()["dry_run"] is True
    finally:
        database.close()


async def test_immediate_stop_does_not_start_pending_run(tmp_path):
    runtime = Runtime(configuration(tmp_path))
    adapter = ReviewAdapter()
    runtime.adapters = {Platform.APPLE: adapter}
    try:
        # No event-loop yield between task creation and the stop request.
        await runtime.start(immediate=True)
        await runtime.stop()
        assert adapter.submits == 0
        assert not runtime.snapshot()["running"]
    finally:
        await runtime.close()


async def test_stopped_pending_task_does_not_prevent_a_later_start(tmp_path):
    runtime = Runtime(configuration(tmp_path, dry_run=True))
    adapter = ReviewAdapter()
    runtime.adapters = {Platform.APPLE: adapter}
    try:
        await runtime.start(immediate=True)
        await runtime.stop()
        assert not adapter.opened
        await runtime.start(immediate=True)
        await asyncio.wait_for(runtime.task, timeout=1)
        assert adapter.opened
        assert adapter.submits == 0
        assert runtime.snapshot()["platforms"]["apple"]["state"] == "READY_TO_SUBMIT"
    finally:
        await runtime.close()


async def test_concurrent_start_requests_create_only_one_run(tmp_path):
    runtime = Runtime(configuration(tmp_path, dry_run=True))
    runtime.adapters = {Platform.APPLE: ReviewAdapter()}
    try:
        results = await asyncio.gather(
            runtime.start(immediate=True),
            runtime.start(immediate=True),
            return_exceptions=True,
        )
        assert sum(isinstance(result, ConfigurationError) for result in results) == 1
        await asyncio.wait_for(runtime.task, timeout=1)
        assert len(runtime.database.recent("runs")) == 1
    finally:
        await runtime.close()


async def test_snapshot_reports_effective_forced_dry_run(tmp_path):
    runtime = Runtime(configuration(tmp_path, dry_run=False))
    adapter = ReviewAdapter()
    runtime.adapters = {Platform.APPLE: adapter}
    try:
        await runtime.start(immediate=True, dry_run=True)
        await asyncio.wait_for(runtime.task, timeout=1)
        assert runtime.snapshot()["dry_run"] is True
        assert adapter.submits == 0
    finally:
        await runtime.close()


async def test_cancellation_during_start_notification_finishes_run(tmp_path):
    class BlockingNotifier:
        def __init__(self):
            self.started = asyncio.Event()

        async def notify(self, event, message, platform=None):
            if event == "started":
                self.started.set()
                await asyncio.Future()

    database = Database(tmp_path / "audit.db")
    notifier = BlockingNotifier()
    engine = Engine(configuration(tmp_path), {Platform.APPLE: ReviewAdapter()}, database, notifier)
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        await asyncio.wait_for(notifier.started.wait(), timeout=1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not engine.running
        assert database.recent("runs")[0]["status"] != "RUNNING"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        database.close()


async def test_fresh_session_opens_page_before_demanding_login_proof(tmp_path):
    adapter = ReviewAdapter(needs_page=True)
    database = Database(tmp_path / "audit.db")
    engine = Engine(configuration(tmp_path, dry_run=True), {Platform.APPLE: adapter}, database)
    task = asyncio.create_task(engine.run(immediate=True))
    try:
        await asyncio.wait_for(adapter.login_checked.wait(), timeout=1)
        assert adapter.opened, "Cannot require human login while no browser page is open"
        await asyncio.wait_for(task, timeout=1)
    finally:
        await engine.stop()
        await asyncio.gather(task, return_exceptions=True)
        database.close()


async def test_next_run_can_use_other_platform_after_confirmed_rejection(tmp_path, monkeypatch):
    async def skip_backoff(stop, seconds):
        await asyncio.sleep(0)
        return not stop.is_set()

    monkeypatch.setattr("src.core.engine.wait_or_stop", skip_backoff)
    apple = ReviewAdapter(reject_first=True)
    jd = ReviewAdapter(Platform.JD)
    database = Database(tmp_path / "audit.db")
    engine = Engine(
        configuration(tmp_path, platforms=(Platform.APPLE,)),
        {Platform.APPLE: apple},
        database,
    )
    try:
        await asyncio.wait_for(engine.run(immediate=True), timeout=3)
        assert apple.submits == 1
        assert database.guard_status() is None
        following = Engine(
            configuration(tmp_path, platforms=(Platform.JD,)),
            {Platform.JD: jd},
            database,
        )
        await asyncio.wait_for(following.run(immediate=True), timeout=3)
        assert jd.submits == 1, "Confirmed rejection must not block an explicit subsequent run"
        assert database.guard_status()["status"] == "SUCCESS"
    finally:
        await engine.stop()
        database.close()
