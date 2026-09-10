import asyncio
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error, Page

from src.browser.inspection import capture_diagnostics
from src.browser.manager import BrowserManager, navigate, validate_url
from src.core.exceptions import ConfigurationError, HumanRequired, SelectorNotFound
from src.core.models import SKU, LoginStatus, OrderResult, OrderReview, Platform, Verification
from src.platforms.base import PlatformAdapter

_CHALLENGES = (
    ("captcha", r"验证码|人机验证|captcha|verify you are human"),
    ("slider", r"拖动滑块|滑块验证|slide to verify"),
    ("sms", r"短信验证|短信验证码|SMS verification|one.time code"),
    ("security", r"安全验证|账号安全|设备验证|人脸验证|异常访问|访问受限|security check"),
    ("address", r"请确认.{0,12}地址|地址.{0,12}(?:无效|异常|不完整)|confirm your address"),
    ("payment", r"支付验证|支付密码|付款验证|payment verification"),
    ("login", r"登录已失效|登录已过期|请重新登录|session expired|sign in again"),
)


class AppleCNAdapter(PlatformAdapter):
    """Phase 1: sessions and inspect work; commerce selectors remain UNKNOWN."""

    platform = Platform.APPLE
    allowed_hosts = ("apple.com", "apple.com.cn")
    require_https = True
    phase = "Phase 2"

    def __init__(self, manager: BrowserManager, screenshots_dir: Path):
        self.manager = manager
        self.screenshots_dir = Path(screenshots_dir)
        self.product_id = ""
        self._lock = asyncio.Lock()

    def _validate_platform_url(self, url: str) -> None:
        validate_url(url)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if not any(
            host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts
        ):
            raise ConfigurationError("Product URL does not belong to the selected platform")
        if self.require_https and (parsed.scheme != "https" or parsed.port not in {None, 443}):
            raise ConfigurationError("Platform URLs require HTTPS on port 443")

    async def login_status(self) -> LoginStatus:
        # A saved profile, an open page or a manual confirmation is not proof.
        async with self._lock:
            verification = await self._detect_verification()
            if verification.required and verification.reason == "login":
                return LoginStatus.REQUIRED
            return LoginStatus.UNKNOWN

    async def open_product(self, product_id: str, url: str) -> None:
        self._validate_platform_url(url)
        async with self._lock:
            page = await self.manager.open(self.platform)
            self.product_id = product_id
            try:
                await self._navigate(page, url)
                await self._guard_human()
            except HumanRequired:
                await self._capture("human_required", "WAITING_HUMAN")
                raise
            await self._capture("open_product", "PREPARING")

    async def inspect(self, url: str, output_dir: Path) -> Path:
        """Read-only inventory, restricted to this platform's official domains."""
        self._validate_platform_url(url)
        async with self._lock:
            page = await self.manager.open(self.platform)
            await self._navigate(page, url)
            verification = await self._detect_verification()
            state = "WAITING_HUMAN" if verification.required else ""
            return await capture_diagnostics(
                page, output_dir, self.platform.value, "inspect", state
            )

    async def _navigate(self, page: Page, url: str) -> None:
        blocked = False

        session = await page.context.new_cdp_session(page)

        async def guard(event: dict) -> None:
            nonlocal blocked
            try:
                self._validate_platform_url(event["request"]["url"])
            except ConfigurationError:
                blocked = True
                await session.send(
                    "Fetch.failRequest",
                    {"requestId": event["requestId"], "errorReason": "BlockedByClient"},
                )
                return
            await session.send("Fetch.continueRequest", {"requestId": event["requestId"]})

        # Chromium Fetch pauses EACH document request, including redirect hops.
        # Playwright page.route only handles a redirect chain's initial request.
        session.on("Fetch.requestPaused", guard)
        await session.send(
            "Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]}
        )
        try:
            try:
                await navigate(page, url)
            except Exception:
                if blocked:
                    raise HumanRequired("Navigation left the approved platform domains") from None
                raise
            try:
                self._validate_platform_url(page.url)
            except ConfigurationError:
                raise HumanRequired("Navigation left the approved platform domains") from None
        finally:
            await session.detach()

    async def _unknown(self, action: str) -> None:
        async with self._lock:
            await self._guard_human()
            await self._capture(action + "_unknown", "WAITING_HUMAN")
            raise SelectorNotFound(
                f"UNKNOWN: {self.platform.value} {action} requires verified {self.phase} selectors"
            )

    async def get_skus(self) -> list[SKU]:
        await self._unknown("get_skus")
        return []  # Unreachable; no guessed product data is returned.

    async def check_stock(self) -> list[SKU]:
        await self._unknown("check_stock")
        return []

    async def select_sku(self, sku: SKU) -> None:
        await self._unknown("select_sku")

    async def add_to_cart(self, quantity: int) -> None:
        await self._unknown("add_to_cart")

    async def goto_checkout(self) -> None:
        await self._unknown("goto_checkout")

    async def verify_order(self) -> OrderReview:
        await self._unknown("verify_order")
        raise AssertionError("Unreachable")

    async def submit_order(self) -> OrderResult:
        await self._unknown("submit_order")
        raise AssertionError("Unreachable")

    async def _detect_verification(self) -> Verification:
        page = self.manager.current_page(self.platform)
        if page is None:
            return Verification()
        try:
            text = await page.locator("body").inner_text(timeout=5000)
            for reason, pattern in _CHALLENGES:
                if re.search(pattern, text, re.IGNORECASE):
                    return Verification(required=True, reason=reason)
            # Generic evidence only: these are safety detectors, not commerce selectors.
            challenge_frames = page.locator(
                'iframe[src*="captcha" i],iframe[title*="captcha" i],'
                '[id*="captcha" i],[data-sitekey]'
            )
            for locator in await challenge_frames.all():
                if await locator.is_visible():
                    return Verification(required=True, reason="captcha")
            return Verification()
        except Error:
            return Verification(required=True, reason="browser_unavailable")

    async def _guard_human(self) -> None:
        verification = await self._detect_verification()
        if verification.required:
            await self._capture("verification", "WAITING_HUMAN")
            raise HumanRequired("需要人工操作: " + verification.reason)

    async def detect_verification(self) -> Verification:
        async with self._lock:
            return await self._detect_verification()

    async def _capture(self, action: str, state: str = "") -> Path | None:
        page = self.manager.current_page(self.platform)
        if page is None:
            return None
        inventory = await capture_diagnostics(
            page, self.screenshots_dir, self.platform.value, action, state
        )
        return inventory.with_suffix(".png")

    async def capture(self, action: str, state: str = "") -> Path | None:
        async with self._lock:
            return await self._capture(action, state)

    async def close(self) -> None:
        async with self._lock:
            await self.manager.close(self.platform)
