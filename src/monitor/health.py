from datetime import UTC, datetime
from typing import Any


def health_snapshot(engine: Any) -> dict:
    snapshot = engine.snapshot()
    return {
        "status": "ok",
        "timestamp": datetime.now(UTC).isoformat(),
        "running": snapshot["running"],
        "order_guard": snapshot["order_guard"],
    }
