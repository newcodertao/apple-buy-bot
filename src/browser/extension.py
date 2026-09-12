"""Local, single-controller CDP relay for the user's explicitly paired browser tab."""

import asyncio
import contextlib
import ipaddress
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from playwright.async_api import Error, Page, async_playwright

from src.browser.groups import profile_platform
from src.browser.session import ProfileLock, apple_state_host
from src.core.engine import finish_cleanup
from src.core.exceptions import HumanRequired
from src.core.models import Platform

router = APIRouter(prefix="/extension")
PLATFORMS = {Platform.APPLE, Platform.JD, Platform.TAOBAO}


def _local(connection) -> bool:
    try:
        return (
            urlsplit("http://" + connection.headers.get("host", "")).hostname
            in {"127.0.0.1", "localhost", "::1"}
            and connection.client is not None
            and ipaddress.ip_address(connection.client.host).is_loopback
        )
    except ValueError:
        return False


def _page_platform(url: str) -> Platform | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return None
        host = parsed.hostname or ""
        if apple_state_host(host):
            return Platform.APPLE
        for domain, platform in (("jd.com", Platform.JD), ("taobao.com", Platform.TAOBAO)):
            if host == domain or host.endswith("." + domain):
                return platform
    except ValueError:
        pass
    return None


class ExtensionBroker:
    """No replay or queues: losing either transport ends the current attachment."""

    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.cdp_url = ""
        self.relay: WebSocket | None = None
        self.controller: WebSocket | None = None
        self.platform: str | None = None
        self._pumps: set[asyncio.Task] = set()
        self._cleanup: asyncio.Task | None = None

    def authenticated(self, token) -> bool:
        return isinstance(token, str) and secrets.compare_digest(token, self.token)

    def status(self) -> dict:
        return {
            "connected": self.relay is not None and self._cleanup is None,
            "controlled": self.controller is not None and self._cleanup is None,
            "platform": self.platform,
        }

    async def _pump(self, socket: WebSocket, *, relay: bool) -> None:
        while True:
            message = await socket.receive_text()
            if relay:
                # Only pairing metadata is consumed; raw CDP remains unchanged.
                try:
                    data = json.loads(message)
                except (TypeError, ValueError):
                    return
                if not isinstance(data, dict):
                    return
                if "type" in data:
                    if data.get("type") == "bound" and data.get("platform") in PLATFORMS:
                        self.platform = data["platform"]
                    continue
            destination = self.controller if relay else self.relay
            if destination is not None:
                await destination.send_text(message)

    async def serve(self, socket: WebSocket, *, relay: bool) -> None:
        task = asyncio.create_task(self._pump(socket, relay=relay))
        self._pumps.add(task)
        try:
            await task
        except (WebSocketDisconnect, RuntimeError):
            pass
        except asyncio.CancelledError:
            # Peer loss cancels this pump; ASGI parent cancellation still propagates.
            if asyncio.current_task().cancelling():
                raise
        finally:
            await self.disconnect(socket)

    async def _disconnect(self) -> None:
        pumps = tuple(self._pumps)
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
        for socket in (self.controller, self.relay):
            if socket is not None:
                with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                    await socket.close(code=1001)
        self.controller = self.relay = None
        self.platform = None
        self._pumps.clear()

    async def disconnect(self, expected: WebSocket | None = None) -> None:
        if expected is not None and expected is not self.relay and expected is not self.controller:
            return
        if self._cleanup is None:
            self._cleanup = asyncio.create_task(self._disconnect())
        cleanup = self._cleanup
        try:
            await finish_cleanup(cleanup)
        finally:
            if cleanup.done() and self._cleanup is cleanup:
                self._cleanup = None


def _manager(connection) -> "ExtensionBrowserManager":
    manager = getattr(connection.app.state.runtime, "manager", None)
    if not isinstance(manager, ExtensionBrowserManager):
        raise HTTPException(409, "请先在本机配置 browser: extension，并重新启动服务")
    return manager


@router.post("/pair")
async def pair(request: Request):
    manager = _manager(request)
    if not _local(request):
        raise HTTPException(403, "Local connection required")
    base = str(request.base_url).rstrip("/")
    ws_base = ("wss" if request.url.scheme == "https" else "ws") + base[base.index(":") :]
    manager.broker.cdp_url = ws_base + "/extension/cdp"
    return {"token": manager.broker.token, "relay_url": ws_base + "/extension/relay"}


@router.get("/status")
async def status(request: Request):
    return _manager(request).broker.status()


