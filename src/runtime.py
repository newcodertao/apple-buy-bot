import asyncio
import contextlib
import json
import os

import yaml

from src.browser.groups import session_key
from src.browser.manager import BrowserManager
from src.core.clock import diagnostics
from src.core.config import AppConfig, ProductTarget, validate_platform_url
from src.core.engine import Engine
from src.core.exceptions import ConfigurationError
from src.core.logging import setup_logging
from src.core.models import OrderReview, Platform
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
        self.database = Database(config.paths.database)
        self.database.initialize()
        self.manager = BrowserManager(config.paths.profiles, headless=config.app.headless)
        self.adapters = self._build_adapters()
        self._owned_adapters = self.adapters
        self.engine = Engine(config, self.adapters, self.database)
        self.task: asyncio.Task | None = None
        self._control = asyncio.Lock()
        self._session_status = {}

    def _build_adapters(self):
        config = self.config
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

    async def open_login(self, platform: Platform = Platform.APPLE) -> dict:
        async with self._control:
            self._require_session_idle(platform)
            page = await self.manager.open_visible(platform)
            # The same adapter domain guard also checks login navigation redirects.
            await self.adapters[platform]._navigate(page, LOGIN_URLS[platform])
            for channel in Platform:
                if session_key(channel) == session_key(platform):
                    self._session_status[channel.value] = "UNKNOWN"
            return {"status": "请在程序浏览器中登录，然后点击检查登录；下次会复用本机登录状态"}

    async def check_login(self, platform: Platform = Platform.APPLE) -> dict:
        async with self._control:
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
            return {"platform": platform.value, "login": login.value, "status": login.value}

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
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                yaml.safe_dump(values, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            os.replace(temporary, path)
            self.config = updated
            self.adapters[platform] = self._build_adapters()[platform]
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

    async def start(self, platforms=None, immediate=False, dry_run=None) -> dict:
        async with self._control:
            if self.task and not self.task.done():
                raise ConfigurationError("Engine is already running")
            for channel in Platform:
                self._require_session_idle(channel)
            if not self.config.targets(platforms):
                raise ConfigurationError(
                    "No enabled product URLs; inspect and configure real URLs first"
                )
            if self.task:
                # Retrieve any exception before replacing task, avoiding silent background failures.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    self.task.result()
            # Keep caller-supplied adapters (including offline integration adapters).
            if self.adapters is self._owned_adapters:
                self.adapters = self._build_adapters()
                self._owned_adapters = self.adapters
            self.engine = Engine(self.config, self.adapters, self.database)
            self.task = asyncio.create_task(
                self.engine.run(platforms=platforms, immediate=immediate, dry_run=dry_run)
            )
            return {"status": "started"}

    async def stop(self) -> dict:
        async with self._control:
            # Cancel the main task too: it may still be queued and have no workers yet.
            if self.task and not self.task.done():
                self.task.cancel()
            await self.engine.stop()
            if self.task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.task
            return {"status": "stopped"}

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
        if self.task and self.task.done() and not self.task.cancelled():
            error = self.task.exception()
            if error:
                result["background_error"] = type(error).__name__
        return result

    async def close(self) -> None:
        await self.stop()
        await self.manager.close()
        self.database.close()
