# Lessons

## Backtest exit fills must lie inside today's [low, high]

- 2026-05-10: Phase-2 equity-swing backtest showed a +437 % single-trade win
  on NUVAMA (R-multiple = 48.8). Two bugs in series: (1) `chandelier_stop_long`
  uses `cummax` over the rolling indicator, so once a stale pre-split high
  (₹7600+) entered the lookback window the trail stop pinned at ~₹7000 forever;
  (2) the exit branch `if low <= pos.current_sl: exit_px = pos.current_sl`
  blindly used the recorded stop as the fill price — even when that price was
  above today's high (i.e. unfillable). The combination produced a "trail
  stop" exit at ₹7074 on a bar whose high was ₹1465. The whole strategy's
  Phase-2 P&L came from this single fictional trade.
- The right fix has two parts: (a) refuse to ratchet a long-side trail stop
  above the current close — that's a clear sign the indicator is stale or
  the data has a structural break — and (b) every exit branch must gate on
  `low <= exit_px <= high` for fillability; gap-throughs are handled
  explicitly with `min(stop, open)` / `max(target, open)` so the fill is
  conservative.
- Same shape as the bare-except-numeric-fallback lesson and the
  threshold-gates-vs-integer-lots lesson: a downstream sentinel value (here
  `pos.current_sl = 7000`) makes a degenerate input look like a healthy
  data point. The optimizer / reporter can't distinguish "winner" from
  "data-anomaly fill". 
- Rules: (1) any cumulative-max indicator (Chandelier, watermark trails) must
  be sanity-bounded by the relevant current-bar value before being
  persisted into position state. (2) every backtest exit must verify
  `low <= exit_px <= high`; an exit "fill" outside that range is a bug.
  (3) when a strategy's Phase-N P&L is dominated by a single trade with R > 5,
  treat it as a bug-shape, not a finding, until proven otherwise.

## STF proxy is not corp-action adjusted — drop the symbol, don't try to fix the bar

- 2026-05-10: NUVAMA's bhavcopy STF prices fell from ₹7614 (Dec) to ₹1110
  (March) — about a 5-6× move. That's a stock split, but each STF contract
  is a fresh series; the proxy panel concatenates them without applying any
  adjustment. Pre-split highs in the rolling indicator window then pollute
  ATR, ADX, donchian-high, AND chandelier-trail. There is no clean way to
  "fix" a single bar — the discontinuity propagates through every downstream
  feature.
- The fix shipped is to drop any symbol with a > 30 % single-bar `pct_change`
  from the entire panel. Operators on a host with NSE archive access run
  `fetch_bhavcopy_eq.py` to populate `data_cache/equity_ohlcv/`, which uses
  the EQ-segment bhavcopy that IS corp-action adjusted, and the loader's
  cache-priority path bypasses the STF proxy entirely.
- Same shape as the dividend-asymmetry lesson (`mean-reversion fails
  systematically around dividend ex-dates`): an underlying-source-level
  structural shift, encoded in the data, that the strategy interprets as
  signal. The simplest defensible fix is exclusion-by-default, with the
  per-symbol whitelist living outside the strategy.

## Index calendars: same edge, worse cost ratio than STF

- 2026-05-09: extended the mean-rev calendar to NIFTY/BANKNIFTY/etc. (IDF). The strategy does have a positive gross edge (+₹1.6k–5k across 1–3 trades on the 63-day replay window) but **costs are 5-10× larger per trade than STF** because notional is 5-10× larger (NIFTY ≈ ₹1.6M/lot, BANKNIFTY ≈ ₹1.7M/lot, vs ₹100k–500k for typical STF). At ~0.05 % round-trip percentage costs, every trade burns ₹3-5k that the spread has to recover. NIFTY's mean spread is ~109 points; entries fire at +1·SD ≈ 130; mean reversion of 30 points = ₹2k per leg — *less than the round-trip cost*.
- Multi-lot doesn't fix it — gross AND costs scale roughly linearly, so the ratio is unchanged. Spread-margin recognition by the exchange (which we don't model) helps margin but not transaction costs.
- This contradicts the intuition that "indices are more liquid so calendars work better there." Liquidity helps slippage, not the percentage-based STT/exchange/GST stack.
- Rules: (1) for any new strategy, the per-trade fixed cost vs typical gross-edge ratio should be sanity-checked at design time, not discovered in backtest. The breakeven move size (in price units) should be small relative to one SD of the entry signal. For index calendars: breakeven ≈ 1 SD = strategy can never net out. (2) When a strategy has a "more liquid is better" architectural premise, verify by looking at notional × percentage-cost — not just turnover.

