"""Local synthetic state only; all browser website requests are intercepted/offline."""

import asyncio
import json
import time
import traceback
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import BrowserType, Error

from src.browser.manager import BrowserManager
from src.browser.session import (
    APPLE_STATE_FILE_ENV,
    APPLE_STATE_MARKER,
    ProfileLock,
    load_local_apple_state,
)
from src.core.exceptions import ConfigurationError, HumanRequired
from src.core.models import Platform


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # Never consume a user's configured credentials in these tests.
    monkeypatch.delenv(APPLE_STATE_FILE_ENV, raising=False)


def cookie(name="fixture-login", value="fixture-value", domain=".apple.com.cn"):
    return {
        "name": name,
        "value": value,
        "domain": domain,
        "path": "/",
        "expires": time.time() + 3600,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }


async def local_origin(page):
    await page.context.set_offline(True)
    await page.route(
        "**/*",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><body>Fixture only</body>",
        ),
    )
    await page.goto("https://secure8.www.apple.com.cn/")


@pytest.mark.browser
async def test_import_context_stays_offline_until_merge_completes(tmp_path, monkeypatch):
    """Exercise production isolation, with a loopback listener proving the boundary."""
    received, launches, phases = [], [], []

    async def receive(reader, writer):
        received.append(await reader.readline())
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    probe_url = f"http://127.0.0.1:{port}/probe"
    source = tmp_path / "fixture-state.json"
    source.write_text(
        json.dumps(
            {
                "cookies": [cookie()],
                "origins": [
                    {
                        "origin": "https://secure8.www.apple.com.cn",
                        "localStorage": [{"name": "fixture-key", "value": "fixture-value"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(APPLE_STATE_FILE_ENV, str(source))
    launch = BrowserType.launch_persistent_context

    async def launch_checked(browser_type, **options):
        launches.append(options.copy())
        context = await launch(browser_type, **options)

        async def loopback_only(route):
            # Never permit real website traffic, even if isolation regresses.
            if route.request.url.startswith(f"http://127.0.0.1:{port}/"):
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", loopback_only)
        return context

    monkeypatch.setattr(BrowserType, "launch_persistent_context", launch_checked)
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    merge = manager._merge_local_state

    async def inspect_real_isolation(context, state):
        assert launches[-1]["offline"] is True
        assert launches[-1]["service_workers"] == "block"
        page = context.pages[0]
        assert await page.evaluate("navigator.onLine") is False
        with pytest.raises(Error, match="ERR_INTERNET_DISCONNECTED"):
            await page.goto(probe_url)
        assert received == []
        phases.append("isolated")
        await merge(context, state)
        # Chrome's internal error document may report navigator.onLine=True;
        # an actual blocked loopback request proves the context stays offline.
        with pytest.raises(Error, match="ERR_INTERNET_DISCONNECTED"):
            await page.goto(probe_url)
        assert received == []
        assert any(item["name"] == "fixture-login" for item in await context.cookies())
        phases.append("merged_offline")

    monkeypatch.setattr(manager, "_merge_local_state", inspect_real_isolation)
    try:
        page = await manager.open(Platform.APPLE)
        assert phases == ["isolated", "merged_offline"] and received == []
        assert await page.evaluate("navigator.onLine") is True
        assert (manager.profiles_dir / "apple" / APPLE_STATE_MARKER).exists()
        assert (await page.goto(probe_url)).status == 200
        assert received  # Same listener is reachable only after the real import completes.
        await local_origin(page)
        assert await page.evaluate("localStorage.getItem('fixture-key')") == "fixture-value"
        await manager.close(Platform.APPLE)
        reopened = await manager.open(Platform.APPLE)
        assert "offline" not in launches[-1] and "service_workers" not in launches[-1]
        assert await reopened.evaluate("navigator.onLine") is True
        assert manager.login_state_import_status(Platform.APPLE) == "PREVIOUS_IMPORT_PROFILE_REUSED"
    finally:
        await manager.close()
        server.close()
        await server.wait_closed()


@pytest.mark.browser
async def test_import_merges_once_and_preserves_newer_profile_state(tmp_path, monkeypatch, caplog):
    profiles = tmp_path / "profiles"
    before = BrowserManager(profiles, headless=True)
    try:
        page = await before.open(Platform.APPLE)
        await local_origin(page)
        await page.context.add_cookies([cookie(value="existing-newer")])
        await page.evaluate("localStorage.setItem('fixture-existing', 'newer')")
    finally:
        await before.close()
    state = {
        "cookies": [cookie(value="older-file"), cookie("fixture-added", "imported")],
        "origins": [
            {
                "origin": "https://secure8.www.apple.com.cn",
                "localStorage": [
                    {"name": "fixture-existing", "value": "older"},
                    {"name": "fixture-added", "value": "imported"},
                ],
            }
        ],
    }
    source = tmp_path / "private-source-name.json"
    source.write_text(json.dumps(state), encoding="utf-8")
    original_file = source.read_bytes()
    monkeypatch.setenv(APPLE_STATE_FILE_ENV, str(source))
    manager = BrowserManager(profiles, headless=True)
    try:
        page = await manager.open(Platform.APPLE)
        assert manager.login_state_import_status(Platform.APPLE) == "IMPORTED_LOGIN_UNVERIFIED"
        assert source.read_bytes() == original_file
        assert (profiles / "apple" / APPLE_STATE_MARKER).read_text() == "version=1\n"
        await local_origin(page)
        assert await page.evaluate("localStorage.getItem('fixture-existing')") == "newer"
        assert await page.evaluate("localStorage.getItem('fixture-added')") == "imported"
        values = {item["name"]: item["value"] for item in await page.context.cookies()}
        assert values["fixture-login"] == "existing-newer"
        assert values["fixture-added"] == "imported"
        await page.evaluate("localStorage.setItem('fixture-added', 'website-refreshed')")
        await page.context.add_cookies([cookie("fixture-added", "website-refreshed")])
        assert await manager.open(Platform.APPLE) is page
        await manager.close(Platform.APPLE)
        # A stale or missing source never overwrites the profile after the first import.
        source.write_text("invalid-json-after-import", encoding="utf-8")
        reopened = await manager.open(Platform.APPLE)
        assert manager.login_state_import_status(Platform.APPLE) == "PREVIOUS_IMPORT_PROFILE_REUSED"
        await local_origin(reopened)
        assert (
            await reopened.evaluate("localStorage.getItem('fixture-added')") == "website-refreshed"
        )
        cookies = {item["name"]: item["value"] for item in await reopened.context.cookies()}
        assert cookies["fixture-added"] == "website-refreshed"
        assert "private-source-name" not in caplog.text
        assert "existing-newer" not in caplog.text and "website-refreshed" not in caplog.text
    finally:
        await manager.close()


async def test_invalid_files_and_foreign_domains_never_launch_or_replace_profile(
    tmp_path, monkeypatch
):
    source = tmp_path / "private-source-name.json"
    profile = tmp_path / "profiles/apple"
    profile.mkdir(parents=True)
    sentinel = profile / "existing-profile-marker"
    sentinel.write_text("keep-existing", encoding="utf-8")
    manager = BrowserManager(profile.parent, headless=True)
    monkeypatch.setenv(APPLE_STATE_FILE_ENV, str(source))
    cases = [
        None,
        "bad-json-with-private-value",
        {"cookies": [cookie(domain=".evil.test")]},
        {"origins": [{"origin": "https://idmsa.apple.com.evil.test", "localStorage": []}]},
        {"origins": [{"origin": "https://idmsa.apple.com", "indexedDB": []}]},
        {"cookies": [cookie() | {"sameSite": ["private-value"]}]},
        {"cookies": [{"name": "fixture", "value": "private-value", "url": 123}]},
    ]
    try:
        for data in cases:
            if data is not None:
                source.write_text(
                    data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
                )
            with pytest.raises(ConfigurationError) as raised:
                await manager.open(Platform.APPLE)
            message = "".join(traceback.format_exception(raised.value))
            assert (
                "private-source-name" not in message
                and "bad-json-with-private-value" not in message
            )
            assert manager._playwright is None and manager.current_page(Platform.APPLE) is None
            assert sentinel.read_text() == "keep-existing"
            lock = ProfileLock(profile)
            lock.acquire()
            lock.release()
        source.write_text(json.dumps([cookie()]), encoding="utf-8")
        assert len(load_local_apple_state(source)["cookies"]) == 1
    finally:
        await manager.close()


@pytest.mark.browser
async def test_import_error_closes_managed_context_without_losing_profile(tmp_path, monkeypatch):
    source = tmp_path / "fixture-state.json"
    source.write_text(json.dumps({"cookies": [cookie()]}), encoding="utf-8")
    monkeypatch.setenv(APPLE_STATE_FILE_ENV, str(source))
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    contexts = []

    async def fail_import(context, state):
        contexts.append(context)
        raise Error("private-cookie-value-and-source-path")

    monkeypatch.setattr(manager, "_merge_local_state", fail_import)
    try:
        with pytest.raises(ConfigurationError) as raised:
            await manager.open(Platform.APPLE)
        assert "private-cookie" not in "".join(traceback.format_exception(raised.value))
        assert contexts and not contexts[0].pages
        assert manager.current_page(Platform.APPLE) is None
        assert not (manager.profiles_dir / "apple" / APPLE_STATE_MARKER).exists()
        lock = ProfileLock(manager.profiles_dir / "apple")
        lock.acquire()
        lock.release()
        original_closes = []

        async def fail_with_unproven_close(context, state):
            original_closes.append(context.close)
            monkeypatch.setattr(
                context, "close", AsyncMock(side_effect=Error("fixture close failed"))
            )
            raise Error("private-import-error")

        monkeypatch.setattr(manager, "_merge_local_state", fail_with_unproven_close)
        with pytest.raises(ConfigurationError):
            await manager.open(Platform.APPLE)
        assert Platform.APPLE in manager._sessions
        assert not manager._sessions[Platform.APPLE].closed.is_set()
        with pytest.raises(HumanRequired, match="上次登录状态导入未完成"):
            await manager.open(Platform.APPLE)
        with pytest.raises(HumanRequired, match="already in use"):
            ProfileLock(manager.profiles_dir / "apple").acquire()
        monkeypatch.setattr(manager._sessions[Platform.APPLE].context, "close", original_closes[0])
        await manager.close()
        lock.acquire()
        lock.release()
    finally:
        await manager.close()


@pytest.mark.browser
async def test_document_failures_keep_only_host_status_and_classification(tmp_path, caplog):
    manager = BrowserManager(tmp_path / "profiles", headless=True)
    try:
        page = await manager.open(Platform.APPLE)
        await page.context.set_offline(True)

        async def route(request):
            if "/timeout" in request.request.url:
                await request.abort("timedout")
            elif "idmsa.apple.com" in request.request.url:
                await request.fulfill(status=403, body="private-response-body")
            else:
                await request.fulfill(
                    status=503,
                    content_type="text/html",
                    body='<iframe src="https://idmsa.apple.com/private-path?token=private-query"></iframe>',
                )

        await page.context.route("**/*", route)
        await page.goto("https://secure8.www.apple.com.cn/private-path?token=private-query")
        with pytest.raises(Error):
            await page.goto("https://secure8.www.apple.com.cn/timeout?token=private-query")
        events = manager.network_diagnostics(Platform.APPLE)
        assert any(item["status"] == 503 and item["scope"] == "main_document" for item in events)
        assert any(item["status"] == 403 and item["scope"] == "iframe" for item in events)
        assert any(item["category"] == "TIMEOUT" for item in events)
        assert all(set(item) == {"host", "scope", "status", "category"} for item in events)
        visible = json.dumps(events) + caplog.text
        assert "private-path" not in visible and "private-query" not in visible
        assert "private-response-body" not in visible
    finally:
        await manager.close()
