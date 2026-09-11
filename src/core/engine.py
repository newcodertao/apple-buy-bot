"""Concurrent monitoring with one persistent permission to submit an order."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from time import perf_counter

from src.browser.groups import session_key
from src.core.config import AppConfig
from src.core.exceptions import CandidateUnavailable, HumanRequired, OrderRejected, RetryableError
from src.core.logging import redact, safe_url
from src.core.models import SKU, CartState, LoginStatus, Platform, PlatformStatus, State
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


def _failure_reason(error: Exception) -> str:
    if isinstance(error, HumanRequired) and str(error):
        return redact(str(error))
    return type(error).__name__


class PurchaseTaken(Exception):
    """Another workflow owns the global order; stop without another browser action."""


async def finish_cleanup(task: asyncio.Task) -> None:
    """Defer caller cancellation until independently owned cleanup has joined its tasks."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


class Engine:
    def __init__(
        self,
        config: AppConfig,
        adapters: dict[Platform, PlatformAdapter],
        database: Database,
        notifier: Notifier | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        waiter: Callable[[asyncio.Event, float], Awaitable[bool]] | None = None,
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
        self._joining: asyncio.Task | None = None
        self._session_holds: dict[str, dict] = {}
        self._owner_channels: dict[str, Platform] = {}
        self._guard_changed = asyncio.Event()
        self._dry_run = config.app.dry_run
        self._immediate = False
        self._sale_time = config.sale.target()
        self._now = now or (lambda: datetime.now(UTC))
        self._waiter = waiter
        self._candidate_cooldowns: dict[tuple[Platform, str, str], datetime] = {}
        self._login_attempted: set[Platform] = set()

    def snapshot(self) -> dict:
        now = self._now()
        return {
            "running": self.running,
            "run_id": self.run_id,
            "time": now.isoformat(),
            "sale_time": self._sale_time.isoformat() if self._sale_time else None,
            "seconds_to_sale": (self._sale_time - now).total_seconds() if self._sale_time else None,
            "dry_run": self._dry_run,
            "execution_mode": self._execution_mode(),
            "auto_submit": self.config.order.auto_submit,
            "mode": self.config.order.mode,
            "payment_method": self.config.order.payment_method,
            "installment_bank": self.config.order.installment_bank,
            "order_guard": self.order_lock.status(),
            "session_holds": {
                key: hold
                for platform in self.adapters
                if (hold := self.session_hold(platform))
                for key in (session_key(platform),)
            },
            "platforms": {p.value: s.model_dump(mode="json") for p, s in self._status.items()},
        }

    def _transition(self, platform: Platform, state: State, message: str = "") -> None:
        status = self._status[platform]
        machine = self._machines[platform]
        if machine.state == State.IDLE and state == State.PREPARING:
            message = f"execution_mode={self._execution_mode()}; {message}"
        if machine.state != state:
            machine.transition(state, sku_id=status.sku.id if status.sku else "", message=message)
        status.state = state
        hold = self._session_holds.get(session_key(platform))
        if hold and hold["active_channel"] == platform.value:
            hold["reason"] = state.value
        if message:
            status.result = message
        changed, self._guard_changed = self._guard_changed, asyncio.Event()
        changed.set()
        logging.getLogger(platform.value).info(
            "platform=%s state=%s action=transition message=%s", platform, state, message
        )

    def session_hold(self, platform: Platform) -> dict | None:
        """Report ownership even after an unpaid/uncertain workflow task has ended."""
        key = session_key(platform)
        guard = self.order_lock.status()
        if guard:
            owner = guard.get("owner", "")
            channel = self._owner_channels.get(owner)
            if channel is None:
                # Read legacy run_id:platform owners without rewriting persisted rows.
                channel = next(
                    (candidate for candidate in Platform if owner.endswith(":" + candidate.value)),
                    None,
                )
            if channel is None or session_key(channel) == key:
                return {
                    "active_channel": channel.value if channel else "unknown",
                    "reason": guard.get("status", "UNKNOWN"),
                }
        hold = self._session_holds.get(key)
        return dict(hold) if hold else None

    def _check_guard(self, platform: Platform) -> None:
        guard = self.order_lock.status()
        if guard and (
            guard.get("owner") != self._owner(platform)
            or guard.get("status") in {"SUCCESS", "UNKNOWN"}
        ):
            raise PurchaseTaken

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
        if name in {
            "check_stock",
            "select_sku",
            "add_to_cart",
            "goto_checkout",
            "verify_order",
            "submit_order",
        }:
            # Preparation, retries and human resume never grant an early purchase.
            # Only explicit immediate mode removes the deadline; dry-run still waits.
            if not await self._scheduler().wait_until():
                raise asyncio.CancelledError
        self._check_guard(platform)
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
            # Diagnostic logging must not replace a successful submit result or
            # the original exception. SQLite remains the submission audit.
            with contextlib.suppress(Exception):
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
            self._check_guard(platform)
            waiters = [
                asyncio.create_task(event.wait()),
                asyncio.create_task(self._guard_changed.wait()),
            ]
            try:
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
            if not event.is_set():
                continue
            event.clear()
            if self._stop.is_set():
                raise asyncio.CancelledError
            try:
                login = await self._call(platform, "login_status")
                self._status[platform].login = login
                verification = await self._call(platform, "detect_verification")
                if (
                    login == LoginStatus.REQUIRED
                    or (
                        self._login_required(platform, next_state)
                        and login != LoginStatus.AUTHENTICATED
                    )
                ) or verification.required:
                    await self._notify("human_required", "登录或安全验证仍未完成", platform)
                    continue
            except (HumanRequired, RetryableError):
                await self._notify("human_required", "页面仍无法确认，请人工检查", platform)
                continue
            self._transition(platform, next_state, "Human resumed; page checked again")
            return
        raise asyncio.CancelledError

    def _login_required(self, platform: Platform, state: State) -> bool:
        return state == State.PREPARING or state not in getattr(
            self.adapters[platform], "public_states", ()
        )

    async def _ensure_clear(self, platform: Platform, next_state: State) -> None:
        while True:
            try:
                login = await self._call(platform, "login_status")
                self._status[platform].login = login
                verification = await self._call(platform, "detect_verification")
                if verification.required and verification.reason != "login":
                    raise HumanRequired("Security verification requires human action")
                if (
                    login == LoginStatus.REQUIRED
                    and platform not in self._login_attempted
                    and callable(getattr(self.adapters[platform], "try_login_from_env", None))
                ):
                    # Only the adapter's normal visible login flow may use local credentials.
                    # A missing credential or challenge never permits a retry loop or navigation.
                    self._login_attempted.add(platform)
                    await self._call(platform, "try_login_from_env")
                    login = await self._call(platform, "login_status")
                    self._status[platform].login = login
                    verification = await self._call(platform, "detect_verification")
                if (
                    login == LoginStatus.REQUIRED
                    or (
                        self._login_required(platform, next_state)
                        and login != LoginStatus.AUTHENTICATED
                    )
                ):
                    raise HumanRequired("Login is required or unknown")
                if verification.required:
                    raise HumanRequired("Security verification requires human action")
                return
            except HumanRequired as exc:
                await self._pause(platform, next_state, _failure_reason(exc))

    async def _action(self, platform: Platform, state: State, name: str, *args) -> None:
        self._transition(platform, state)
        while True:
            try:
                await self._ensure_clear(platform, state)
                if name == "add_to_cart" and platform == Platform.APPLE:
                    adapter = self.adapters[platform]
                    cart_state = getattr(adapter, "cart_state", None)
                    if cart_state is None:
                        raise HumanRequired("Apple cart status is unavailable; inspect the page")
                    if cart_state == CartState.NOT_ATTEMPTED:
                        await self._call(platform, name, *args)
                    if adapter.cart_state != CartState.CART_VERIFIED:
                        await self._call(platform, "verify_cart", *args)
                    if adapter.cart_state != CartState.CART_VERIFIED:
                        raise HumanRequired("Cart item and quantity still require verification")
                else:
                    await self._call(platform, name, *args)
                return
            except (HumanRequired, RetryableError, TimeoutError, ConnectionError) as exc:
                await self._pause(platform, state, _failure_reason(exc))
                # Other channels retain their existing no-replay protection and checkout
                # review. Apple additionally proves the exact bag before announcing success.
                if name == "add_to_cart" and platform != Platform.APPLE:
                    return

    async def _review(self, platform: Platform, sku: SKU):
        preferences = self.config.preferences_for(sku.product_id)
        self._transition(platform, State.VERIFYING)
        while True:
            try:
                await self._ensure_clear(platform, State.VERIFYING)
                review = await self._call(platform, "verify_order")
                if platform == Platform.APPLE:
                    verify_checkout(review, sku, preferences)
                else:
                    verify_checkout(
                        review,
                        sku,
                        preferences,
                        target=self.config.products[sku.product_id].platforms[platform],
                        order_policy=self.config.order,
                    )
                verification = await self._call(platform, "detect_verification")
                if verification.required:
                    raise HumanRequired("Verification appeared during checkout review")
                return review
            except (HumanRequired, RetryableError, TimeoutError, ConnectionError) as exc:
                await self._pause(platform, State.VERIFYING, _failure_reason(exc))

    def _owner(self, platform: Platform) -> str:
        return f"{self.run_id}:{platform.value}"

    async def _claim(self, platform: Platform) -> bool:
        return not self._stop.is_set() and self.order_lock.acquire(self._owner(platform))

    async def _purchase(self, platform: Platform, sku: SKU) -> bool:
        preferences = self.config.preferences_for(sku.product_id)
        try:
            await self._action(platform, State.SELECTING_SKU, "select_sku", sku)
        except CandidateUnavailable as exc:
            self._candidate_cooldowns[(platform, sku.product_id, sku.id)] = self._now() + timedelta(
                seconds=max(10, self._interval(platform))
            )
            self._transition(platform, State.MONITORING, redact(str(exc)))
            return False
        await self._action(platform, State.ADDING_CART, "add_to_cart", preferences.quantity)
        await self._capture(platform, "cart")
        await self._notify("cart", "成功加入购物车", platform)
        await self._action(platform, State.CHECKOUT, "goto_checkout")
        await self._capture(platform, "checkout")
        await self._notify("checkout", "已进入结算，正在核对订单", platform)
        review = await self._review(platform, sku)
        owner = self._owner(platform)
        if not await self._claim(platform):
            self._transition(platform, State.STOPPED, "Another order owns the purchase reservation")
            return True
        self._transition(platform, State.READY_TO_SUBMIT, "Checkout verified; purchase reserved")
        if self._dry_run or not self.config.order.auto_submit:
            self.database.record_order(
                self.run_id, sku, "READY_TO_SUBMIT", "Submission disabled", review=review
            )
            await self._notify("ready", "订单已核对；提交开关关闭，未下单", platform)
            return True
        # Both switches are checked again after a fresh complete checkout review.
        review = await self._review(platform, sku)
        guard = self.order_lock.status()
        if not guard or guard.get("owner") != owner or guard.get("status") != "CLAIMED":
            self._transition(platform, State.STOPPED, "Purchase reservation is no longer valid")
            return True
        if self._stop.is_set() or self._dry_run or not self.config.order.auto_submit:
            self._transition(platform, State.READY_TO_SUBMIT, "Submission disabled")
            return True
        self._transition(platform, State.READY_TO_SUBMIT, "Fresh order verification passed")
        # Commit SUBMITTING before the first possible irreversible adapter operation.
        self.order_lock.mark_submission(owner, "SUBMITTING")
        self._transition(platform, State.SUBMITTING)
        try:
            result = await self._call(platform, "submit_order")
        except OrderRejected:
            self.order_lock.mark_submission(owner, "REJECTED")
            self.order_lock.release(owner)
            self.database.record_order(
                self.run_id, sku, "REJECTED", "Confirmed rejection", review=review
            )
            raise
        except (Exception, asyncio.CancelledError) as exc:
            self.order_lock.mark_submission(owner, "UNKNOWN")
            self.database.record_order(
                self.run_id, sku, "UNKNOWN", type(exc).__name__, review=review
            )
            self._transition(
                platform, State.WAITING_HUMAN, "Submission outcome UNKNOWN; do not resubmit"
            )
            await self._capture(platform, "submission_unknown")
            await self._notify("unknown", "订单结果未知，已保留锁；请人工核对订单", platform)
            return True
        if result.status == "SUCCESS" and result.order_id and result.order_id.strip():
            self.order_lock.mark_submission(owner, "SUCCESS")
            self._transition(platform, State.SUCCESS, "Order confirmed; payment remains manual")
            self.database.record_order(
                self.run_id,
                sku,
                "SUCCESS",
                "Confirmed order identifier received",
                review=review,
                result=result,
            )
            await self._capture(platform, "success")
            await self._notify("success", "订单已确认；请人工完成支付", platform)
        elif result.status == "REJECTED":
            self.order_lock.mark_submission(owner, "REJECTED")
            self.order_lock.release(owner)
            self.database.record_order(
                self.run_id, sku, "REJECTED", "Confirmed rejection", review=review, result=result
            )
            raise OrderRejected("Confirmed rejection")
        else:
            self.order_lock.mark_submission(owner, "UNKNOWN")
            self.database.record_order(
                self.run_id,
                sku,
                "UNKNOWN",
                "No conclusive order result",
                review=review,
                result=result,
            )
            self._transition(
                platform, State.WAITING_HUMAN, "Submission outcome UNKNOWN; do not resubmit"
            )
            await self._capture(platform, "submission_unknown")
            await self._notify("unknown", "订单结果未知，已保留锁；请人工核对订单", platform)
        return True

    def _scheduler(self) -> Scheduler:
        return Scheduler(self._sale_time, self._stop, now=self._now, waiter=self._waiter)

    def _execution_mode(self) -> str:
        return ("immediate" if self._immediate else "scheduled") + (
            "_dry_run" if self._dry_run else "_live"
        )

    async def _prepare(self, platform: Platform, product_id: str, url: str) -> None:
        scheduler = self._scheduler()
        self._transition(platform, State.PREPARING, "Checking session and scheduled stages")
        if not await scheduler.wait_until(-600):
            raise asyncio.CancelledError
        # Opening a page gives a fresh persistent context a visible place to log in.
        await self._call(platform, "open_product", product_id, url)
        opened_at = perf_counter()
        await self._capture(platform, "product_open")
        await self._ensure_clear(platform, State.PREPARING)
        self._transition(platform, State.WAITING, "Waiting for T-3min product preparation")
        if not await scheduler.wait_until(-180):
            raise asyncio.CancelledError
        if platform == Platform.APPLE or perf_counter() - opened_at >= self._interval(platform):
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

    def _interval(self, platform: Platform) -> float:
        interval = polling_interval(self.config.monitor, self._sale_time)
        if platform != Platform.APPLE:
            interval = max(interval, self.config.platforms[platform].refresh_interval)
        return interval

    async def _worker(self, channels: dict[Platform, list[tuple[str, str]]]) -> None:
        """One task owns the page; only a completed stock check may yield to a peer."""
        queue = list(channels)
        progress = {
            platform: {"index": 0, "checks": 0, "retries": 0, "prepared": False}
            for platform in channels
        }
        platform = queue[0]
        key = session_key(platform)
        try:
            while queue and not self._stop.is_set():
                platform = queue.pop(0)
                status = self._status[platform]
                current = progress[platform]
                targets = channels[platform]
                owner = self._owner(platform)
                self._session_holds[key] = {
                    "active_channel": platform.value,
                    "reason": status.state.value,
                }
                product_id, url = targets[current["index"] % len(targets)]
                status.product_id = product_id
                self._machines[platform].product_id = product_id
                delay = self._interval(platform)
                try:
                    self._check_guard(platform)
                    if not current["prepared"]:
                        await self._prepare(platform, product_id, url)
                        current["prepared"] = True
                    else:
                        self._transition(platform, State.MONITORING)
                        await self._call(platform, "open_product", product_id, url)
                        await self._ensure_clear(platform, State.MONITORING)
                    current["checks"] += 1
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
                    candidates = [
                        sku
                        for sku in rank_skus(skus, self.config.preferences_for(product_id))
                        if self._candidate_cooldowns.get(
                            (platform, product_id, sku.id), self._now()
                        )
                        <= self._now()
                    ]
                    status.stock = bool(candidates)
                    if skus and not candidates:
                        status.sku = skus[0]
                        status.result = skus[0].delivery or "没有符合配置的可购买 SKU"
                    for candidate in candidates:
                        if (
                            self._candidate_cooldowns.get(
                                (platform, product_id, candidate.id), self._now()
                            )
                            > self._now()
                        ):
                            continue
                        status.sku = candidate
                        self._transition(platform, State.STOCK_FOUND)
                        await self._capture(platform, "stock_found")
                        await self._notify("stock", "检测到符合配置的库存", platform)
                        if await self._purchase(platform, candidate):
                            return
                    current["index"] += 1
                    if current["checks"] < self.config.monitor.max_checks:
                        queue.append(platform)
                    else:
                        self._transition(platform, State.FAILED, "Stock check limit reached")
                except HumanRequired as exc:
                    await self._pause(
                        platform,
                        State.MONITORING if current["prepared"] else State.PREPARING,
                        _failure_reason(exc),
                    )
                    # Human pauses do not create unbounded automatic request retries.
                    current["retries"] += 1
                    status.retries = current["retries"]
                    if current["checks"] < self.config.monitor.max_checks:
                        queue.insert(0, platform)
                    else:
                        self._transition(platform, State.FAILED, "Stock check limit reached")
                except OrderRejected:
                    # An explicit rejection releases only this confirmed attempt. Do not
                    # replay the cart or submit in the same run to try again.
                    self._transition(platform, State.FAILED, "Order rejected; no automatic replay")
                    await self._notify("failed", "订单被明确拒绝，本轮不重复下单", platform)
                except (RetryableError, TimeoutError, ConnectionError) as exc:
                    guard = self.order_lock.status()
                    if guard and guard.get("owner") == owner:
                        self.order_lock.release(owner)
                    current["retries"] += 1
                    status.retries = current["retries"]
                    self._transition(platform, State.FAILED, type(exc).__name__)
                    await self._capture(platform, "error")
                    await self._notify("failed", "操作失败，按重试上限退避", platform)
                    if current["retries"] <= self.config.monitor.max_retries:
                        self._transition(platform, State.RETRYING, "Bounded retry with backoff")
                        delay = backoff(
                            current["retries"] - 1,
                            1,
                            self.config.monitor.backoff_max,
                            self.config.monitor.jitter,
                            getattr(exc, "retry_after", 0),
                        )
                        if platform != Platform.APPLE:
                            delay = max(delay, self._interval(platform))
                        queue.insert(0, platform)
                if queue and not await (self._waiter or wait_or_stop)(self._stop, delay):
                    raise asyncio.CancelledError
            if self._stop.is_set():
                raise asyncio.CancelledError
        except PurchaseTaken:
            self._transition(platform, State.STOPPED, "Another platform reserved the purchase")
        except asyncio.CancelledError:
            owner = self._owner(platform)
            guard = self.order_lock.status()
            if guard and guard.get("owner") == owner and guard.get("status") == "SUBMITTING":
                self.order_lock.mark_submission(owner, "UNKNOWN")
                self._transition(
                    platform, State.WAITING_HUMAN, "Submission interrupted; outcome UNKNOWN"
                )
            elif self._status[platform].state not in {State.SUCCESS, State.STOPPED} and (
                not guard or guard.get("owner") != owner or guard.get("status") not in UNCERTAIN
            ):
                self._transition(platform, State.STOPPED, "Stopped by user")
        except Exception as exc:
            # Unclassified failures may follow a partial UI action: no automatic replay.
            guard = self.order_lock.status()
            if (
                guard
                and guard.get("owner") == self._owner(platform)
                and guard.get("status") == "SUCCESS"
            ):
                # A confirmed receipt is still a fact if writing its audit event fails.
                self._status[platform].state = self._machines[platform].state = State.SUCCESS
                raise
            if self._status[platform].state != State.SUCCESS:
                self._transition(platform, State.WAITING_HUMAN, type(exc).__name__)
            await self._capture(platform, "error")
            await self._notify("failed", "未知错误已停止自动操作，请人工检查", platform)
        finally:
            held_states = {State.WAITING_HUMAN, State.READY_TO_SUBMIT, State.SUCCESS}
            if self._status[platform].state not in held_states:
                self._session_holds.pop(key, None)
            for peer in queue:
                if self._status[peer].state not in {State.STOPPED, State.FAILED}:
                    self._transition(peer, State.STOPPED, "Shared session workflow ended")
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
        self._joining = None
        self._candidate_cooldowns.clear()
        self._login_attempted.clear()
        self._dry_run = self.config.app.dry_run or dry_run is True
        self._immediate = bool(immediate)
        self._sale_time = None if immediate else self.config.sale.target()
        self.run_id = self.database.create_run(
            self._sale_time.isoformat() if self._sale_time else None,
            ",".join(dict.fromkeys(product for _, product, _ in targets)),
        )
        for platform, items in grouped.items():
            self._owner_channels[self._owner(platform)] = platform
            if self.session_hold(platform) and self._status[platform].state in {
                State.SUCCESS,
                State.READY_TO_SUBMIT,
                State.WAITING_HUMAN,
            }:
                continue
            self._status[platform] = PlatformStatus(platform=platform, product_id=items[0][0])
            self._machines[platform] = StateMachine(
                platform,
                items[0][0],
                recorder=lambda event: self.database.record_event(self.run_id, event),
            )
        if self.order_lock.status() or any(self.session_hold(p) for p in grouped):
            for platform in grouped:
                if self._status[platform].state in {
                    State.SUCCESS,
                    State.READY_TO_SUBMIT,
                    State.WAITING_HUMAN,
                }:
                    continue
                self._transition(platform, State.PREPARING)
                self._transition(
                    platform, State.WAITING_HUMAN, "Existing order guard requires reconciliation"
                )
            self.database.finish_run(self.run_id, "BLOCKED")
            return self.snapshot()
        self.running = True
        try:
            await self._notify("started", "程序启动；京东独立监测，淘宝天猫共用会话并串行操作")
            sessions: dict[str, dict[Platform, list[tuple[str, str]]]] = {}
            for platform in sorted(grouped, key=lambda p: p == Platform.TAOBAO):
                sessions.setdefault(session_key(platform), {})[platform] = grouped[platform]
            self._tasks = [
                asyncio.create_task(self._worker(channels)) for channels in sessions.values()
            ]
            await asyncio.gather(*self._tasks)
        finally:
            try:
                # gather propagates a worker error before its siblings finish.
                # Keep ownership and task references until every sibling is joined.
                await self._join_workers()
            finally:
                self._tasks = []
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
        return self.snapshot()

    async def _join_workers(self) -> None:
        async def cancel_and_join(tasks):
            for task in tasks:
                if not task.done() and not task.cancelling():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        if self._joining is None:
            self._joining = asyncio.create_task(cancel_and_join(list(self._tasks)))
        await finish_cleanup(self._joining)

    async def stop(self) -> None:
        self._stop.set()
        self._guard_changed.set()
        for event in self._resume.values():
            event.set()
        await self._join_workers()
        # Explicit stop releases in-memory navigation ownership. Persistent order
        # guards still report their hold and cannot be cleared by stopping a task.
        self._session_holds.clear()

    async def resume(self, platform: Platform | None = None) -> None:
        if platform is not None:
            hold = self.session_hold(platform)
            if hold and hold["active_channel"] in Platform:
                platform = Platform(hold["active_channel"])
        guard = self.order_lock.status()
        for target, event in self._resume.items():
            if platform is not None and target != platform:
                continue
            if guard and guard.get("status") in UNCERTAIN:
                await self._notify(
                    "unknown", "订单结果仍需人工核对；resume 不会解锁或重复提交", target
                )
                continue
            if self._status[target].state == State.WAITING_HUMAN:
                event.set()
