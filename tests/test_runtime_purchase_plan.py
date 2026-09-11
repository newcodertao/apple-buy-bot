"""Normal Runtime entry, real Apple adapter, local pages and explicit human controls."""

import asyncio
import json
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_audit_protected_run import (
    ACCOUNT_NAV,
    ATTACH,
    BAG,
    CHECKOUT,
    PRODUCT,
    PRODUCT_URL,
    configuration,
)

from src.core.config import ProductTarget
from src.core.exceptions import ConfigurationError
from src.core.models import CartState, Platform
from src.order.plan import draft_plan
from src.runtime import Runtime
from src.web.app import create_app


async def paused(runtime, after=""):
    async def wait():
        while True:
            data = runtime.snapshot()["platforms"]["apple"]
            if data["state"] == "WAITING_HUMAN" and after in data["result"]:
                return data
            if runtime.task.done():
                await runtime.task
                raise AssertionError(f"Run ended before expected human pause: {data}")
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait(), timeout=15)


@pytest.mark.browser
async def test_runtime_first_login_plan_product_address_and_resume(tmp_path, monkeypatch):
    runtime = Runtime(configuration(tmp_path))
    original_open = runtime.manager._open
    attached, calls, actions = set(), [], []
    logged_in = False
    login_url = "/shop/signIn/account"
    logged_out_nav = '<nav><a href="/shop/signIn/account">登录</a></nav>'

    async def local_open(platform, headless):
        page = await original_open(platform, headless)
        if id(page.context) in attached:
            return page
        attached.add(id(page.context))
        await page.context.set_offline(True)
        await page.context.expose_function(
            "recordFixtureAction", lambda action: actions.append(action)
        )

        async def route(request_route):
            nonlocal logged_in
            parsed = urlsplit(request_route.request.url)
            if parsed.hostname != "www.apple.com.cn":
                await request_route.abort()
                return
            if parsed.path == login_url:
                body = (
                    '<a id="complete-local-login" href="/shop/fixture-login-ok">'
                    '完成本地模拟登录</a>'
                )
            else:
                if parsed.path == "/shop/fixture-login-ok":
                    logged_in = True
                pages = {
                    urlsplit(PRODUCT_URL).path: PRODUCT,
                    "/shop/fixture-login-ok": PRODUCT,
                    "/shop/attach": ATTACH,
                    "/shop/bag": BAG,
                    "/shop/checkout": CHECKOUT,
                }
                body = (ACCOUNT_NAV if logged_in else logged_out_nav) + pages[parsed.path]
            await request_route.fulfill(
                content_type="text/html; charset=utf-8", body="<!doctype html>" + body
            )

        await page.context.route("**/*", route)
        return page

    monkeypatch.setattr(runtime.manager, "_open", local_open)  # Transport only, real adapter.
    adapter = runtime.adapters[Platform.APPLE]
    for name in (
        "login_status",
        "open_product",
        "check_stock",
        "select_sku",
        "add_to_cart",
        "verify_cart",
        "goto_checkout",
        "verify_order",
        "submit_order",
    ):
        actual = getattr(adapter, name)

        async def record(*args, _name=name, _actual=actual):
            calls.append(_name)
            return await _actual(*args)

        monkeypatch.setattr(adapter, name, record)

    try:
        assert runtime.plan_snapshot()["approved"] is False
        # No Adapter call or confirmation was made before the normal Runtime start.
        assert calls == [] and runtime.manager.current_page(Platform.APPLE) is None
        await runtime.start([Platform.APPLE], immediate=True, dry_run=True)
        await paused(runtime, "Login")
        page = runtime.manager.current_page(Platform.APPLE)
        assert actions == [] and "add_to_cart" not in calls
        await page.locator('a[href="/shop/signIn/account"]').click()
        await page.locator("#complete-local-login").click()  # Synthetic human, not credentials.
        assert (await runtime.check_login())["login"] == "AUTHENTICATED"
        await runtime.resume(Platform.APPLE)

        await paused(runtime, "购买计划")
        await runtime.approve_plan(runtime.plan_snapshot()["digest"])
        await runtime.resume(Platform.APPLE)
        await paused(runtime, "国行")
        assert adapter.cart_state == CartState.NOT_ATTEMPTED and actions == []
        await runtime.confirm_checkout(Platform.APPLE, "market")
        await runtime.resume(Platform.APPLE)

        await paused(runtime, "address")
        assert actions == ["add", "view_bag", "checkout"]
        assert adapter.cart_state == CartState.CART_VERIFIED
        await runtime.confirm_checkout(Platform.APPLE, "address")
        await runtime.resume(Platform.APPLE)
        await asyncio.wait_for(runtime.task, timeout=10)
        snapshot = runtime.snapshot()
        assert snapshot["platforms"]["apple"]["state"] == "READY_TO_SUBMIT"
        assert snapshot["dry_run"] is True
        assert actions == ["add", "view_bag", "checkout"]
        assert "submit_order" not in calls and not page.is_closed()
        assert snapshot["order_guard"]["status"] == "CLAIMED"
        plan_text = runtime._plan_path.read_text(encoding="utf-8")
        assert "测试地址" not in plan_text
        assert json.loads(plan_text)["address_fingerprint"]
        approved = runtime.purchase_plan
        rebuilt = runtime._build_adapters()[Platform.APPLE]
        # Runtime.start preserves the live instance; even stop cannot reset its cart attempt.
        await runtime.stop()
        with pytest.raises(ConfigurationError):
            await runtime.start([Platform.APPLE], immediate=True)
        assert runtime.adapters[Platform.APPLE] is adapter
        assert adapter.purchase_plan == approved and rebuilt is not adapter
    finally:
        await runtime.close()


