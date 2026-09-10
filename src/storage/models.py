"""SQLite schema; monetary values are decimal strings, never binary floats."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    sale_time TEXT,
    product TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    timestamp TEXT NOT NULL,
    platform TEXT NOT NULL,
    product TEXT NOT NULL,
    sku TEXT NOT NULL,
    old_state TEXT NOT NULL,
    new_state TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    timestamp TEXT NOT NULL,
    platform TEXT NOT NULL,
    product TEXT NOT NULL,
    sku TEXT NOT NULL,
    available INTEGER NOT NULL CHECK (available IN (0, 1)),
    price TEXT NOT NULL,
    currency TEXT NOT NULL,
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0)
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    platform TEXT NOT NULL,
    product TEXT NOT NULL,
    sku TEXT NOT NULL,
    price TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_guard (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    owner TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('CLAIMED', 'SUBMITTING', 'UNKNOWN', 'SUCCESS', 'REJECTED')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS stock_checks_run ON stock_checks(run_id, id);
CREATE INDEX IF NOT EXISTS orders_run ON orders(run_id, id);
"""
