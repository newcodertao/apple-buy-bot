import json

import pytest
from fastapi.testclient import TestClient

from src.core.config import load_config
from src.main import initialize, main, parser
from src.web.app import create_app


def test_init_never_overwrites_existing_config(tmp_path):
    path = tmp_path / "config/config.yaml"
    assert initialize(path)["created_config"]
    original = path.read_text(encoding="utf-8") + "\n# preserved user edit\n"
    path.write_text(original, encoding="utf-8")
    assert not initialize(path)["created_config"]
    assert path.read_text(encoding="utf-8") == original
    assert (tmp_path / "data/database.db").is_file()


@pytest.mark.parametrize(
    "args",
    [
        ["init"],
        ["login", "apple"],
        ["login", "jd"],
        ["login", "tmall"],
        ["check-login"],
        ["check-stock"],
        ["dry-run", "apple"],
        ["dry-run", "all"],
        ["run"],
        ["status"],
        ["web"],
        ["doctor"],
        ["inspect", "apple", "https://www.apple.com/cn/"],
    ],
)
def test_required_commands_parse(args):
    assert parser().parse_args(args).command == args[0]


def test_cli_stock_blocks_missing_targets_without_browser(tmp_path, capsys):
    path = tmp_path / "config/config.yaml"
    initialize(path)
    assert main(["--config", str(path), "check-stock"]) == 0
    assert "BLOCKED" in capsys.readouterr().out
    assert main(["--config", str(path), "dry-run", "apple"]) == 2
    assert "No enabled product URLs" in capsys.readouterr().err


def test_web_reads_and_control_boundary(tmp_path):
    path = tmp_path / "config/config.yaml"
    initialize(path)
    app = create_app(load_config(path))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        for endpoint in ("/status", "/platforms", "/products", "/events", "/orders", "/health"):
            response = client.get(endpoint)
            assert response.status_code == 200, endpoint
            assert response.headers["cache-control"] == "no-store"
        assert client.get("/").status_code == 200
        status = client.get("/status").json()
        assert status["dry_run"] and not status["auto_submit"]
        assert status["platforms"]["apple"]["login"] == "UNKNOWN"
        assert client.post("/start", json={}).status_code == 403
        headers = {"X-Apple-Bot-Control": "local"}
        assert client.post("/start", json={}, headers=headers).status_code == 409
        assert client.post("/start", json={"auto_submit": True}, headers=headers).status_code == 422
        assert client.post("/start", json={"dry_run": False}, headers=headers).status_code == 422
        assert (
            client.post("/login", json={"password": "test-only"}, headers=headers).status_code
            == 422
        )
        assert client.post("/resume", json={}, headers=headers).status_code == 409
        assert client.post("/stop", headers=headers).status_code == 200
        assert client.get("/events?limit=10001").status_code == 422
        assert client.get("/status", headers={"Host": "evil.example"}).status_code == 403
        assert (
            client.post("/stop", headers={**headers, "Origin": "https://evil.example"}).status_code
            == 403
        )
        assert client.get("/status").json()["order_guard"] is None
        assert client.post("/finish-task").status_code == 403
        database = app.state.runtime.database
        assert database.claim_order("fixture-owner")
        database.mark_submission("fixture-owner", "SUBMITTING")
        database.mark_submission("fixture-owner", "SUCCESS")
        orders = database.recent("orders", 10)
        result = client.post("/finish-task", headers=headers)
        assert result.status_code == 200
        assert result.json()["ended"] and not result.json()["can_start_new_task"]
        assert database.guard_status()["status"] == "SUCCESS"
        assert database.recent("orders", 10) == orders


def test_cli_status_uses_sqlite_history(tmp_path, capsys):
    path = tmp_path / "config/config.yaml"
    initialize(path)
    assert main(["--config", str(path), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["order_guard"] is None
