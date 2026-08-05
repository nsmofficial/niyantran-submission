"""Settings for the guardrail gateway.

Everything is overridable so tests (and the restart test in particular) can point
a fresh app at an existing SQLite file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The pepper is read from the environment and never written to the database, so a
# stolen `tokens` table is not enough to recognise or forge a token. Rotating it
# invalidates every issued token by design; that is the emergency lever.
DEV_PEPPER = "dev-pepper-not-for-production"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    pepper: str = DEV_PEPPER
    rate_limit_per_minute: int = 5
    rate_window_seconds: float = 60.0
    default_monthly_budget_inr: float = 100.0
    # The stub answers in ~2s so the client-side timeout case is demonstrable.
    # Tests set this to 0.
    stub_latency_seconds: float = 2.0
    # The bug the junior's PR did not fix: an upstream call with no deadline.
    upstream_timeout_seconds: float = 10.0
    max_question_chars: int = 2000
    # The byte cap has to sit well above the character cap: 2000 characters of
    # Devanagari is about 6 KB in UTF-8. It is enforced while reading, so an
    # oversized body is refused instead of being buffered and then rejected.
    max_body_bytes: int = 8192


def from_env() -> Settings:
    return Settings(
        db_path=Path(os.environ.get("NIYANTRAN_DB", "niyantran_gateway.sqlite3")).resolve(),
        pepper=os.environ.get("NIYANTRAN_TOKEN_PEPPER", DEV_PEPPER),
        rate_limit_per_minute=int(os.environ.get("NIYANTRAN_RATE_LIMIT", "5")),
        default_monthly_budget_inr=float(os.environ.get("NIYANTRAN_DEFAULT_BUDGET_INR", "100")),
        stub_latency_seconds=float(os.environ.get("NIYANTRAN_STUB_LATENCY", "2.0")),
        upstream_timeout_seconds=float(os.environ.get("NIYANTRAN_UPSTREAM_TIMEOUT", "10.0")),
    )
