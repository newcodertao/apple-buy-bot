"""SQLite audit records and one durable submit reservation per database.

Never put cookies, credentials, URLs with query strings, or raw page text in
these records. Messages supplied by callers must be static, non-sensitive text.
"""

import math
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from src.core.logging import redact
from src.core.models import SKU
from src.storage.models import SCHEMA


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")

    def initialize(self) -> None:
        with self._lock:
            self.connection.executescript(SCHEMA)

    def create_run(self, sale_time: str | None, product: str) -> str:
        run_id = uuid4().hex
        with self._lock:
            self.connection.execute(
                "INSERT INTO runs(run_id, start_time, sale_time, product) VALUES (?, ?, ?, ?)",
                (run_id, timestamp(), sale_time, product),
            )
        return run_id

    def record_event(self, run_id: str, event: dict) -> None:
        with self._lock:
            self.connection.execute(
                """INSERT INTO events(run_id, timestamp, platform, product, sku,
                old_state, new_state, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    str(event.get("timestamp", timestamp())),
                    str(event.get("platform", "")),
                    str(event.get("product", event.get("product_id", ""))),
                    str(event.get("sku", event.get("sku_id", ""))),
                    str(event.get("old_state", "")),
                    str(event.get("new_state", "")),
                    redact(str(event.get("message", ""))),
                ),
            )

    def record_stock(self, run_id: str, sku: SKU, latency_ms: float) -> None:
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("Latency must be finite and non-negative")
        with self._lock:
            self.connection.execute(
                """INSERT INTO stock_checks(run_id, timestamp, platform, product,
                sku, available, price, currency, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, timestamp(), sku.platform.value, sku.product_id, sku.id,
                 int(sku.available), str(sku.price), sku.currency, latency_ms),
            )

    def record_order(self, run_id: str, sku: SKU, status: str, message: str = "") -> int:
        with self._lock:
            cursor = self.connection.execute(
                """INSERT INTO orders(run_id, platform, product, sku, price,
                currency, status, created_at, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, sku.platform.value, sku.product_id, sku.id, str(sku.price),
                 sku.currency, status, timestamp(), redact(message)),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, timestamp(), run_id),
            )

    def recent(self, table: str, limit: int = 100) -> list[dict]:
        if table not in {"runs", "events", "stock_checks", "orders"}:
            raise ValueError("Unsupported audit table")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Limit must be an integer from 1 to 1000")
        order_by = "rowid" if table == "runs" else "id"
        with self._lock:
            rows = self.connection.execute(
                f"SELECT * FROM {table} ORDER BY {order_by} DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_order(self, owner: str) -> bool:
        if not owner or not owner.strip():
            raise ValueError("Order owner must be non-empty")
        with self._lock:
            # One INSERT guarded by a unique key is atomic across processes.
            now = timestamp()
            cursor = self.connection.execute(
                """INSERT OR IGNORE INTO order_guard
                (singleton, owner, status, created_at, updated_at)
                VALUES (1, ?, 'CLAIMED', ?, ?)""", (owner, now, now)
            )
            return cursor.rowcount == 1

    def mark_submission(self, owner: str, status: str) -> None:
        allowed = {
            "SUBMITTING": ("CLAIMED",),
            "UNKNOWN": ("SUBMITTING",),
            "SUCCESS": ("SUBMITTING", "UNKNOWN"),
            "REJECTED": ("CLAIMED", "SUBMITTING"),
        }
        if status not in allowed:
            raise ValueError("Invalid submission status")
        prior_states = allowed[status]
        placeholders = ", ".join("?" for _ in prior_states)
        with self._lock:
            cursor = self.connection.execute(
                f"""UPDATE order_guard SET status = ?, updated_at = ?
                WHERE singleton = 1 AND owner = ? AND status IN ({placeholders})""",
                (status, timestamp(), owner, *prior_states),
            )
            if cursor.rowcount != 1:
                raise ValueError("Submission guard owner or transition is invalid")

    def release_order(self, owner: str) -> bool:
        with self._lock:
            cursor = self.connection.execute(
                """DELETE FROM order_guard WHERE singleton = 1 AND owner = ?
                AND status IN ('CLAIMED', 'REJECTED')""", (owner,)
            )
            return cursor.rowcount == 1

    def reconcile_order(self, owner: str, *, confirmed_no_order: bool) -> bool:
        """Only a human-confirmed account order-history check may clear ambiguity."""
        if confirmed_no_order is not True:
            raise ValueError("Explicit confirmation that no order exists is required")
        with self._lock:
            cursor = self.connection.execute(
                """DELETE FROM order_guard WHERE singleton = 1 AND owner = ?
                AND status IN ('CLAIMED', 'SUBMITTING', 'UNKNOWN', 'REJECTED')""", (owner,)
            )
            return cursor.rowcount == 1

    def guard_status(self) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT owner, status, created_at, updated_at FROM order_guard WHERE singleton = 1"
            ).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self.connection.close()
