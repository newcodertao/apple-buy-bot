import asyncio
from datetime import UTC, datetime

import pytest

from src.core.config import AppConfig
from src.core.engine import Engine
from src.core.exceptions import HumanRequired, RetryableError, SelectorNotFound
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
from src.notify.base import Notifier
from src.order.lock import OrderLock
from src.platforms.base import PlatformAdapter
from src.storage.database import Database


class QuietNotifier(Notifier):
    async def notify(self, event, message, platform=None):
        pass


class FakeAdapter(PlatformAdapter):
    """All orders are in-memory fixtures; this class has no network or browser."""

    def __init__(self, platform):
        self.platform = platform
        self.calls = []
        self.challenge = False
        self.login = LoginStatus.AUTHENTICATED
        self.stock_error = None
        self.submit_error = None
        self.submit_result = OrderResult(status="SUCCESS", order_id="fixture-order-only")
        self.review_updates = {}
        self.second_review_updates = {}
        self.review_count = 0
        self.cart_error = None
        self.submit_wait = None
        self.active = 0
        self.max_active = 0
        self.sku = SKU(
            id=f"fixture-{platform}",
            platform=platform,
            product_id="fixture",
            model="Fixture Phone",
            capacity="512GB",
            color="黑色",
            price=100,
            available=True,
            seller_id="fixture-seller",
            platform_item_id="fixture-item",
            region="fixture-region",
            observed_at=datetime.now(UTC),
            stock_state=StockState.AVAILABLE,
            sale_mode=SaleMode.NORMAL,
        )

    async def tick(self, action):
        self.calls.append(action)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        self.active -= 1

    async def login_status(self):
        await self.tick("login_status")
        return self.login

    async def open_product(self, product_id, url):
        await self.tick("open_product")

    async def get_skus(self):
        await self.tick("get_skus")
        return [self.sku]

    async def check_stock(self):
        await self.tick("check_stock")
        if self.stock_error:
            raise self.stock_error
        return [self.sku]

    async def select_sku(self, sku):
        await self.tick("select_sku")

    async def add_to_cart(self, quantity):
        await self.tick("add_to_cart")
        if self.cart_error:
            raise self.cart_error

    async def goto_checkout(self):
        await self.tick("goto_checkout")

    async def verify_order(self):
        await self.tick("verify_order")
        self.review_count += 1
        review = OrderReview(
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
        updates = self.review_updates.copy()
        if self.review_count >= 2:
            updates.update(self.second_review_updates)
        return review.model_copy(update=updates)

    async def submit_order(self):
        await self.tick("submit_order")
        if self.submit_wait:
            await self.submit_wait.wait()
        if self.submit_error:
            raise self.submit_error
        return self.submit_result

    async def detect_verification(self):
        await self.tick("detect_verification")
        return Verification(required=self.challenge)

    async def capture(self, action, state=""):
        await self.tick("capture:" + action)
        return None

    async def close(self):
        await self.tick("close")


def make_engine(
    tmp_path,
    *,
    dry_run=False,
    auto_submit=True,
    mode="race",
    platforms=None,
    max_retries=2,
    max_checks=10,
):
    platforms = platforms or [Platform.APPLE]
    config = AppConfig.model_validate(
        {
            "app": {"dry_run": dry_run},
            "product": {"model_priority": ["Fixture Phone"]},
            "products": {
                "fixture": {
                    "model": "Fixture Phone",
                    "platforms": {
                        p.value: {
                            "url": {
                                Platform.APPLE: "https://www.apple.com.cn/shop/buy-iphone/fixture",
                                Platform.JD: "https://item.jd.com/fixture.html",
                                Platform.TMALL: "https://detail.tmall.com/item.htm?id=fixture",
                                Platform.TAOBAO: "https://item.taobao.com/item.htm?id=fixture",
                            }[p],
                            "seller_ids": ["fixture-seller"],
                            "region": "fixture-region",
                        }
                        for p in platforms
                    },
                }
            },
            "monitor": {"max_retries": max_retries, "max_checks": max_checks, "jitter": 0},
            "order": {"auto_submit": auto_submit, "mode": mode},
        }
    )
    adapters = {p: FakeAdapter(p) for p in platforms}
    database = Database(tmp_path / "test.db")
    engine = Engine(config, adapters, database, QuietNotifier())
    return engine, adapters, database


async def wait_state(engine, state, platform=Platform.APPLE):
    async def probe():
        while engine.snapshot()["platforms"][platform]["state"] != state:
            await engine._guard_changed.wait()

    await asyncio.wait_for(probe(), timeout=2)


@pytest.mark.parametrize(
    "dry_run,auto_submit,expected",
    [
        (True, True, 0),
        (True, False, 0),
        (False, False, 0),
        (False, True, 1),
    ],
)
async def test_both_submit_switches_and_full_audit(tmp_path, dry_run, auto_submit, expected):
    engine, adapters, db = make_engine(tmp_path, dry_run=dry_run, auto_submit=auto_submit)
    result = await engine.run(immediate=True)
    adapter = adapters[Platform.APPLE]
    assert adapter.calls.count("submit_order") == expected
    state = result["platforms"]["apple"]["state"]
    assert state == ("SUCCESS" if expected else "READY_TO_SUBMIT")
    assert db.guard_status()["status"] == ("SUCCESS" if expected else "CLAIMED")
    assert adapter.review_count == (2 if expected else 1)
    assert len(db.recent("stock_checks")) == 1
    assert db.recent("orders")[0]["status"] == state
    events = list(reversed(db.recent("events")))
    assert events[0]["old_state"] == "IDLE"
    assert events[-1]["new_state"] == state
    assert all(
        left["new_state"] == right["old_state"]
        for left, right in zip(events, events[1:], strict=False)
    )
    assert "close" not in adapter.calls
    assert adapter.max_active == 1


@pytest.mark.parametrize("mode", ["race", "parallel"])
async def test_four_channels_can_never_submit_more_than_once(tmp_path, mode):
    engine, adapters, db = make_engine(tmp_path, mode=mode, platforms=list(Platform))
    result = await engine.run(immediate=True)
    assert sum(a.calls.count("submit_order") for a in adapters.values()) == 1
    assert sum(v["state"] == "SUCCESS" for v in result["platforms"].values()) == 1
    assert db.guard_status()["status"] == "SUCCESS"
    # Alibaba channels queue on one session; the losing queued channel may stay untouched.
    assert all(a.calls.count("check_stock") <= 1 for a in adapters.values())
    assert all(a.max_active <= 1 for a in adapters.values())


async def test_dry_run_override_never_submits(tmp_path):
    engine, adapters, _ = make_engine(tmp_path)
    await engine.run(immediate=True, dry_run=True)
    assert "submit_order" not in adapters[Platform.APPLE].calls


async def test_bad_order_waits_without_more_clicks_then_rechecks_on_resume(tmp_path):
    engine, adapters, _ = make_engine(tmp_path)
    adapter = adapters[Platform.APPLE]
    adapter.review_updates = {"sku_id": "wrong"}
    task = asyncio.create_task(engine.run(immediate=True))
    await wait_state(engine, "WAITING_HUMAN")
    # Allow the initial incident capture to complete, then ensure the pause is passive.
    for _ in range(5):
        await asyncio.sleep(0)
    previous = adapter.calls.copy()
    for _ in range(10):
        await asyncio.sleep(0)
    assert adapter.calls == previous
    assert "submit_order" not in previous
    adapter.review_updates = {}
    await engine.resume()
    await asyncio.wait_for(task, timeout=2)
    assert adapter.calls.count("add_to_cart") == 1
    assert adapter.calls.count("verify_order") == 3
    assert adapter.calls.count("submit_order") == 1


async def test_fresh_review_immediately_before_submit_catches_changed_sku(tmp_path):
    engine, adapters, db = make_engine(tmp_path)
    adapter = adapters[Platform.APPLE]
    adapter.second_review_updates = {"sku_id": "changed-after-first-review"}
    task = asyncio.create_task(engine.run(immediate=True))
    await wait_state(engine, "WAITING_HUMAN")
    assert adapter.review_count == 2
    assert "submit_order" not in adapter.calls
    assert db.guard_status()["status"] == "CLAIMED"
    await engine.stop()
    await task


async def test_verification_and_login_never_bypassed_on_resume(tmp_path):
    engine, adapters, _ = make_engine(tmp_path)
    adapter = adapters[Platform.APPLE]
    adapter.challenge = True
    task = asyncio.create_task(engine.run(immediate=True))
    await wait_state(engine, "WAITING_HUMAN")
    await engine.resume()
    for _ in range(15):
        await asyncio.sleep(0)
    assert "select_sku" not in adapter.calls
    assert engine.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN"
    adapter.challenge = False
    await engine.resume()
    await asyncio.wait_for(task, timeout=2)
    assert adapter.calls.count("submit_order") == 1


async def test_ambiguous_cart_action_is_not_repeated_on_resume(tmp_path):
    engine, adapters, _ = make_engine(tmp_path)
    adapter = adapters[Platform.APPLE]
    adapter.cart_error = HumanRequired("fixture challenge after click")
    task = asyncio.create_task(engine.run(immediate=True))
    await wait_state(engine, "WAITING_HUMAN")
    await engine.resume()
    await asyncio.wait_for(task, timeout=2)
    assert adapter.calls.count("add_to_cart") == 1
    assert adapter.calls.count("submit_order") == 1


async def test_unknown_selector_pauses_and_can_be_stopped(tmp_path):
    engine, adapters, _ = make_engine(tmp_path)
    adapter = adapters[Platform.APPLE]
    adapter.stock_error = SelectorNotFound("fixture unknown selector")
    task = asyncio.create_task(engine.run(immediate=True))
    await wait_state(engine, "WAITING_HUMAN")
    assert adapter.calls.count("check_stock") == 1
    await engine.stop()
    await task
    assert not engine.running
    assert engine.snapshot()["platforms"]["apple"]["state"] == "STOPPED"
    assert "submit_order" not in adapter.calls


async def test_retry_limit_and_retry_after_are_honored(tmp_path, monkeypatch):
    delays = []

    async def wait(stop, seconds):
        delays.append(seconds)
        return not stop.is_set()

    monkeypatch.setattr("src.core.engine.wait_or_stop", wait)
    engine, adapters, _ = make_engine(tmp_path, max_retries=2)
    adapter = adapters[Platform.APPLE]
    adapter.stock_error = RetryableError(retry_after=40)
    result = await engine.run(immediate=True)
    assert result["platforms"]["apple"]["state"] == "FAILED"
    assert adapter.calls.count("check_stock") == 3
    assert delays == [40, 40]
    assert "submit_order" not in adapter.calls


@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError(), HumanRequired()])
async def test_submission_exception_is_unknown_and_survives_restart(tmp_path, failure):
    engine, adapters, db = make_engine(tmp_path)
    adapter = adapters[Platform.APPLE]
    adapter.submit_error = failure
    result = await engine.run(immediate=True)
    assert result["platforms"]["apple"]["state"] == "WAITING_HUMAN"
    assert db.guard_status()["status"] == "UNKNOWN"
    assert not engine.order_lock.release(db.guard_status()["owner"])
    previous = adapter.calls.copy()
    await engine.resume()
    await engine.run(immediate=True)
    assert adapter.calls == previous
    assert adapter.calls.count("submit_order") == 1
    replacement = Database(tmp_path / "test.db")
    assert replacement.guard_status()["status"] == "UNKNOWN"
    assert not OrderLock(replacement).acquire("other-process")
    replacement.close()


