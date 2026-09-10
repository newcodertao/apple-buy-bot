"""Session controls preserve the run and use the same program profile."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.core.config import load_config
from src.core.exceptions import ConfigurationError
from src.core.models import LoginStatus, Platform
from src.main import initialize
from src.runtime import Runtime


@pytest.fixture
async def runtime(tmp_path):
    path = tmp_path / "config/config.yaml"
    initialize(path)
    instance = Runtime(load_config(path))
    try:
        yield instance
    finally:
        await instance.close()


async def test_busy_login_controls_do_not_navigate(runtime, monkeypatch):
    open_visible = AsyncMock()
    monkeypatch.setattr(runtime.manager, "open_visible", open_visible)
    runtime.task = asyncio.create_task(asyncio.Event().wait())
    for operation in (runtime.open_login, runtime.check_login):
        with pytest.raises(ConfigurationError, match="运行中"):
            await operation()
    open_visible.assert_not_called()


async def test_manual_login_and_status_never_claim_success_from_opening(runtime, monkeypatch):
    page = object()
    open_visible = AsyncMock(return_value=page)
    monkeypatch.setattr(runtime.manager, "open_visible", open_visible)
    adapter = runtime.adapters[Platform.APPLE]
    navigate = AsyncMock()
    monkeypatch.setattr(adapter, "_navigate", navigate)
    await runtime.open_login()
    open_visible.assert_awaited_once_with(Platform.APPLE)
    assert navigate.await_args.args[0] is page
    assert runtime.snapshot()["platforms"]["apple"]["login"] == "UNKNOWN"
    monkeypatch.setattr(runtime.manager, "current_page", lambda _: page)
    monkeypatch.setattr(adapter, "login_status", AsyncMock(return_value=LoginStatus.REQUIRED))
    assert (await runtime.check_login())["login"] == "REQUIRED"
    assert runtime.snapshot()["platforms"]["apple"]["login"] == "REQUIRED"
    assert runtime.database.guard_status() is None
