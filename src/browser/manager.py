import asyncio
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright

from src.browser.groups import profile_platform
from src.browser.session import ProfileLock
from src.core.exceptions import ConfigurationError, HumanRequired, RetryableError
from src.core.models import LoginStatus, Platform

BROWSER_CHANNEL = "chrome"


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname
        valid = valid and not parsed.username and not parsed.password
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("A valid HTTP(S) URL without credentials is required")


@dataclass
class BrowserSession:
    context: BrowserContext
    page: Page
    lock: ProfileLock
    closed: asyncio.Event
    headless: bool = False


class BrowserManager:
    """One persistent stable Chrome context per session group, guarded across processes."""

    def __init__(self, profiles_dir: Path, headless: bool = False):
        self.profiles_dir = Path(profiles_dir).resolve()
        self.headless = headless
        self._playwright: Playwright | None = None
        self._sessions: dict[Platform, BrowserSession] = {}
        self._lock = asyncio.Lock()

    def current_page(self, platform: Platform) -> Page | None:
        session = self._sessions.get(profile_platform(platform))
        if session and not session.closed.is_set() and not session.page.is_closed():
            return session.page
        return None

    async def open(self, platform: Platform) -> Page:
        return await self._open(profile_platform(platform), self.headless)

    async def open_visible(self, platform: Platform) -> Page:
        """An explicit manual-login window uses the same persistent profile."""
        platform = profile_platform(platform)
        session = self._sessions.get(platform)
        if session and session.headless:
            await self.close(platform)
        return await self._open(Platform(platform), headless=False)

    async def _open(self, platform: Platform, headless: bool) -> Page:
        platform = profile_platform(platform)
        async with self._lock:
            session = self._sessions.get(platform)
            if session and not session.closed.is_set():
                if session.page.is_closed():
                    session.page = await session.context.new_page()
                return session.page
            if session:
                session.lock.release()
                self._sessions.pop(platform)
            profile_lock = ProfileLock(self.profiles_dir / platform.value)
            profile_lock.acquire()
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profiles_dir / platform.value),
                    channel=BROWSER_CHANNEL,
                    headless=headless,
                    accept_downloads=False,
                    viewport={"width": 1280, "height": 900},
                )
                context.set_default_timeout(10_000)
                context.set_default_navigation_timeout(30_000)
                closed = asyncio.Event()
                context.on("close", lambda _: closed.set())
                page = context.pages[0] if context.pages else await context.new_page()
                self._sessions[platform] = BrowserSession(
                    context, page, profile_lock, closed, headless
                )
                return page
            except BaseException as error:
                profile_lock.release()
                if isinstance(error, Error):
                    raise HumanRequired(
                        "正式版 Google Chrome 无法打开；请检查安装、浏览器策略或程序 profile 占用"
                    ) from None
                raise

    async def close(self, platform: Platform | None = None) -> None:
        async with self._lock:
            targets = list(self._sessions) if platform is None else [profile_platform(platform)]
            for target in targets:
                session = self._sessions.get(target)
                if session:
                    try:
                        if not session.closed.is_set():
                            await session.context.close()
                            session.closed.set()
                    finally:
                        # Cancellation or an error is not proof Chrome closed.
                        # Retain the context and its OS lock so a later close can retry.
                        if session.closed.is_set():
                            session.lock.release()
                            self._sessions.pop(target, None)
            if not self._sessions and self._playwright:
                await self._playwright.stop()
                self._playwright = None

    async def manual_login(self, platform: Platform, url: str) -> LoginStatus:
        """Persist manual browser work; profile existence is never authentication proof."""
        validate_url(url)
        platform = profile_platform(platform)
        if platform in self._sessions:
            raise HumanRequired("Close the existing platform session before manual login")
        page = await self._open(platform, headless=False)
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Error:
                # Leave a visible page available for the user to recover navigation.
                print("页面未完成加载，可在浏览器中手动继续。")
            session = self._sessions[platform]
            print("请在浏览器手动登录。完成后按 Enter 或关闭浏览器以保存；登录状态仍待验证。")
            confirmed = asyncio.Event()
            loop = asyncio.get_running_loop()

            def read_confirmation() -> None:
                try:
                    input()
                except (EOFError, OSError):
                    return
                if not loop.is_closed():
                    loop.call_soon_threadsafe(confirmed.set)

            # A daemon avoids hanging process exit when the user closes the browser.
            if sys.stdin and sys.stdin.isatty():
                threading.Thread(target=read_confirmation, daemon=True).start()
            tasks = [
                asyncio.create_task(session.closed.wait()),
                asyncio.create_task(confirmed.wait()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return LoginStatus.UNKNOWN
        finally:
            await self.close(platform)


async def navigate(page: Page, url: str) -> None:
    validate_url(url)
    response = None

    def remember_document(candidate):
        nonlocal response
        request = candidate.request
        if request.is_navigation_request() and request.frame == page.main_frame:
            response = candidate

    # Stable Chrome can emit a real HTTP response and then fail navigation
    # while displaying its error page. Preserve Retry-After from that response.
    page.on("response", remember_document)
    failed = False
    try:
        response = await page.goto(url, wait_until="domcontentloaded")
    except Error:
        failed = True
    finally:
        page.remove_listener("response", remember_document)
    if response and (response.status == 429 or response.status >= 500):
        delay = retry_after_seconds(response.headers.get("retry-after", ""))
        raise RetryableError("Platform temporarily unavailable", retry_after=delay)
    if response and response.status in {401, 403}:
        raise HumanRequired("Platform access requires manual verification")
    if failed:
        raise RetryableError("Browser navigation failed or timed out") from None


def retry_after_seconds(value: str, now: datetime | None = None) -> float:
    """Honor the server's full Retry-After minimum, including RFC HTTP dates."""
    value = value.strip()
    if not value:
        return 0.0
    if value.isascii() and value.isdigit():
        try:
            delay = float(value)
        except (OverflowError, ValueError):
            raise HumanRequired("Server retry delay requires manual review") from None
        if not isfinite(delay):
            raise HumanRequired("Server retry delay requires manual review")
        return delay
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - (now or datetime.now(UTC))).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0
