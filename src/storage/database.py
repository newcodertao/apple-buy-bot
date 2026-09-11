"""SQLite audit records and one durable submit reservation per database.

Never put cookies, credentials, URLs with query strings, or raw page text in
these records. Messages supplied by callers must be static, non-sensitive text.
"""

import math
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from src.browser.groups import session_key
from src.core.logging import redact
from src.core.models import SKU, OrderResult, OrderReview, Platform
from src.storage.models import ORDER_COLUMNS, SCHEMA


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
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(orders)")}
            if columns and not ORDER_COLUMNS.keys() <= columns:
                # SQLite backup includes committed WAL data; never copy only the .db file.
                backup_path = self.path.with_name(
                    f"{self.path.name}.pre-marketplace-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.bak"
                )
                with closing(sqlite3.connect(backup_path)) as backup:
                    self.connection.backup(backup)
                self.connection.execute("BEGIN IMMEDIATE")
                try:
                    # Another process may have completed the same migration while we waited.
                    columns = {
                        row["name"] for row in self.connection.execute("PRAGMA table_info(orders)")
                    }
                    for name, definition in ORDER_COLUMNS.items():
                        if name not in columns:
                            self.connection.execute(
                                f"ALTER TABLE orders ADD COLUMN {name} {definition}"
                            )
                    self.connection.execute("COMMIT")
                except BaseException:
                    self.connection.execute("ROLLBACK")
                    raise
            self.connection.executescript(SCHEMA)
            if self.connection.execute("PRAGMA user_version").fetchone()[0] < 1:
                self.connection.execute("PRAGMA user_version = 1")

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
                (
                    run_id,
                    timestamp(),
                    sku.platform.value,
                    sku.product_id,
                    sku.id,
                    int(sku.available),
                    str(sku.price),
                    sku.currency,
                    latency_ms,
                ),
            )

    def record_order(
        self,
        run_id: str,
        sku: SKU,
        status: str,
        message: str = "",
        *,
        review: OrderReview | None = None,
        result: OrderResult | None = None,
    ) -> int:
        with self._lock:
            cursor = self.connection.execute(
                """INSERT INTO orders(run_id, platform, product, sku, price,
                currency, status, created_at, message, order_id, quantity, total_price,
                seller_id, platform_item_id, session_group, payment_state, financing_state,
                review_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    sku.platform.value,
                    sku.product_id,
                    sku.id,
                    str(sku.price),
                    sku.currency,
                    status,
                    timestamp(),
                    redact(message),
                    result.order_id if result else None,
                    review.quantity if review else None,
                    str(review.total_price) if review else None,
                    review.seller_id if review else sku.seller_id,
                    review.platform_item_id if review else sku.platform_item_id,
                    session_key(sku.platform),
                    result.payment_state if result else "UNKNOWN",
                    result.financing_state if result else "UNKNOWN",
                    review.model_dump_json() if review else None,
                ),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: str, status: str) -> None:
        with self._lock:
            self.connection.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, timestamp(), run_id),
            )

    def update_order_status(
        self,
        order_id: str,
        platform: Platform,
        *,
        payment_state: str,
        financing_state: str,
    ) -> bool:
        """Update a known local order's payment details without touching its reservation."""
        if not order_id or not order_id.strip():
            return False
        if payment_state not in {"UNKNOWN", "UNPAID", "PAID", "CANCELLED", "CLOSED"}:
            raise ValueError("Unsupported payment state")
        if financing_state not in {"UNKNOWN", "ELIGIBLE", "INELIGIBLE"}:
            raise ValueError("Unsupported financing state")
        with self._lock:
            cursor = self.connection.execute(
                """UPDATE orders SET payment_state = ?, financing_state = ?
                WHERE order_id = ? AND platform = ?""",
                (payment_state, financing_state, order_id, Platform(platform).value),
            )
            return cursor.rowcount > 0

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
                VALUES (1, ?, 'CLAIMED', ?, ?)""",
                (owner, now, now),
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
                AND status IN ('CLAIMED', 'REJECTED')""",
                (owner,),
            )
            return cursor.rowcount == 1

    def reconcile_order(self, owner: str, *, confirmed_no_order: bool) -> bool:
        """Only a human-confirmed account order-history check may clear ambiguity."""
        if confirmed_no_order is not True:
            raise ValueError("Explicit confirmation that no order exists is required")
        with self._lock:
            cursor = self.connection.execute(
                """DELETE FROM order_guard WHERE singleton = 1 AND owner = ?
                AND status IN ('CLAIMED', 'SUBMITTING', 'UNKNOWN', 'REJECTED')""",
                (owner,),
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
