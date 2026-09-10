import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.models import Platform, Verification
from src.main import inspect_page


async def test_inspect_keeps_challenge_open_and_only_rechecks_after_resume(tmp_path, monkeypatch):
    inventory = tmp_path / "inspect.json"
    inventory.write_text(json.dumps({"metadata": {"state": "WAITING_HUMAN"}}))
    queue = asyncio.Queue()
    monkeypatch.setattr("src.main.console_queue", lambda: queue)
    adapter = SimpleNamespace(
        inspect=AsyncMock(return_value=inventory),
        detect_verification=AsyncMock(side_effect=[Verification(required=True), Verification()]),
    )
    runtime = SimpleNamespace(
        adapters={Platform.APPLE: adapter},
        manager=SimpleNamespace(current_page=lambda _: object()),
    )
    task = asyncio.create_task(
        inspect_page(runtime, Platform.APPLE, "https://www.apple.com", tmp_path)
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    adapter.detect_verification.assert_not_called()
    await queue.put("resume")
    await queue.put("resume")
    await asyncio.wait_for(task, 2)
    assert adapter.detect_verification.await_count == 2
    assert adapter.inspect.await_count == 1  # No navigation replay after human input.


async def test_inspect_normal_page_finishes_without_input(tmp_path):
    inventory = tmp_path / "inspect.json"
    inventory.write_text(json.dumps({"metadata": {"state": ""}}))
    adapter = SimpleNamespace(inspect=AsyncMock(return_value=inventory))
    runtime = SimpleNamespace(adapters={Platform.APPLE: adapter})
    await inspect_page(runtime, Platform.APPLE, "https://www.apple.com", tmp_path)
