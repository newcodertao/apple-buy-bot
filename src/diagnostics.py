import asyncio
import importlib.metadata
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

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
        executable = Path(playwright.chromium.executable_path)
        checks["chromium"] = {
            "status": "PASS" if await asyncio.to_thread(executable.is_file) else "FAIL",
            "path": str(executable),
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
    checks["profiles"] = {
        p.value: {
            "exists": (config.paths.profiles / p.value).is_dir(),
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
