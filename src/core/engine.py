"""Concurrent monitoring with one persistent permission to submit an order."""

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter

from src.core.config import AppConfig
from src.core.exceptions import HumanRequired, OrderRejected, RetryableError
from src.core.logging import safe_url
from src.core.models import SKU, LoginStatus, Platform, PlatformStatus, State
from src.core.scheduler import Scheduler, backoff
from src.core.state_machine import StateMachine
from src.monitor.stock_monitor import polling_interval, wait_or_stop
from src.notify.base import Notifier
from src.notify.console import ConsoleNotifier
from src.order.checkout import verify_checkout
from src.order.lock import OrderLock
from src.order.priority import rank_skus
from src.platforms.base import PlatformAdapter
from src.storage.database import Database

UNCERTAIN = {"SUBMITTING", "UNKNOWN", "SUCCESS"}


class Engine:
    def __init__(
        self,
        config: AppConfig,
        adapters: dict[Platform, PlatformAdapter],
        database: Database,
        notifier: Notifier | None = None,
    ):
        self.config, self.adapters, self.database = config, adapters, database
        self.database.initialize()
        self.notifier = notifier or ConsoleNotifier()
        self.order_lock = OrderLock(database)
        self.running = False
        self.run_id: str | None = None
        self._stop = asyncio.Event()
        self._resume = {p: asyncio.Event() for p in adapters}
        self._status = {p: PlatformStatus(platform=p) for p in adapters}
        self._machines: dict[Platform, StateMachine] = {}
        self._tasks: list[asyncio.Task] = []
        self._active_platforms: set[Platform] = set()
        self._guard_changed = asyncio.Event()
        self._dry_run = config.app.dry_run
        self._sale_time = config.sale.target()

    def snapshot(self) -> dict:
        now = datetime.now(UTC)
        return {
            "running": self.running,
            "run_id": self.run_id,
            "time": now.isoformat(),
            "sale_time": self._sale_time.isoformat() if self._sale_time else None,
            "seconds_to_sale": (self._sale_time - now).total_seconds() if self._sale_time else None,
            "dry_run": self._dry_run,
            "auto_submit": self.config.order.auto_submit,
            "mode": self.config.order.mode,
            "order_guard": self.order_lock.status(),
            "platforms": {p.value: s.model_dump(mode="json") for p, s in self._status.items()},
        }

    def _transition(self, platform: Platform, state: State, message: str = "") -> None:
        status = self._status[platform]
        machine = self._machines[platform]
        if machine.state != state:
            machine.transition(state, sku_id=status.sku.id if status.sku else "", message=message)
        status.state = state
        if message:
            status.result = message
        changed, self._guard_changed = self._guard_changed, asyncio.Event()
        changed.set()
        logging.getLogger(platform.value).info(
            "platform=%s state=%s action=transition message=%s", platform, state, message
        )

    async def _notify(self, event: str, message: str, platform: Platform | None = None) -> None:
        try:
            await self.notifier.notify(event, message, platform)
        except Exception as exc:
            logging.getLogger("engine").warning("action=notify error=%s", type(exc).__name__)

    async def _capture(self, platform: Platform, action: str) -> None:
        try:
            await self.adapters[platform].capture(action, self._status[platform].state.value)
        except Exception as exc:
            logging.getLogger(platform.value).warning("action=capture error=%s", type(exc).__name__)

    async def _call(self, platform: Platform, name: str, *args):
        if self._stop.is_set():
            raise asyncio.CancelledError
        started = perf_counter()
        result, exception = "ok", ""
        try:
            return await getattr(self.adapters[platform], name)(*args)
        except BaseException as exc:
            result, exception = "failed", type(exc).__name__
            raise
        finally:
            elapsed = (perf_counter() - started) * 1000
            key = {
                "check_stock": "stock_check_ms",
                "select_sku": "sku_select_ms",
                "add_to_cart": "cart_ms",
                "goto_checkout": "checkout_ms",
                "submit_order": "submit_ms",
            }.get(name, name + "_ms")
            self._status[platform].timings[key] = round(elapsed, 3)
            product = self.config.products.get(self._status[platform].product_id)
            target = product.platforms.get(platform) if product else None
            logging.getLogger(platform.value).info(
                "platform=%s state=%s action=%s elapsed_ms=%.3f "
                "configured_url=%s result=%s exception=%s",
                platform,
                self._status[platform].state,
                name,
                elapsed,
                safe_url(target.url) if target else "",
                result,
                exception,
            )

    async def _pause(self, platform: Platform, next_state: State, reason: str) -> None:
        event = self._resume[platform]
        event.clear()
        self._transition(platform, State.WAITING_HUMAN, reason)
        await self._capture(platform, "human_required")
        await self._notify("human_required", "需要人工操作；处理后输入 resume", platform)
        while not self._stop.is_set():
            await event.wait()
            event.clear()
            if self._stop.is_set():
                raise asyncio.CancelledError
            try:
                login = await self._call(platform, "login_status")
                self._status[platform].login = login
                verification = await self._call(platform, "detect_verification")
                if login != LoginStatus.AUTHENTICATED or verification.required:
                    await self._notify("human_required", "登录或安全验证仍未完成", platform)
                    continue
            except (HumanRequired, RetryableError):
                await self._notify("human_required", "页面仍无法确认，请人工检查", platform)
                continue
            self._transition(platform, next_state, "Human resumed; page checked again")
            return
        raise asyncio.CancelledError

    async def _ensure_clear(self, platform: Platform, next_state: State) -> None:
        while True:
            try:
                login = await self._call(platform, "login_status")
                self._status[platform].login = login
                if login != LoginStatus.AUTHENTICATED:
                    raise HumanRequired("Login is required or unknown")
                verification = await self._call(platform, "detect_verification")
                if verification.required:
                    raise HumanRequired("Security verification requires human action")
                return
            except HumanRequired as exc:
                await self._pause(platform, next_state, type(exc).__name__)

    async def _action(self, platform: Platform, state: State, name: str, *args) -> None:
        self._transition(platform, state)
        while True:
            try:
                await self._ensure_clear(platform, state)
                await self._call(platform, name, *args)
                return
            except (HumanRequired, RetryableError, TimeoutError, ConnectionError) as exc:
                # Repeating an ambiguous cart click could increase quantity. The user
                # checks the cart; resume continues to checkout and verifies its total.
                await self._pause(platform, state, type(exc).__name__)
                if name == "add_to_cart":
                    return

    async def _review(self, platform: Platform, sku: SKU):
        preferences = self.config.preferences_for(sku.product_id)
        self._transition(platform, State.VERIFYING)
        while True:
            try:
                await self._ensure_clear(platform, State.VERIFYING)
                review = await self._call(platform, "verify_order")
                verify_checkout(review, sku, preferences)
                verification = await self._call(platform, "detect_verification")
                if verification.required:
                    raise HumanRequired("Verification appeared during checkout review")
                return review
            except (HumanRequired, RetryableError, TimeoutError, ConnectionError) as exc:
                await self._pause(platform, State.VERIFYING, type(exc).__name__)

    def _owner(self, platform: Platform) -> str:
        return f"{self.run_id}:{platform.value}"

    async def _wait_for_guard(self, platform: Platform) -> bool:
        """Wait without browser activity while another live workflow owns the order."""
        while not self._stop.is_set():
            guard = self.order_lock.status()
            if not guard or guard.get("owner") == self._owner(platform):
                return True
            if guard.get("status") in {"SUCCESS", "UNKNOWN"}:
                return False
            if not guard.get("owner", "").startswith(f"{self.run_id}:"):
                return False
            owner_platform = guard["owner"].rsplit(":", 1)[-1]
            if owner_platform not in self._active_platforms:
                return False
            if guard.get("status") == "CLAIMED" and (
                self._dry_run or not self.config.order.auto_submit
            ):
                return False
            event = self._guard_changed
            await event.wait()
        return False

    async def _claim(self, platform: Platform) -> bool:
        while not self._stop.is_set():
            if self.order_lock.acquire(self._owner(platform)):
                return True
            if not await self._wait_for_guard(platform):
                return False
        return False

    async def _purchase(self, platform: Platform, sku: SKU) -> None:
        preferences = self.config.preferences_for(sku.product_id)
        await self._action(platform, State.SELECTING_SKU, "select_sku", sku)
        await self._action(platform, State.ADDING_CART, "add_to_cart", preferences.quantity)
        await self._capture(platform, "cart")
        await self._notify("cart", "成功加入购物车", platform)
        await self._action(platform, State.CHECKOUT, "goto_checkout")
        await self._capture(platform, "checkout")
        await self._notify("checkout", "已进入结算，正在核对订单", platform)
        await self._review(platform, sku)
        owner = self._owner(platform)
        if not await self._claim(platform):
            self._transition(platform, State.STOPPED, "Another order owns the purchase reservation")
            return
        self._transition(platform, State.READY_TO_SUBMIT, "Checkout verified; purchase reserved")
        if self._dry_run or not self.config.order.auto_submit:
            self.database.record_order(self.run_id, sku, "READY_TO_SUBMIT", "Submission disabled")
            await self._notify("ready", "订单已核对；提交开关关闭，未下单", platform)
            return
        # Both switches are checked again after a fresh complete checkout review.
        await self._review(platform, sku)
        guard = self.order_lock.status()
        if not guard or guard.get("owner") != owner or guard.get("status") != "CLAIMED":
            self._transition(platform, State.STOPPED, "Purchase reservation is no longer valid")
            return
        if self._stop.is_set() or self._dry_run or not self.config.order.auto_submit:
            self._transition(platform, State.READY_TO_SUBMIT, "Submission disabled")
            return
        self._transition(platform, State.READY_TO_SUBMIT, "Fresh order verification passed")
        # Commit SUBMITTING before the first possible irreversible adapter operation.
        self.order_lock.mark_submission(owner, "SUBMITTING")
        self._transition(platform, State.SUBMITTING)
        try:
            result = await self._call(platform, "submit_order")
        except OrderRejected:
            self.order_lock.mark_submission(owner, "REJECTED")
            self.order_lock.release(owner)
            self.database.record_order(self.run_id, sku, "REJECTED", "Confirmed rejection")
            raise
        except (Exception, asyncio.CancelledError) as exc:
            self.order_lock.mark_submission(owner, "UNKNOWN")
            self.database.record_order(self.run_id, sku, "UNKNOWN", type(exc).__name__)
            self._transition(
                platform, State.WAITING_HUMAN, "Submission outcome UNKNOWN; do not resubmit"
            )
            await self._capture(platform, "submission_unknown")
            await self._notify("unknown", "订单结果未知，已保留锁；请人工核对订单", platform)
            return
        if result.status == "SUCCESS" and result.order_id and result.order_id.strip():
            self.order_lock.mark_submission(owner, "SUCCESS")
            self.database.record_order(
                self.run_id, sku, "SUCCESS", "Confirmed order identifier received"
            )
            self._transition(platform, State.SUCCESS, "Order confirmed; payment remains manual")
            await self._capture(platform, "success")
            await self._notify("success", "订单已确认；请人工完成支付", platform)
        elif result.status == "REJECTED":
            self.order_lock.mark_submission(owner, "REJECTED")
            self.order_lock.release(owner)
            self.database.record_order(self.run_id, sku, "REJECTED", "Confirmed rejection")
            raise OrderRejected("Confirmed rejection")
        else:
            self.order_lock.mark_submission(owner, "UNKNOWN")
            self.database.record_order(self.run_id, sku, "UNKNOWN", "No conclusive order result")
            self._transition(
                platform, State.WAITING_HUMAN, "Submission outcome UNKNOWN; do not resubmit"
            )
            await self._capture(platform, "submission_unknown")
            await self._notify("unknown", "订单结果未知，已保留锁；请人工核对订单", platform)

    async def _prepare(self, platform: Platform, product_id: str, url: str) -> None:
        scheduler = Scheduler(self._sale_time, self._stop)
        self._transition(platform, State.PREPARING, "Checking session and scheduled stages")
        if not await scheduler.wait_until(-600):
            raise asyncio.CancelledError
        # Opening a page gives a fresh persistent context a visible place to log in.
        await self._call(platform, "open_product", product_id, url)
        await self._capture(platform, "product_open")
        await self._ensure_clear(platform, State.PREPARING)
        self._transition(platform, State.WAITING, "Waiting for T-3min product preparation")
        if not await scheduler.wait_until(-180):
            raise asyncio.CancelledError
        await self._call(platform, "open_product", product_id, url)
        await self._capture(platform, "product_open")
        if not await scheduler.wait_until(-30):
            raise asyncio.CancelledError
        await self._ensure_clear(platform, State.WAITING)
        if not await scheduler.wait_until(-5):
            raise asyncio.CancelledError
        self._transition(platform, State.MONITORING, "Product ready; entering staged monitoring")
        if not await scheduler.wait_until(0):
            raise asyncio.CancelledError

    async def _worker(self, platform: Platform, targets: list[tuple[str, str]]) -> None:
        status = self._status[platform]
        index, checks, retries = 0, 0, 0
        owner = self._owner(platform)
        try:
            while checks < self.config.monitor.max_checks and not self._stop.is_set():
                product_id, url = targets[index % len(targets)]
                status.product_id = product_id
                self._machines[platform].product_id = product_id
                try:
                    guard = self.order_lock.status()
                    if (
                        guard
                        and guard.get("owner") != owner
                        and (self.config.order.mode == "race" or guard.get("status") in UNCERTAIN)
                    ):
                        if not await self._wait_for_guard(platform):
                            self._transition(
                                platform, State.STOPPED, "Another platform reserved the purchase"
                            )
                            return
                    if checks == 0 and retries == 0:
                        await self._prepare(platform, product_id, url)
                    else:
                        self._transition(platform, State.MONITORING)
                        await self._ensure_clear(platform, State.MONITORING)
                        await self._call(platform, "open_product", product_id, url)
                    checks += 1
                    skus = await self._call(platform, "check_stock")
                    status.last_check = datetime.now(UTC)
                    for sku in skus:
                        self.database.record_stock(
                            self.run_id, sku, status.timings["stock_check_ms"]
                        )
                    if any(
                        sku.platform != platform or sku.product_id != product_id for sku in skus
                    ):
                        raise HumanRequired(
                            "Stock response belongs to a different product or platform"
                        )
                    candidates = rank_skus(skus, self.config.preferences_for(product_id))
                    status.stock = bool(candidates)
                    if candidates:
                        status.sku = candidates[0]
                        self._transition(platform, State.STOCK_FOUND)
                        await self._capture(platform, "stock_found")
                        await self._notify("stock", "检测到符合配置的库存", platform)
                        await self._purchase(platform, candidates[0])
                        return
                    index += 1
                    if checks < self.config.monitor.max_checks:
                        await wait_or_stop(
                            self._stop, polling_interval(self.config.monitor, self._sale_time)
                        )
                except HumanRequired as exc:
                    await self._pause(platform, State.MONITORING, type(exc).__name__)
                    # Human pauses do not create unbounded automatic request retries.
                    retries += 1
                except (RetryableError, OrderRejected, TimeoutError, ConnectionError) as exc:
                    guard = self.order_lock.status()
                    if guard and guard.get("owner") == owner:
                        self.order_lock.release(owner)
                    retries += 1
                    status.retries = retries
                    self._transition(platform, State.FAILED, type(exc).__name__)
                    await self._capture(platform, "error")
                    await self._notify("failed", "操作失败，按重试上限退避", platform)
                    if retries > self.config.monitor.max_retries:
                        return
                    self._transition(platform, State.RETRYING, "Bounded retry with backoff")
                    delay = backoff(
                        retries - 1,
                        1,
                        self.config.monitor.backoff_max,
                        self.config.monitor.jitter,
                        getattr(exc, "retry_after", 0),
                    )
                    if not await wait_or_stop(self._stop, delay):
                        raise asyncio.CancelledError from None
            if self._stop.is_set():
                raise asyncio.CancelledError
            self._transition(platform, State.FAILED, "Stock check limit reached")
        except asyncio.CancelledError:
            guard = self.order_lock.status()
            if guard and guard.get("owner") == owner and guard.get("status") == "SUBMITTING":
                self.order_lock.mark_submission(owner, "UNKNOWN")
                self._transition(
                    platform, State.WAITING_HUMAN, "Submission interrupted; outcome UNKNOWN"
                )
            elif not guard or guard.get("owner") != owner or guard.get("status") not in UNCERTAIN:
                self._transition(platform, State.STOPPED, "Stopped by user")
        except Exception as exc:
            # Unclassified failures may follow a partial UI action: no automatic replay.
            self._transition(platform, State.WAITING_HUMAN, type(exc).__name__)
            await self._capture(platform, "error")
            await self._notify("failed", "未知错误已停止自动操作，请人工检查", platform)
        finally:
            self._active_platforms.discard(platform)
            changed, self._guard_changed = self._guard_changed, asyncio.Event()
            changed.set()

    async def run(self, platforms=None, immediate: bool = False, dry_run: bool | None = None):
        if self.running:
            raise RuntimeError("Engine is already running")
        targets = self.config.targets(platforms)
        grouped: dict[Platform, list[tuple[str, str]]] = {}
        for platform, product_id, url in targets:
            if platform in self.adapters:
                grouped.setdefault(platform, []).append((product_id, url))
        if not grouped:
            raise ValueError("No enabled platform has a configured product URL and adapter")
        self.database.initialize()
        self._stop.clear()
        self._dry_run = self.config.app.dry_run or dry_run is True
        self._sale_time = None if immediate else self.config.sale.target()
        self.run_id = self.database.create_run(
            self._sale_time.isoformat() if self._sale_time else None,
            ",".join(dict.fromkeys(product for _, product, _ in targets)),
        )
        for platform, items in grouped.items():
            self._status[platform] = PlatformStatus(platform=platform, product_id=items[0][0])
            self._machines[platform] = StateMachine(
                platform,
                items[0][0],
                recorder=lambda event: self.database.record_event(self.run_id, event),
            )
        if self.order_lock.status():
            for platform in grouped:
                self._transition(platform, State.PREPARING)
                self._transition(
                    platform, State.WAITING_HUMAN, "Existing order guard requires reconciliation"
                )
            self.database.finish_run(self.run_id, "BLOCKED")
            return self.snapshot()
        self.running = True
        try:
            await self._notify("started", "程序启动；平台分别监测，最终订单使用单一锁")
            self._active_platforms = set(grouped)
            self._tasks = [asyncio.create_task(self._worker(p, t)) for p, t in grouped.items()]
            await asyncio.gather(*self._tasks)
        finally:
            self.running = False
            states = [self._status[p].state for p in grouped]
            outcome = next(
                (
                    s.value
                    for s in (State.SUCCESS, State.WAITING_HUMAN, State.READY_TO_SUBMIT)
                    if s in states
                ),
                "STOPPED" if self._stop.is_set() else "FAILED",
            )
            self.database.finish_run(self.run_id, outcome)
            self._tasks = []
        return self.snapshot()

    async def stop(self) -> None:
        self._stop.set()
        self._guard_changed.set()
        for event in self._resume.values():
            event.set()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def resume(self, platform: Platform | None = None) -> None:
        guard = self.order_lock.status()
        for target, event in self._resume.items():
            if platform is not None and target != platform:
                continue
            if (
                guard
                and guard.get("owner") == self._owner(target)
                and guard.get("status") in UNCERTAIN
            ):
                await self._notify(
                    "unknown", "订单结果仍需人工核对；resume 不会解锁或重复提交", target
                )
                continue
            if self._status[target].state == State.WAITING_HUMAN:
                event.set()
