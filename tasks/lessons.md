# Lessons

## A heavier autoresearch config must be re-fit to its systemd TimeoutStartSec

- 2026-06-06: `taleb-autoresearch.service` (Sat 10:00 IST timer) failed with
  `result=timeout` — systemd SIGTERM'd it at exactly 2h (`TimeoutStartSec=2h`).
  The journal looked empty because the wrapper (`run_weekly_autoresearch.sh`)
  redirects all output to `logs/autoresearch-<date>.log` and only `tail`s to
  journald at the END, which the kill pre-empted. The log file showed it reached
  `[25/40]` experiments in ~118 min (~4.7 min each) → a full 40-exp sweep needs
  ~3.1h.
- Cause: the 2026-06-01 config change (`--metric gamma_theta_ratio
  --eval-cycles 3 --window-days 5`) makes each experiment replay 3 captured tick
  sessions, and those tapes are ~1.2 GB each (~3.6 GB parsed per experiment).
  The prior week (2026-05-30) ran the lighter pre-06-01 config and finished
  under 2h. 2026-06-06 was the first Saturday cron under the heavier config.
- Impact was contained: no `candidate_params_<date>.json` that week, but
  `best_params.json` was verified intact (byte-identical to the start-of-run
  `.preautoresearch` backup) and nothing live/traded was touched.
- Fix: bumped `TimeoutStartSec` 2h → 4h (fits ~3.1h with margin; no research
  loss). Takeaway: **when you make an autoresearch eval heavier (more cycles,
  bigger tape, more experiments), re-check it against the unit's
  `TimeoutStartSec` — a oneshot that overruns is SIGTERM'd mid-sweep and
  silently produces no candidate.** The deeper fix (deferred) is to parse each
  ~1.2 GB tape once and reuse it across experiments instead of re-reading
  ~144 GB/week.

## A new backend router needs THREE wiring updates, not two

- 2026-05-17: Deployed the `/pair-paper-compare` router. Backend was
  wired (router registered in `backend/main.py`, route added to
  `deploy/smoke.sh` ROUTES). Loopback smoke passed (`curl 127.0.0.1:8000/...`
  → 401, as expected for a gated route). Public-URL smoke returned
  200 with the SPA's `index.html` — nginx's regex allowlist in
  `deploy/nginx-dashboard.conf.example` (and in the live
  `/etc/nginx/sites-enabled/dashboard`) had not been updated, so the
  request fell through `try_files $uri /index.html`.
- The takeaway: a new public API prefix requires **three** edits in
  lockstep — (1) register the router in `backend/main.py`, (2) add to
  `deploy/smoke.sh` ROUTES, (3) add to the regex allowlist in
  `deploy/nginx-dashboard.conf.example` AND patch the live
  `/etc/nginx/sites-enabled/dashboard` on the VPS (the live file is
  hand-edited, not symlinked to the template). Skipping step (3) means
  the route works for TestClient and loopback `curl` but never for
  real browsers. The user-memory `feedback_verify_with_public_url`
  is the same class of bug from the other side — both say "always
  run `deploy/smoke.sh https://<public-host>` after a deploy that
  adds a router; loopback alone is necessary but not sufficient."

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
  `market_data/fetch_bhavcopy_eq.py` to populate `data_cache/equity_ohlcv/`, which uses
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

## Paper-z windows must not be diluted by intraday observations

- 2026-05-13: `pair-paper.service` closed all 3 monitored pairs FLAT but
  realized ₹−39,251 across 28 round-trips against transaction costs of
  ₹39,285 — friction was the entire bleed. The backtest baseline expected
  ~₹1,020/day gross edge; paper ran ~225× the backtest's signal cadence
  (9 round-trips per pair per session vs ~5 round-trips per pair per
  127-day backtest).
- Cause A — seed dilution: `_observe_spread` appended every minute-tick
  whose spread moved >1 paisa to the same `_spread_history` that
  `_z_score` reads as its rolling distribution. The seed was 60 days of
  daily bhavcopy closes; after ~60 ticks the rolling-60 window was
  mostly intraday observations. Std collapsed to intraday wiggle, so
  `|z|=2` started triggering on intraday-noise excursions instead of
  true 2σ daily-spread moves. The strategy was tuned (sweep on
  daily-bar bhavcopy) for the daily-bar distribution; the production
  z-denominator silently switched distributions during the session.
- Cause B — no cost-hurdle: entries fired on any `|z| ≥ entry_z`
  regardless of expected ₹ move. Each round-trip costs ~0.21% of leg
  notional + ~₹80 fixed (≈ ₹2.1k at the ₹10L per-leg cap). At the
  diluted z-scale, expected ₹ move per round-trip was below friction —
  guaranteed-negative-EV trades fired anyway.
- Fix shipped (2026-05-13): (1) `_observe_spread` no longer mutates
  `_spread_history` — the rolling z-window is seed-only, daily, set
  once at __init__ from bhavcopy, untouched intraday. (2) New
  `min_edge_multiplier` config knob (default 1.5) gates entries on
  `expected_gain ≥ multiplier × round_trip_cost` computed at entry
  quotes. `0` disables for emergency rollback.
- Same shape as the bare-except-numeric-fallback lesson: a downstream
  sentinel (the rolling-window std) silently changed meaning
  intraday, and the strategy kept producing nominally-valid signals
  against the new meaning. No exception, no alert, just a friction
  bleed visible only after EOD reconciliation.
