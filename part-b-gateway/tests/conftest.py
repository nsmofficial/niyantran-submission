from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gateway import app as app_module
from gateway import config, db, tokens


@pytest.fixture
def settings(tmp_path):
    # stub_latency_seconds=0 so the suite is fast; the 2s default only exists so
    # the JS client can demonstrate a client-side timeout.
    return config.Settings(db_path=tmp_path / "gateway.sqlite3", stub_latency_seconds=0.0)


@pytest.fixture
def client(settings):
    # TestClient must be used as a context manager or the lifespan never runs and
    # the upstream client is never created.
    with TestClient(app_module.create_app(settings)) as c:
        yield c


@pytest.fixture
def mint(settings):
    """Mint a token straight into the database. Returns (token_id, raw_token)."""

    def _mint(budget_inr: float = 100.0, label: str = "test"):
        db.init(settings.db_path)
        conn = db.connect(settings.db_path)
        try:
            return tokens.create(conn, budget_inr=budget_inr, pepper=settings.pepper, label=label)
        finally:
            conn.close()

    return _mint


@pytest.fixture
def rows(settings):
    """Read helper for asserting on what the gateway persisted."""

    def _rows(sql: str, params: tuple = ()):
        conn = db.connect(settings.db_path)
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    return _rows


def auth(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}
