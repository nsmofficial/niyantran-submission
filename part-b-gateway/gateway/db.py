"""SQLite schema and connections.

Everything that has to survive a restart lives in this one file on disk: the
monthly spend counter, the rate-limit window, and the usage log. An in-process
dict would be reset by every deploy and multiplied by the worker count.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id                   TEXT PRIMARY KEY,   -- public identifier; this is what we log
    token_hash           TEXT NOT NULL UNIQUE,
    label                TEXT NOT NULL DEFAULT '',
    monthly_budget_paise INTEGER NOT NULL,
    created_at           TEXT NOT NULL,
    revoked_at           TEXT                -- NULL means active
);

-- Money is stored as integer paise. Accumulating rupees as floats drifts, and
-- the cap is an equality-sensitive comparison ("once it reaches the budget").
CREATE TABLE IF NOT EXISTS spend (
    token_id    TEXT NOT NULL,
    month       TEXT NOT NULL,               -- 'YYYY-MM' in IST
    spent_paise INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_id, month)
);

-- One row per request. There is deliberately no column for the question text:
-- a political-data company that keeps a record of what its clients asked has
-- created a subpoena target and a leak that ends the client, so the only safe
-- design is not to have the data. Cost and status are all billing needs.
CREATE TABLE IF NOT EXISTS usage_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id   TEXT,                         -- NULL when the presented token was unknown
    ts         TEXT NOT NULL,                -- UTC, ISO-8601
    status     INTEGER NOT NULL,
    cost_paise INTEGER NOT NULL DEFAULT 0
);

-- The rate-limit window is durable for the same reason the spend counter is.
-- Kept separate from usage_log because that log is pruned on a retention
-- schedule and the limiter must not silently widen when it is.
CREATE TABLE IF NOT EXISTS rate_events (
    token_id TEXT NOT NULL,
    at       REAL NOT NULL                   -- unix seconds
);
CREATE INDEX IF NOT EXISTS ix_rate_events ON rate_events (token_id, at);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    # isolation_level=None: no implicit transactions, so the gates below can use
    # explicit BEGIN IMMEDIATE where atomicity actually matters.
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init(path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
