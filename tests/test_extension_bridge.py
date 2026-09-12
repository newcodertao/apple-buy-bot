import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.browser.extension import ExtensionBrowserManager, router
from src.browser.session import ProfileLock
from src.core.exceptions import HumanRequired
from src.core.models import Platform

ORIGIN = "chrome-extension://" + "a" * 32


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.runtime = SimpleNamespace(manager=ExtensionBrowserManager(tmp_path / "profiles"))
    app.include_router(router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30001)) as client:
        yield client


def test_relay_forwards_once_rejects_takeover_and_closes_both_on_loss(client):
    token = client.post("/extension/pair").json()["token"]
    headers = {"Authorization": "Bearer " + token}
    with client.websocket_connect(
        "ws://localhost/extension/relay", headers={"Origin": ORIGIN}
    ) as extension:
        extension.send_json({"token": token})
        assert extension.receive_json() == {"type": "paired"}
        with client.websocket_connect(
            "ws://localhost/extension/cdp", headers=headers
        ) as controller:
            command = '{"id":1,"method":"Browser.getVersion"}'
            controller.send_text(command)
            assert extension.receive_text() == command
            extension.send_json({"type": "bound", "platform": "jd", "targetId": "jd-123"})
            reply = '{"id":1,"result":{"product":"Chrome/140"}}'
            extension.send_text(reply)
            assert controller.receive_text() == reply
            assert client.get("/extension/status").json() == {
                "connected": True,
                "controlled": True,
                "platform": "jd",
            }
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("ws://localhost/extension/cdp", headers=headers):
                    pass
            with client.websocket_connect(
                "ws://localhost/extension/relay", headers={"Origin": ORIGIN}
            ) as duplicate:
                duplicate.send_json({"token": token})
                with pytest.raises(WebSocketDisconnect):
                    duplicate.receive_json()
            extension.close()
            with pytest.raises(WebSocketDisconnect):
                controller.receive_text()
    assert client.get("/extension/status").json() == {
        "connected": False,
        "controlled": False,
        "platform": None,
    }
    # Reconnecting does not resurrect queued commands or the old controller.
    with client.websocket_connect(
        "ws://localhost/extension/relay", headers={"Origin": ORIGIN}
    ) as extension:
        extension.send_json({"token": token})
        assert extension.receive_json() == {"type": "paired"}
        with client.websocket_connect(
            "ws://localhost/extension/cdp", headers=headers
        ) as controller:
            controller.send_text('{"id":2,"method":"Target.getTargets"}')
            assert extension.receive_json()["id"] == 2
            controller.close()
            with pytest.raises(WebSocketDisconnect):
                extension.receive_text()


def test_pair_and_websocket_auth_require_local_explicit_token(client):
    response = client.post("/extension/pair")
    assert response.json()["relay_url"] == "ws://localhost/extension/relay"
    token = response.json()["token"]
    assert token not in str(client.get("/extension/status").json())
    for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": ORIGIN, "Host": "evil"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("ws://localhost/extension/relay", headers=headers):
                pass
    with client.websocket_connect(
        "ws://localhost/extension/relay", headers={"Origin": ORIGIN}
    ) as extension:
        extension.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect):
            extension.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "ws://localhost/extension/cdp", headers={"Authorization": "Bearer wrong"}
        ):
            pass
    client.app.state.runtime.manager = object()
    assert client.post("/extension/pair").status_code == 409


def test_remote_client_cannot_pair(tmp_path):
    app = FastAPI()
    app.state.runtime = SimpleNamespace(manager=ExtensionBrowserManager(tmp_path))
    app.include_router(router)
    with TestClient(app, base_url="http://localhost", client=("10.0.0.2", 50000)) as remote:
        assert remote.post("/extension/pair").status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with remote.websocket_connect(
                "ws://localhost/extension/relay", headers={"Origin": ORIGIN}
            ):
                pass


def fake_playwright(monkeypatch, manager):
    page = Mock()
    page.url = "https://item.taobao.com/item.htm?id=123"
    page.is_closed.return_value = False
    browser = Mock()
    browser.is_connected.return_value = True
    browser.contexts = [SimpleNamespace(pages=[page])]
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)), stop=AsyncMock()
    )
    monkeypatch.setattr(
        "src.browser.extension.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)),
    )
    manager.broker.relay = SimpleNamespace(close=AsyncMock())
    manager.broker.cdp_url = "ws://localhost/extension/cdp"
    return page, browser, playwright


async def test_manager_reuses_tab_and_same_profile_lock_without_closing_browser(
    monkeypatch, tmp_path
):
    manager = ExtensionBrowserManager(tmp_path)
    page, browser, playwright = fake_playwright(monkeypatch, manager)
    assert await manager.open(Platform.TAOBAO) is page
    assert await manager.open_visible(Platform.TAOBAO) is page
    playwright.chromium.connect_over_cdp.assert_awaited_once()
    competing = ProfileLock(tmp_path / "tmall")
    with pytest.raises(HumanRequired, match="already in use"):
        competing.acquire()
    with pytest.raises(HumanRequired, match="已有平台"):
        await manager.open(Platform.JD)
    await manager.close()
    playwright.stop.assert_awaited_once()
    browser.close.assert_not_called()
    page.close.assert_not_called()
    competing.acquire()
    competing.release()


async def test_close_cancellation_waits_for_detach_and_lock_release(monkeypatch, tmp_path):
    manager = ExtensionBrowserManager(tmp_path)
    _, browser, playwright = fake_playwright(monkeypatch, manager)
    await manager.open(Platform.TAOBAO)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_stop():
        entered.set()
        await release.wait()

    playwright.stop.side_effect = delayed_stop
    close = asyncio.create_task(manager.close())
    await entered.wait()
    close.cancel()
    await asyncio.sleep(0)
    assert not close.done()
    assert manager._profile_lock is not None
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert manager._profile_lock is None
    assert not manager.broker._pumps
    browser.close.assert_not_called()
