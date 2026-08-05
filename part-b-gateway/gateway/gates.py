"""The money and rate gates, and the usage log.

Both counters live in SQLite so they survive a restart. Both are written inside
an explicit transaction so two concurrent requests cannot both read the same
pre-increment value.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

# India never observes DST, so a fixed +05:30 offset is exact and needs no tz
# database on the host. Billing months are local months, not UTC ones.
IST = timezone(timedelta(hours=5, minutes=30))


def current_month(now: datetime | None = None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).strftime("%Y-%m")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def admit(conn: sqlite3.Connection, token_id: str, limit: int, window: float, now: float) -> bool:
    """Sliding window. True if this call is admitted and has been recorded.

    Rejected calls are not recorded, so a client that hammers the endpoint does
    not extend its own lockout past the advertised one minute.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM rate_events WHERE at < ?", (now - window,))
        (used,) = conn.execute(
            "SELECT COUNT(*) FROM rate_events WHERE token_id = ? AND at >= ?",
            (token_id, now - window),
        ).fetchone()
        if used >= limit:
            conn.execute("COMMIT")
            return False
        conn.execute("INSERT INTO rate_events (token_id, at) VALUES (?, ?)", (token_id, now))
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


def spent_paise(conn: sqlite3.Connection, token_id: str, month: str) -> int:
    row = conn.execute(
        "SELECT spent_paise FROM spend WHERE token_id = ? AND month = ?", (token_id, month)
    ).fetchone()
    return row["spent_paise"] if row else 0


def log_usage(conn: sqlite3.Connection, token_id: str | None, status: int,
              cost_paise: int = 0) -> None:
    conn.execute(
        "INSERT INTO usage_log (token_id, ts, status, cost_paise) VALUES (?, ?, ?, ?)",
        (token_id, utc_now_iso(), status, cost_paise),
    )


def record_call(conn: sqlite3.Connection, token_id: str, month: str, cost_paise: int) -> None:
    """Add the cost to the month and write the usage row in one transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO spend (token_id, month, spent_paise) VALUES (?, ?, ?)"
            " ON CONFLICT(token_id, month) DO UPDATE SET spent_paise = spent_paise + excluded.spent_paise",
            (token_id, month, cost_paise),
        )
        conn.execute(
            "INSERT INTO usage_log (token_id, ts, status, cost_paise) VALUES (?, ?, 200, ?)",
            (token_id, utc_now_iso(), cost_paise),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
