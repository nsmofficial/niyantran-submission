"""Tests for the guardrail gateway.

The first five classes are the ones the assignment asks for. The rest exist
because each of them is a bug I have either shipped or reviewed before.
"""

from __future__ import annotations

import concurrent.futures as futures
import os
import pathlib
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import app as app_module
from gateway import config, db, gates, tokens
from tests.conftest import auth

QUESTION = {"question": "What is the electorate of this constituency?"}


# --------------------------------------------------------------------------- #
# 1. A valid call succeeds and is logged
# --------------------------------------------------------------------------- #

def test_valid_call_returns_the_grounded_shape(client, mint):
    _, raw = mint()
    r = client.post("/ask", json=QUESTION, headers=auth(raw))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"answer", "citations", "cost_inr"}
    assert body["citations"] == ["d01", "d05"]  # passed through, not dropped
    assert body["cost_inr"] == 4.50
    assert body["answer"]


def test_valid_call_is_logged_without_the_question(client, mint, rows):
    token_id, raw = mint()
    client.post("/ask", json=QUESTION, headers=auth(raw))
    log = rows("SELECT * FROM usage_log")
    assert len(log) == 1
    assert log[0]["token_id"] == token_id
    assert log[0]["status"] == 200
    assert log[0]["cost_paise"] == 450
    assert log[0]["ts"]
    assert "question" not in log[0]