@router.websocket("/relay")
async def relay(websocket: WebSocket):
    if not _local(websocket) or not re.fullmatch(
        r"chrome-extension://[a-p]{32}", websocket.headers.get("origin", "")
    ):
        await websocket.close(code=1008)
        return
    try:
        broker = _manager(websocket).broker
    except HTTPException:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        handshake = await asyncio.wait_for(websocket.receive_json(), timeout=10)
    except (WebSocketDisconnect, ValueError, TimeoutError):
        await websocket.close(code=1008)
        return
    if (
        not isinstance(handshake, dict)
        or not broker.authenticated(handshake.get("token"))
        or not broker.cdp_url
        or broker.relay is not None
        or broker._cleanup is not None
    ):
        await websocket.close(code=1008)
        return
    broker.relay = websocket
    try:
        await websocket.send_json({"type": "paired"})
        await broker.serve(websocket, relay=True)
    finally:
        await broker.disconnect(websocket)


@router.websocket("/cdp")
async def cdp(websocket: WebSocket):
    try:
        broker = _manager(websocket).broker
    except HTTPException:
        await websocket.close(code=1008)
        return
    auth = websocket.headers.get("authorization", "")
    if (
        not _local(websocket)
        or websocket.headers.get("origin")
        or not auth.startswith("Bearer ")
        or not broker.authenticated(auth[7:])
        or broker.relay is None
        or broker.controller is not None
        or broker._cleanup is not None
    ):
        await websocket.close(code=1008)
        return
    # Reserve before awaiting: two simultaneous controllers cannot take over.
    broker.controller = websocket
    try:
        await websocket.accept()
        await broker.serve(websocket, relay=False)
    finally:
        await broker.disconnect(websocket)


class ExtensionBrowserManager:
    """Reuse one paired tab without launching, copying, or closing any user profile."""

    channel = "extension"
    headless = False

    def __init__(self, profiles_dir: Path):
        self.profiles_dir = Path(profiles_dir).resolve()
        self.broker = ExtensionBroker()
        self._playwright = None
        self._browser = None
        self._page: Page | None = None
        self._platform: Platform | None = None
        self._profile_lock: ProfileLock | None = None
        self._lock = asyncio.Lock()

    def profile_dir(self, platform: Platform) -> Path:
        return self.profiles_dir / profile_platform(platform).value

    def login_state_import_status(self, platform: Platform) -> str:
        return "PAIRED_BROWSER_LOGIN_UNVERIFIED"

    def network_diagnostics(self, platform: Platform) -> list[dict]:
        return []

    def current_page(self, platform: Platform) -> Page | None:
        if (
            platform == self._platform
            and self._page is not None
            and not self._page.is_closed()
            and self._browser is not None
            and self._browser.is_connected()
        ):
            return self._page
        return None

    async def open_visible(self, platform: Platform) -> Page:
        return await self.open(platform)

    async def open(self, platform: Platform) -> Page:
        platform = Platform(platform)
        if platform not in PLATFORMS:
            raise HumanRequired("浏览器扩展仅支持淘宝、京东和 Apple")
        async with self._lock:
            if page := self.current_page(platform):
                return page
            if self._platform is not None and self._platform != platform:
                raise HumanRequired("扩展已有平台会话，请先正常结束旧任务，再配对其他平台页面")
            if not self.broker.status()["connected"] or not self.broker.cdp_url:
                raise HumanRequired("请在本机控制台配对扩展，并在目标商品标签页点击连接")
            if self._playwright is not None:
                raise HumanRequired(
                    "浏览器连接已中断，请先正常结束旧任务，再重新配对；不要重复加购"
                )
            profile_lock = ProfileLock(self.profile_dir(platform))
            profile_lock.acquire()
            self._profile_lock = profile_lock
            self._platform = platform
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self.broker.cdp_url,
                    headers={"Authorization": "Bearer " + self.broker.token},
                    timeout=15_000,
                    no_defaults=True,
                )
                pages = [page for context in self._browser.contexts for page in context.pages]
                if len(pages) != 1 or _page_platform(pages[0].url) != platform:
                    raise HumanRequired("请在扩展中配对所选平台的一个商品或登录标签页")
                self._page = pages[0]
                self._page.set_default_timeout(10_000)
                self._page.set_default_navigation_timeout(30_000)
                return self._page
            except BaseException as error:
                await finish_cleanup(asyncio.create_task(self._detach()))
                if isinstance(error, Error):
                    raise HumanRequired("扩展连接未完成，请检查扩展状态后重新配对") from None
                raise

    async def _detach(self) -> None:
        # Closing the transport detaches chrome.debugger. Never close a user page/context.
        await self.broker.disconnect()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._page = None
        self._platform = None
        if self._profile_lock is not None:
            self._profile_lock.release()
            self._profile_lock = None

    async def close(self, platform: Platform | None = None) -> None:
        async with self._lock:
            if platform is not None and platform != self._platform:
                return
            await finish_cleanup(asyncio.create_task(self._detach()))
