"""Token minting, hashing, lookup and revocation, plus the tiny CLI.

    python -m gateway.tokens create --budget 100 --label terminal
    python -m gateway.tokens revoke <token_id>
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timezone

from . import config, db

PREFIX = "niy_"


def mint() -> str:
    """A new bearer token. 256 bits of CSPRNG entropy; guessing is not a threat."""
    return PREFIX + secrets.token_urlsafe(32)


def hash_token(raw: str, pepper: str) -> str:
    # We store only this. If the database leaks, the attacker gets hashes of
    # secrets they still cannot present, and no token has to be rotated in a panic.
    # HMAC-SHA256 rather than bcrypt/argon2 on purpose: those salt per row, so
    # every lookup would have to try every row, and their slowness buys nothing
    # against a 256-bit random secret. The pepper lives outside the database.
    return hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()


def create(
    conn: sqlite3.Connection,
    *,
    budget_inr: float,
    pepper: str,
    label: str = "",
) -> tuple[str, str]:
    """Returns (token_id, raw_token). The raw token is never persisted."""
    raw = mint()
    token_id = secrets.token_hex(6)
    conn.execute(
        "INSERT INTO tokens (id, token_hash, label, monthly_budget_paise, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            token_id,
            hash_token(raw, pepper),
            label,
            round(budget_inr * 100),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    return token_id, raw


def revoke(conn: sqlite3.Connection, token_id: str) -> bool:
    cur = conn.execute(
        "UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), token_id),
    )
    return cur.rowcount > 0


def lookup(conn: sqlite3.Connection, raw: str, pepper: str) -> sqlite3.Row | None:
    # Parameterised, and by hash, so the presented value never reaches the SQL text.
    return conn.execute(
        "SELECT id, label, monthly_budget_paise, revoked_at FROM tokens WHERE token_hash = ?",
        (hash_token(raw, pepper),),
    ).fetchone()


def _main() -> None:
    settings = config.from_env()
    parser = argparse.ArgumentParser(prog="gateway.tokens")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="mint a token and print it once")
    p_create.add_argument("--budget", type=float, default=settings.default_monthly_budget_inr,
                          help="monthly budget in rupees")
    p_create.add_argument("--label", default="")

    p_revoke = sub.add_parser("revoke", help="revoke a token by id")
    p_revoke.add_argument("token_id")

    args = parser.parse_args()
    db.init(settings.db_path)
    conn = db.connect(settings.db_path)
    try:
        if args.cmd == "create":
            token_id, raw = create(conn, budget_inr=args.budget, pepper=settings.pepper,
                                   label=args.label)
            print(f"token id : {token_id}")
            print(f"token    : {raw}")
            print(f"budget   : INR {args.budget:.2f} / month")
            print(f"database : {settings.db_path}")
            print("\nCopy the token now. Only its hash is stored, so this is the only time"
                  "\nit can be displayed.")
        else:
            ok = revoke(conn, args.token_id)
            print(f"revoked {args.token_id}" if ok
                  else f"no active token with id {args.token_id}")
    finally:
        conn.close()


if __name__ == "__main__":
    _main()
