import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error

from src.browser.manager import BrowserManager, navigate, retry_after_seconds
from src.browser.session import ProfileLock
from src.core.exceptions import ConfigurationError, HumanRequired, RetryableError, SelectorNotFound
from src.core.models import LoginStatus, Platform
from src.platforms.apple_cn.adapter import AppleCNAdapter
from src.platforms.jd.adapter import JDAdapter
from src.platforms.taobao.adapter import TaobaoAdapter
from src.platforms.tmall.adapter import TmallAdapter

pytestmark = pytest.mark.browser


async def test_missing_stable_chrome_releases_profile_without_fallback(tmp_path):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    launch = AsyncMock(side_effect=Error("test-only missing browser"))
    manager._playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock()
    )
    try:
        with pytest.raises(HumanRequired, match="正式版 Google Chrome 无法打开"):
            await manager.open(Platform.APPLE)
        assert launch.await_count == 1
        assert launch.await_args.kwargs["channel"] == "chrome"
        assert manager.current_page(Platform.APPLE) is None
        lock = ProfileLock(tmp_path / "profiles" / "apple")
        lock.acquire()
        lock.release()
    finally:
        await manager.close()


class LocalFixtureAdapter(AppleCNAdapter):
    """HTTP is allowed only by this local test subclass, never production config."""

    allowed_hosts = ("127.0.0.1",)
    require_https = False


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/blocked")
                self.end_headers()
                return
            if self.path.startswith("/busy"):
                self.send_response(429)
                delay = "600" if self.path.startswith("/busy-long") else "2"
                if self.path.startswith("/busy-date"):
                    delay = format_datetime(datetime.now(UTC) + timedelta(seconds=900), usegmt=True)
                self.send_header("Retry-After", delay)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><title>Local browser fixture</title><body>Ready</body>"
            )

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


