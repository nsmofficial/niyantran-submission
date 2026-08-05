"""The guardrail gateway.

One public endpoint, `POST /ask`, with three gates in front of Niyantran AI:

    401  unknown or revoked token
    429  more than N calls in the last minute for this token
    402  this token's monthly rupee budget is spent

The gates run in that order on purpose: an unknown caller cannot be rate limited,
and a throttled caller should not be allowed to spend. Auth also runs before the
request body is parsed, so we never process input from an unauthenticated caller.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from . import config, db, gates, tokens, upstream_stub


def _bearer(header: str | None) -> str | None:
    """Strict prefix parse. `str.replace("Bearer ", "")` would strip the word
    anywhere in the value and would turn a missing header into an empty token."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _inr(paise: int) -> str:
    return f"INR {paise / 100:.2f}"


def create_app(settings: config.Settings) -> FastAPI:
    db.init(settings.db_path)
    upstream = upstream_stub.build(settings.stub_latency_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        transport = httpx.ASGITransport(app=upstream)
        async with httpx.AsyncClient(transport=transport, base_url="http://niyantran-ai") as client:
            app.state.upstream = client
            yield

    # No /docs, /redoc or /openapi.json. This service is meant to sit on a public
    # URL, and an interactive schema browser is attack surface we get nothing from.
    app = FastAPI(title="Niyantran guardrail gateway", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/ask")
    async def ask(request: Request) -> dict:
        conn = db.connect(settings.db_path)
        try:
            raw = _bearer(request.headers.get("authorization"))
            row = tokens.lookup(conn, raw, settings.pepper) if raw else None
            if row is None or row["revoked_at"] is not None:
                # No token id to log: an unknown credential has no identity here.
                gates.log_usage(conn, row["id"] if row else None, 401)
                raise HTTPException(
                    401, "Unknown or revoked token.", headers={"WWW-Authenticate": "Bearer"}
                )
            token_id = row["id"]

            if not gates.admit(conn, token_id, settings.rate_limit_per_minute,
                               settings.rate_window_seconds, time.time()):
                gates.log_usage(conn, token_id, 429)
                raise HTTPException(
                    429,
                    f"Rate limit exceeded: {settings.rate_limit_per_minute} calls per minute "
                    f"per token. Retry in under a minute.",
                    headers={"Retry-After": str(int(settings.rate_window_seconds))},
                )

            month = gates.current_month()
            spent = gates.spent_paise(conn, token_id, month)
            budget = row["monthly_budget_paise"]
            if spent >= budget:
                gates.log_usage(conn, token_id, 402)
                raise HTTPException(
                    402,
                    f"Monthly budget exhausted for token {token_id}: {_inr(spent)} of "
                    f"{_inr(budget)} spent in {month}. The counter resets on the 1st (IST); "
                    f"raise the budget to continue sooner.",
                )

            question = await _question(request, conn, token_id, settings)

            try:
                # The one slow call in the request, and the only thing awaited.
                # asyncio.wait_for, not the httpx timeout, because the deadline has
                # to hold for any transport we point this client at.
                response = await asyncio.wait_for(
                    request.app.state.upstream.post("/ask", json={"question": question}),
                    timeout=settings.upstream_timeout_seconds,
                )
            except (asyncio.TimeoutError, httpx.TimeoutException):
                gates.log_usage(conn, token_id, 504)
                raise HTTPException(504, "Niyantran AI did not answer in time.")

            if response.status_code != 200:
                # Never bill for an answer we did not get, and never let an
                # upstream failure surface as an unexplained 500.
                gates.log_usage(conn, token_id, 502)
                raise HTTPException(502, "Niyantran AI returned an error.")

            answer = response.json()
            cost_paise = round(float(answer["cost_inr"]) * 100)
            gates.record_call(conn, token_id, month, cost_paise)
            # Citations are passed straight through: grounded, cited answers are
            # the reason the Terminal calls Niyantran AI instead of an LLM.
            return {
                "answer": answer["answer"],
                "citations": answer["citations"],
                "cost_inr": cost_paise / 100,
            }
        finally:
            conn.close()

    return app


def create_default_app() -> FastAPI:
    """Entry point for `uvicorn gateway.app:create_default_app --factory`, which
    is how you run more than one worker — and how you see that the counters are
    genuinely shared rather than per-process."""
    return create_app(config.from_env())


async def _read_capped(request: Request, limit: int) -> bytes:
    """Read the body, refusing to buffer more than `limit` bytes.

    Checking the length after reading would mean a caller could still make us
    hold an arbitrary amount of their data in memory first. The declared length
    is checked when present, and the stream is cut off regardless in case it
    is absent or lying.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _TooLarge
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise _TooLarge
    return bytes(body)


class _TooLarge(Exception):
    pass


async def _question(request: Request, conn, token_id: str, settings: config.Settings) -> str:
    """Parse and bound the body. An uncapped question is an uncapped bill."""
    try:
        raw = await _read_capped(request, settings.max_body_bytes)
    except _TooLarge:
        gates.log_usage(conn, token_id, 413)
        raise HTTPException(413, f"Body must be under {settings.max_body_bytes} bytes.")
    try:
        question = json.loads(raw)["question"]
        if not isinstance(question, str) or not question.strip():
            raise ValueError
        if len(question) > settings.max_question_chars:
            raise ValueError
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        gates.log_usage(conn, token_id, 400)
        raise HTTPException(
            400, f"Body must be JSON with a 'question' string of 1-"
                 f"{settings.max_question_chars} characters.")
    return question