- Rules: (1) when a strategy is tuned against a specific bar cadence
  (daily / intraday / tick), the production code path must enforce the
  same cadence end-to-end — never let a higher-resolution stream
  silently dilute a lower-resolution baseline. (2) any
  mean-reversion strategy with realistic friction needs an explicit
  cost-hurdle gate; "the band is wide enough" is not a substitute when
  std collapses or notional caps shrink the per-trade move. (3) if
  paper realized P&L ≈ −1 × transaction_costs over a session, treat it
  as a signal-cadence-vs-cost-model bug, not unlucky alpha — verify
  trade count against the backtest's expected cadence before assuming
  the strategy "just had a bad day".

## EOD flatten was an artefact, not a strategy decision (2026-05-19)
- Incident: RELIANCE/ITC entered 15:02:19 at z=-2.01, force-flattened
  at 15:25:00 by `run_paper_pairs`'s `FLATTEN_AT` hard-coded constant
  — −₹3,850 on 23 minutes of hold time, with the strategy's own exit
  triggers (mean-revert, stop-z, max-hold) never given a chance to
  fire. The flatten was structural, not strategic.
- Root cause: the paper runner was a `oneshot` systemd unit. Without
  cross-session state, "process exits at 15:30" implicitly meant
  "positions can't survive overnight," so the EOD flatten was the
  only safe way to close the loop.
- Fix: replaced the unconditional flatten with disk-persisted state
  (`data_cache/pair_paper_state_<system>.json`) restored at next
  session start. Open positions now exit only on strategy triggers,
  plus an expiry-day force-flatten so contracts don't go to
  settlement.
- Rule: when an operational constraint (process lifecycle, file
  rotation, deploy cadence) is silently shaping strategy behaviour,
  separate the two. Operational artefacts should not become
  pseudo-strategy decisions. Check periodically: for each hard-coded
  EOD/session-boundary action, ask "would the strategy do this if it
  could speak?" If no, the constraint is leaking and needs an explicit
  bridge.

## A memory note can be partially stale — verify the unblock path before promising "just one click" (2026-05-27)
- Incident: investigating why the Taleb hedger keeps losing money,
  the user asked "why have we not enabled phase 3 and above". I
  cited the `project_taleb_profitability_uplift_2026_05_23` memory
  which said "All the plumbing is in place; just `systemctl start
  taleb-autoresearch.service`". The user said "kick off autoresearch
  and patch the chain fetcher" — assuming the patch was a single
  scoped action. On exploration, three additional gates were
  discovered:
  (a) `_get_options_chain` returned one expiry — calendar builder
      always returned `[]`.
  (b) `market_data/tick_capture.py` only subscribed to the front weekly — every
      captured tape session was single-expiry.
  (c) `deploy/run_weekly_autoresearch.sh` passed `--data $CSV` which
      bypassed `runners/run_autoresearch.py:198`'s captured-tape replay
      path entirely.
- Rule: memory notes are point-in-time observations. When a memory
  asserts a code path is wired, verify the actual call chain end to
  end (chain fetcher → proposer → wrapper → entry script) before
  pitching it as "ready to ship". The `<system-reminder>` on every
  memory read says this literally — treat it as load-bearing, not
  boilerplate.
- Application: before recommending a documented "ready to ship"
  action, grep for the actual callers of each named component and
  read at least the function signatures. Cheap to do; the cost of
  promising a one-step unblock and then discovering three steps in
  the middle of the user's session is much higher than a 60-second
  audit at the start.

## A silent-identity fallback hides leg-lookup bugs in tests
- Incident: a smoke test of the new cross-session persistence kept
  re-appending exit legs to `state.legs` instead of removing them, so
  flatten left the position OPEN. State was correct in production;
  the smoke's `_cached_futures = {}` was the only thing different.
- Root cause: `_symbol_from_tradingsymbol` falls back to *identity*
  when the cache misses — returns the futures tradingsymbol where the
  caller expects an equity symbol. `_apply_fill` then searches
  `state.legs` for a leg whose `symbol == "RELIANCE26MAYFUT"`, finds
  none, treats the exit as a fresh entry, and appends a new leg.
- Rule: silent identity-fallbacks in symbol-resolution helpers are a
  trap. They're tolerable in production where the cache is warm; in
  tests they turn a missing fixture into a plausible-but-wrong run.
  When writing a strategy-level test, populate `_cached_futures` for
  every leg the strategy will touch, not just the entry legs.

## Test against REAL serialized shapes, not assumed ones
- Incident: the new `scripts/portfolio_view.py` (cross-strategy portfolio
  view) shipped with reader tests that all passed, but a `/code-review`
  found two crashes that fire only when a position is OPEN: the arbitrage
  reader called `.values()` on `open_calendars` (serialized as a LIST,
  arbitrage.py:550) and `collect()` iterated buy_on_gap `state.positions`
  as a list (it is a DICT keyed by symbol, buy_on_gap.py:632).
- Root cause: the tests fed fixtures built from a scoping *summary* of the
  state shapes ("dict keyed by underlying") rather than the actual
  serialize methods / on-disk files. The fabricated shapes matched the
  reader's wrong assumption, so red never showed. The real state files
  were FLAT at build time, so the live run didn't exercise the open path.
- Rule: when a reader parses another module's persisted output, derive the
  test fixture from that module's real `serialize`/`to_dict` (or a real
  on-disk file with an open position), never from prose. If the live
  artifact is empty, construct the open shape from the producer's code, not
  from memory. Same family as the parity-gate rule — assert against the
  producer, not against your assumption. Belt-and-suspenders for a
  read-only tool: tolerate list-or-dict containers and isolate each source
  so one shape change can't crash the whole view.
