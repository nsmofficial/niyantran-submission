# Niyantran — Tech Lead take-home

**Submitted by:** Sahil Navadiya

| | Deliverable | |
| --- | --- | --- |
| **Part A** | [`part-a-memo.md`](part-a-memo.md) · [PDF](part-a-memo.pdf) | Architecture decision memo — 2 pages |
| **Part B** | [`part-b-gateway/`](part-b-gateway/) | Working guardrail gateway — see its [README](part-b-gateway/README.md) |
| **Part C** | [`part-c-review.md`](part-c-review.md) · [PDF](part-c-review.pdf) | Review of the Appendix B pull request — 1 page |

To see Part B working in about two minutes, follow
[part-b-gateway/README.md](part-b-gateway/README.md): mint two tokens, start the
gateway, run `node client/demo.mjs`. `python -m pytest -q` runs 42 tests from any
working directory.

The PDFs are rendered at A4, 2 cm margins, 10.5 pt body with every heading level
larger than the body text — the page counts are real, not the product of a
squeezed stylesheet.

## How the three parts line up

They are deliberately one argument, not three documents.

- The memo argues the privacy and tenancy boundary has to be **structural** — a
  database user that cannot see rows outside the credential's scope — rather than
  a condition in application code. Part C finds that condition missing from the
  junior's pull request, and Part B's README says why the gateway deliberately
  does *not* implement it.
- The memo argues the counters must survive a restart *and* be shared across
  processes. Part C flags the in-memory `RATE = {}` as a live production bug.
  Part B puts both counters in SQLite, proves durability with a test that kills
  the app and re-opens the same file, and the README shows the limit holding at
  five across four uvicorn workers.
- The memo argues tokens are stored hashed because databases leak. Part C flags
  the `raw_token` column. Part B stores an HMAC and asserts in a test that the
  raw token appears nowhere in the database file.

## Assumptions and known trade-offs

Stated inline in both the memo and the gateway README rather than hidden. The
four worth knowing before the review call:

1. **The monthly cap can overshoot by one call per token**, because the cost is
   only known after the call returns. Bounded by the rate limit, tested, and
   documented rather than quietly ignored.
2. **The public/private source filter is not in Part B.** The stub has no
   knowledge base to filter, and putting the filter in the gateway would
   contradict the memo's central argument.
3. **The token pepper defaults to a development value.** Production must set
   `NIYANTRAN_TOKEN_PEPPER`; a test proves that rotating it invalidates every
   issued token.
4. **The stub runs in-process**, reached over an ASGI transport rather than a
   second port. It is a real request/response cycle against a separate app, so
   pointing at the live service is a change of `base_url` — but you cannot
   `curl` the stub directly.