@pytest.mark.parametrize(
    "result",
    [
        OrderResult(status="UNKNOWN"),
        OrderResult(status="SUCCESS"),
        OrderResult(status="SUCCESS", order_id="   "),
        OrderResult(status="clicked"),
    ],
)
async def test_click_or_success_without_order_id_is_not_success(tmp_path, result):
    engine, adapters, db = make_engine(tmp_path)
    adapters[Platform.APPLE].submit_result = result
    snapshot = await engine.run(immediate=True)
    assert snapshot["platforms"]["apple"]["state"] == "WAITING_HUMAN"
    assert db.guard_status()["status"] == "UNKNOWN"


async def test_stop_during_submission_persists_unknown(tmp_path):
    engine, adapters, db = make_engine(tmp_path)
    adapter = adapters[Platform.APPLE]
    adapter.submit_wait = asyncio.Event()
    task = asyncio.create_task(engine.run(immediate=True))
    await wait_state(engine, "SUBMITTING")
    await asyncio.sleep(0)
    await engine.stop()
    await task
    assert db.guard_status()["status"] == "UNKNOWN"
    assert adapter.calls.count("submit_order") == 1


async def test_confirmed_rejection_releases_lock_without_replaying_purchase(tmp_path, monkeypatch):
    async def wait(stop, seconds):
        return not stop.is_set()

    monkeypatch.setattr("src.core.engine.wait_or_stop", wait)
    engine, adapters, db = make_engine(tmp_path, max_retries=1)
    adapters[Platform.APPLE].submit_result = OrderResult(status="REJECTED")
    result = await engine.run(immediate=True)
    assert result["platforms"]["apple"]["state"] == "FAILED"
    assert adapters[Platform.APPLE].calls.count("submit_order") == 1
    assert adapters[Platform.APPLE].calls.count("add_to_cart") == 1
    assert db.guard_status() is None


async def test_dry_run_reservation_blocks_second_run(tmp_path):
    engine, adapters, db = make_engine(tmp_path, dry_run=True)
    await engine.run(immediate=True)
    calls = adapters[Platform.APPLE].calls.copy()
    result = await engine.run(immediate=True)
    assert result["platforms"]["apple"]["state"] == "READY_TO_SUBMIT"
    assert adapters[Platform.APPLE].calls == calls
    assert db.guard_status()["status"] == "CLAIMED"
