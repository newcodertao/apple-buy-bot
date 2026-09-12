import asyncio
import contextlib
import json
import os

import yaml

from src.browser.groups import session_key
from src.browser.manager import BrowserManager
from src.core.clock import diagnostics
from src.core.config import AppConfig, ProductTarget, validate_platform_url
from src.core.engine import Engine, finish_cleanup
from src.core.exceptions import ConfigurationError
from src.core.logging import setup_logging
from src.core.models import CartState, OrderReview, Platform
from src.order.plan import draft_plan, load_plan, save_plan
from src.platforms.apple_cn.adapter import AppleCNAdapter
from src.platforms.apple_cn.selectors import LOGIN_URL
from src.platforms.jd.adapter import JDAdapter
from src.platforms.taobao.adapter import TaobaoAdapter
from src.platforms.tmall.adapter import TmallAdapter
from src.storage.database import Database

LOGIN_URLS = {
    Platform.APPLE: LOGIN_URL,
    Platform.JD: "https://passport.jd.com/new/login.aspx",
    Platform.TMALL: "https://login.tmall.com/",
    Platform.TAOBAO: "https://login.taobao.com/",
}


class Runtime:
    """One process owns the engine and browser sessions; SQLite protects cross-process submit."""

    def __init__(self, config: AppConfig):
        self.config = config
        setup_logging(config.paths.logs)
        self._plan_path = config.paths.root / "data" / "purchase-plan.json"
        self.purchase_plan = load_plan(self._plan_path, draft_plan(config))
        self.database = Database(config.paths.database)
        self.database.initialize()
        self.manager = BrowserManager(config.paths.profiles, headless=config.app.headless)
        self.adapters = self._build_adapters()
        self._bind_plan()
        self.engine = Engine(config, self.adapters, self.database)
        self.task: asyncio.Task | None = None
        self._control = asyncio.Lock()
        self._session_status = {}
        self._closed = False

    def _build_adapters(self, config=None):
        config = config or self.config
        preferences = {key: config.preferences_for(key) for key in config.products}
        adapters = {
            Platform.APPLE: AppleCNAdapter(
                self.manager,
                config.paths.screenshots,
                {key: config.preferences_for(key) for key in config.products},
                allow_submit=not config.app.dry_run and config.order.auto_submit,
                payment_method=config.order.payment_method,
                installment_bank=config.order.installment_bank,
            ),
        }
        for platform, adapter_type in (
            (Platform.JD, JDAdapter),
            (Platform.TMALL, TmallAdapter),
            (Platform.TAOBAO, TaobaoAdapter),
        ):
            adapters[platform] = adapter_type(
                self.manager,
                config.paths.screenshots,
                preferences,
                targets={
                    key: product.platforms[platform]
                    for key, product in config.products.items()
                    if platform in product.platforms
                },
                order_policy=config.order,
                allow_submit=not config.app.dry_run and config.order.auto_submit,
            )
        return adapters

    def _require_session_idle(self, platform: Platform) -> None:
        if self.task and not self.task.done():
            raise ConfigurationError("运行中请在当前浏览器处理问题，然后点击继续")
        hold = self.engine.session_hold(platform)
        if hold:
            raise ConfigurationError(
                f"{hold['active_channel']} 正保留结算或订单页面：{hold['reason']}；请先核对结果"
            )
        adapter = self.adapters.get(platform)
        if (
            getattr(adapter, "_cart_attempted", False)
            or getattr(adapter, "_submit_attempted", False)
            or getattr(adapter, "cart_state", CartState.NOT_ATTEMPTED) != CartState.NOT_ATTEMPTED
        ):
            raise ConfigurationError("已有加购或提交尝试；保留原页面核对，不能重建购买流程")

    def _bind_plan(self) -> None:
        adapter = self.adapters.get(Platform.APPLE)
        if isinstance(adapter, AppleCNAdapter):
            adapter.purchase_plan = self.purchase_plan
            adapter.bind_saved_address = self._bind_saved_address

    def _bind_saved_address(self, fingerprint: str) -> None:
        """Persist the first complete saved address under the approved start terms."""
        plan = self.purchase_plan
        if (
            plan.approved_at is None
            or plan.address_basis != "selected_saved_address_first_checkout_bind"
            or not fingerprint
            or (plan.address_fingerprint and plan.address_fingerprint != fingerprint)
        ):
            raise ConfigurationError("收货地址未获本次授权或已经变化，请在官网核对")
        if plan.address_fingerprint == fingerprint:
            return
        updated = plan.model_copy(update={"address_fingerprint": fingerprint})
        save_plan(self._plan_path, updated)
        self.purchase_plan = updated
        self._bind_plan()

    def _require_login_idle(self, platform: Platform) -> None:
        # A recorded order prevents another purchase, not logging in to review it.
        # Never navigate away from an owned checkout or an in-flight task.
        adapter = self.adapters.get(platform)
        if (
            platform == Platform.APPLE
            and not (self.task and not self.task.done())
            and self.manager.current_page(platform) is None
            and not getattr(adapter, "_cart_attempted", False)
            and not getattr(adapter, "_submit_attempted", False)
        ):
            return
        self._require_session_idle(platform)

    def plan_snapshot(self) -> dict:
        plan = self.purchase_plan
        return {
            **plan.terms(),
            "digest": plan.digest,
            "approved": plan.approved_at is not None,
            "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
            "address_confirmed": bool(plan.address_fingerprint),
            "confirmed_product_count": len(plan.market_fingerprints),
        }

    async def approve_plan(self, digest: str) -> dict:
        """The local UI/CLI must present the exact terms before approving this revision."""
        async with self._control:
            if digest != self.purchase_plan.digest or not self.purchase_plan.products:
                raise ConfigurationError("购买计划已变化或没有 Apple 商品，请重新查看后确认")
            if self.task and not self.task.done():
                state = self.engine.snapshot()["platforms"].get("apple", {}).get("state")
                if state != "WAITING_HUMAN":
                    raise ConfigurationError("请等待任务暂停后确认购买计划")
            updated = self.purchase_plan.approved()
            save_plan(self._plan_path, updated)
            self.purchase_plan = updated
            self._bind_plan()
            return {"status": "本次购买计划已保存；提交开关保持原值", "plan": self.plan_snapshot()}

    async def open_login(self, platform: Platform = Platform.APPLE) -> dict:
        async with self._control:
            self._require_login_idle(platform)
            page = await self.manager.open_visible(platform)
            # The same adapter domain guard also checks login navigation redirects.
            await self.adapters[platform]._navigate(page, LOGIN_URLS[platform])
            for channel in Platform:
                if session_key(channel) == session_key(platform):
                    self._session_status[channel.value] = "UNKNOWN"
            return {"status": "请在程序浏览器中登录，然后点击检查登录；下次会复用本机登录状态"}

    async def check_login(self, platform: Platform = Platform.APPLE) -> dict:
        async with self._control:
            hold = self.engine.session_hold(platform)
            if hold and hold["active_channel"] != platform.value:
                raise ConfigurationError("请选择当前占用会话的平台检查登录")
            # Read the existing page during a human pause; never navigate away
            # from a paused checkout or an existing receipt to check authentication.
            if self.task and not self.task.done():
                state = self.engine.snapshot()["platforms"].get(platform.value, {}).get("state")
                if state != "WAITING_HUMAN":
                    raise ConfigurationError("任务运行中，请等待暂停后检查当前页面登录状态")
            elif self.manager.current_page(platform) is None:
                self._require_session_idle(platform)
            adapter = self.adapters[platform]
            if self.manager.current_page(platform) is None:
                targets = self.config.targets([platform])
                if targets:
                    _, product_id, url = targets[0]
                    await adapter.open_product(product_id, url)
                else:
                    page = await self.manager.open_visible(platform)
                    await adapter._navigate(page, LOGIN_URLS[platform])
            login = await adapter.login_status()
            self._session_status[platform.value] = login.value
            details = getattr(adapter, "login_status_detail", None)
            return {
                **(await details() if details else {}),
                "platform": platform.value,
                "login": login.value,
                "status": login.value,
                "state_import": self.manager.login_state_import_status(platform),
                "network": self.manager.network_diagnostics(platform),
            }

    async def check_product(self, platform: Platform, product_id: str) -> dict:
        async with self._control:
            self._require_session_idle(platform)
            product = self.config.products.get(product_id)
            target = product.platforms.get(platform) if product else None
            if not target or not target.url:
                raise ConfigurationError("请先保存该平台的商品链接")
            adapter = self.adapters[platform]
            await adapter.open_product(product_id, target.url)
            inspect_details = getattr(adapter, "inspect_product_details", None)
            if inspect_details:
                return {"status": "商品检查完成", **await inspect_details()}
            skus = await adapter.check_stock()
            return {"status": "商品检查完成", "skus": [s.model_dump(mode="json") for s in skus]}

    async def save_target(self, platform: Platform, product_id: str, target: ProductTarget) -> dict:
        async with self._control:
            self._require_session_idle(platform)
            if product_id not in self.config.products:
                raise ConfigurationError("请选择已有商品")
            try:
                if target.url:
                    validate_platform_url(platform, target.url)
                values = self.config.model_dump(mode="json")
                values["products"][product_id]["platforms"][platform.value] = target.model_dump(
                    mode="json"
                )
                values["platforms"].setdefault(
                    platform.value, {"enabled": True, "refresh_interval": 10}
                )
                updated = AppConfig.model_validate(values)
            except ValueError:
                raise ConfigurationError("商品链接或配置无效") from None
            path = self.config._source_path
            if path is None:
                raise ConfigurationError("当前配置没有文件来源，无法保存")
            updated._root = self.config._root
            updated._source_path = path
            next_plan = draft_plan(updated)
            if next_plan.digest == self.purchase_plan.digest:
                next_plan = self.purchase_plan
            # Prepare every fallible in-memory replacement before publishing the
            # file: a build failure must never pair a new target with old approval.
            next_adapter = self._build_adapters(updated)[platform]
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                yaml.safe_dump(values, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            os.replace(temporary, path)
            self.config = updated
            self.purchase_plan = next_plan
            self.adapters[platform] = next_adapter
            self._bind_plan()
            self.engine.config = updated
            return {"status": "商品配置已保存"}

    async def read_order(self, platform: Platform) -> dict:
        async with self._control:
            if self.task and not self.task.done():
                raise ConfigurationError("任务运行中，请等待当前步骤完成")
            hold = self.engine.session_hold(platform)
            if hold and hold["active_channel"] != platform.value:
                raise ConfigurationError("请先选择占用此会话的下单平台")
            adapter = self.adapters[platform]
            stored = next(
                (
                    row
                    for row in self.database.recent("orders", 100)
                    if row["platform"] == platform.value and row.get("order_id")
                ),
                None,
            )
            if stored and stored.get("review_json") and hasattr(adapter, "restore_review"):
                try:
                    review = OrderReview.model_validate(json.loads(stored["review_json"]))
                    adapter.restore_review(review)
                except (ValueError, TypeError):
                    raise ConfigurationError("已存订单摘要无法核对，请在官网查看该笔订单") from None
            expected_id = stored["order_id"] if stored else None
            result = await adapter.read_order_status(expected_id)
            if expected_id and result.order_id and result.order_id != expected_id:
                raise ConfigurationError("当前页面与已记录订单不一致，请打开对应订单后重查")
            if expected_id and result.status == "SUCCESS":
                self.database.update_order_status(
                    expected_id,
                    platform,
                    payment_state=result.payment_state,
                    financing_state=result.financing_state,
                )
            data = result.model_dump(mode="json")
            # Order references stay local; a suffix is enough for identifying the receipt.
            if data.get("order_id"):
                data["order_id"] = "***" + data["order_id"][-4:]
            return data

    async def inspect_checkout(self, platform: Platform) -> dict:
        async with self._control:
            hold = self.engine.session_hold(platform)
            if hold and hold["active_channel"] != platform.value:
                raise ConfigurationError("请选择当前占用会话的平台")
            reader = getattr(self.adapters[platform], "inspect_checkout", None)
            if reader is None:
                raise ConfigurationError("该平台结算页尚无已验证的检查入口，请直接查看浏览器")
            return await reader()

    async def confirm_checkout(self, platform: Platform, kind: str) -> dict:
        """Bind an explicit local user's confirmation to the current visible page."""
        if kind not in {"address", "market"}:
            raise ConfigurationError("未知的确认项目")
        async with self._control:
            hold = self.engine.session_hold(platform)
            if hold and hold["active_channel"] != platform.value:
                raise ConfigurationError("请选择当前占用会话的平台")
            if self.task and not self.task.done():
                state = self.engine.snapshot()["platforms"].get(platform.value, {}).get("state")
                if state != "WAITING_HUMAN":
                    raise ConfigurationError("请等待流程暂停后，在当前页面核对并确认")
            if (
                platform == Platform.APPLE
                and isinstance(self.adapters[platform], AppleCNAdapter)
                and self.purchase_plan.approved_at is None
            ):
                raise ConfigurationError("请先查看并批准本次购买计划")
            result = await getattr(self.adapters[platform], "confirm_" + kind)()
            if platform == Platform.APPLE and self.purchase_plan.approved_at is not None:
                if kind == "address":
                    changes = {"address_fingerprint": result["address_fingerprint"]}
                else:
                    fingerprint = result["market_evidence"].removeprefix("MANUAL_CN:")
                    changes = {
                        "market_fingerprints": sorted(
                            set(self.purchase_plan.market_fingerprints) | {fingerprint}
                        )
                    }
                updated = self.purchase_plan.model_copy(update=changes)
                save_plan(self._plan_path, updated)
                self.purchase_plan = updated
                self._bind_plan()
            return {"status": "本次确认已绑定当前页面；页面变化后须重新确认", **result}

    async def start(self, platforms=None, immediate=False, dry_run=None) -> dict:
        async with self._control:
            if self._closed:
                raise ConfigurationError("Runtime is closed")
            if self.engine.running or (self.task and not self.task.done()):
                raise ConfigurationError("Engine is already running")
            for channel in Platform:
                self._require_session_idle(channel)
            if not self.config.targets(platforms):
                raise ConfigurationError(
                    "No enabled product URLs; inspect and configure real URLs first"
                )
            if (
                any(p == Platform.APPLE for p, _, _ in self.config.targets(platforms))
                and isinstance(self.adapters.get(Platform.APPLE), AppleCNAdapter)
                and self.purchase_plan.approved_at is None
            ):
                raise ConfigurationError("请先一次确认本次商品、预算、已保存地址依据和付款方式")
            if self.task:
                # Retrieve any exception before replacing task, avoiding silent background failures.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    self.task.result()
            # A new Engine must not reset live adapter attempt flags or approvals.
            self._bind_plan()
            self.engine = Engine(self.config, self.adapters, self.database)
            self.task = asyncio.create_task(
                self.engine.run(platforms=platforms, immediate=immediate, dry_run=dry_run)
            )
            return {"status": "started"}

    async def _stop_locked(self) -> None:
        async def stop_owned_tasks():
            # Cancel the main task too: it may still be queued and have no workers yet.
            if self.task and not self.task.done() and not self.task.cancelling():
                self.task.cancel()
            try:
                await self.engine.stop()
            finally:
                if self.task:
                    await asyncio.gather(self.task, return_exceptions=True)

        await finish_cleanup(asyncio.create_task(stop_owned_tasks()))

    async def stop(self) -> dict:
        async with self._control:
            await self._stop_locked()
            return {"status": "stopped"}

    async def finish_task(self) -> dict:
        """End local automation without closing pages or reconciling any order."""
        async with self._control:
            await self._stop_locked()
            guard = self.database.guard_status()
            retained = {}
            for platform, adapter in self.adapters.items():
                cart_state = getattr(adapter, "cart_state", CartState.NOT_ATTEMPTED)
                if (
                    getattr(adapter, "_cart_attempted", False)
                    or getattr(adapter, "_submit_attempted", False)
                    or cart_state != CartState.NOT_ATTEMPTED
                ):
                    retained[platform.value] = {
                        "cart_state": str(cart_state),
                        "submit_attempted": bool(getattr(adapter, "_submit_attempted", False)),
                    }
            blocked = bool(guard or retained)
            return {
                "status": (
                    "本机任务已结束，浏览器与订单记录保留；已有交易保护仍需人工核对"
                    if blocked
                    else "本机任务已结束，浏览器与记录保留；可以开始下一次任务"
                ),
                "ended": True,
                "can_start_new_task": not blocked,
                "order_guard": guard["status"] if guard else None,
                "retained_protection": retained,
            }

    async def resume(self, platform=None) -> dict:
        if not self.task or self.task.done():
            raise ConfigurationError("No running task is waiting for human input")
        await self.engine.resume(platform)
        return {"status": "resume_requested"}

    def snapshot(self) -> dict:
        result = self.engine.snapshot()
        result["clock"] = diagnostics(self.config.sale.target(), self.config.sale.timezone)
        result["running"] = bool(self.task and not self.task.done())
        if not result["running"]:
            for platform in Platform:
                login = self._session_status.get(platform.value)
                if login and platform.value in result["platforms"]:
                    result["platforms"][platform.value]["login"] = login
        result["sessions"] = {
            p.value: {"group": session_key(p), "hold": self.engine.session_hold(p)}
            for p in Platform
        }
        result["capabilities"] = {
            platform.value: getattr(adapter, "live_validation", {})
            for platform, adapter in self.adapters.items()
        }
        result["order_guard"] = self.database.guard_status()
        result["purchase_plan"] = self.plan_snapshot()
        result["cart_states"] = {
            platform.value: str(getattr(adapter, "cart_state", CartState.NOT_ATTEMPTED))
            for platform, adapter in self.adapters.items()
        }
        result["login_diagnostics"] = {
            platform.value: {
                "state_import": self.manager.login_state_import_status(platform),
                "network": self.manager.network_diagnostics(platform),
            }
            for platform in self.adapters
        }
        if self.task and self.task.done() and not self.task.cancelled():
            error = self.task.exception()
            if error:
                result["background_error"] = type(error).__name__
        return result

    async def close(self) -> None:
        async with self._control:
            if self._closed:
                return

            async def close_owned_resources():
                await self._stop_locked()
                await self.manager.close()
                self.database.close()
                self._closed = True

            # Keep all three resources owned until closure actually succeeds.
            # Caller cancellation waits; a real close failure remains retryable.
            await finish_cleanup(asyncio.create_task(close_owned_resources()))
