import asyncio
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Error, Page, Playwright, async_playwright

from src.browser.groups import profile_platform
from src.browser.session import (
    APPLE_STATE_FILE_ENV,
    APPLE_STATE_MARKER,
    ProfileLock,
    apple_state_host,
    load_local_apple_state,
)
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
    network_events: list[dict] = field(default_factory=list)
    initialization_failed: bool = False


class BrowserManager:
    """One persistent stable Chrome context per session group, guarded across processes."""

    def __init__(self, profiles_dir: Path, headless: bool = False):
        self.profiles_dir = Path(profiles_dir).resolve()
        self.headless = headless
        self._playwright: Playwright | None = None
        self._sessions: dict[Platform, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._import_status: dict[Platform, str] = {}

    def login_state_import_status(self, platform: Platform) -> str:
        """Import is not proof that the website accepted this login state."""
        return self._import_status.get(profile_platform(platform), "NOT_CONFIGURED")

    def network_diagnostics(self, platform: Platform) -> list[dict]:
        session = self._sessions.get(profile_platform(platform))
        return [item.copy() for item in session.network_events] if session else []

    def _record_network_failures(self, platform: Platform, session: BrowserSession) -> None:
        def record(request, status=None, failure=""):
            if request.resource_type != "document":
                return
            try:
                host = urlsplit(request.url).hostname or ""
                if host.startswith("secure") and apple_state_host(host):
                    host = "secure*.www.apple.com.cn"
                elif not apple_state_host(host) and host not in {
                    "item.jd.com",
                    "cart.jd.com",
                    "passport.jd.com",
                    "detail.tmall.com",
                    "login.tmall.com",
                    "login.taobao.com",
                    "item.taobao.com",
                }:
                    host = "other"
                scope = "iframe" if request.frame.parent_frame else "main_document"
            except (Error, ValueError):
                host, scope = "other", "document"
            category = "HTTP_ERROR"
            if status is None:
                category = next(
                    (
                        label
                        for token, label in (
                            ("TIMED_OUT", "TIMEOUT"),
                            ("NAME_NOT_RESOLVED", "DNS"),
                            ("CERT_", "TLS"),
                            ("SSL_", "TLS"),
                            ("CONNECTION_", "CONNECTION"),
                            ("BLOCKED", "BLOCKED"),
                            ("ABORTED", "ABORTED"),
                        )
                        if token in failure
                    ),
                    "NETWORK_ERROR",
                )
            entry = {"host": host, "scope": scope, "status": status, "category": category}
            session.network_events.append(entry)
            del session.network_events[:-20]
            logging.getLogger(platform.value).warning(
                "browser_document host=%s scope=%s status=%s category=%s",
                host,
                scope,
                status,
                category,
            )

        session.context.on(
            "response",
            lambda response: (
                record(response.request, response.status) if response.status >= 400 else None
            ),
        )
        session.context.on(
            "requestfailed", lambda request: record(request, failure=request.failure or "")
        )

    async def _merge_local_state(self, context: BrowserContext, state: dict) -> None:
        """Add missing state only; synthetic import pages never contact Apple."""
        existing = {
            (item["name"], item["domain"], item["path"]) for item in await context.cookies()
        }
        cookies = [
            item
            for item in state["cookies"]
            if (item["name"], item["domain"], item["path"]) not in existing
        ]
        if cookies:
            await context.add_cookies(cookies)
        if not state["origins"]:
            return
        temporary = await context.new_page()
        try:
            await temporary.route(
                "**/*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="<!doctype html><title>Local login state import</title>",
                ),
            )
            for origin in state["origins"]:
                await temporary.goto(origin["origin"] + "/", wait_until="domcontentloaded")
                await temporary.evaluate(
                    """entries => {
                    for (const {name,value} of entries) {
                        if (localStorage.getItem(name) === null) localStorage.setItem(name,value);
                    }
                }""",
                    origin["localStorage"],
                )
        finally:
            await temporary.close()

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
                if session.initialization_failed:
                    raise HumanRequired("上次登录状态导入未完成，请先关闭该程序浏览器再重试")
                if session.page.is_closed():
                    session.page = await session.context.new_page()
                return session.page
            if session:
                session.lock.release()
                self._sessions.pop(platform)
            profile_lock = ProfileLock(self.profiles_dir / platform.value)
            profile_lock.acquire()
            context = None
            try:
                local_state = None
                marker = self.profiles_dir / platform.value / APPLE_STATE_MARKER
                if platform == Platform.APPLE:
                    if marker.exists():
                        self._import_status[platform] = "PREVIOUS_IMPORT_PROFILE_REUSED"
                    elif source := os.environ.get(APPLE_STATE_FILE_ENV):
                        local_state = load_local_apple_state(Path(source))
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profiles_dir / platform.value),
                    channel=BROWSER_CHANNEL,
                    headless=headless,
                    chromium_sandbox=True,
                    accept_downloads=False,
                    viewport={"width": 1280, "height": 900},
                    **(
                        {"offline": True, "service_workers": "block"}
                        if local_state is not None
                        else {}
                    ),
                )
                context.set_default_timeout(10_000)
                context.set_default_navigation_timeout(30_000)
                closed = asyncio.Event()
                context.on("close", lambda _: closed.set())
                page = context.pages[0] if context.pages else await context.new_page()
                self._sessions[platform] = BrowserSession(
                    context, page, profile_lock, closed, headless
                )
                self._record_network_failures(platform, self._sessions[platform])
                if local_state is not None:
                    try:
                        await self._merge_local_state(context, local_state)
                        marker.write_text("version=1\n", encoding="ascii")
                        await context.set_offline(False)
                    except (Error, OSError, ValueError):
                        raise ConfigurationError(
                            "本机 Apple 登录状态导入未完成；现有 profile 已保留，"
                            "请检查文件或改为手动登录"
                        ) from None
                    self._import_status[platform] = "IMPORTED_LOGIN_UNVERIFIED"
                return page
            except BaseException as error:
                session = self._sessions.get(platform)
                if session and context is not None:
                    session.initialization_failed = True
                    try:
                        await context.close()
                        session.closed.set()
                    except BaseException:
                        # Preserve the managed context and lock when closure is not proven.
                        pass
                    if session.closed.is_set():
                        profile_lock.release()
                        self._sessions.pop(platform, None)
                else:
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