## Volume thresholds in shares vs contracts: silently empties the universe

- 2026-05-09: implementing the Varsity-style mean-reversion calendar I shipped a `min_avg_volume = 100_000` default thinking in **share** terms. STF (single-stock-futures) volumes in `bhavcopy.TtlTradgVol` are in **contracts**. Liquid front-month names trade ~5k–25k contracts/day; next-month is far thinner (~1k–5k until it rolls). Result: zero trades on the entire 125-day backtest, no error, the report cheerfully said "Calendars opened: 0" and I almost shipped the strategy as broken.
- Same shape as the rehedge-count and zero-trade-fitness lessons: a per-instrument unit mismatch produces a silent no-op, downstream metrics increment as if work was done, no alarm fires.
- Rules: (1) when adding a unit-bearing threshold, write a comment on the SAME LINE clarifying the unit (`# in contracts, not shares`). (2) Calibrate defaults against one known liquid name before merging — for the calendar strategy, RELIANCE (~22k front, ~2.5k next) is the canary. (3) Any new strategy whose backtest opens zero trades on 100+ days of data should be treated as a bug, not a finding, until proven otherwise.

## Mean-reversion fails systematically around dividend ex-dates

- The Varsity calendar-spread chapter notes "all SBIN long trades lost, only shorts won" — the implementation reproduced this *very* strongly. On the 125-day archive replay with longs allowed, ~95% of LONG_CALENDAR trades (entered when spread crossed below mean − 1·SD) lost. Inspection of the entry contexts: the spread "crashed" because of a dividend ex-date — the future legitimately repriced lower and stayed there. The strategy interpreted a one-time structural shift as a mean-reversion opportunity and held into the loss.
- The cleanliness gate / per-symbol q overlay help on the basis arm but don't catch this case (the spread *change* is the signal, not the level). The simplest fix is what we shipped: `allow_long = false` by default, with the operator flipping it on per-name only after seeing the per-symbol breakdown.
- Rules: (1) for any mean-reversion strategy on prices that can step-change (dividends, stock splits, special distributions), default-disable the side that benefits from "spread will rise back up" — that's the side most exposed to one-way structural moves. (2) Backtest reporters must show per-symbol AND per-direction breakdown; the bottom-line P&L hides the asymmetry. (3) When the strategy's underlying source (Varsity in this case) explicitly mentions a per-name asymmetry, encode it as a default, not as a footnote.

## Flat-fitness landscapes silently lobotomize the optimizer

- The 2026-05-09 weekly autoresearch ran 40 experiments and every single one — including the unmodified baseline — returned `sharpe_ratio = 0.000000`. Diagnosis: all 3 training windows from the April CSV produced 0 trades under the seed params (low-vol regime, RV/IV never crossed `min_rv_iv_ratio = 1.22`). With 0 trades, `daily_pnl_history` stays empty and `get_strategy_metrics()` defaults `sharpe_ratio` to 0. Every mutation tied at 0 → nothing accepted → random walk stuck at the seed → "best params" emitted are byte-identical to last week's. The cron reported success; systemd was happy.
- Same shape as the prior lot-size lesson and the bare-except / numeric-fallback incident: a downstream sentinel value (here `0.0`) makes a degenerate input look like a healthy data point. The optimizer can't distinguish "this candidate is mediocre" from "this candidate took zero actions on data where no candidate could act".
- Rules: (1) any optimizer that scores backtests must treat `total_trades == 0` as a distinct sentinel (we use `-1e6`), separate from real failures (`-999999`). Without this, the loss surface is flat in the dead zone and gradient-following methods can't escape. (2) before launching a sweep, pre-screen every training window with the seed params; drop windows where the seed produces 0 trades, and raise (not warn) if none survive. The alternative is silently spending the full experiment budget on windows that have no reachable signal. (3) "no error + no improvement" after a long search is a *symptom*, not a result — go look at the per-trial metric column for variance. Identical scores across 40 trials means the metric, not the strategy, is what's broken.
- Implementation lives in `autoresearch_loop.ZERO_TRADE_PENALTY` and the pre-screen block in `run_autoresearch.main()`. Both should stay together; deleting one without the other recreates the silent failure mode.

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

