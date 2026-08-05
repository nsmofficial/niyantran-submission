# Niyantran guardrail gateway (Part B)

A working version of the money and credential gates from the Part A memo: a small
FastAPI service that stands in front of Niyantran AI and refuses to let a machine
credential authenticate loosely, call too often, or spend past its budget.

No LLM, no API keys, no external services. The upstream is a stub that waits ~2s
and returns the real answer shape.

## Run it (about two minutes)

```bash
python -m venv .venv
. .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Mint two tokens. Each is printed once and only its hash is stored:

```bash
python -m gateway.tokens create --budget 100  --label demo-main
python -m gateway.tokens create --budget 4.50 --label demo-tight
```

Start the gateway (leave it running):

```bash
python -m gateway
```

In a second terminal, walk every outcome — a grounded answer, 401, 402, a
client-side timeout, and 429 — with the two tokens you just minted:

```bash
node client/demo.mjs --main <token> --tight <token>
```

It takes about ten seconds and prints distinct handling for each case. Node 18+,
no npm install.

Tests, from any working directory:

```bash
python -m pytest -q
```

42 tests, about 40 seconds. Developed on Python 3.14; the code avoids anything
newer than 3.10, but 3.10–3.13 are untested here.

To see what the gateway recorded — one row per request, and no question text:

```bash
python -c "import sqlite3; [print(r) for r in sqlite3.connect('niyantran_gateway.sqlite3').execute('SELECT id, token_id, ts, status, cost_paise FROM usage_log ORDER BY id')]"
```

Same trick lists tokens, if you need an id to revoke and no longer have the printout:

```bash
python -c "import sqlite3; [print(r) for r in sqlite3.connect('niyantran_gateway.sqlite3').execute('SELECT id, label, monthly_budget_paise, revoked_at FROM tokens')]"
```

## What the gateway does

`POST /ask`, guarded by three gates that run in this order:

| Gate | Failure | Why it is where it is |
| --- | --- | --- |
| Bearer token | `401` | Unknown *or revoked*. Runs before the body is read, so we never parse input from an unauthenticated caller. |
| Rate limit | `429` | 5 calls per rolling minute per token. An unknown caller cannot be rate limited, so this must follow auth. |
| Monthly cap | `402` | Per-token rupee budget. A throttled caller should be told to slow down, not to top up — so this follows the rate gate. |

Then the body is read, capped at 8 KB while reading (`413`) and validated
(`400`); an upstream failure is `502` and an upstream timeout `504`. Neither of
the last two is billed. There is no `/docs`, `/redoc` or `/openapi.json` — this
service is designed for a public URL and a schema browser is surface with no
upside.

A success returns the upstream answer unchanged, **including its citations**:

```json
{"answer": "...", "citations": ["d01", "d05"], "cost_inr": 4.5}
```

Every request writes exactly one `usage_log` row — token id, timestamp, status,
cost — including the ones that never reached the upstream.

## Design notes

**Tokens are stored as `HMAC-SHA256(pepper, token)` and nothing else.** If the
database leaks, the attacker holds hashes of secrets they still cannot present,
and nothing has to be rotated in a panic. HMAC rather than bcrypt or argon2 on
purpose: those salt per row, so every lookup would have to try every row, and
their slowness buys nothing against a 256-bit random secret. The pepper is read
from `NIYANTRAN_TOKEN_PEPPER` and never written to the database — **set it in
production**; the default is a development placeholder. Rotating it invalidates
every issued token, which is the deliberate emergency lever.

**All three counters live in SQLite**, not in a dict. The spend counter, the
rate-limit window and the usage log are in one file, so `python -m gateway`,
kill, restart, and the month's spend and the current minute's window are both
still there. An in-process counter is reset by every deploy and multiplied by
the worker count — which is the live bug in the Appendix B code. To see that the
counters really are shared rather than per-process, run it with more than one:

```bash
uvicorn gateway.app:create_default_app --factory --workers 4 --port 8000
```

Five calls a minute stays five across all four workers.

**Money is stored as integer paise.** Rupee floats drift when accumulated, and
the cap is an equality-sensitive comparison: the assignment says the gate closes
once spend *reaches* the budget, so `>=`, not `>`.

**Months are IST calendar months.** India never observes DST, so a fixed +05:30
offset is exact and needs no tz database on the host. Billing months are local
months, not UTC ones.

**The cap can overshoot by one call.** The gate checks the balance before the
call, but the cost is only known after it. The overshoot is bounded by the number
of calls in flight, which the rate gate already caps at five — worst case about
₹22 past a ₹100 budget. Reserving an estimate up front and reconciling afterwards
would close that; it is not worth the complexity at this budget size, and it is
a deliberate choice rather than an oversight.

**The question text is never stored.** There is no column for it, and no log line
carries it. A political-data company that keeps a record of what its clients
asked has built a subpoena target and a leak that ends the client; the only
version of that data that cannot leak is the one we do not have. Cost and status
are everything billing needs.

**The upstream call has a deadline** (`asyncio.wait_for`, 10s) and is the only
thing awaited in the request. A timeout is a `504` and an upstream failure is a
`502`; neither is billed, and both are logged. The database connection is opened
per request and never held across the upstream call.

**The public-sources-only boundary is deliberately not in this gateway.** It
belongs in Niyantran AI's retrieval layer, which is stubbed here. A source-type
filter bolted onto the gateway would be exactly the "discouraged rather than
impossible" design the memo argues against, so implementing it here would
contradict Part A. See the memo for where it does belong.

## Layout

```
gateway/config.py         settings, all overridable for tests
gateway/db.py             schema and connections; the comments explain the columns
gateway/tokens.py         mint, hash, look up, revoke — and the CLI
gateway/gates.py          the rate window and the spend counter, both transactional
gateway/upstream_stub.py  Niyantran AI stand-in: ~2s, canned grounded answer
gateway/app.py            the gateway itself
client/demo.mjs           Terminal-side client, walks all five outcomes
tests/test_gateway.py     42 tests
```

The gateway reaches the stub over an ASGI transport — a real request/response
cycle against a separate app — so pointing at the live service is a change of
`base_url` in `create_app` and nothing else.

## Environment

All optional; the defaults run out of the box.

| Variable | Default | |
| --- | --- | --- |
| `NIYANTRAN_DB` | `./niyantran_gateway.sqlite3` | where the durable state lives |
| `NIYANTRAN_TOKEN_PEPPER` | dev placeholder | **set this in production** |
| `NIYANTRAN_RATE_LIMIT` | `5` | calls per minute per token |
| `NIYANTRAN_DEFAULT_BUDGET_INR` | `100` | default for `tokens create` |
| `NIYANTRAN_STUB_LATENCY` | `2.0` | set `0` for a fast demo, but the client timeout case then needs a shorter client deadline |
| `NIYANTRAN_UPSTREAM_TIMEOUT` | `10.0` | deadline on the call to Niyantran AI |

## Not built, on purpose

No admin API, no JWTs, no Redis, no Docker, no metrics endpoint, no retries. The
assignment asked for three gates, a usage log, a client and tests; everything
here is one of those or a direct correctness consequence of one.