async def test_plan_persists_and_config_changes_require_fresh_approval(tmp_path):
    config = configuration(tmp_path)
    runtime = Runtime(config)
    await runtime.approve_plan(runtime.plan_snapshot()["digest"])
    await runtime.close()
    reopened = Runtime(config)
    try:
        assert reopened.plan_snapshot()["approved"]
        assert reopened.adapters[Platform.APPLE].purchase_plan == reopened.purchase_plan
        changed = config.model_copy(
            update={"order": config.order.model_copy(update={"payment_method": "installments"})}
        )
        changed._root = tmp_path
        assert draft_plan(changed).digest != reopened.purchase_plan.digest
        with pytest.raises(ConfigurationError):
            await reopened.approve_plan("stale-plan-version")
    finally:
        await reopened.close()
    changed_runtime = Runtime(changed)
    try:
        assert not changed_runtime.plan_snapshot()["approved"]
        assert not changed_runtime.purchase_plan.address_fingerprint
    finally:
        await changed_runtime.close()


def test_plan_api_requires_exact_explicit_approval_without_submission_switches(tmp_path):
    app = create_app(configuration(tmp_path))
    headers = {"X-Apple-Bot-Control": "local"}
    with TestClient(app, base_url="http://127.0.0.1") as client:
        draft = client.get("/purchase-plan").json()
        assert not draft["approved"] and draft["products"][0]["max_total"] == "6799"
        body = {"digest": draft["digest"], "confirmed": True}
        assert client.post("/approve-plan", json=body).status_code == 403
        for bad in ({"digest": draft["digest"]}, {**body, "auto_submit": True}):
            assert client.post("/approve-plan", headers=headers, json=bad).status_code == 422
        assert client.post("/approve-plan", headers=headers, json=body).status_code == 200
        assert client.get("/status").json()["dry_run"] is True


async def test_target_save_failure_cannot_pair_new_target_with_old_approval(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    config._source_path = tmp_path / "config.yaml"
    config._source_path.write_text("original config", encoding="utf-8")
    runtime = Runtime(config)
    try:
        await runtime.approve_plan(runtime.purchase_plan.digest)
        original_plan = runtime.purchase_plan
        original_adapter = runtime.adapters[Platform.APPLE]
        build = runtime._build_adapters
        product_id = next(iter(config.products))
        target = ProductTarget(url="https://www.apple.com.cn/shop/buy-iphone/changed-target")

        def fail_build(_config=None):
            raise RuntimeError("injected adapter construction failure")

        monkeypatch.setattr(runtime, "_build_adapters", fail_build)
        with pytest.raises(RuntimeError, match="injected"):
            await runtime.save_target(Platform.APPLE, product_id, target)
        assert config._source_path.read_text(encoding="utf-8") == "original config"
        assert runtime.config is config and runtime.purchase_plan is original_plan
        assert runtime.adapters[Platform.APPLE] is original_adapter

        monkeypatch.setattr(runtime, "_build_adapters", build)
        await runtime.save_target(Platform.APPLE, product_id, target)
        assert runtime.purchase_plan.digest != original_plan.digest
        assert runtime.purchase_plan.approved_at is None
        assert runtime.adapters[Platform.APPLE].purchase_plan is runtime.purchase_plan
        assert "changed-target" in config._source_path.read_text(encoding="utf-8")
    finally:
        await runtime.close()
