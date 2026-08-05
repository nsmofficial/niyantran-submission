// Terminal-side client for the guardrail gateway.
//
//   node client/demo.mjs --main <token> --tight <token> [--url http://127.0.0.1:8000]
//
// Walks the five outcomes the Terminal has to survive in production: a grounded
// answer, 401, 402, 429, and a client-side timeout. No dependencies; Node 18+.

const CLIENT_TIMEOUT_MS = 900; // deliberately under the ~2s the AI takes

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const BASE = arg("url", process.env.NIY_URL || "http://127.0.0.1:8000");
const MAIN = arg("main", process.env.NIY_TOKEN_MAIN);
const TIGHT = arg("tight", process.env.NIY_TOKEN_TIGHT);

if (!MAIN || !TIGHT) {
  console.error(
    "Need two tokens. Mint them with:\n" +
      "  python -m gateway.tokens create --budget 100  --label demo-main\n" +
      "  python -m gateway.tokens create --budget 4.50 --label demo-tight\n" +
      "then: node client/demo.mjs --main <token> --tight <token>",
  );
  process.exit(2);
}

const QUESTION = "What is the electorate of this constituency?";

/** One call. Returns a tagged result rather than throwing, so the caller can
 *  branch on the outcome the same way a UI would. */
async function ask(token, { timeoutMs = 15000 } = {}) {
  let response;
  try {
    response = await fetch(`${BASE}/ask`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify({ question: QUESTION }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (err) {
    if (err.name === "TimeoutError" || err.name === "AbortError") {
      return { kind: "timeout", after: timeoutMs };
    }
    return { kind: "network", message: err.message };
  }
  const body = await response.json().catch(() => ({}));
  if (response.ok) return { kind: "answer", ...body };
  return {
    kind: String(response.status),
    detail: body.detail ?? response.statusText,
    retryAfter: response.headers.get("retry-after"),
  };
}

/** Each outcome gets its own handling. A single catch-all here is how a client
 *  ends up retrying a 402 forever or telling a user to "try again" on a 401. */
function render(result) {
  switch (result.kind) {
    case "answer":
      console.log("  Niyantran AI:");
      for (const line of wrap(result.answer, 76)) console.log(`    ${line}`);
      const cites = result.citations ?? [];
      console.log(`    Sources: ${cites.length ? cites.map((c) => `[${c}]`).join(" ") : "none"}`);
      console.log(`    Cost this call: INR ${Number(result.cost_inr ?? 0).toFixed(2)}`);
      return;
    case "401":
      console.log("  Not authorised. The token is unknown or has been revoked.");
      console.log("  Action: stop retrying and mint a replacement token. Retrying cannot help.");
      return;
    case "429":
      console.log(`  Throttled. ${result.detail}`);
      console.log(`  Action: back off for ${result.retryAfter ?? 60}s, then retry the same request.`);
      return;
    case "402":
      console.log(`  Budget exhausted. ${result.detail}`);
      console.log("  Action: this is a spend cap doing its job, not a fault. Surface it to an");
      console.log("          operator; do not retry, and do not fall back to a direct LLM call.");
      return;
    case "timeout":
      console.log(`  No response within ${result.after}ms; the request was aborted here.`);
      console.log("  Action: the server may still have finished and billed the call, so any");
      console.log("          retry has to be treated as a possible duplicate, not a free one.");
      return;
    default:
      console.log(`  Transport error: ${result.message}`);
  }
}

function wrap(text, width) {
  const out = [];
  let line = "";
  for (const word of text.split(/\s+/)) {
    if ((line + " " + word).trim().length > width) {
      out.push(line.trim());
      line = word;
    } else line += " " + word;
  }
  if (line.trim()) out.push(line.trim());
  return out;
}

function step(n, title) {
  console.log(`\n[${n}] ${title}`);
}

const main = async () => {
  console.log(`Gateway: ${BASE}  (this takes about 10 seconds)`);

  step(1, "A grounded answer with its citations");
  render(await ask(MAIN));

  step(2, "401 - a token that was never issued");
  render(await ask("niy_this-token-does-not-exist"));

  step(3, "402 - a token whose monthly budget runs out mid-session");
  render(await ask(TIGHT));
  render(await ask(TIGHT));

  // The timeout case runs before the burst: it needs a rate-limit slot of its own.
  step(4, `client-side timeout - we give up after ${CLIENT_TIMEOUT_MS}ms`);
  render(await ask(MAIN, { timeoutMs: CLIENT_TIMEOUT_MS }));

  step(5, "429 - a burst past 5 calls a minute");
  const burst = await Promise.all([1, 2, 3, 4, 5].map(() => ask(MAIN)));
  const throttled = burst.filter((r) => r.kind === "429").length;
  console.log(`  ${burst.length - throttled} admitted, ${throttled} throttled.`);
  render(burst.find((r) => r.kind === "429") ?? burst[0]);

  console.log("");
};

main().catch((err) => {
  console.error(`\nCould not reach ${BASE}. Is the gateway running?\n  ${err.message}`);
  process.exit(1);
});