- A new router can pass every `tests/test_backend.py` assertion and still 500-equivalent in production if the nginx prefix whitelist (`location ~ ^/(...)`) wasn't updated to include it. `fastapi.testclient.TestClient` instantiates the ASGI app directly — it never sees nginx — so it is structurally incapable of catching this. Shipped this regression twice: 2026-05-05 with `/pair-candidates`, then again 2026-05-11 with `/equity/*` (page broke with "Unexpected token '<', '<!doctype '... is not valid JSON" because `SMOKE_PUBLIC_URL` wasn't set so the public smoke silently no-op'd).
- `deploy/smoke.sh` exists for this. The single load-bearing assertion is `Content-Type: application/json` — when nginx falls through to the SPA fallback, the response is `text/html` regardless of HTTP status, and that's the discriminator. As of 2026-05-11, `redeploy.sh` auto-detects the public URL from `/etc/nginx/sites-enabled/dashboard`'s `server_name` so the public-host smoke runs by default — `SMOKE_PUBLIC_URL` is now an override, not an opt-in.
- **When adding a new public router, update all three places**:
  1. `backend/main.py` — `app.include_router(...)`
  2. `deploy/nginx-dashboard.conf.example` AND the live `/etc/nginx/sites-enabled/dashboard` — add the prefix to the API `location ~ ^/(...)` regex
  3. `deploy/smoke.sh` ROUTES array — one row per public path
- The auto-smoke-against-public-URL will now fail the deploy if step 2 is missed. Don't bypass it.

## `uv pip compile` embeds the output path in the autogen header

- `uv pip compile … --output-file /path/to/foo.lock` writes a header comment that includes the literal `--output-file` argument: `# This file was autogenerated by uv via the following command: …`. If a drift-check writes the regenerated lock to `mktemp` then diffs against the canonical `requirements.lock`, the comparison **always** fails on that one line — false-positive drift. For a constrained dev lock (`--constraint requirements.lock`), every transitive that came from the constraint also gets a `# via -c <constraint-path>` annotation, so the temp path leaks into the body too.
- Fix: regenerate inside a `mktemp -d` directory using the canonical filenames (`requirements.lock`, `requirements-dev.lock`), then diff. The output is byte-identical when there's no real drift. See `deploy/check_lockfile.sh` for the pattern. Don't try to filter the noise with `diff --ignore-matching-lines` — too easy to mask genuine drift in the same comment style.

## `ProtectHome=true` on a unit whose WorkingDirectory is under `/root` silently fails every fire

- 2026-05-12: discovered `pair-paper.service` and `pair-verify.service` had been failing at `status=200/CHDIR` on every timer fire — for at minimum 4 days (journal retention) and likely since first install. No `paper-pairs-*.log`, no `pair_paper_eod_*.json`, no positions taken, no P&L recorded. The timer happily rescheduled the failed fire each weekday so nothing surfaced as "stuck"; only an explicit `systemctl status` (or absence of expected output files) made it visible.
- Cause: both units set `ProtectHome=true` while `WorkingDirectory=/root/algo-trading/taleb-karpathy-kite`. `ProtectHome=true` hides `/root` (and `/home`, `/run/user`) from the unit's mount namespace, so `chdir()` fires before `ExecStart` and exits 200 before Python is even launched. The sister unit `taleb-hedger.service` works because it does NOT set `ProtectHome` — same `WorkingDirectory`, just no namespace hiding.
- Fix shipped: drop-ins at `/etc/systemd/system/pair-paper.service.d/override.conf` and `/etc/systemd/system/pair-verify.service.d/override.conf`, each containing `[Service]\nProtectHome=false`. `systemctl daemon-reload`, then a manual fire of `pair-paper.service` confirmed the unit now reaches Python, authenticates, screens, and hits its own `Started after 15:30 — nothing to do today.` self-gate as designed.
- Same shape as the bare-except-numeric-fallback lesson: systemd's "Succeeded"/"Failed" only marks the surface state; a unit that fails *before* `ExecStart` runs leaves no application-level evidence at all. The application logs are the ground truth for whether the cron is doing useful work — their absence is the signal.
- Rules: (1) any new hardened service must be smoke-tested with `systemctl start <unit>` once before being left to the timer — relying on "the timer will catch the next window" hides CHDIR/namespace failures forever. (2) when copy-pasting a hardening block (`ProtectSystem=strict`, `ProtectHome=...`, `PrivateTmp=...`) between units, audit it against the unit's actual `WorkingDirectory` and `ReadWritePaths` — they are not freely composable. (3) for any cron whose output is a file (an EOD JSON, a log, a report), add an "expected freshness" check somewhere that flags if today's file is missing past the expected write time. The cron silently not firing is structurally indistinguishable from the cron firing and writing nothing unless that check exists.
