# Lessons

## Threshold gates in continuous units, executors in integer lots: silent no-op

- The 2026-05-07 paper session triggered the rehedge gate ~150 times and executed zero hedges. Every threshold-pass was followed by `_generate_hard_delta_proposals` returning `[]` — silently — because `lots = round(delta / lot_size)` rounded to 0 for any drift below 0.5 lots. Threshold was 0.15 lots; lot_size jumped from 25 → 65 (NIFTY restructuring). The gate said "go", the executor said "<1 lot, never mind", and the only log was "Hedge decision: Safe to use futures…", which read like success. Net: a held straddle bled theta with zero gamma scalping (today's −₹1,358).
- This is the same shape as the 2026-05-04 bare-except / numeric-fallback incident: a permitted-but-meaningless no-op silently masquerades as a working code path, and downstream metrics (`rehedge_count`, `gamma_scalp_pnl`) keep incrementing as if work was done.
- Rules: (1) any function whose contract is "produce executable side effects" must log when it produces none — silent `return []` is forbidden in execution paths. (2) thresholds expressed in fractional units must be ≥ the minimum unit the executor can actually trade; pick the threshold to make `round(threshold) >= 1`. (3) when a strategy parameter has lot/sizing implications, surface the executable lot size in the optimizer's search space (synthetic data must use the live `lot_size`, not a default of 25).
- Optimizer-blindness corollary: `rehedge_count` increments at the *bottom* of `check_and_rehedge`, before sizing filters proposals. `gamma_scalp_pnl` accumulates `0.5·γ·dS²` (an estimate, not realized P/L). A param set that produces zero hedges looks fine to the search. Re-tuned param search must score on a metric tied to executions, not intentions.

## Tailwind breakpoints

- Default breakpoints are `sm` (640), `md` (768), `lg` (1024), `xl` (1280), `2xl` (1536). There is **no `xs:`** unless you add it under `theme.screens` in `tailwind.config.js`. Using `xs:inline` silently does nothing — caught and removed during the issue #1 fix.
- "Mobile" in this project means anything below `sm` (640px). Phones are typically 360–414 logical pixels wide; design for 360.

## Audit-before-edit on responsive work

- Before changing responsive layouts, list every grid/flex container and note which already has responsive variants (`md:grid-cols-2`, `flex-wrap`, etc.). Most components in this repo already responded — only Header, RunPage outer header, and MetricsRow needed touching. Editing the rest would have been churn.

## Tables on mobile

- The shadcn `<Table>` primitive in `components/ui/table.tsx` already wraps the `<table>` in `<div className="relative w-full overflow-auto">`. That gives horizontal touch-scroll for free; do not re-wrap consumers in another `overflow-auto`.

## Icon-only buttons need labels

- When collapsing a button to icon-only on small screens (`<LogOut className="h-4 w-4 sm:mr-2" /><span className="hidden sm:inline">Logout</span>`), add `aria-label` to the button so screen readers and tap-to-read still announce it.

## Sort ISO timestamps with localeCompare, not Date()

- API timestamps in this repo (e.g. `RunSummary.created_at`) come from `datetime.now().isoformat()` on the Python side — **naive** ISO-8601 like `2026-05-04T19:00:00.123456`, no timezone suffix. They sort correctly lexically because every record uses the same shape and the same implicit (server-local) timezone. Use `b.created_at.localeCompare(a.created_at)` for newest-first. Avoid `new Date(a) - new Date(b)` — it allocates two Date objects per compare and is less obvious at a glance.
- Footnote: the backend really should emit timezone-aware ISO (`datetime.now(timezone.utc).isoformat()`) so cross-machine comparisons stay correct. Out of scope for issue #2.

## Don't rely on backend response order

- The `/runs` endpoint returned chronological order *in practice*, so the original RunsList just did `[...runs].reverse()`. That's fragile — any backend change (added pagination, switched ORM ordering, parallel fetch) silently breaks the UI. When ordering matters, sort explicitly on the field that defines the order.

## Probe the smoke-test port before assuming it's free

- When a curl-then-pipe-to-jq pipeline fails with `JSONDecodeError: Expecting value`, the first thing to check is whether something else is already listening on the port — an ambient service can return HTML 404s with `Content-Type: text/html` and the JSON parser blows up confusingly. `lsof -i :<port>` identifies the squatter; pick a less-common port (e.g. 8788) when starting an ad-hoc backend.

## Project venv shebangs are absolute and brittle

- `.venv/bin/uvicorn` (and similar entry-point scripts) carry an absolute shebang to `.venv/bin/python3.12`. If the project directory is moved (e.g. Downloads → Desktop), those scripts break with `bad interpreter`. Workaround: invoke as `.venv/bin/python -m uvicorn ...` instead. Recreating the venv in-place would also fix it.

## CSV → API: prefer file-mtime over a separate "generated_at" column

- Pipeline outputs (like `pair_candidates.csv`) don't carry their own timestamp. Surfacing freshness via `Path.stat().st_mtime` keeps the producer simple and means the freshness signal can never drift from the file. Use `datetime.fromtimestamp(..., tz=timezone.utc).isoformat(timespec="milliseconds")` — see the next lesson on why microsecond precision breaks Safari.

## Always clamp ISO timestamps to millisecond precision for the wire

- Python's `datetime.isoformat()` defaults to **microsecond** precision (`2026-05-04T13:47:03.954864+00:00`). Chrome/Firefox parse that fine, but Safari/JavaScriptCore only honours up to **3** fractional digits per the ECMAScript spec — it returns Invalid Date, and any downstream `toLocaleString({dateStyle, timeStyle})` then throws "the string did not match the expected pattern." Always pass `timespec="milliseconds"` on any ISO string that crosses the API boundary.
- Pre-existing endpoints in this repo that emit `datetime.now().isoformat()` (e.g. `RunSummary.created_at`) have the same latent bug — they only avoid it because the frontend currently uses `localeCompare` for sort instead of `new Date()`. If a future caller does `new Date(created_at).toLocaleString(...)` on Safari, fix it at the source the same way.

## Kite spot-quote keys for indices are display names, not derivatives tickers

- `kite.quote(["NSE:NIFTY"])` returns `{}` — Kite Connect's quote feed indexes the **spot** price under the index's display name (with spaces), not the derivatives ticker. Confirmed live against ZP6019 on 2026-05-05:
  - `NSE:NIFTY 50` → `last_price=24119.3` ✓
  - `NSE:NIFTY BANK` → `last_price=54878.5` ✓
  - `NSE:NIFTY` / `NSE:NIFTY50` / `NSE:BANKNIFTY` → `{}` (Kite silently omits the key)
- The map lives at module level in `strategies/taleb_karpathy.py` as `_INDEX_SPOT_SYMBOLS`. `f"NSE:{underlying}"` is correct for stocks (e.g. `NSE:RELIANCE`); for indices, the map must be consulted. Extend it when adding a new index underlying.

## Bare-except + numeric fallback = silent poison for the rest of the session

- The 2026-05-04 incident: `_get_spot_price` had `try ... except: return 0.0`. Wrong symbol → KeyError → caught → spot=0.0 → `math.log(0/K)` exploded on every tick for 5+ hours. systemd reported "Succeeded" because the run-loop caught each downstream exception and continued. **339 stack traces, 0 trades, 0 alerts.**
- Rules: (1) do not bare-except; catch `Exception` and log it with the input that produced it. (2) never silently substitute a numeric sentinel (`0.0`, `-1`) that downstream code can consume — return `None` and force the caller to handle absence. (3) for hot paths, add a consecutive-failure counter that escalates WARN → ERROR after N=5 so a sustained failure mode lights up in journal greps as something other than the symptom.
- Test coverage corollary: `mock.kite.quote.return_value = {}` *literally exercises the bug path*. If you mock that return shape, you must also have a positive-path test that asserts the real-shape extraction works. Otherwise the test suite "passes" by exercising the broken fallback exactly the way production hits it.

## TestClient coverage ≠ proxy coverage — keep an HTTP smoke check in front of nginx

- A new router can pass every `tests/test_backend.py` assertion and still 500-equivalent in production if the nginx prefix whitelist (`location ~ ^/(...)`) wasn't updated to include it. `fastapi.testclient.TestClient` instantiates the ASGI app directly — it never sees nginx — so it is structurally incapable of catching this. Shipped exactly this regression on 2026-05-05 with `/pair-candidates`.
- `deploy/smoke.sh` exists for this. The single load-bearing assertion is `Content-Type: application/json` — when nginx falls through to the SPA fallback, the response is `text/html` regardless of HTTP status, and that's the discriminator. `redeploy.sh` runs it against `127.0.0.1:8000` always and `$SMOKE_PUBLIC_URL` if set, gating the deploy.
- When adding a new public router: add a row to the `ROUTES` array in `deploy/smoke.sh`. Pick `allow_503=1` only if the data source can be legitimately absent on a fresh deploy.

## `uv pip compile` embeds the output path in the autogen header

- `uv pip compile … --output-file /path/to/foo.lock` writes a header comment that includes the literal `--output-file` argument: `# This file was autogenerated by uv via the following command: …`. If a drift-check writes the regenerated lock to `mktemp` then diffs against the canonical `requirements.lock`, the comparison **always** fails on that one line — false-positive drift. For a constrained dev lock (`--constraint requirements.lock`), every transitive that came from the constraint also gets a `# via -c <constraint-path>` annotation, so the temp path leaks into the body too.
- Fix: regenerate inside a `mktemp -d` directory using the canonical filenames (`requirements.lock`, `requirements-dev.lock`), then diff. The output is byte-identical when there's no real drift. See `deploy/check_lockfile.sh` for the pattern. Don't try to filter the noise with `diff --ignore-matching-lines` — too easy to mask genuine drift in the same comment style.
