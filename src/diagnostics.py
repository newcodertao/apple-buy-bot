import importlib.metadata
import sys

import httpx
from playwright.async_api import Error, async_playwright

from src.browser.groups import session_key
from src.browser.manager import BrowserManager
from src.core.clock import diagnostics
from src.core.config import AppConfig
from src.core.logging import safe_url
from src.core.models import Platform
from src.storage.database import Database


async def doctor(config: AppConfig, online: bool = False) -> dict:
    checks = {
        "python": {
            "status": "PASS" if sys.version_info >= (3, 12) else "FAIL",
            "version": sys.version.split()[0],
        },
        "config": {"status": "PASS"},
    }
    checks["playwright"] = {"status": "PASS", "version": importlib.metadata.version("playwright")}
    async with async_playwright() as playwright:
        try:
            # A disposable headless launch checks the actual selected browser,
            # never the account profile or a bundled browser's unrelated path.
            browser = await playwright.chromium.launch(
                channel=config.app.browser, headless=True, chromium_sandbox=True
            )
            try:
                checks[config.app.browser] = {
                    "status": "PASS",
                    "channel": config.app.browser,
                    "version": browser.version,
                }
            finally:
                await browser.close()
        except Error:
            checks[config.app.browser] = {
                "status": "FAIL",
                "channel": config.app.browser,
                "reason": "所选浏览器无法启动，请检查安装和浏览器策略",
            }
    database = Database(config.paths.database)
    try:
        database.initialize()
        database.recent("runs", 1)
        checks["database"] = {"status": "PASS", "guard": database.guard_status()}
    except Exception as exc:
        checks["database"] = {"status": "FAIL", "reason": type(exc).__name__}
    finally:
        database.close()
    manager = BrowserManager(config.paths.profiles, channel=config.app.browser)
    checks["profiles"] = {
        p.value: {
            "exists": manager.profile_dir(p).is_dir(),
            "session_group": session_key(p),
            "login": "UNKNOWN",
            "reason": "Profile existence is not authentication evidence",
        }
        for p in Platform
    }
    targets = config.targets()
    checks["targets"] = {
        "status": "PASS" if targets else "BLOCKED",
        "urls": [safe_url(t[2]) for t in targets],
        "reason": "Configured URLs still require live selector validation",
    }
    checks["clock"] = diagnostics(config.sale.target(), config.sale.timezone)
    checks["network"] = {
        "status": "NOT RUN",
        "reason": "Use doctor --online for one public request",
    }
    if online:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                response = await client.head("https://www.apple.com/cn/")
            checks["network"] = {
                "status": "PASS" if response.status_code < 400 else "FAIL",
                "http_status": response.status_code,
            }
        except httpx.HTTPError as exc:
            checks["network"] = {"status": "FAIL", "reason": type(exc).__name__}
    return checks