# --------------------------------------------------------------------------- #
# 2. 401
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "headers",
    [
        {},                                       # no header at all
        {"Authorization": ""},                    # empty
        {"Authorization": "Bearer"},              # scheme only
        {"Authorization": "Bearer "},             # scheme and nothing else
        {"Authorization": "niy_something"},       # no scheme
        {"Authorization": "Basic niy_something"}, # wrong scheme
        {"Authorization": "Bearer niy_unknown"},  # well formed, not issued
    ],
)
def test_bad_credentials_are_401(client, headers):
    r = client.post("/ask", json=QUESTION, headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_revoked_token_is_401(client, mint, settings):
    token_id, raw = mint()
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200
    conn = db.connect(settings.db_path)
    try:
        assert tokens.revoke(conn, token_id) is True
    finally:
        conn.close()
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 401


def test_sql_injection_in_the_header_is_just_an_unknown_token(client, mint, rows):
    """The junior's PR authenticates on this input. Ours must not, and must not
    lose the token table either."""
    mint()
    for payload in ["' OR '1'='1", "' OR 1=1 --", "x'; DROP TABLE tokens; --", "%", "_"]:
        r = client.post("/ask", json=QUESTION, headers={"Authorization": f"Bearer {payload}"})
        assert r.status_code == 401, payload
    assert len(rows("SELECT id FROM tokens")) == 1


def test_bearer_is_a_prefix_not_a_substring(client, mint):
    """`auth.replace("Bearer ", "")` would accept this; a prefix parse must not."""
    _, raw = mint()
    r = client.post("/ask", json=QUESTION, headers={"Authorization": f"Bearer Bearer {raw}"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# 3. 429 on the sixth call in a minute
# --------------------------------------------------------------------------- #

def test_sixth_call_in_a_minute_is_429(client, mint, settings):
    assert settings.rate_limit_per_minute == 5
    assert settings.rate_window_seconds == 60.0
    _, raw = mint()
    for i in range(5):
        assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200, i
    r = client.post("/ask", json=QUESTION, headers=auth(raw))
    assert r.status_code == 429
    assert r.headers["retry-after"] == "60"
    assert "5 calls per minute" in r.json()["detail"]


def test_rate_limit_is_per_token(client, mint):
    _, first = mint()
    _, second = mint()
    for _ in range(5):
        client.post("/ask", json=QUESTION, headers=auth(first))
    assert client.post("/ask", json=QUESTION, headers=auth(first)).status_code == 429
    assert client.post("/ask", json=QUESTION, headers=auth(second)).status_code == 200


def test_the_window_slides(settings, mint):
    """Driven at the gate directly with explicit timestamps: a wall-clock version
    of this test would either sleep for a minute or be flaky."""
    token_id, _ = mint()
    conn = db.connect(settings.db_path)
    try:
        t0 = 1_000_000.0
        for i in range(5):
            assert gates.admit(conn, token_id, 5, 60.0, t0 + i) is True
        assert gates.admit(conn, token_id, 5, 60.0, t0 + 5) is False    # sixth, still inside
        assert gates.admit(conn, token_id, 5, 60.0, t0 + 61) is True    # first has aged out
    finally:
        conn.close()


def test_rate_limit_survives_a_restart(settings, mint):
    """The in-memory dict in the junior's PR is reset by every deploy."""
    _, raw = mint()
    with TestClient(app_module.create_app(settings)) as first:
        for _ in range(5):
            assert first.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200
    with TestClient(app_module.create_app(settings)) as second:
        assert second.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 429


# --------------------------------------------------------------------------- #
# 4. 402 once the budget is spent
# --------------------------------------------------------------------------- #

def test_402_once_the_month_is_spent(client, mint):
    # Budget 9.00, cost 4.50 a call: two calls land exactly on the cap.
    _, raw = mint(budget_inr=9.0)
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200
    r = client.post("/ask", json=QUESTION, headers=auth(raw))
    assert r.status_code == 402


def test_the_cap_triggers_on_reaching_not_exceeding(client, mint):
    """'Once the month's accumulated cost_inr reaches it' — so >=, not >."""
    _, raw = mint(budget_inr=4.50)
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 402


def test_402_message_says_what_to_do(client, mint):
    _, raw = mint(budget_inr=4.50)
    client.post("/ask", json=QUESTION, headers=auth(raw))
    detail = client.post("/ask", json=QUESTION, headers=auth(raw)).json()["detail"]
    assert "INR 4.50" in detail        # spent
    assert "resets on the 1st" in detail
    assert gates.current_month() in detail


def test_spend_accumulates_exactly(client, mint, rows):
    """Costs are converted to integer paise at the edge. Summing rupees as floats
    drifts for ordinary values, and the cap is an equality comparison."""
    _, raw = mint(budget_inr=1000.0)
    for _ in range(5):
        assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200
    assert rows("SELECT spent_paise FROM spend")[0]["spent_paise"] == 2250


def test_a_previous_month_does_not_count_against_this_one(client, mint, settings):
    token_id, raw = mint(budget_inr=4.50)
    conn = db.connect(settings.db_path)
    try:
        conn.execute("INSERT INTO spend (token_id, month, spent_paise) VALUES (?, '2020-01', 99999)",
                     (token_id,))
    finally:
        conn.close()
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200


# --------------------------------------------------------------------------- #
# 5. The spend survives a restart
# --------------------------------------------------------------------------- #

def test_spend_survives_a_restart(settings, mint, rows):
    _, raw = mint(budget_inr=9.0)

    with TestClient(app_module.create_app(settings)) as first:
        assert first.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200
        assert first.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200

    # Same SQLite file, brand new app object: this is what a process restart
    # looks like from the database's point of view.
    with TestClient(app_module.create_app(settings)) as second:
        r = second.post("/ask", json=QUESTION, headers=auth(raw))
        assert r.status_code == 402

    assert rows("SELECT spent_paise FROM spend")[0]["spent_paise"] == 900


# --------------------------------------------------------------------------- #
# Storage: what a database leak would actually hand over
# --------------------------------------------------------------------------- #

def _all_bytes(path):
    blob = b""
    for suffix in ["", "-wal", "-shm"]:
        p = path.with_name(path.name + suffix)
        if p.exists():
            blob += p.read_bytes()
    return blob


def test_only_a_hash_of_the_token_is_stored(client, mint, settings, rows):
    _, raw = mint()
    client.post("/ask", json=QUESTION, headers=auth(raw))

    stored = rows("SELECT * FROM tokens")[0]
    assert "raw_token" not in stored
    assert raw not in stored.values()
    assert stored["token_hash"] == tokens.hash_token(raw, settings.pepper)
    assert raw.encode() not in _all_bytes(settings.db_path)


def test_the_question_is_never_written_to_disk(client, mint, settings):
    _, raw = mint()
    secret = "who funded the opposition candidate in ward 14"
    client.post("/ask", json={"question": secret}, headers=auth(raw))

    columns = {c[1] for c in db.connect(settings.db_path).execute("PRAGMA table_info(usage_log)")}
    assert "question" not in columns
    assert secret.encode() not in _all_bytes(settings.db_path)


def test_a_different_pepper_invalidates_every_token(settings, mint):
    """The pepper lives outside the database, so rotating it is the panic lever."""
    _, raw = mint()
    rotated = config.Settings(db_path=settings.db_path, pepper="rotated", stub_latency_seconds=0.0)
    with TestClient(app_module.create_app(rotated)) as c:
        assert c.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 401


# --------------------------------------------------------------------------- #
# Gate ordering, logging completeness, and the upstream deadline
# --------------------------------------------------------------------------- #

def test_auth_is_checked_before_the_budget(client, mint, settings):
    """A revoked token that is also over budget must read as 401, not 402."""
    token_id, raw = mint(budget_inr=4.50)
    client.post("/ask", json=QUESTION, headers=auth(raw))       # spends the budget
    conn = db.connect(settings.db_path)
    try:
        tokens.revoke(conn, token_id)
    finally:
        conn.close()
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 401


def test_the_rate_gate_is_checked_before_the_budget(client, mint):
    """A throttled caller must not be told to top up; it must be told to slow down."""
    _, raw = mint(budget_inr=4.50)
    client.post("/ask", json=QUESTION, headers=auth(raw))       # 200, budget now spent
    for _ in range(4):
        assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 402
    assert client.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 429


def test_every_outcome_writes_exactly_one_row(client, mint, rows):
    # Ordered so each gate is the one that fires: the body is only parsed once
    # auth, rate and budget have all passed.
    _, raw = mint(budget_inr=4.50)
    client.post("/ask", content=b"not json", headers=auth(raw))                    # 400
    client.post("/ask", json=QUESTION, headers=auth(raw))                          # 200
    client.post("/ask", json=QUESTION, headers={"Authorization": "Bearer niy_x"})  # 401
    client.post("/ask", json=QUESTION, headers=auth(raw))                          # 402

    log = rows("SELECT status, token_id, cost_paise FROM usage_log ORDER BY id")
    assert [r["status"] for r in log] == [400, 200, 401, 402]
    assert log[2]["token_id"] is None          # an unknown credential has no identity
    assert [r["cost_paise"] for r in log] == [0, 450, 0, 0]


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"question": ""}', b'{"question": 7}'])
def test_a_bad_body_is_400_not_500(client, mint, body):
    _, raw = mint()
    assert client.post("/ask", content=body, headers=auth(raw)).status_code == 400


def test_an_oversized_question_is_rejected_before_it_costs_anything(client, mint, rows):
    """Just over the character cap but under the byte cap: a 400, not a 413."""
    _, raw = mint()
    r = client.post("/ask", json={"question": "x" * 2500}, headers=auth(raw))
    assert r.status_code == 400
    assert rows("SELECT * FROM spend") == []


def test_an_oversized_body_is_413_and_is_never_buffered(client, mint, rows, settings):
    """A 40 MB body must be refused on the declared length, not read and then
    measured. Anything else lets a caller decide how much memory we hold."""
    _, raw = mint()
    r = client.post("/ask", content=b'{"question":"' + b"x" * 40_000_000 + b'"}',
                    headers={**auth(raw), "content-type": "application/json"})
    assert r.status_code == 413
    assert str(settings.max_body_bytes) in r.json()["detail"]
    assert rows("SELECT status FROM usage_log") == [{"status": 413}]


def test_a_lying_content_length_is_still_cut_off(client, mint):
    """No declared length at all (chunked upload): the read itself must stop."""
    _, raw = mint()

    def chunks():
        for _ in range(200):
            yield b"x" * 1000

    r = client.post("/ask", content=chunks(),
                    headers={**auth(raw), "content-type": "application/json"})
    assert r.status_code == 413


def test_the_schema_browser_is_not_exposed(client):
    """This service is meant for a public URL; /docs is surface with no upside."""
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert client.get(path).status_code == 404, path


def test_a_multibyte_question_within_the_character_cap_is_accepted(client, mint):
    """2000 Devanagari characters is ~6 KB of UTF-8, so the byte cap must not
    reject a question that is legal by the character cap."""
    _, raw = mint()
    r = client.post("/ask", json={"question": "क्षेत्र" * 280}, headers=auth(raw))
    assert r.status_code == 200


def test_a_failing_upstream_is_502_and_is_not_billed(settings, mint, rows):
    """An upstream error must not read as a 500, and must not be charged for."""
    _, raw = mint()
    application = app_module.create_app(settings)
    with TestClient(application) as c:
        application.state.upstream = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
            base_url="http://niyantran-ai",
        )
        r = c.post("/ask", json=QUESTION, headers=auth(raw))
    assert r.status_code == 502
    assert rows("SELECT * FROM spend") == []
    assert rows("SELECT status, cost_paise FROM usage_log") == [{"status": 502, "cost_paise": 0}]


def test_a_slow_upstream_is_504_and_is_not_billed(settings, mint, rows):
    """The bug the junior's PR notices in a comment and leaves in place."""
    slow = config.Settings(db_path=settings.db_path, stub_latency_seconds=0.5,
                           upstream_timeout_seconds=0.05)
    _, raw = mint()
    with TestClient(app_module.create_app(slow)) as c:
        r = c.post("/ask", json=QUESTION, headers=auth(raw))
    assert r.status_code == 504
    assert rows("SELECT * FROM spend") == []
    assert rows("SELECT status, cost_paise FROM usage_log") == [{"status": 504, "cost_paise": 0}]


# --------------------------------------------------------------------------- #
# Concurrency: the counters are shared state, so prove they are transactional
# --------------------------------------------------------------------------- #

def test_concurrent_callers_cannot_exceed_the_rate_limit(settings, mint):
    token_id, _ = mint()
    now = time.time()

    def attempt(_):
        conn = db.connect(settings.db_path)
        try:
            return gates.admit(conn, token_id, 5, 60.0, now)
        finally:
            conn.close()

    with futures.ThreadPoolExecutor(max_workers=12) as pool:
        admitted = list(pool.map(attempt, range(40)))
    assert sum(admitted) == 5


def test_concurrent_spend_is_not_lost(settings, mint):
    token_id, _ = mint()
    month = gates.current_month()

    def add(_):
        conn = db.connect(settings.db_path)
        try:
            gates.record_call(conn, token_id, month, 450)
        finally:
            conn.close()

    with futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(add, range(40)))

    conn = db.connect(settings.db_path)
    try:
        assert gates.spent_paise(conn, token_id, month) == 40 * 450
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The token script, end to end
# --------------------------------------------------------------------------- #

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run_cli(settings, *args):
    # cwd and env are set explicitly so the suite passes from any working
    # directory, not just the repository root.
    return subprocess.run(
        [sys.executable, "-m", "gateway.tokens", *args],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        env={**os.environ,
             "NIYANTRAN_DB": str(settings.db_path),
             "NIYANTRAN_TOKEN_PEPPER": settings.pepper},
    )


def test_the_create_script_prints_a_usable_token_once(settings):
    out = _run_cli(settings, "create", "--budget", "100", "--label", "terminal").stdout
    token_id = next(l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("token id"))
    raw = next(l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("token    "))
    assert raw.startswith("niy_")
    assert "only time" in out

    with TestClient(app_module.create_app(settings)) as c:
        assert c.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 200

    _run_cli(settings, "revoke", token_id)
    with TestClient(app_module.create_app(settings)) as c:
        assert c.post("/ask", json=QUESTION, headers=auth(raw)).status_code == 401