@pytest.mark.asyncio
async def test_persistent_profile_survives_close_reopen_and_platforms_are_isolated(
    tmp_path, local_site
):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    try:
        page, same_page = await asyncio.gather(
            manager.open(Platform.APPLE), manager.open(Platform.APPLE)
        )
        assert page is same_page
        await navigate(page, local_site)
        await page.evaluate("localStorage.setItem('fixture-marker', 'persisted')")
        await page.context.add_cookies(
            [
                {
                    "name": "fixture-session",
                    "value": "test-only-cookie",
                    "url": local_site,
                    "expires": time.time() + 3600,
                }
            ]
        )
        jd_page = await manager.open(Platform.JD)
        await navigate(jd_page, local_site)
        assert await jd_page.evaluate("localStorage.getItem('fixture-marker')") is None
        assert not await jd_page.context.cookies()
        await manager.close(Platform.APPLE)
        reopened = await manager.open(Platform.APPLE)
        await navigate(reopened, local_site)
        assert await reopened.evaluate("localStorage.getItem('fixture-marker')") == "persisted"
        cookies = await reopened.context.cookies()
        assert any(cookie["name"] == "fixture-session" for cookie in cookies)
        adapter = AppleCNAdapter(manager, tmp_path / "screenshots")
        assert await adapter.login_status() == LoginStatus.UNKNOWN
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_profile_lock_prevents_duplicate_process_and_releases_after_close(tmp_path):
    first = BrowserManager(tmp_path / "profiles", headless=True)
    second = BrowserManager(tmp_path / "profiles", headless=True)
    try:
        await first.open(Platform.APPLE)
        with pytest.raises(HumanRequired, match="already in use"):
            await second.open(Platform.APPLE)
        await first.close()
        assert not (await second.open(Platform.APPLE)).is_closed()
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_redacted_inspection_inventory_html_and_screenshot(tmp_path, local_site):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    adapter = LocalFixtureAdapter(manager, tmp_path / "screenshots")
    try:
        page = await manager.open(Platform.APPLE)
        await navigate(page, local_site + "/?token=QUERY_SECRET")
        await page.set_content("""
            <!doctype html><html><body>
            <button role="button" data-testid="buy-product">购买</button>
            <a href="/buy?token=LINK_SECRET#SESSION_FRAGMENT">购物袋</a>
            <input name="login" value="INPUT_SECRET">
            <input type="password" value="PASSWORD_SECRET">
            <input type="hidden" value="HIDDEN_SECRET">
            <textarea>TEXTAREA_SECRET</textarea>
            <div hidden>HIDDEN_CONTENT_SECRET</div>
            <div style="display:none">CSS_HIDDEN_SECRET</div>
            <div id="payment-details">PAYMENT_SECRET</div>
            <script>window.privateToken = 'SCRIPT_SECRET';</script>
            <p>手机号: 13800138000 身份证号: 110101199001010011</p>
            <p>token=VISIBLE_SECRET account@example.com</p>
            </body></html>
        """)
        # Preserve fixture content while exercising inspect's navigation and output API.
        html = await page.content()
        await page.route(
            "**/fixture*",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            ),
        )
        inventory_path = await adapter.inspect(
            local_site + "/fixture?token=QUERY_SECRET", tmp_path / "inspect"
        )
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        assert inventory["buttons"][0]["text"] == "购买"
        assert inventory["links"][0]["text"] == "购物袋"
        assert inventory["roles"]
        assert inventory["data_attributes"]
        assert inventory["inputs"]
        assert inventory["metadata"]["redacted"] is True
        assert inventory["metadata"]["screenshot_mode"].startswith("layout_only")
        for text_path in inventory_path.parent.glob("*"):
            if text_path.suffix in {".json", ".html"}:
                contents = text_path.read_text(encoding="utf-8")
                for private in (
                    "QUERY_SECRET",
                    "LINK_SECRET",
                    "SESSION_FRAGMENT",
                    "INPUT_SECRET",
                    "PASSWORD_SECRET",
                    "HIDDEN_SECRET",
                    "TEXTAREA_SECRET",
                    "HIDDEN_CONTENT_SECRET",
                    "CSS_HIDDEN_SECRET",
                    "PAYMENT_SECRET",
                    "SCRIPT_SECRET",
                    "13800138000",
                    "110101199001010011",
                    "VISIBLE_SECRET",
                    "account@example.com",
                ):
                    assert private not in contents
        assert inventory_path.with_suffix(".png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert inventory_path.with_suffix(".html").exists()
        assert "QUERY_SECRET" in page.url  # Only the evidence copy was redacted.
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [AppleCNAdapter, JDAdapter, TmallAdapter, TaobaoAdapter])
async def test_unknown_commerce_selectors_stop_without_clicking(tmp_path, adapter_type):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    adapter = adapter_type(manager, tmp_path / "screenshots")
    try:
        assert await adapter.login_status() == LoginStatus.UNKNOWN
        with pytest.raises(SelectorNotFound, match="UNKNOWN"):
            await adapter.get_skus()
        with pytest.raises(SelectorNotFound, match="UNKNOWN"):
            await adapter.submit_order()
        assert manager.current_page(adapter.platform) is None
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["请完成短信验证", "请拖动滑块", "账号安全验证", "请确认收货地址", "支付验证", "captcha"],
)
async def test_challenge_pauses_and_keeps_browser_open(tmp_path, message):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    adapter = AppleCNAdapter(manager, tmp_path / "screenshots")
    try:
        page = await manager.open(Platform.APPLE)
        await page.set_content(f"<body><p>{message}</p><button>Continue</button></body>")
        assert (await adapter.detect_verification()).required
        with pytest.raises(HumanRequired, match="需要人工操作"):
            await adapter.get_skus()
        assert not page.is_closed()
        assert list((tmp_path / "screenshots").glob("*.png"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_inspect_rejects_wrong_domain_before_open_and_blocks_redirect(tmp_path, local_site):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    adapter = AppleCNAdapter(manager, tmp_path / "screenshots")
    try:
        with pytest.raises(ConfigurationError, match="does not belong"):
            await adapter.inspect(local_site, tmp_path / "inspect")
        assert manager.current_page(Platform.APPLE) is None
        adapter = LocalFixtureAdapter(manager, tmp_path / "screenshots")
        with pytest.raises(HumanRequired, match="approved platform"):
            await adapter.inspect(local_site + "/redirect", tmp_path / "inspect")
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,minimum,maximum", [("/busy", 2, 2), ("/busy-long", 600, 600), ("/busy-date", 895, 900)]
)
async def test_http_429_reports_bounded_retry_after(tmp_path, local_site, path, minimum, maximum):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    try:
        page = await manager.open(Platform.APPLE)
        with pytest.raises(RetryableError) as raised:
            await navigate(page, local_site + path)
        assert minimum <= raised.value.retry_after <= maximum
    finally:
        await manager.close()


def test_retry_after_dates_and_unrepresentable_delay():
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    future = format_datetime(now + timedelta(seconds=1200), usegmt=True)
    assert retry_after_seconds(future, now) == 1200
    assert retry_after_seconds("600", now) == 600
    assert retry_after_seconds(format_datetime(now - timedelta(seconds=30)), now) == 0
    assert retry_after_seconds("invalid", now) == 0
    with pytest.raises(HumanRequired, match="manual review"):
        retry_after_seconds("9" * 400, now)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://www.apple.com/cn/", "https://www.apple.com:444/cn/"])
async def test_production_adapter_rejects_insecure_scheme_or_port(tmp_path, url):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    adapter = AppleCNAdapter(manager, tmp_path / "screenshots")
    with pytest.raises(ConfigurationError, match="HTTPS on port 443"):
        await adapter.inspect(url, tmp_path / "inspect")
    assert manager.current_page(Platform.APPLE) is None


@pytest.mark.asyncio
async def test_inspect_challenge_records_waiting_human(tmp_path, local_site):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    adapter = LocalFixtureAdapter(manager, tmp_path / "screenshots")
    try:
        page = await manager.open(Platform.APPLE)
        await page.route(
            "**/challenge",
            lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body="<p>请拖动滑块</p>"
            ),
        )
        evidence = await adapter.inspect(local_site + "/challenge", tmp_path / "inspect")
        snapshot = json.loads(evidence.read_text(encoding="utf-8"))
        assert snapshot["metadata"]["state"] == "WAITING_HUMAN"
        assert not page.is_closed()
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["browser_close", "enter"])
async def test_manual_login_saves_on_close_or_confirmation(
    tmp_path, local_site, monkeypatch, finish
):
    ready = asyncio.Event()
    confirmation = threading.Event()

    class HeadlessLoginFixtureManager(BrowserManager):
        requested_headless = None

        async def _open(self, platform, headless):
            self.requested_headless = headless
            page = await super()._open(platform, headless=True)
            ready.set()
            return page

    def confirm_input():
        if not confirmation.wait(timeout=10):
            raise EOFError
        return ""

    monkeypatch.setattr("src.browser.manager.sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", confirm_input)
    manager = HeadlessLoginFixtureManager(tmp_path / "profiles", headless=True)
    task = asyncio.create_task(manager.manual_login(Platform.APPLE, local_site))
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        page = manager.current_page(Platform.APPLE)
        assert manager.requested_headless is False  # Production manual login requests a visible UI.
        await page.wait_for_url(local_site + "/")
        await page.wait_for_load_state("domcontentloaded")
        await page.evaluate("localStorage.setItem('manual-fixture', 'saved')")
        if finish == "browser_close":
            try:
                await page.context.close()  # Models the user closing the browser window.
            except Error as error:
                # The manager may stop Playwright after observing the close event,
                # before this test-only close RPC receives its final response.
                assert "closed" in str(error).lower() and page.is_closed()
        else:
            confirmation.set()  # Models the user pressing Enter after local manual changes.
        assert await asyncio.wait_for(task, timeout=10) == LoginStatus.UNKNOWN
        assert manager.current_page(Platform.APPLE) is None
        reopened = await manager.open(Platform.APPLE)
        await navigate(reopened, local_site)
        assert await reopened.evaluate("localStorage.getItem('manual-fixture')") == "saved"
    finally:
        confirmation.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.close()
