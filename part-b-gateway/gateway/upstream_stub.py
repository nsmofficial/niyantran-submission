"""Stand-in for Niyantran AI.

Same wire shape as the real RAG service, ~2s of latency, no LLM and no key. The
gateway talks to it over an ASGI transport, i.e. a genuine request/response
cycle, so pointing at the real service is a change of base_url and nothing else.
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI

ANSWER = (
    "Under the 2024 delimitation notification the constituency boundary is unchanged. "
    "Both cited sources give the electorate as 2.41 lakh as of the January 2026 roll."
)
CITATIONS = ["d01", "d05"]
COST_INR = 4.50


def build(latency_seconds: float) -> FastAPI:
    app = FastAPI(title="Niyantran AI (stub)")

    @app.post("/ask")
    async def ask(payload: dict) -> dict:  # noqa: ARG001 - the stub ignores the question
        await asyncio.sleep(latency_seconds)
        return {"answer": ANSWER, "citations": list(CITATIONS), "cost_inr": COST_INR}

    return app
