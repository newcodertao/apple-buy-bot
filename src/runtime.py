import asyncio
import contextlib

from src.browser.manager import BrowserManager
from src.core.clock import diagnostics
from src.core.config import AppConfig
from src.core.engine import Engine
from src.core.exceptions import ConfigurationError
from src.core.logging import setup_logging
from src.core.models import Platform
from src.platforms.apple_cn.adapter import AppleCNAdapter
from src.platforms.jd.adapter import JDAdapter
from src.platforms.tmall.adapter import TmallAdapter
from src.storage.database import Database


class Runtime:
    """One process owns the engine and browser sessions; SQLite protects cross-process submit."""

    def __init__(self, config: AppConfig):
        self.config = config
        setup_logging(config.paths.logs)
        self.database = Database(config.paths.database)
        self.database.initialize()
        self.manager = BrowserManager(config.paths.profiles, headless=config.app.headless)
        self.adapters = {
            Platform.APPLE: AppleCNAdapter(self.manager, config.paths.screenshots),
            Platform.JD: JDAdapter(self.manager, config.paths.screenshots),
            Platform.TMALL: TmallAdapter(self.manager, config.paths.screenshots),
        }
        self.engine = Engine(config, self.adapters, self.database)
        self.task: asyncio.Task | None = None
        self._control = asyncio.Lock()

    async def start(self, platforms=None, immediate=False, dry_run=None) -> dict:
        async with self._control:
            if self.task and not self.task.done():
                raise ConfigurationError("Engine is already running")
            if not self.config.targets(platforms):
                raise ConfigurationError(
                    "No enabled product URLs; inspect and configure real URLs first"
                )
            if self.task:
                # Retrieve any exception before replacing task, avoiding silent background failures.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    self.task.result()
            self.engine = Engine(self.config, self.adapters, self.database)
            self.task = asyncio.create_task(
                self.engine.run(platforms=platforms, immediate=immediate, dry_run=dry_run)
            )
            return {"status": "started"}

    async def stop(self) -> dict:
        async with self._control:
            # Cancel the main task too: it may still be queued and have no workers yet.
            if self.task and not self.task.done():
                self.task.cancel()
            await self.engine.stop()
            if self.task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.task
            return {"status": "stopped"}

    async def resume(self, platform=None) -> dict:
        if not self.task or self.task.done():
            raise ConfigurationError("No running task is waiting for human input")
        await self.engine.resume(platform)
        return {"status": "resume_requested"}

    def snapshot(self) -> dict:
        result = self.engine.snapshot()
        result["clock"] = diagnostics(self.config.sale.target(), self.config.sale.timezone)
        result["running"] = bool(self.task and not self.task.done())
        result["order_guard"] = self.database.guard_status()
        if self.task and self.task.done() and not self.task.cancelled():
            error = self.task.exception()
            if error:
                result["background_error"] = type(error).__name__
        return result

    async def close(self) -> None:
        await self.stop()
        await self.manager.close()
        self.database.close()
