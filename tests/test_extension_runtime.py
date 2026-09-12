"""Real extension/Runtime integration on local HTML, never real shopping or credentials."""

import asyncio
import contextlib
import json
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from playwright.async_api import Error, async_playwright
from test_audit_protected_run import (
    ACCOUNT_NAV,
    ATTACH,
    BAG,
    CHECKOUT,
    PRODUCT,
    PRODUCT_URL,
    configuration,
)

from src.core.models import CartState, Platform
from src.runtime import Runtime
from src.web.app import create_app


@pytest.mark.browser
async def test_extension_web_runtime_local_purchase_keeps_normal_tabs(tmp_path, monkeypatch):
    for key in (
        "APPLE_BUY_BOT_USERNAME", "APPLE_BUY_BOT_PASSWORD", "APPLE_BUY_BOT_STORAGE_STATE_FILE"
    ):
        monkeypatch.delenv(key, raising=False)
    config = configuration(tmp_path / "runtime")
    config = config.model_copy(
        update={"app": config.app.model_copy(update={"browser": "extension"})}
    )
    runtime = Runtime(config)
    app = create_app(runtime=runtime)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    service = asyncio.create_task(server.serve(sockets=[sock]))
    browser = None
    actions = []
    logged_in = False
    checkout_steps = f"""
      <input type="radio" data-autom="saved-address" checked>
      <button data-autom="shipping-continue-button" onclick="this.hidden=true;
        document.querySelector('#fixture-payment').hidden=false">继续选择付款方式</button>
      <div id="fixture-payment" hidden>
        <input type="radio" data-autom="checkout-billingOptions-WECHAT">
        <button data-autom="continue-button-review" onclick="fixtureReview()">检查订单</button>
      </div><script>function fixtureReview() {{
        document.body.innerHTML = {json.dumps(ACCOUNT_NAV + CHECKOUT)};
      }}</script>"""

    async def local_route(route):
        nonlocal logged_in
        parsed = urlsplit(route.request.url)
        if parsed.scheme == "chrome-extension":
            await route.continue_()  # Local extension modules, no website request.
            return
        if parsed.hostname != "www.apple.com.cn":
            await route.abort()
            return
        if parsed.path == "/shop/fixture-login-ok":
            logged_in = True
        pages = {
            urlsplit(PRODUCT_URL).path: PRODUCT,
            "/shop/fixture-login-ok": PRODUCT,
            "/shop/attach": ATTACH,
            "/shop/bag": BAG,
            "/shop/checkout": checkout_steps,
        }
        if parsed.path not in pages:
            await route.abort()
            return
        nav = ACCOUNT_NAV if logged_in else (
            '<nav id="globalnav"><a href="/shop/signIn/account">登录</a></nav>'
            '<a id="complete-local-login" href="/shop/fixture-login-ok">本地模拟登录</a>'
        )
        await route.fulfill(content_type="text/html; charset=utf-8", body=nav + pages[parsed.path])

    async def until(predicate):
        async with asyncio.timeout(25):
            while not predicate():  # noqa: ASYNC110
                await asyncio.sleep(0.02)

    try:
        await until(lambda: server.started)
        async with async_playwright() as pw:
            extension = tmp_path / "extension"
            shutil.copytree(Path(__file__).parents[1] / "extension", extension)
            manifest_path = extension / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Opening a popup as a test page lacks Chrome's real toolbar activeTab grant.
            # The temporary test copy can read only this routed fixture origin.
            manifest["host_permissions"].append("https://www.apple.com.cn/*")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            browser = await pw.chromium.launch_persistent_context(
                str(tmp_path / "isolated-browser"),
                channel="chromium",
                headless=True,
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            )
            worker = browser.service_workers[0] if browser.service_workers else (
                await browser.wait_for_event("serviceworker", timeout=10000)
            )
            await browser.route(re.compile(r"^https?://"), local_route)
            await browser.expose_function(
                "recordFixtureAction", lambda action: actions.append(action)
            )
            product = browser.pages[0]
            await product.goto(PRODUCT_URL)
            other = await browser.new_page()
            await other.set_content("<h1>Unrelated local tab</h1>")
            async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=30) as client:
                headers = {"X-Apple-Bot-Control": "local"}

                async def control(path, body):
                    response = await client.post(path, json=body, headers=headers)
                    assert response.status_code == 200, response.text
                    return response.json()

                pair = await control("/extension/pair", {})
                # Filled by the normal popup message interface; no Adapter state injection.
                popup = await browser.new_page()
                popup_errors = []
                popup.on("pageerror", lambda error: popup_errors.append(str(error)))
                popup.on("console", lambda event: (
                    popup_errors.append(event.text) if event.type == "error" else None
                ))
                await popup.goto(worker.url.rsplit("/", 1)[0] + "/popup.html")
                await popup.locator("#service").fill(base)
                await popup.locator("#token").fill(pair["token"])
                await product.bring_to_front()
                await popup.locator("#connect").click()
                try:
                    await until(lambda: runtime.manager.broker.status()["connected"])
                except TimeoutError:
                    pytest.fail(
                        str(popup_errors) + await popup.locator("#status").inner_text()
                    )
                status = await control("/check-login", {"platform": "apple"})
                assert status["login"] == "REQUIRED"
                assert not runtime.plan_snapshot()["approved"] and actions == []
                denied = await client.post("/start", json={"platforms": ["apple"]}, headers=headers)
                assert denied.status_code == 409
                plan = (await client.get("/purchase-plan")).json()
                await control("/approve-plan", {"digest": plan["digest"], "confirmed": True})
                await control("/start", {
                    "platforms": ["apple"], "immediate": True, "dry_run": True,
                })
                await until(
                    lambda: runtime.snapshot()["platforms"]["apple"]["state"] == "WAITING_HUMAN"
                )
                assert actions == []
                await product.locator("#complete-local-login").click()
                login = await control("/check-login", {"platform": "apple"})
                assert login["login"] == "AUTHENTICATED"
                await control("/resume", {"platform": "apple"})
                await asyncio.wait_for(runtime.task, 30)
                snapshot = runtime.snapshot()
                assert snapshot["platforms"]["apple"]["state"] == "READY_TO_SUBMIT", snapshot
                assert actions == ["add", "view_bag", "checkout"]
                assert runtime.adapters[Platform.APPLE].cart_state == CartState.CART_VERIFIED
                guard = runtime.database.guard_status()
                assert guard and guard["status"] == "CLAIMED"
                await control("/finish-task", {})
                assert runtime.database.guard_status() == guard
                assert not product.is_closed() and not other.is_closed()
                assert await other.locator("h1").inner_text() == "Unrelated local tab"
                assert runtime.manager.current_page(Platform.APPLE) is None
    finally:
        await runtime.stop()
        await runtime.manager.close()
        if browser is not None:
            with contextlib.suppress(Error):  # The test Playwright context may already have exited.
                await browser.close()  # Only this test's disposable browser.
        server.should_exit = True
        await asyncio.wait_for(service, 10)
        sock.close()
