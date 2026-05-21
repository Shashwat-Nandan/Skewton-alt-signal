# LIVE-readiness review — pair_trading first live deployment (2026-05-21)

## Context
- Today: 2026-05-21. Target: pair_trading flipped to `mode=live` ~week of 2026-05-25.
- Other strategies (taleb_karpathy, varsity_equity_swing) stay on paper for now.
- Approach: fresh independent review across four dimensions; agents return
  structured gap lists, I synthesize and verify before fixing anything.
- Dirty tree at review start: in-flight positions-tracker work (backend
  `routers/positions.py`, frontend `PositionsPage.tsx`, related lib edits).
  Audits will read this code in place; fixes will not collide with it
  unless explicitly flagged in triage.

## Phase 1 — Parallel audits (read-only)
Four general-purpose subagents, each scoped to one dimension. Each returns
a punch list of (a) what's already correct, (b) gaps with severity rationale,
(c) recommended fix sketch. No code edits in this phase.

- [ ] Strategy audit: pair_trading entry/exit/hedging/state, orphan+restore
  path, EOD persistence, hedge-ratio drift, expiry handling
- [ ] Broker audit: kite_auth, paper-vs-live code-path divergence, order
  idempotency, partial fills, reject paths, throttling, reconciliation
- [ ] Risk audit: per-leg caps, daily loss limit, max open positions,
  kill-switch, exposure ceilings, runaway-loop guard
- [ ] Ops audit: systemd units, secrets/env, holiday calendar, state-file
  durability, alerting, monitoring, backup/recovery, observability

## Phase 2 — Synthesize gap list
- [ ] Reconcile findings, dedupe, severity-grade (Crit/High/Med/Low),
  estimate fix effort per gap
- [ ] Cross-check against `tasks/security-followups.md` to avoid
  re-discovering known-deferred items
- [ ] Present prioritised triage table

## Phase 3 — User triage
- [ ] User picks the Crit/High set to fix in this session

## Phase 4 — Fixes
- [ ] One change per gap. I read the actual code (not just the agent
  summary) before editing. Verify after each fix per Rule 12.

## Phase 5 — Pre-flight checklist for first live session
- [ ] Sized-down config (1 lot, low notional cap)
- [ ] Manual kill-switch documented and tested
- [ ] `kite.positions()` reconciliation step before each session
- [ ] Alert path (failure → notification) verified end-to-end
- [ ] Documented rollback-to-paper plan

## Phase 6 — Document deferred items
- [ ] Anything not fixed → `tasks/live-readiness-deferred.md` with severity,
  blast-radius, explicit "go-live blocker yes/no"

## Review

All 10 Criticals closed in 9 commits this session (one per Critical, with
C1+C2 and C10+H11 paired by natural coupling):

| Critical | Commit | One-line |
|---|---|---|
| C7 | `58af67a` | runners refuse to start if holidays.csv is stale or partial |
| C5 | `907fc4b` | flag-file kill switch (HALT_ALL, HALT_NEW_ENTRIES) |
| C8 | `561feba` | OnFailure=notify-failure@%n on all 12 trading units |
| C9 | `1b8fb34` | atomic state-file backups + refuse-to-start-on-orphan |
| C1, C2 | `b9fe452` | MARKET orders, poll order_history, entry-batch reversal |
| C3 | `1e17712` | kite.positions() reconciliation in live mode |
| C4 | `cd711c3` | --max-daily-loss-inr → HALT_DAILY_LOSS auto-trip |
| C10, H11 | `f028329` | exit on held contract (not today's front-month); fail-loud on phantom legs |
| C6 | `f82451e` | --mode live with triple-lock + circuit-breaker required |

**Test coverage**: pair_trading test suite grew from 46 → 56 tests. All passing.
10 new tests cover the C1/C2/C10/H11 behaviour (live PENDING/REJECTED paths,
entry-batch reversal, exit on rolled contract, tradingsymbol reverse-map raises).

**Test outside-the-suite verifications**:
- C7 assert exercised: year-count check fires on current 6-entry file, horizon check fires near year-end, plausible 12-entry list passes.
- C5 kill switch exercised: baseline/HALT_NEW_ENTRIES/HALT_ALL all behave per spec; transition log lines fire on flip.
- C8 notifier exercised: direct invocation produces journal CRITICAL line as expected (external channel paths verifiable via env-vars at deploy).
- C9 backup helper exercised: archive creates timestamped backup, prune keeps last N, assert raises iff backups exist for missing state.
- C3 reconcile exercised: paper-only skip, live-match OK, live-mismatch raises, kite.positions() exception raises, unknown broker positions WARN.
- C4 limit exercised: disabled (0) no-op, under-limit no-op, breach touches flag, idempotent re-check.
- C6 gates exercised: `--mode live` without env/flag/circuit-breaker each refuses with specific message.

**What's NOT done** (deliberately, with explicit go-live-blocker calls):
See `tasks/live-readiness-deferred.md`. Top of mind from there:
- 19 Highs documented (none are go-live blockers for a sized-down first
  session, but H1/H3/H7 should land within first week of live).
- Mediums + Lows enumerated for later sweeps.

**Pre-flight checklist** for the first live session lives at
`deploy/VPS_DEPLOYMENT.md` §7.9.

**Data still to populate**: `holidays.csv` 2026 NSE list. The fail-loud
guard now refuses to start until populated — operator must supply from the
NSE "Holidays — Trading" PDF. The WebFetch attempt during this session
timed out (NSE anti-bot); operator paste is the reliable path.

**Lessons / behaviours worth preserving** — see `tasks/lessons.md` for any
new entries added this session.

---
# Persistence-screened pair trading — parallel paper system (2026-05-17)

## Motivation
Investigation 2026-05-17 showed the current pair-trading screener admits pairs
on a single 6-month cointegration window. Across 6 rolling windows of NIFTY 50
STF data:
- 287 unique pairs ever passed `p<0.05`. 258 of them (90%) passed in only ONE
  window — they're statistical noise, not durable economic linkages.
- Zero pairs passed in all 6 windows. Three pairs in 3, one in 4.

Backtest comparison across three OOS test windows (W4, W5, W7 spanning Feb 2025
→ Feb 2026) of pairs admitted under "≥2 of 6 windows passed `p<0.05`":
- W4: 8 persistent pairs, 8/8 profitable, ₹549k net
- W5: 5 persistent pairs, 5/5 profitable, ₹568k net
- W7: 10 persistent pairs, 9/10 profitable, ₹617k net
- Aggregate: 22/23 pair-level win rate, ₹1.73M net across three OOS windows.

The same NIFTY 50 universe under the current single-window screener produced
−₹450k on the 70/30 OOS split (May–Oct 2025) — collapse driven by junk pairs
that won't carry. Persistence-based admission survives where the baseline
breaks; baseline's good windows are slice luck.

Decisions captured 2026-05-17:
- Build a parallel paper-trading system using V0 admission (≥2 of 6 windows).
  Both run side-by-side. No backfill — persistent system trades only persistent
  pairs even if that means a smaller book.
- Surface comparison in dashboard tile + CLI compare script.
- Run both for 5 trading days minimum before any live/decision-making step.

## Design (architecture)
Two paper-trading systems sharing Kite session and execution path; differ only
in which candidates CSV they read:

```
NIFTY 49 bhavcopy cache (TATAMOTORS dropped post 2025-10-23)
    │
    ├──→ screen_pairs.py (baseline, single 6-mo window)
    │       → data_cache/pair_candidates.csv
    │       → run_paper_pairs.py --system baseline
    │           → data_cache/pair_paper_eod_<date>.json
    │           → logs/paper-pairs-<date>.log
    │
    └──→ screen_pairs.py --persistence-windows 6 --persistence-min 2
            → data_cache/pair_candidates_persistent.csv
            → run_paper_pairs.py --system persistent
                --candidates data_cache/pair_candidates_persistent.csv
                → data_cache/pair_paper_persistent_eod_<date>.json
                → logs/paper-pairs-persistent-<date>.log
```

## Tunables (held identical across both systems for fair A/B)
- entry_z=2.0, exit_z=0.75, stop_z=4.0
- lookback_days=60, max_holding_days=7
- lots_per_leg=1, max_leg_notional=₹1,000,000
- Same 4-pass runner filter (β + quality floor + composite score +
  leg-concentration cap). Only the candidates CSV differs.
- Persistent system: persistence_windows=6, persistence_min=2,
  window_days=130, step_days=45 (matches OOS validation defaults).

## Implementation tasks
1. **screen_pairs.py — persistence mode**
   - Add CLI: `--persistence-windows N --persistence-min M --out PATH`
   - When `--persistence-windows` is set, run N rolling-window screens
     (window=130d, step=45d) instead of the single-window screen, output
     only pairs with ≥M passes. Hedge ratio sourced from the LATEST window
     where the pair passed.
   - Defaults preserve current behaviour (single-window screen).
   - Write output to `--out` if provided, else `pair_candidates.csv`.

2. **run_paper_pairs.py — parameterize candidates path and system tag**
   - Add CLI: `--candidates PATH --system NAME`
   - Default `--candidates` → existing `CANDIDATES_PATH`.
   - Default `--system baseline` preserves existing log/EOD filenames.
   - Non-default system suffixes the EOD JSON
     (`pair_paper_persistent_eod_<date>.json`) and log file
     (`paper-pairs-persistent-<date>.log`).
   - EOD payload gains a top-level `"system": "<NAME>"` field for the
     dashboard ingest to discriminate.

3. **Deploy units (systemd)**
   - `deploy/pair-paper-persistent.service` — mirrors pair-paper.service
     ExecStart but adds `--candidates ...persistent.csv --system persistent`.
   - `deploy/pair-paper-persistent.timer` — same OnCalendar as
     pair-paper.timer + 1 min offset (09:12 IST) to avoid TOTP race.
   - Wrap the screener in `deploy/run_weekly_pair_screen_persistent.sh`
     or extend the existing screener wrapper to produce both CSVs in
     one run. Preferred: extend the existing wrapper (saves a second
     bhavcopy fetch, two systemd units instead of three).

4. **compare_paper_systems.py — CLI head-to-head**
   - Reads `data_cache/pair_paper_eod_*.json` and
     `data_cache/pair_paper_persistent_eod_*.json` for a date range.
   - Prints per-day per-pair P&L and aggregate by system.
   - Highlights pairs traded in both systems vs unique-to-system.

5. **Dashboard tile**
   - Backend: new router `backend/routers/pair_paper_compare.py` exposing
     `/api/pair-paper-compare?days=N`. Reads both EOD JSON families, returns
     daily + aggregate rows.
   - Frontend: new card in the pair-trading dashboard that shows
     baseline vs persistent system: per-day net P&L, cumulative net,
     pair counts, win rate.
   - Add route to `deploy/smoke.sh` ROUTES array (per lessons.md).

6. **Smoke + sign-off**
   - Run screen_pairs.py with persistence flags locally; confirm
     `pair_candidates_persistent.csv` has 2–10 pairs.
   - Run `run_paper_pairs.py --force --system persistent` in dry-run
     mode (out of market hours OK with `--force`) to confirm pair
     selection + auth + logging are wired.
   - Both systemd units enable cleanly on the VPS.
   - First trading day produces both EOD JSONs.
   - 5-day comparison after first full trading week.

## Acceptance
- screen_pairs.py without flags is byte-identical to today's output (no
  behavioural change to the baseline pipeline).
- run_paper_pairs.py without flags writes to today's filename
  (`pair_paper_eod_<date>.json` — no `_baseline` suffix), preserves logs.
- The persistent system's EOD JSON is consumable by an existing-tooling
  Python reader (same schema, plus `system` field).
- Dashboard tile reads both, shows aggregate over the last 5 trading days.
- compare_paper_systems.py works for any date range, including dates
  where only one system produced output.

## Risk surface
- **Both systems share Kite session**: paper mode only — no real orders.
  Two simultaneous quote() calls per tick are well under rate limits.
- **Pair overlap**: if both systems pick the same pair, they each maintain
  independent paper state. Capital comparison is per-system, not per-pair.
- **Persistent screener may produce 0 pairs**: runner will fail gracefully
  ("No tradeable pairs after β + quality + concentration filters"). EOD
  JSON will be empty `{pairs: []}` but still written.
- **First-time persistent screen takes longer** (~6× the cointegration
  passes). screen-pairs.timer ceiling is 15min; persistent variant should
  stay within budget. Bench locally before deploying.

## Review (2026-05-17 implementation)
Implemented as planned, in one session, with no surprises. Local end-to-end
smoke shows:

- `screen_pairs.py --persistence-min 2` against the current bhavcopy archive
  (453d, NIFTY 49 — TATAMOTORS dropped post 2025-10-23 STF restructure)
  produces 10 persistent pairs over 8 rolling windows. After the runner's
  4-pass quality floor, 2 pairs admit: **CIPLA/ITC** (pers=2, p=0.013, HL=4.6d)
  and **BAJAJFINSV/BAJFINANCE** (pers=2, p=0.003, HL=2.4d). HDFCLIFE/NTPC —
  the W7 false-positive from the fresh-persistence verification — is
  naturally dropped by the quality floor (corr 0.615 < 0.65 floor), so V0
  + the existing quality filter is self-correcting.

- `run_paper_pairs.py` with no flags is byte-identical in behaviour to the
  previous version: same default candidates path, same log/EOD filenames.
  With `--system persistent --candidates …persistent.csv` it suffixes
  filenames and labels the EOD payload.

- The dashboard backend registers `/pair-paper-compare`, returns 401 for
  unauthenticated callers, and the frontend typechecks clean.

- `compare_paper_systems.py` reads both EOD families and merges them
  correctly (verified with a synthetic persistent EOD, since the real
  runner has not produced one yet).

### VPS deploy steps (when ready)
1. `git pull` on the VPS.
2. `sudo cp deploy/pair-paper-persistent.{service,timer} /etc/systemd/system/`
3. `sudo systemctl daemon-reload`
4. Trigger the screener once to generate the new CSV:
   `sudo systemctl start screen-pairs.service`
   (Confirm `data_cache/pair_candidates_persistent.csv` exists.)
5. `sudo systemctl enable --now pair-paper-persistent.timer`
6. Verify `systemctl list-timers | grep pair-paper`.
7. Monday 09:12 IST: both runners fire in parallel.

### Watch points during the 5-day window
- Day-1 EOD: check `data_cache/pair_paper_persistent_eod_*.json` exists
  AND the dashboard tile loads (re-check `/api/pair-paper-compare` route).
- After day-5: run `compare_paper_systems.py --days 5` and review the
  per-pair contribution. If persistent < baseline, drill into WHY (was
  it the pair selection or the quality filter being too tight on persistent
  pairs?).
- If persistent pairs admit 0 most days (book too small), consider
  loosening QUALITY_MAX_PVALUE for persistent only — but that requires
  another code change, not a config toggle (the constant is module-level
  in run_paper_pairs.py).

---

# Varsity-style equity swing strategy (2026-05-10)

## Motivation
User wants a medium-to-long-term equity directional system grounded in the
Varsity modules: trend (Module 2), gap behaviour (Module 5 sentiment), market
profile (POC/VAH/VAL), OI confluence on F&O names, and FII/DII flow overlay,
with Module 9 risk management (ATR-sized stops, RR ≥ 2). Run twice daily as
cron, surface signals + paper P&L on the dashboard with full trade detail
(entry / SL / target / position size / status). This is the **first equity-
directional strategy** in this repo, which is otherwise options/derivatives.

User decisions captured 2026-05-10:
- Universe: **Nifty 200**.
- Cadence: **twice daily** — post-open scan and post-close scan.
- Mode: **signals + paper trading** (no live in v1; `live` stays gated).
- Signals: **Trend + Gap + Market Profile + OI + FII/DII** (full set).

## Design constraints (from `tasks/lessons.md`)
- Volume thresholds for equity must be in **shares** (bhavcopy `EQ` segment) —
  comment the unit on the same line where defined. Do NOT mix with F&O contract
  volumes (which is what the calendar lesson got wrong).
- Strategy must NEVER bare-except + return `0.0` from a price/quote helper.
  Return `None` and force callers to handle absence. Add a consecutive-failure
  counter that escalates WARN→ERROR after 5 consecutive misses.
- Backtest must treat `total_trades == 0` as a **distinct sentinel** (not flat
  zero score) to keep autoresearch hookup viable later.
- Any new public API route MUST be added to `deploy/smoke.sh` ROUTES array
  before deploy (TestClient passes are necessary but not sufficient).
- Spot keys: `f"NSE:{symbol}"` is correct for stocks (no index-name special
  case needed since Nifty 200 constituents are equities).
- Any unit-bearing threshold gets `# unit: <X>` on the same line.
- Function whose contract is "produce executable side effects" must log when
  it produces none — silent `return []` is forbidden.

## Architecture

**New strategy:** `strategies/varsity_equity_swing.py` subclassing
`BaseStrategy`. `option_type="EQ"` is added to `TradeProposal` semantics
(string is already free-form; no schema change needed but `validate_order`
already accepts `EQ`-style symbols since the regex is `[A-Z0-9&\-]{3,30}`).

**Universe loader:** `data_cache/nifty200.csv` (one column `symbol`). Either
hand-maintained or refreshed via a small `fetch_nifty200_constituents.py`.

**Data sources** (all free):
| Signal       | Source                             | Cache                              | Cadence |
|--------------|------------------------------------|------------------------------------|---------|
| Daily OHLCV  | NSE bhavcopy EQ archive (existing) | `data_cache/bhavcopy_eq/`          | EOD     |
| Intraday 30m | Kite historical (existing)         | `backend/bars.db`                  | EOD     |
| F&O OI       | NSE bhavcopy F&O (existing)        | `data_cache/bhavcopy_raw/`         | EOD     |
| FII/DII flow | NSE `fiidiiTradeReact` API         | `data_cache/fii_dii/YYYY-MM-DD.json` | EOD   |

**Risk manager (Varsity Module 9):**
- Position size: `shares = (total_capital * risk_per_trade_pct) / (entry - SL)`
  where `risk_per_trade_pct = 1 %` and `SL = entry − 2.5 × ATR(14)`.
- Hard SL at entry, Chandelier trail (highest_high − 3 × ATR) once 1× SL is
  green.
- Target: 2 × SL distance (RR=2). Optional scale-out at 1.5× (out of scope v1).
- Time stop: 20 trading days flat → exit.
- Portfolio: max 6 open positions, max 30 % gross exposure of `total_capital`.

**Signal pipeline (per scan):**
1. Universe filter: liquidity (avg daily turnover > ₹50 cr, 20-day median).
2. Trend filter: `SMA50 > SMA200` AND `ADX(14) > 20`.
3. Setup trigger (any-of):
   - Pullback to 20 EMA (close within 0.5 ATR), prior-day green candle.
   - Breakout > 20-day high with vol > 1.5× 20d avg.
4. Gap filter: skip if |open − prev_close| / prev_close > 2 %.
5. Market profile (uses `market_profile.py` on 5-day rolling 30m bars):
   prefer entries near VAL (long) when above VAH; reject if at POC and stalling.
6. OI confluence (only for F&O names; non-F&O names skip this gate):
   long buildup (price ↑, OI ↑) → green; short buildup (price ↓, OI ↑) → red.
7. FII/DII overlay: **5-day cumulative net FII cash** > 0 → tilt toward longs;
   < 0 → require stronger setup (score threshold +1).
8. Score = sum of weighted booleans; rank desc; take top N (≤ remaining slots).

**Paper-mode harness:** mirrors `run_paper.py`. Persists open positions to a
new SQLite table `equity_positions` (cols: id, run_id, symbol, side, entry_dt,
entry_px, sl, target, atr_at_entry, qty, status, exit_dt, exit_px, exit_reason,
pnl). Each scan also marks-to-market open positions and triggers SL/target/
time-stop exits.

**Backend API** (`backend/routers/equity_swing.py`):
- `GET /equity/positions?status=open|closed` — full row dump.
- `GET /equity/signals?date=YYYY-MM-DD` — pending signals from latest scan.
- `GET /equity/scans` — last N scan summaries (date, mode, n_signals, n_skipped).
- `GET /equity/fii-dii` — last 30 trading days of cash + index futures flows.
- `deploy/smoke.sh` ROUTES updated for each.

**Frontend** (`frontend/src/pages/EquitySwingPage.tsx`):
- Open positions table — symbol, entry_dt, entry, current, SL, target, P&L,
  days held, status badge (OPEN / SL_HIT / TARGET_HIT / TIME_STOP / TRAIL_STOP).
- Today's signals card — symbol, score, rationale, suggested entry/SL/target/qty.
- FII/DII tile — last 5d cumulative cash + futures, sparkline.
- Reuse `ProposalTable.tsx` row pattern; add a "Trade Details" drawer with
  the full signal-time snapshot (trend, profile context, OI delta).
- Route added to `App.tsx`; entry on `Home.tsx` strategy picker.

**Live runtime:**
- `run_equity_swing.py` — single CLI, `--scan {open|close}` flag, `--mode
  {signals|paper}`. Loads strategy, runs `scan_and_propose` →
  `check_and_rehedge` (which exits triggered positions) → `execute_proposals`.
- `deploy/equity-swing-open.timer` — `Mon..Fri 09:30 IST`.
- `deploy/equity-swing-close.timer` — `Mon..Fri 15:35 IST` (after bhavcopy is
  available). Each timer launches a `equity-swing-{open,close}.service` unit
  that calls `run_equity_swing.py` with the matching flag.
- Both write JSONL to `logs/signals-YYYY-MM-DD.jsonl` (existing convention,
  flock-locked appends from `BaseStrategy._emit_signal`).
- Paper mode also persists positions to `dashboard.db.equity_positions`.

## Phasing (build-then-validate at each phase)

### Phase 1 — Strategy core + backtest [no live, no UI]
- [ ] Universe: add `data_cache/nifty200.csv` (hand-curated for v1) + tiny
      loader in `strategies/varsity_equity_swing.py`.
- [ ] Indicator library `strategies/_indicators.py`: `sma`, `ema`, `atr`,
      `adx`, `donchian_high` — pure pandas, no Kite dep.
- [ ] `VarsityEquitySwingStrategy(BaseStrategy)`:
      - `scan_and_propose()` → trend + gap + setup + ATR sizing + risk gate.
      - `check_and_rehedge()` → SL/target/time-stop/Chandelier exits.
      - `execute_proposals()` → signals JSONL + paper book.
      - `generate_eod_report()` → P&L, win rate, expectancy, n_open, n_closed.
- [ ] `backtest_varsity_equity.py` — replays bhavcopy EQ archive (3-year
      window default). Must:
      - emit per-trade ledger TSV with entry, exit, holding period, R-multiple;
      - print summary block (Sharpe, Calmar, max DD, profit factor, win rate);
      - return ZERO_TRADE_PENALTY = -1e6 when 0 trades, separate from errors;
      - apply 0.2 % round-trip cost (delivery STT 0.1 % sell + slippage).
- [ ] `tests/test_varsity_equity_swing.py` — unit tests for ATR sizing
      (asserts non-zero shares for normal ATR), trend filter, gap filter,
      Chandelier trail, time-stop. Plus an integration smoke that runs the
      strategy over a fixture and asserts at least one trade fires (catches
      the "silently empties the universe" lesson).
- [ ] Run backtest end-to-end on the available bhavcopy archive. Sanity-check
      output: per-symbol breakdown, win rate by direction, average holding
      period 5–25 trading days for a "swing" claim.
- [ ] STOP. Review backtest with user before proceeding.

### Phase 2 — Market profile + OI confluence
- [ ] Wire `market_profile.py` into the scan: build a 5-day rolling profile
      per symbol from `backend/bars.db` 30-min bars. Add a setup gate using
      VAH/VAL/POC.
- [ ] Add OI gate: read `data_cache/bhavcopy_raw/` F&O segment for the symbol
      (skip if no F&O); compute price-vs-OI delta for the last 5 sessions;
      veto on short-buildup, boost on long-buildup.
- [ ] Re-run backtest. Compare metrics vs Phase 1 baseline. Document delta.
- [ ] If MP/OI gates degrade Sharpe, default-disable them and surface as
      tunables (Varsity calendar dividend lesson: encode known asymmetries
      as defaults, not footnotes).

### Phase 3 — FII/DII + live runtime + paper book
- [ ] `fetch_fii_dii.py` — pulls NSE `fiidiiTradeReact` JSON, writes
      `data_cache/fii_dii/YYYY-MM-DD.json`. Idempotent. Add a `--lookback N`
      backfill flag.
- [ ] FII/DII overlay in scan; backtest replay needs synthetic backfill from
      NSE bulk archive (fall back to skipping the gate when not available).
- [ ] `run_equity_swing.py` CLI wrapper (signals + paper modes).
- [ ] `dashboard.db.equity_positions` table + persistence helpers in
      `backend/db.py`. Schema migration on startup (additive only, won't
      conflict with existing `runs/proposals/pnl_snapshots`).
- [ ] systemd: `deploy/equity-swing-open.{service,timer}` and
      `deploy/equity-swing-close.{service,timer}`. Fetcher dependency:
      close-scan timer waits for `fetch-fii-dii` + `fetch-bhavcopy` to land.
- [ ] Smoke: trigger both timers manually, verify JSONL written, paper book
      updated, no 500s in journalctl.

### Phase 4 — Backend API + frontend
- [x] `backend/routers/equity_swing.py` with the four endpoints listed above.
      Wire into `backend/main.py`. Add `pages/EquitySwingPage.tsx`.
- [x] Update `deploy/smoke.sh` ROUTES — `/equity/positions`,
      `/equity/signals`, `/equity/scans`, `/equity/fii-dii`. All four gated,
      so probe expects 401 + application/json (matches existing rows).
- [x] `pages/EquitySwingPage.tsx` — open positions table, today's signals
      card, FII/DII tile, trade-detail drawer (side sheet). Wired into
      `App.tsx` + `Header.tsx` NavLink.
- [x] Backend tests in `tests/test_backend_equity.py` (13 tests, mirrors
      `test_pair_candidates.py` pattern — monkeypatches LOG_DIR/FII_CACHE_DIR
      to tmp_path).
- [x] TestClient probe: all four routes return 401 + `application/json` when
      unauthed (router-level verification).
- [x] Live `smoke.sh` against `uvicorn` on `127.0.0.1:8123` — all 10 routes
      green (6 pre-existing + 4 new equity routes). Real production smoke pipeline.
- [x] Production SPA dist verified: `frontend/dist/index.html` + 776 KB JS
      bundle serves correctly, grep confirms `/equity-swing` route and all
      four `/equity/*` API paths are baked into the bundle.
- [x] End-to-end data path exercised: fetched 199 days of EQ bhavcopy (41,367
      rows × 209 symbols), ran `run_equity_swing.py --scan close --mode paper
      --force`. Strategy executed across full universe, DB writes clean,
      session exited cleanly. Open positions resumed correctly from DB.

### Phase 5 — Documentation + ops
- [x] Add `[equity_swing]` section to `config_template.ini` with all tunables
      and inline unit comments (Phase 1/2/3 grouped, defaults match
      `VarsityEquitySwingStrategy.DEFAULTS`).
- [x] Update `ARCHITECTURE.md` — new strategy row in §4.2, full §4.4 stanza
      ("Varsity Equity Swing"), API table extended with /equity/* rows, and
      `deploy/equity-swing-*` units listed in §12.2.
- [x] Update `README.md` — quickstart step 5 (run_equity_swing.py), entry
      points table, strategies table.
- [x] Final review: 206-test sweep passes (test_backend, test_backend_equity,
      test_pair_candidates, test_varsity_equity_swing, test_market_profile,
      test_persistence, test_dashboard_auth, test_backtest, test_arbitrage,
      test_calendar_meanreversion, test_risk_analyzer, test_trade_proposer).
      One pre-existing failure in test_pair_trading.py::test_hedge_qty_matches_notional
      confirmed on baseline (git stash) — not introduced by this work.
- [x] `bash -n deploy/smoke.sh` clean; `systemd-analyze verify` clean on all
      four `equity-swing-*` units (only complaint is the deliberate
      `/opt/taleb-karpathy-kite/...` placeholder path).
- [x] `npm run build` clean (vite build emits 776 KB bundle, no TS errors).

## Out of scope for v1
- Live trading mode (kite.place_order). Signals + paper only.
- Autoresearch hookup (Phase 6 candidate; design keeps the door open via the
  ZERO_TRADE_PENALTY-compatible scorer).
- Scale-outs / partial profit booking. Single binary exit per position.
- Sector rotation / sector-relative strength. Stock-level only.
- Intraday 5-min bar signals. Daily + 30-min bars only.

## Review

### Phase 1 — shipped 2026-05-10

**Code delivered**
- `data_cache/nifty200.csv` (209 symbols, derived from F&O STF universe).
- `strategies/_indicators.py` — `sma`, `ema`, `atr`, `adx`, `donchian_high/low`,
  `chandelier_stop_long`. Pure pandas, NaN warm-up (no zero-fallback).
- `strategies/_eq_data.py` — equity OHLCV loader: per-symbol cache CSVs
  (preferred) or front-month STF proxy from existing F&O bhavcopy archive.
  Volume converted to share-equivalent via `NewBrdLotQty`.
- `strategies/varsity_equity_swing.py` — `VarsityEquitySwingStrategy` with
  trend (SMA short/long + ADX), gap, pullback-or-breakout setup, ATR sizing,
  SL/target/Chandelier/time-stop state machine; signals + paper modes; live
  raises NotImplementedError. Registered in `strategies/__init__.py`.
- `backtest_varsity_equity.py` — date-by-date replay; entries at next-bar
  open (no look-ahead), intraday-touch exits, 0.20 % round-trip cost,
  ZERO_TRADE_PENALTY = -1e6 sentinel, per-trade ledger TSV, per-symbol
  breakdown, full metrics block (Sharpe, Calmar, max DD, profit factor,
  CAGR).
- `fetch_bhavcopy_eq.py` — NSE EQ-segment fetcher writing per-symbol
  OHLCV to `data_cache/equity_ohlcv/`. Idempotent. Will run on the user's
  VPS where NSE archives are reachable; the dev sandbox blocks them.
- `tests/test_varsity_equity_swing.py` — 17 tests covering indicators,
  strategy gates, position state machine, integration smoke that asserts
  ≥1 trade fires (catches the silent-empty-universe lesson) and zero-trade
  returns sentinel (catches flat-fitness lesson).

**Test status**: 17/17 new tests pass, 292 prior tests pass, 3 pre-existing
`test_pair_trading.py` failures unchanged (documented on main).

**Backtest run on local STF-proxy archive** (125 days, 2025-10-31 → 2026-05-07,
209-symbol universe, SMA20/50, ADX>18, ATR-stop 2.5×, RR=2):
- 30 trades, avg holding 9.8 days (within 5-25 day swing target).
- Win rate 26.7 %, avg R = 0.486, profit factor 0.54.
- Net P&L −₹75k on ₹1M capital → −7.5 % gross / ~−15 % CAGR-equiv.
- Exit mix SL_HIT=21 / TIME_STOP=4 / TARGET_HIT=3 / TRAIL_STOP=2.
- Sharpe -1.47, max DD -14.4 %.

**Caveats on the result**
- 125-day window forces SMA20/50 instead of canonical SMA50/200 — much
  more whipsaw-prone (Varsity Module 2 is explicit that 50/200 is the
  reference). This alone explains a large fraction of the SL-heavy mix.
- This window (Nov-2025–May-2026) is the same regime the autoresearch
  lesson flagged as "low-vol / no trend reachable" — multiple low-quality
  setups during a sideways market.
- STF proxy has ~0.3 % basis vs spot; immaterial at ATR-stop scale.
- No MP / OI / FII gates yet — those land in Phase 2 / 3 and are designed
  precisely to filter low-quality setups.

**Conclusion**: pipeline, harness, sizing, exits, and reporting all behave
as designed. The win rate / Sharpe are not investable as-is; they should
not be — Phase 2 (MP + OI confluence) and Phase 3 (FII/DII overlay) exist
to add the filters. Phase 1 deliverable is the **working substrate**, not
the final P&L.

**Recommendation**: proceed to Phase 2. Re-run backtest after MP/OI gates
land — only commit to Phase 3+ if the gates demonstrably tighten the win
rate or expectancy.

**Operator action item**: on the VPS, run
`python fetch_bhavcopy_eq.py --days 800` to populate
`data_cache/equity_ohlcv/`. Once that lands, re-running the backtest with
canonical SMA50/200 on a 3-year window will give a more honest baseline.

### Phase 2 — shipped 2026-05-10

**Code delivered**
- `strategies/_market_profile_eq.py` — daily-bar volume-profile helper with
  rolling N-day VAH/POC/VAL per (symbol, date). Pure pandas, no bars.db dep.
- `strategies/_oi_signal.py` — F&O bhavcopy reader producing per-(symbol,
  date) OI classification (LONG_BUILDUP / SHORT_COVERING / SHORT_BUILDUP /
  LONG_UNWINDING / NEUTRAL). Uses **total OI summed across all live
  expiries**, not front-month only — that fixed a 300 %+ calendar-roll
  artifact discovered during smoke-test (front-month OI craters to 0 on
  expiry day, the new front-month inherits the bulk).
- Strategy wiring: gate features merged into `_ensure_features`; `_signal_at`
  now applies MP veto (close < VAL → reject), MP boost (close > VAH →
  +1 score), OI veto (SHORT_BUILDUP → reject), OI boost (LONG_BUILDUP →
  +1 score). All gates have config flags.
- Backtest CLI `--mp on|off  --oi on|off`.
- 8 new tests covering value-area math, OI classification, MP veto, OI
  veto, OI boost. Total 25 strategy tests, all green.

**Two latent bugs found and fixed during the Phase 2 backtest**
1. Chandelier trail can pin to a stale rolling-max when the source data
   has corp-action discontinuity (NUVAMA had a ~5:1 split mid-archive;
   pre-split highs of ₹7600+ stayed in the lookback window). Strategy
   now refuses to update the trail stop unless `chandelier < close`.
2. Exit fills must lie within today's [low, high] range. Previously the
   exit logic could "fill" at a stop level above today's high (the bug
   that produced the spurious 437 % NUVAMA win). Now each exit branch
   gates on reachability; SL/target gaps are explicitly handled.
3. Data-side: STF proxy loader drops symbols with any single-bar move
   > 30 % across the whole panel — proxy data isn't corp-action adjusted
   and partial detection isn't worth the bug surface. Operators bypass
   this filter by populating the per-symbol EQ cache via
   `fetch_bhavcopy_eq.py`. Same shape as the dividend-asymmetry lesson:
   one-time structural shifts must be encoded as a default-exclusion.

**Phase 2 backtest** (same window/params as Phase 1 baseline, post-fixes):

| Config       | Trades | Win % | PF   | Avg R | Net P&L  | Sharpe | Max DD  |
|--------------|--------|-------|------|-------|----------|--------|---------|
| Baseline     | 29     | 24.1  | 0.33 | −0.47 | −₹115k   | −2.47  | −15.2 % |
| MP only      | 25     | 24.0  | 0.36 | −0.49 | −₹109k   | −2.22  | −14.6 % |
| **OI only**  | 29     | 34.5  | 0.46 | −0.27 | **−₹70k**| **−1.72** | **−10.4 %** |
| Both         | 28     | 25.0  | 0.38 | −0.49 | −₹90k    | −2.26  | −12.7 % |

**Decisions** (per the dividend-asymmetry lesson):
- **OI gate default ON** — clear win-rate (+10 pp) and Sharpe (+0.75)
  improvement, real edge.
- **MP gate default OFF** — neutral-to-slightly-harmful on this window
  on STF-proxy data; remains available as a tunable. Likely deserves
  re-evaluation once the EQ cache is populated (split-adjusted EQ is
  cleaner than STF proxy, and MP is sensitive to clean price levels).
- **Strategy is still net-loss** on this 125-day window — that's the
  data regime, not the gates. Phase 3 (FII/DII overlay + cron) will
  add another filter; Phase 1 caveats about needing canonical SMA50/200
  on a longer window still apply.

**Tests**: 25/25 strategy tests pass, 300/303 repo-wide pass; 3 prior
pair_trading failures unchanged.

**Recommendation**: proceed to Phase 3 (FII/DII overlay, cron entry-point,
paper-book persistence). Re-evaluate MP default once split-adjusted EQ
data lands.

---

# Arbitrage / calendar review — priority 1–4 fixes (2026-05-09)

## Motivation
Code-review against Varsity (Trading Systems / Calendar Spreads) surfaced four
correctness issues on `strategies/arbitrage.py`. User authorized fixes for
priority items 1–4 in the review's suggested-priority list.

## Plan
- [x] **#1 — prefix-collision** in `_symbol_from_tradingsymbol`. Authoritative
  `_ts_to_name` populated by `_build_fut_index`; longest-prefix fallback when
  the map is unseeded. 3 regression tests in `TestSymbolAttribution`.
- [x] **#2 — per-trade `closed_trades.realized_pnl`.** `CalendarTrade` now
  carries `_baseline_realized` / `_baseline_costs` snapshotted at first fill;
  archive records the delta. Backtest reporter rewritten to sort by per-trade
  P&L and show top 5 + bottom 5. Regression test in `TestPerTradePnL`.
- [x] **#3 — per-symbol dividend yield.** `dividend_yields` config CSV parsed
  by `_parse_yield_map`; `_get_dividend_yield` + plumbed through
  `_fair_future`, `_annualized_basis`, and `carry_diff`. Backtest CLI gained
  `--dividend-yields`. Verified live: TCS basis on 2026-04-17 dropped from
  `-33.10%` to `-3.17%` annualized when `TCS=0.30` was passed.
  4 tests in `TestPerSymbolDividendYield`.
- [x] **#4a (2c) — spot-fallback suppresses basis arm.** Snapshot carries
  `spot_is_fallback`; `scan_and_propose` debug-logs and skips the basis arm
  when set. 2 tests in `TestSpotFallbackSuppression`.
- [x] **#4b (2d) — rolled-out leg pricing.** `_build_calendar_exit` and
  `_update_unrealized` log WARN/DEBUG on missing leg, keep last-known mark.
  1 test in `TestRolledLegPricing`.

## Out of scope (deferred)
- 2e placeholder `LONG_CALENDAR` between first and second leg fill (review §2e).
- §3 and §4 items (review priority 5).

## Review
- All 38 `tests/test_arbitrage.py` cases pass (was 23 pre-change).
- 3 pre-existing failures in `tests/test_pair_trading.py` are on `main` and
  unrelated to this work — confirmed via `git stash` round-trip.
- `config.ini` is gitignored; the new `dividend_yields` line is documented
  inline in the local copy only. If the operator wants the config example to
  live in version control, add an `[arbitrage]` section to `config_template.ini`.
- Backtester smoke test on RELIANCE/INFY/TCS for 2026-04-01..17 ran clean
  end-to-end.

---

# Dashboard auth — gate the API behind a password-cookie session

## Motivation

Per the security review (CRITICAL #1), the backend has no authentication. Any
internet caller who can reach `dashboard.propelytics.in` can `POST /runs`
against the operator's cached Kite session. Decision (2026-05-05): cookie-
session with password login (Option B), 7-day TTL, plaintext password in
`.env`, migrate Kite OAuth endpoints under the new gate too.

## Plan

- [ ] **Backend** — `backend/settings.py` gains `dashboard_password`,
  `dashboard_session_secret`, `dashboard_session_max_age_days` (default 7);
  fail-fast at app start if either secret is empty.
- [ ] **Backend** — add Starlette `SessionMiddleware` (HttpOnly + Secure +
  SameSite=Lax + 7d) wired up via `dashboard_url` for `https_only`.
- [ ] **Backend** — new `backend/dashboard_auth.py` with `require_session`
  dependency.
- [ ] **Backend** — new `backend/routers/dashboard_session.py`:
  `POST /session/login`, `POST /session/logout`, `GET /session/me`. These
  are the ONLY routes outside the gate.
- [ ] **Backend** — every other router (`auth`, `strategies`, `runs`,
  `market-profile`, `pair-candidates`) registered with
  `dependencies=[Depends(require_session)]`.
- [ ] **Backend** — pytest coverage in `tests/test_dashboard_auth.py`
  (login pass/fail, gating, logout, tampered cookie, expired cookie).
  Install `httpx` so the existing TestClient suites run too.
- [ ] **Frontend** — `lib/api.ts` adds session endpoints + 401 → typed
  `UnauthorizedError`; QueryClient global onError invalidates the session
  query so a stale session kicks the user back to the login page mid-flow.
- [ ] **Frontend** — new `pages/DashboardLoginPage.tsx`. Single password
  field, error state, no signup/forgot.
- [ ] **Frontend** — `App.tsx` gates the entire app on the session query
  before any other route renders.
- [ ] **Frontend** — `Header.tsx` "Logout" button now clears the dashboard
  session (the per-Kite Disconnect can stay implicit — it expires daily).
- [ ] **Ops** — append `DASHBOARD_SESSION_SECRET` (random token-urlsafe(64))
  to `.env` without echoing it. Operator sets `DASHBOARD_PASSWORD` in their
  own shell. Add nginx `limit_req` on `/session/login` (defer if scope creep).
- [ ] **Ops** — update `backend/README.md` with the two new env vars.
- [ ] **Verify** — pytest green; backend boots and refuses to boot without
  the secrets; smoke-test login → API access → logout → access blocked
  via curl from outside the SPA.

## Review

Shipped (CRITICAL #1 closed):

- `backend/settings.py`: `dashboard_password`, `dashboard_session_secret`,
  `dashboard_session_max_age_days`. `get_settings()` now refuses to return
  if either secret is empty — backend won't boot in a half-configured state.
- `backend/dashboard_auth.py` (new): `password_matches` (constant-time
  compare), `mark_authenticated`, `clear_session`, `is_authenticated`,
  `require_session` FastAPI dependency. Bare 401 with no leak about which
  check failed.
- `backend/routers/dashboard_session.py` (new): `/session/me`,
  `/session/login`, `/session/logout` — the only public surface.
- `backend/main.py`: SessionMiddleware (HttpOnly + SameSite=Lax + Secure
  on HTTPS, 7d) and `Depends(require_session)` on every other router.
  Lax (not Strict) so the Kite OAuth callback still carries the cookie.
- `tests/test_dashboard_auth.py` (new, 20 cases): login pass/fail, gating
  on every router, logout, tampered cookie, public-route exemption.
  `tests/conftest.py` stamps test env vars; `tests/_helpers.py::login_client`
  is the per-test login. 235 tests green (was 190+45 with 8 httpx-blocked).
- `frontend/src/lib/api.ts`: `UnauthorizedError` thrown on any 401, plus
  `sessionStatus` / `sessionLogin` / `sessionLogout`.
- `frontend/src/main.tsx`: QueryCache `onError` invalidates the session
  query on any cross-component 401, so a mid-session expiry flips the
  whole app back to the login page without per-component handling.
- `frontend/src/App.tsx`: `useQuery(["session"])` gates the entire app
  before any route renders.
- `frontend/src/pages/DashboardLoginPage.tsx` (new): single password form,
  autoFocus, error state, no signup/forgot.
- `frontend/src/components/Header.tsx`: "Sign out" now clears the
  dashboard session (Kite token expires daily on its own).
- `/etc/nginx/sites-enabled/dashboard` + `deploy/nginx-dashboard.conf.example`:
  added `session` to the API prefix whitelist.
- `deploy/smoke.sh`: every gated route now expected to return 401 (still
  asserts JSON content-type — same proxy-correctness signal). `/session/me`
  is the only 200-expected route. 12/12 probes green across both bases.
- `backend/README.md`: documents the password gate, the secret-generation
  one-liner, and the rotate-by-changing-secret semantics.
- `.env`: `DASHBOARD_SESSION_SECRET` appended via `printf …(secrets.token_urlsafe(64))`
  (value never echoed). `DASHBOARD_PASSWORD` set by the operator with `read -rs`.

Verified end-to-end:

- pytest: 235 passed.
- Public-host smoke (https://dashboard.propelytics.in): /session/me → 200,
  every other route → 401, all JSON.
- Authed cycle via curl: login (204) → /strategies (200) → logout (204)
  → /strategies (401). Wrong password → 401.
- Backend refuses to start with either secret missing (verified by reading
  the code path; `get_settings()` raises before app construction).

Out of scope (deliberate):

- nginx `limit_req` on `/session/login` — easy to add later, but a single
  attacker hammering one endpoint over HTTPS won't materially change the
  threat model with a long-enough password.
- bcrypt-hashing the password at rest. `.env` is the trust boundary; the
  hash only buys protection against env-file leaks where the running
  process is uncompromised, which is a narrow scenario for this setup.
- Rate-limiting / lockout / 2FA — single-operator dashboard, deferred.

---

# Issue #3 — Surface current pair-trading candidates in frontend

## Audit findings

- `screen_pairs.py` (root) generates 11 metrics per candidate and writes them to `data_cache/pair_candidates.csv`. Daily systemd timer (`deploy/screen-pairs.timer`) regenerates it Mon-Fri at 19:00 IST. **No DB persistence** — CSV is the source of truth.
- The screener does **not** carry a z-score in its output. The pair-trading strategy computes z-score per-tick at runtime against a rolling window (`strategies/pair_trading.py:277`). Issue requires a z-score visible in the candidate listing.
- `data_cache/pair_candidates.csv` already has `spread_mean` and `spread_std` from the panel — so a "z-score at screen time" is computable as `(spread[-1] - spread_mean) / spread_std` with the panel data already in memory during screening.
- No FastAPI endpoint exposes candidates; nearest pattern is `backend/routers/runs.py`.
- Frontend has no candidate browser. `ProposalTable.tsx` has reusable pair-grouping logic but isn't a fit — it consumes signals/trades from a run, not screener output.
- `<Table>` primitive (`frontend/src/components/ui/table.tsx`) already wraps in `overflow-auto`, so mobile horizontal scroll is free (per lessons.md).

## Plan

- [x] **Phase 1 — Backend data**: extend `screen_pairs.py` to write `latest_spread`, `latest_z_score`, `last_close_a`, `last_close_b`, `last_data_date` per candidate. Regenerate CSV.
- [x] **Phase 2 — Backend API**: new `backend/routers/pair_candidates.py` exposing `GET /pair-candidates` with a typed `PairCandidate` model and a `generated_at` timestamp from CSV mtime. Register router in `backend/main.py`.
- [x] **Phase 3 — Frontend**: add types + `api.pairCandidates`, new `PairCandidatesPage` with sortable table (default: |z-score| desc) and a min-|z| filter input, route + Header nav link.
- [x] **Phase 4 — Verify**: hit endpoint with curl (56 candidates, top by |z| are ADANIPORTS/LT z=2.83, ADANIPORTS/HEROMOTOCO z=2.74, ASIANPAINT/NTPC z=2.70). `npm run build` passes.

## Review

Changes shipped:

- `screen_pairs.py`: appended 5 fields to each candidate dict — `latest_spread`, `latest_z_score`, `last_close_a`, `last_close_b`, `last_data_date`. Z-score uses the panel-wide mean/std (consistent with the rest of the row's metrics, computed from the same panel).
- `data_cache/pair_candidates.csv`: regenerated with the new columns.
- `backend/routers/pair_candidates.py`: new router. CSV-only read path — no SQLite — since the daily systemd timer is the canonical source. `generated_at` derived from file mtime so the UI can display freshness. Pydantic model coerces empty/`nan` strings to `None` defensively.
- `backend/main.py`: imports + registers the new router.
- `frontend/src/lib/types.ts`: `PairCandidate` and `PairCandidatesResponse` types.
- `frontend/src/lib/api.ts`: `api.pairCandidates`.
- `frontend/src/pages/PairCandidatesPage.tsx`: new page. Sortable columns (click headers, default |z| desc), min-|z| filter, badge highlighting on |z| ≥ 2, empty/loading/error states, "Generated at <ts>" header with last bar date.
- `frontend/src/App.tsx`: registers `/pair-candidates` route.
- `frontend/src/components/Header.tsx`: adds "Pair Candidates" nav link (collapses to "Pairs" under sm), with a `GitBranch` icon.

## Acceptance criteria check

- [x] Candidates visible in frontend without inspecting logs/backend state — page at `/pair-candidates`.
- [x] Each row shows full metric set — pair, latest leg prices, z-score, rank, p-value, half-life, correlation, spread vol %, hedge ratio. Tooltip on column headers explains each.
- [x] Refresh in line with backend cadence — react-query refetch every 5 min picks up CSV regeneration; `generated_at` shown in header so staleness is visible.
- [x] Sortable by z-score (and every other numeric column). Filterable by min |z|.

## Out of scope

- Live z-score from running strategies (would require coupling the candidate page to active runs — the screener z-score at last bar is sufficient for the MVP).
- Manual "Re-screen now" button (multi-second Python job, defer until needed).
- Persisting candidates to SQLite (CSV already covered by systemd timer).
- Per-pair detail page (could chart the spread + rolling z-score; defer).

## Notes / known small gaps

- The Z-score reported is computed at *screen time* (panel mean/std vs latest bar of the panel). The live pair-trading strategy uses a *rolling* lookback that may differ slightly. For "what is the strategy considering?" this is close enough — both views look at the same candidate set with comparable spread stats.
- `backend/run_manager.py:38` still uses naive `datetime.now()` (per issue #2 lessons). The new endpoint emits a timezone-aware `generated_at` from `datetime.fromtimestamp(..., tz=timezone.utc)` — small inconsistency but isolated to this surface.

---

# Post-deploy API smoke test

## Motivation

On 2026-05-05 a deploy shipped a new router (`/pair-candidates`) that the existing pytest suite covered, but the live frontend still broke because nginx's API prefix whitelist was not updated. Requests fell through to the SPA, which returned `<!doctype html>`, which the frontend tried to `JSON.parse`. `tests/test_backend.py` cannot catch this — `TestClient` bypasses the reverse proxy entirely. We need an end-to-end probe.

## Plan

- [x] Write `deploy/smoke.sh` — bash + curl, takes one or more base URLs, probes every public no-auth route, asserts `Content-Type: application/json` (the precise discriminator vs. nginx fallthrough). Per-route status check: `2xx` always, with `503` permitted only for `/pair-candidates` (CSV may be absent on first deploy).
- [x] Routes covered: `/auth/status`, `/strategies`, `/runs`, `/market-profile/symbols`, `/pair-candidates`. Path-param and auth-gated endpoints stay out of scope (already covered by pytest). `GET /` dropped after the first run flagged it: nginx's `location /` *intentionally* serves the SPA's `index.html` for HTML5 router fallback, so the meta endpoint is unreachable through the public host by design.
- [x] Wire into `deploy/redeploy.sh` — runs after the `is-active` check, against `127.0.0.1:8000` always and `$SMOKE_PUBLIC_URL` if set. A failed smoke fails the deploy with exit 3.
- [x] Verify end-to-end: passes against both bases when correct; with `pair-candidates` deleted from the live nginx whitelist, smoke reproduces today's failure (`status=200 ctype=text/html ... nginx likely fell through to SPA`) with exit 1. nginx restored.
- [x] Capture lesson in `tasks/lessons.md`: `TestClient`-level coverage ≠ proxy coverage; `Content-Type: application/json` is the load-bearing assertion.

## Review

Files added/changed:

- `deploy/smoke.sh` — new. Bash + curl, retries connect briefly (uvicorn warmup after restart), 5 routes × N base URLs, fails fast on the first non-JSON or non-2xx (with `/pair-candidates` allowed 503 on a fresh deploy).
- `deploy/redeploy.sh` — added a step 6 that builds a `SMOKE_BASES` array (always localhost; appends `$SMOKE_PUBLIC_URL` if set) and invokes `smoke.sh`, exiting 3 on failure.
- `tasks/lessons.md` — new section: TestClient-level coverage cannot see nginx; the content-type discriminator is the bug-catcher; instructions for updating the routes list.

Key design choices:

- **Bash + curl** over pytest: smoke runs from inside `redeploy.sh`, no venv assumption. The whole script is ~80 lines and depends only on `curl`/`mktemp`/`sed`-free.
- **Content-Type, not just status code**: nginx returns `200 text/html` when it falls through to the SPA, so a status-only check would pass on the exact regression we're trying to catch. The script's load-bearing line is `[[ "$ctype" != application/json* ]]`.
- **Two base URLs, not one**: `127.0.0.1:8000` catches backend regressions; `$SMOKE_PUBLIC_URL` catches nginx whitelist drift. Same probe code, two layers.
- **Auth-gated and path-param routes excluded**: `tests/test_backend.py` already exercises them with mocks. Smoke is for "is the route reachable from a real HTTP client at all", not for testing logic.
- **Allow-503 list, kept tight**: only `/pair-candidates` (CSV may legitimately be missing). Anything else returning 503 from a fresh deploy is genuinely broken.

---

# Pin Python deps with hashes (security follow-up #2)

## Motivation

Per `tasks/security-followups.md` #2: no `requirements.txt`, `pyproject.toml`,
or `Pipfile` in the repo. The project venv at `.venv/` (Python 3.11.15, 53
packages) is whatever `pip install` produced over time. There is no
reproducible way to rebuild it, and a future supply-chain compromise of any
transitive dep — including the ones the operator never explicitly installed
— executes with the same blast radius as the running process. Hash-pinning
turns "trust whoever publishes to PyPI tomorrow" into "trust exactly the
artifact bytes we audited today."

Two stale `.cpython-38.pyc` files in `__pycache__/` (`greeks_engine`,
`market_profile`, `risk_analyzer`, `trade_proposer`) confirm earlier 3.8 →
3.11 churn — pinning is also the right time to clear those.

## Approach

Use **`uv pip compile --generate-hashes`** rather than `pip-tools`. `uv` is
already on the box (`/root/.local/bin/uv`); `pip-tools` is not installed
anywhere. Output format and `--require-hashes` semantics on the install
side are identical, so this is a pure plumbing choice. No new dep added
to the project venv.

Two-file split:
- `requirements.in` — top-level deps only, hand-written, no pins.
- `requirements.lock` — full transitive closure with `==` pins and
  `--hash=sha256:…` per artifact. Generated, not edited.

## Plan

- [x] **Manifest** — `requirements.in` (13 runtime deps) and
  `requirements-dev.in` (`pytest`, `httpx`).
- [x] **Lockfile** — `requirements.lock` (49 packages) and
  `requirements-dev.lock` (10 packages, constrained against the
  runtime lock). Generated with `uv pip compile --generate-hashes
  --python-version 3.11`.
- [x] **Verify install** — throwaway uv venv installed cleanly with
  `--require-hashes`; 241 pytest tests passed against it.
- [x] **CI check** — both `.github/workflows/lockfile.yml` (regen +
  `git diff --exit-code` + clean-venv install) **and**
  `deploy/check_lockfile.sh` (same regen-and-diff logic, called from
  `redeploy.sh` step 4 before the service restart). Drift demo:
  appending `requests` to `requirements.in` made the script exit 11
  with a precise diff; restoring brought it back to clean.
- [x] **Stale artifacts** — four `__pycache__/*.cpython-38.pyc` files
  removed; `.gitignore` already excluded `*.pyc` and `__pycache__/`.
- [x] **Docs** — root `README.md` Quick start now points at
  `--require-hashes` and there's a new "Reproducing the venv" section
  with the install + regen commands and a paragraph on the two-layer
  drift enforcement.
- [x] **Verify** — `pytest tests/` 241 passed in the existing project
  venv. Top-level imports + `starlette.middleware.sessions`,
  `statsmodels.tsa.stattools`, `scipy.stats` all OK.

## Review

Files added:

- `requirements.in`, `requirements-dev.in` — top-level manifests, no
  pins. Source of truth for "what does the project actually import?"
- `requirements.lock` (~63KB, 46 packages, fully hashed),
  `requirements-dev.lock` (~3KB, 10 packages, constrained against the
  runtime lock so shared transitives like `anyio` / `idna` /
  `typing-extensions` can't drift between layers).
- `.github/workflows/lockfile.yml` — drift gate on PR. Triggers only on
  changes to `requirements*.{in,lock}` or itself. Two assertions:
  regen + `git diff --exit-code`, and a clean-venv install with
  `--require-hashes`.
- `deploy/check_lockfile.sh` — same drift logic as the CI workflow,
  invoked from `redeploy.sh` step 4. Exists so an out-of-band deploy
  from a side branch (which would skip the PR-gated workflow) still
  can't ship a drifted lock. Exit 10 if `uv` is missing, exit 11 on
  drift.

Files changed:

- `deploy/redeploy.sh` — new step 4 (lockfile drift check) inserted
  between the FF and the frontend rebuild. Comment numbers shifted.
- `README.md` — Quick-start `pip install …` line replaced with
  `--require-hashes` install. New "Reproducing the venv" section
  documents the two-file split, the install command, the regen
  command, and the two-layer drift enforcement.

Files removed:

- `__pycache__/greeks_engine.cpython-38.pyc`,
  `__pycache__/market_profile.cpython-38.pyc`,
  `__pycache__/risk_analyzer.cpython-38.pyc`,
  `__pycache__/trade_proposer.cpython-38.pyc` — stale 3.8 bytecode left
  over from the 3.8 → 3.11 migration. Already gitignored, so this is a
  filesystem cleanup, not a tracked change.

## Key design choices

- **`uv` over `pip-tools`.** `uv` was already on `/root/.local/bin/uv`;
  `pip-tools` was not. `uv pip compile --generate-hashes` produces the
  same `--require-hashes`-compatible output with the same `==` pins
  and `--hash=sha256:…` annotations. No new dev dep added to the
  project.
- **Two-file split (.in / .lock), not one.** A single `requirements.txt`
  produced by `pip freeze` mixes intentional deps with transitives,
  giving no signal about "what does this project actually need?"
  Splitting makes bumps explicit: edit `.in`, regen `.lock`. The
  reviewer sees exactly which line you intended to change.
- **Constrained dev lock (`-c requirements.lock`).** Without this,
  `pytest`-side deps like `anyio` could resolve to a different version
  than the runtime side, even though both layers install into the same
  venv. The constraint pins shared transitives to whatever the runtime
  lock decided.
- **Belt-and-braces drift check (workflow + redeploy script).** PR
  gate alone leaves a hole: an admin can hot-fix on the VPS by
  cherry-picking onto a non-`main` branch and running `redeploy.sh`,
  bypassing GitHub Actions. The redeploy-side script closes that.
  Costs ~2s per deploy; saves a "lock looked fine in PR but the deployed
  branch had a drifted lock" incident.
- **`mktemp -d` + cd, not output-to-tmpfile.** First version of
  `check_lockfile.sh` failed clean runs because uv embeds the
  `--output-file` path in the autogen header. Using a tmp directory
  with the canonical filenames inside it produces byte-identical
  output that diffs cleanly.

## Out of scope (deliberate)

- Migrating to `pyproject.toml` / PEP 621. Pure plumbing benefit; same
  lockfile semantics and same threat model whether the source-of-truth
  manifest is `requirements.in` or `[project.dependencies]`. Defer
  unless we adopt a build backend.
- Pinning the Python interpreter version. `pyvenv.cfg` records 3.11.15
  and the lockfile is generated with `--python-version 3.11`. A real
  Python upgrade is its own decision separate from dep hygiene.
- Frontend deps. `package-lock.json` is already committed and already
  hash-locks every npm artifact via `integrity:` fields. Same threat
  model, different ecosystem, already solved.
- Bumping the existing project venv to match the lockfile (lock has
  `cryptography 48.0.0` / `pydantic 2.13.4` / `pyOpenSSL 26.2.0`; live
  venv is one minor older on each). `pip install --require-hashes
  -r requirements.lock -r requirements-dev.lock` against the live venv
  on this dev box would in-place upgrade those three. Tests already
  pass on the current versions and the lock; the actual upgrade is a
  one-command operator action and it's safe to defer to the next
  redeploy. Listed here so it doesn't get lost.

## Out of scope (deliberate)

- Migrating to `pyproject.toml` / `[project.dependencies]`. Pure plumbing
  benefit; does not change the threat model. `requirements.in` is the
  same source of truth.
- Pinning the system Python interpreter. The repo's venv is created from
  uv-managed Python 3.11.15 (per `pyvenv.cfg`); a Python upgrade is its
  own decision separate from dep hygiene.
- Pinning frontend (npm) deps. `package-lock.json` already does this and
  is committed. Out of this follow-up's scope.
- Removing `anthropic` import from `claude_example.py`. That file is
  untracked (a demo) and not part of the deployed surface.

---

# Hedge silently no-op — fix sizing/threshold mismatch (2026-05-08)

## Motivation

Today's paper session (`logs/paper-2026-05-07.log`) crossed the rehedge gate
~150 times and executed exactly zero futures hedges. The straddle held flat
all day, bleeding theta with no gamma capture (net −₹1,358). Root cause: the
gate is in fractional lots (`rehedge_delta_threshold = 0.15`), and the
hard-hedge sizer is `lots = round(delta/lot_size)`. NIFTY lot_size is 65
post-restructuring, so any drift below 0.5 lots (= 32.5 discrete delta) rounds
to 0 and `_generate_hard_delta_proposals` returns `[]` silently. Today's peak
drift was 0.33 lots — never close.

## Plan

- [x] **Visibility** — `_generate_hard_delta_proposals` now logs when sizing
  rounds to 0 lots so the failure mode is grep-able.
- [x] **Threshold floor** — `best_params.json` and `config.ini` default
  `rehedge_delta_threshold` raised 0.15 → 0.6. 0.6 is clear of Python's
  banker's-rounding tie at exactly 0.5.
- [x] **Search space** — `autoresearch_loop.TUNABLE_RANGES` widened from
  (0.05, 0.30) to (0.5, 1.5) so the optimizer can no longer pick a value
  below the executable floor.
- [x] **Synthetic lot size** — `backtest.generate_synthetic_data` now
  defaults `lot_size` from a per-underlying dict (NIFTY=65, BANKNIFTY=15,
  FINNIFTY=25) instead of a hard-coded 25, so the optimizer's synthetic
  fallback path sees realistic rounding.
- [x] **Tests** — `pytest tests/test_taleb_karpathy.py tests/test_backtest.py`
  passes (75 tests).
- [x] **Re-sweep** — `run_autoresearch.py` running in background against
  `data_cache/NIFTY_20260407_20260507.csv` (20 intraday days), 30
  experiments × 3 cycles, metric=net_pnl, seed=42. Pre-resweep
  `best_params.json` snapshotted to `best_params.pre-resweep-2026-05-07.json`.
- [x] **Lesson** — captured in `tasks/lessons.md` ("Threshold gates in
  continuous units, executors in integer lots").

## Review

Code change is small (one log line + one threshold value + one tuple + one
default-dict). The leverage comes from making the silent failure mode loud:
any future lot-size change or threshold drift now lights up a log line
immediately rather than producing a flat day.

## Out of scope (deliberate)

- Switching the optimizer's gamma-scalp metric from estimated `0.5·γ·dS²`
  to realized hedge P/L. The current metric scored a "hedge nothing" param
  set as healthy — that's a real blind spot, but it's an autoresearch
  refactor, not a hotfix.
- Backfilling a smoke test that asserts a hedge fires when `delta_in_lots
  > threshold`. Worth doing; queued behind the resweep.
- Reconsidering whether 0.6 lots is the right *operational* threshold (vs.
  defending against the rounding edge). The resweep will pick a sweep-
  optimal value within (0.5, 1.5) and overwrite this baseline.

---

# Autoresearch optimizer-blindness fix (zero-trade penalty + window pre-screen)

## Motivation

The 2026-05-09 weekly cron ran 40 experiments and every single one — including
the baseline — returned `sharpe_ratio = 0.000000`. Investigation: all 3 April
training windows produced **0 trades** under the seed params (the regime never
cleared `min_rv_iv_ratio = 1.22`). With 0 trades, `daily_pnl_history` stayed
empty and `get_strategy_metrics()` defaulted `sharpe_ratio` to 0. Every mutation
tied at 0, nothing was ever accepted, and the "best params" written out were
identical to the prior week's incumbent — falsely framed as a converged optimum.
Same shape as `lessons.md` "Optimizer-blindness corollary".

## Plan

- [x] `autoresearch_loop.py`: add module-level `ZERO_TRADE_PENALTY = -1e6`;
      apply inside `_run_experiment` cycle loop when `total_trades == 0`.
- [x] `run_autoresearch.py`: import the constant, apply same penalty in both
      branches of `patched_run`.
- [x] `run_autoresearch.py`: pre-screen historical training windows after
      `_split_data_into_windows`. Drop windows where seed params produce 0
      trades. Raise `RuntimeError` with actionable message if all windows are
      dead. Warn (don't fail) if holdout produces 0 trades.
- [x] Verify negative path: cron-style invocation against April CSV exits 1
      with the actionable error.
- [x] Verify positive path: with loosened seed (`min_rv_iv_ratio=0.7`,
      `entry_iv_percentile_min=1.0`) all 3 windows kept; tight seed on a
      tradable window correctly applies the penalty.
- [x] Test suite: `tests/test_taleb_karpathy.py` 63/63 pass; full suite has
      3 failures in `tests/test_pair_trading.py` that are unrelated (notional
      sizing math) and pre-existing.

## Review

Three small edits, no new files. Net effect: the optimizer now has gradient
even when most cycles produce no trades, and dead training windows are removed
up-front instead of consuming 40 cycles producing identical 0.0 scores. The
error message tells the operator exactly what to do (widen the data window,
loosen the entry gate, or inspect the CSV).

What this does NOT fix: if a regime is genuinely untradable AND the seed isn't
tradable anywhere, the run errors out — which is the correct behavior, but
means the operator needs to act manually. A future improvement (deferred) is
to make the runner auto-loosen the seed and retry, but that should be a
separate, opt-in feature, not silent magic.

## Out of scope (deliberate)

- Adaptive `mutation_step_size` or multi-axis mutation (option D from the
  diagnosis). Independent improvement; not needed to fix the silent failure.
- Changing the metric formula in `strategies/taleb_karpathy.py` so that 0
  trades returns something other than 0 sharpe. The strategy code is correct;
  the right place to interpret "no trades" as "bad candidate" is the optimizer.
- Auto-promotion of candidate to `best_params.json`. Promotion stays manual.
- The 3 pre-existing pair-trading test failures (`test_hedge_qty_matches_notional`
  and notional-cap pair). Out of scope for this hotfix.

---

# Pair-paper cost hurdle + seed-only z (2026-05-13)

## Motivation

2026-05-13 paper session ended at ₹−39,251 across 3 pairs / 28 round-trips.
Realized losses (~₹39.3k) ≈ transaction costs (~₹39.3k) — virtually all
the bleed was friction. Diagnosis (see `tasks/lessons.md` entry to be
added):

1. No cost hurdle / minimum-edge filter. `scan_and_propose` enters on any
   `|z| ≥ entry_z` regardless of expected ₹ move vs cost.
2. Rolling z-window dilution. `_observe_spread` (pair_trading.py:283-313)
   appends every minute-tick whose spread moves > 1 paisa. The seed is
   daily bhavcopy closes; after ~60 ticks the rolling window is mostly
   intraday observations, std collapses, `|z|=2` becomes an intraday-noise
   trigger. Backtest baseline expected ~5 round-trips per pair over 127
   days; paper today ran ~9 round-trips per pair in one session — ~225×
   the backtest cadence.

## Plan

### Change 1 — Seed-only spread history (no intraday append)

- `strategies/pair_trading.py:_observe_spread`: remove the append-on-move
  block. Function still returns `(spread, prices)` for decision-making,
  but `_spread_history` becomes immutable after `_seed_spread_history`.
- Daily roll-forward is handled by the existing cron model: a new process
  starts every morning, `_seed_spread_history` re-reads bhavcopy which
  contains yesterday's close. Confirms backtest cadence: 1 daily
  observation appended per trading day, via the seed, not intraday.
- Fail-loud at `__init__`: if seeded history < `max(20, lookback_days // 4)`,
  log a WARNING explicitly stating "z unavailable this session — no
  entries will fire". Currently this only surfaces per-tick as a debug
  log inside `_z_score`.

### Change 2 — Cost-hurdle entry filter

- Add config knob `min_edge_multiplier` in `[pair_trading]`. Default
  `1.5`. `0.0` disables the hurdle (parity with current behaviour for
  emergency rollback).
- Helper `_expected_edge_passes_cost_hurdle(z_now, prices, qty_a, qty_b,
  fut_a, fut_b) -> bool`:
  - Expected ₹ move per unit spread = `qty_a × fut_a.lot_size`
    (Varsity Ch. 13 share-count β-weighted P&L — net of β·B hedge,
    one unit of spread change = qty_A_shares of ₹ P&L).
  - Expected Δspread = `(|z_now| - exit_z) × std`.
  - Round-trip cost = sum of `estimate_transaction_cost` for the 4 fills
    (entry A buy/sell + entry B + symmetric exit). Reuses the existing
    `strategies.taleb_karpathy.estimate_transaction_cost` import already
    present at line 463.
  - Pass iff `expected_gain_inr ≥ min_edge_multiplier × round_trip_cost`.
- Wire into `_build_entry_proposals` after qty sizing, before returning.
  On reject, log INFO at the same verbosity as the existing skip-on-
  cap log (line 360) and return `[]`.
- Refactor: extract `_rolling_window_stats() -> Optional[Tuple[mean, std]]`
  out of `_z_score` so both the z-score and the hurdle helper share one
  source of truth for the window stats.

### Tests (`tests/test_pair_trading.py`)

- `TestObserveSpreadDedup` becomes stale (the bug it covered no longer
  has a code path). Replace with one positive test
  `test_observe_spread_does_not_mutate_history` that verifies repeated
  calls leave `_spread_history` length unchanged. Encodes the new
  invariant.
- New class `TestCostHurdle`:
  - `test_hurdle_blocks_low_edge_entry` — small std, `|z|` just past
    `entry_z`, expected gain ≈ cost → 0 proposals + INFO log.
  - `test_hurdle_allows_high_edge_entry` — wide std, large `|z|` →
    proposals returned.
  - `test_hurdle_disabled_with_zero_multiplier` — `min_edge_multiplier=0`
    behaves like today: any `|z| ≥ entry_z` produces proposals.

### Config

- `config.ini` `[pair_trading]`: add `min_edge_multiplier = 1.5` with a
  short WHY comment citing today's incident.
- `config_template.ini`: same.

### Lessons

- Append a `tasks/lessons.md` entry: "Paper z-windows must not be
  diluted by intraday observations when the strategy was tuned on
  daily-bar spread distributions. Cost-hurdle gating is non-optional
  for high-frequency mean-reversion. Verify in paper before assuming
  backtest edge survives."

## Tradeoffs / assumptions surfaced

1. **`min_edge_multiplier = 1.5` is a chosen-not-swept default.** Tighter
   value reduces frequency more aggressively; looser approaches today's
   over-trading. A proper backtest sweep is a follow-up (out of scope
   for this fix).
2. **Seed-only z means a fast intraday dislocation is judged against
   yesterday's std baseline.** That is intentional — it matches the
   daily-bar cadence the parameters were tuned on. If we later want
   intraday adaptation, the right shape is a separate "intraday spread
   regime" detector, not appending to the daily window.
3. **Hurdle uses approximate exit cost at entry prices.** Real exit
   prices will differ; the approximation is conservative on average
   (treats exit cost as if we exit immediately at entry price). Good
   enough as a filter; not load-bearing on P&L computation.
4. **Not touching `entry_z`, `exit_z`, `lookback_days`, `max_holding_days`**
   — all came from cited sweeps. The fix is filter-quality, not band
   geometry.

## Checklist

- [x] Refactor `_z_score` to use new `_rolling_window_stats()` helper
- [x] Remove intraday append from `_observe_spread`; keep observation
      logic
- [x] Loud-startup warning when seed history is too thin to z-score
- [x] Add `min_edge_multiplier` config knob (config.ini). Skipped
      `config_template.ini` — it has no `[pair_trading]` block at all,
      so nothing to update; live config is the source of truth.
- [x] Implement `_expected_edge_passes_cost_hurdle` and wire into
      `_build_entry_proposals`
- [x] Replace `TestObserveSpreadDedup` with `TestObserveSpreadSeedOnly`
      (2 tests encoding the new invariant)
- [x] Add `TestCostHurdle` (3 tests)
- [x] `pytest tests/test_pair_trading.py`: 27 pass, 3 fail —
      same 3 pre-existing sizing-math failures noted in the prior
      autoresearch-fix section (`test_hedge_qty_matches_notional`,
      `test_no_cap_means_no_change`, `test_cap_scales_lots_per_leg_down`);
      no new regressions. Full suite outside pair_trading: 287 pass.
- [x] Append `tasks/lessons.md` entry
- [x] Review section at end of this todo block

## Review

Three code-level changes in `strategies/pair_trading.py` plus a config
knob, a 10-test refresh, and a lessons entry.

1. **Seed-only z** — `_observe_spread` returns `(spread, prices)` and
   nothing else; `_spread_history` is now pinned to the daily bhavcopy
   seed loaded at `__init__`. The rolling window `_z_score` uses
   matches the daily-bar distribution the parameters were swept on.
2. **Cost-hurdle gate** — `_expected_edge_passes_cost_hurdle` runs
   inside `_build_entry_proposals` after sizing. Expected ₹ gain =
   `(|z| − exit_z) × rolling_std × qty_a_shares`; round-trip cost is
   summed from `estimate_transaction_cost` for all four legs at entry
   prices. Refuses when expected gain < `min_edge_multiplier × cost`.
3. **Loud startup warning** — `__init__` logs a WARNING if the seed
   produced fewer than `max(20, lookback_days // 4)` observations,
   making the silent-no-entries failure mode operator-visible.

Net effect on tomorrow's `pair-paper.service` session: signals fire on
the daily-bar z distribution (not intraday-noise dilution) and only
when the expected ₹ move clears 1.5× friction. Today's behaviour
(28 round-trips at ~₹1.4k friction each) is statistically blocked.

What this does NOT do:
- It doesn't sweep `min_edge_multiplier`. 1.5 is the working default
  that matches the diagnosis; a proper backtest sweep with realistic
  cost modelling is a follow-up.
- It doesn't change the z-band geometry (`entry_z=2.0`, `exit_z=0.75`,
  `lookback_days=60`, `max_holding_days=7`) — all came from cited
  sweeps and remain in scope. The fix is filter-quality.
- It doesn't touch the 3 pre-existing sizing-math test failures — out
  of scope, as documented in the prior autoresearch fix's Review.

## Out of scope (deliberate)

- Backtest sweep for `min_edge_multiplier` value. Should be done with
  the live cost model and the daily-bar spread distribution, then
  committed alongside any tightening of the default.
- A post-exit time cooldown ("don't re-enter same pair within N
  minutes"). The seed-only z change already removes the main driver
  of rapid re-entry (intraday dilution). If trade frequency is still
  too high after this change ships, a cooldown is the next lever.
- Refresh `_spread_history` mid-session from a new bhavcopy. Bhavcopy
  is EOD-only; this isn't a real lever within a single session.

# Remove EOD flatten — hold to strategy exit only (2026-05-19)

## Motivation
Today's 2026-05-19 session entered RELIANCE/ITC at 15:02:19 (z=-2.01) and
the EOD flatten at 15:25:00 force-closed it 23 minutes later for a -₹3,850
loss. The strategy's exit triggers (mean-revert at |z|<=0.75, stop at
effective_stop_z, MAX_HOLD at 7d) never had a chance to fire. The paper
runner's `FLATTEN_AT = (15, 25)` is an operational artefact (oneshot
systemd unit dies overnight), not a strategic exit condition.

User decision 2026-05-19: positions should be held to a strategy-driven
exit. Apply to both `--system=baseline` and `--system=persistent`. On
futures expiry, force-flatten on the contract's last trading day (no
overnight roll, no entries near expiry needed beyond that — `MAX_HOLD`=7d
keeps positions away from deep-expiry territory naturally).

## Design

### Position lifecycle change
- Replace unconditional `flatten_one` loop at session end (line 545-546)
  with a `persist_state_one` loop that serialises each strategy's
  in-memory state to disk.
- At session start (after `build_strategies`), restore state from disk
  for any pair whose saved state exists.
- For pairs whose saved state has an OPEN position but is NOT in today's
  refreshed candidate list: instantiate a strategy for them anyway, so
  we can manage them to their strategy exit. No new entries possible
  (position is already open).
- Hedge ratio: if saved state has `position != FLAT`, use saved β
  (the trade is on the books with that ratio); else accept today's
  screener β.

### State file
- `data_cache/pair_paper_state_<system>.json` (per system).
- Atomic write: write to `.tmp` then `os.rename`.
- Schema:
  ```
  {
    "system": "baseline",
    "updated_at": "2026-05-19T15:30:00",
    "pairs": [
      {
        "pair": ["RELIANCE", "ITC"],
        "hedge_ratio": 1.5463,
        "state": {
          "position": "LONG_SPREAD",
          "entry_z": -2.01,
          "entry_time": "2026-05-19T15:02:19",
          "entry_spread": 845.70,
          "effective_stop_z": 4.0,
          "legs": [
            {"symbol": "RELIANCE", "tradingsymbol": "RELIANCE26MAYFUT",
             "lot_size": 250, "quantity": 2, "entry_price": 1327.00,
             "current_price": 1323.20},
            ...
          ],
          "realized_pnl": -3849.95,
          "unrealized_pnl": 0.0,
          "total_transaction_costs": 1969.95,
          "closed_trades": [...],
          "spread_history": [...]
        }
      }
    ]
  }
  ```

### Strategy serialise/deserialise
- New on `PairTradingStrategy`:
  - `serialize_state() -> dict`: dataclass `asdict` on state, plus
    `_spread_history`, `hedge_ratio`, `symbol_a`, `symbol_b`.
  - `restore_state(blob: dict) -> None`: reconstructs `PairState`,
    `PairLeg` list, and `_spread_history`. Fails loudly on missing keys
    rather than silently defaulting (Rule 12).
- `datetime` round-trip via ISO strings.

### EOD sidecar semantics
- The verifier (`verify_pair_paper.py:158`) computes
  `paper_today_pnl = realized_pnl + unrealized_pnl`. Carrying state
  across sessions breaks this unless we tell it the per-session delta.
- Add two new fields to `generate_eod_report()`:
  - `session_realized_delta`: `realized_pnl − session_start_realized`
  - `session_unrealized_delta`: `unrealized_pnl − session_start_unrealized`
- The strategy snapshots `session_start_realized` /
  `session_start_unrealized` immediately after `restore_state` (or at
  `__init__` if no prior state).
- Verifier updated to prefer `session_*_delta` when present, fall back
  to `realized_pnl + unrealized_pnl` for backwards compatibility on
  pre-rebuild sidecars.

### Expiry-day force flatten
- Add `_legs_expire_on(today: date) -> bool` on the strategy.
  Reads each open leg's `tradingsymbol`, matches to instruments CSV
  (cached) for the expiry date, returns True if any leg expires today.
- In `run_paper_pairs.py`, before persist_state at session end:
  if `strategy._legs_expire_on(today)` and `position != FLAT`,
  call `flatten_one(strategy)` first (reason = `EXIT_EXPIRY`).

### CLI safety hatch
- Add `--force-flatten-on-exit` (default False) for ops use:
  fall back to the old "flatten everything" path when set.

### Files touched
- `run_paper_pairs.py` — main behavioural change
- `strategies/pair_trading.py` — serialise/deserialise + expiry helper
  + session-delta fields
- `verify_pair_paper.py` — prefer session_*_delta fields
- `tests/test_pair_trading.py` (existing) — add roundtrip test +
  dropped-pair-management test

### NOT changing (deliberate, per Rule 3 surgical-changes)
- `MAX_HOLD = 7d` uses calendar days, not trading days. This existed
  before. Weekends and holidays count against hold-time. Surface in the
  Review section; not part of this change.
- The `persistent` system's candidate-selection logic (V0 admission)
  is untouched.
- `compare_paper_systems.py` and dashboard backend — same EOD filenames,
  same shape with added optional fields. Should be transparent. If
  they break, fix as a follow-up.

## Implementation checklist

- [ ] Add `serialize_state` / `restore_state` to `PairTradingStrategy`
- [ ] Add `session_realized_delta` / `session_unrealized_delta` to
      `generate_eod_report`
- [ ] Add `_legs_expire_on(today)` helper
- [ ] Modify `run_paper_pairs.py`:
  - [ ] Load state file at startup, restore matching strategies
  - [ ] Include open-position pairs missing from today's candidates
  - [ ] Replace EOD flatten loop with persist-state loop
  - [ ] Add expiry-day flatten before persist
  - [ ] Add `--force-flatten-on-exit` CLI flag
  - [ ] Persist on KeyboardInterrupt too
- [ ] Update `verify_pair_paper.py` to prefer session_*_delta
- [ ] Tests: roundtrip + dropped-pair management
- [ ] Run existing pair_trading tests to confirm no regression
- [ ] Manual paper-runner smoke (mock kite, short tick window)

## Review (2026-05-19)

### What shipped
- **`strategies/pair_trading.py`**: `serialize_state()` / `restore_state()`
  for cross-session persistence. `_capture_session_baseline()` snapshots
  realized/unrealized at session start so `generate_eod_report()` emits new
  `session_realized_delta` / `session_unrealized_delta` fields even when
  cumulative P&L survives across sessions. `legs_expire_on(today: date)`
  inspects open legs' tradingsymbols against the instruments dump.
  `hedge_ratio` and `_spread_history` are intentionally **not** serialised
  (runner decides; daily bhavcopy seed is always fresher).
- **`run_paper_pairs.py`**:
  - `FLATTEN_AT` renamed `SESSION_END_AT` to reflect the new semantics.
  - `load_prior_state()` (missing-file-safe, corrupt-JSON-safe) +
    `write_state_file()` (atomic via `.tmp` → `os.replace`).
  - `restore_matching_strategies()` honours saved β for OPEN positions
    (locks the entry ratio) and re-seeds spread history at that β; FLAT
    saved pairs accept today's screener β.
  - `build_orphan_strategies()` instantiates strategies for pairs with
    OPEN positions that have dropped out of today's candidate list, so
    they're still managed to exit.
  - `end_of_session()` consolidates: (1) expiry-day flatten,
    (2) `--force-flatten-on-exit` operator hatch, (3) persist state,
    (4) write EOD sidecar. Same path on `KeyboardInterrupt`.
- **`verify_pair_paper.py`**: prefers session-delta fields when present,
  falls back to `realized + unrealized` for pre-rebuild sidecars.
- **Tests**: 14 new tests in `test_pair_trading.py` (serialise/restore
  roundtrip, expiry helper, session-delta arithmetic), 14 new in new
  file `test_run_paper_pairs_state.py` (load/write/restore/orphan). Full
  suite: 321 tests pass.
- **Smoke**: 4-session manual lifecycle (enter → persist → restore →
  force-flatten → expiry-flatten) all green.

### Surprise during smoke
Initial fixture had `_cached_futures = {}` which made
`_symbol_from_tradingsymbol` return the futures tradingsymbol instead of
the equity symbol, so `_apply_fill` failed to find existing legs and
*added new ones* on exit. The bug was in the test scaffolding, not in
production code (real runner populates the cache via `_resolve_futures`).
Worth knowing: `_symbol_from_tradingsymbol` silently degrades to identity
when the cache misses, rather than raising. That's by design (it's used
when fills come back from kite mid-flight and the cache might be cold),
but it's a quiet failure mode.

### What this does NOT do (deliberate, per Rule 3)
- `max_holding_days = 7d` still measures **calendar days**, not trading
  days. Weekends and holidays count against hold-time. With overnight
  holds now real, this matters more — a Tuesday entry that holds through
  a long weekend burns 4 days before Friday's close. Out of scope for
  this change; flag for a follow-up sweep if the time-stop turns out to
  be too tight after a few weeks of live data.
- `compare_paper_systems.py` and dashboard backend (`backend/main.py`,
  `pair_paper_compare`) read the same EOD sidecar files with the same
  shape — just with two new optional fields. Smoke didn't exercise
  these. If a dashboard tile breaks reporting cumulative P&L instead of
  per-day, the fix is to switch it to `session_realized_delta` the same
  way the verifier was switched.
- The `--persistent` system's V0 admission logic is untouched. Same
  rebuild applies — now both systems hold to strategy-defined exits.

### Open follow-ups (not blockers)
- **Calendar vs. trading days** for `max_holding_days` — see above.
- **Late-entry behaviour** — historically, EOD flatten penalised
  late-day entries (RELIANCE/ITC 2026-05-19 took the costs without time
  to revert). With overnight holds, late entries are now fine — but
  worth a backtest to confirm the late-entry distribution isn't skewed
  toward poorer subsequent days.
- **Futures roll** — current expiry-day flatten avoids settlement risk
  but means the strategy can't take a position spanning the
  contract switch. If `max_holding_days` and contract-month line up
  unluckily, valid trades get killed by expiry. Watch for this.

# Extend rebuild to Taleb hedger — `run_paper.py` (2026-05-19)

## Motivation
Today's 2026-05-19 Taleb session entered a long ATM straddle at 09:16 IST
and got force-flattened at 15:25 by `run_paper.py`'s `FLATTEN_AT`. Same
structural issue as pair-trading: an operational artefact (oneshot
systemd unit, no overnight process) leaking into strategy behaviour.

`max_holding_period_hours=22.0` is set in `best_params.json` — the
strategy is *parameterised* for ~1 calendar day holds, but the runner's
15:25 cut effectively caps it at ~6 hours. Tuning and execution disagree.

User decision 2026-05-19 (post-pair-trading-rebuild): apply the same
pattern. Hold to strategy-defined exits only, force-flatten only on
contract expiry or operator hatch.

## Design (mirrors pair-trading rebuild)

### State persistence
- `serialize_state()` / `restore_state(blob)` on `TalebKarpathyStrategy`.
- Serialises full `HedgeState`: `positions` (list of `OptionContract`),
  `futures_hedge_delta`/`futures_lots`/`futures_entry_vwap`, all P&L
  counters (`realized_pnl`, `unrealized_pnl`, `total_pnl`,
  `gamma_scalp_pnl`, `theta_decay_paid`, `total_transaction_costs`),
  history arrays (`closed_trades`, `daily_pnl_history`,
  `_current_day_pnl`, `_current_trading_date`).
- Skips computable derivatives: `portfolio_greeks` (recomputed each tick),
  `bleed_history` / `stability_history` / `last_hedge_decision` /
  `monte_carlo_report` (rolling diagnostics — rebuild from positions
  is cheap), `_attribution_baseline` (re-anchored on next entry).
- File: `data_cache/taleb_paper_state.json` (single strategy, no
  per-system suffix needed — only one Taleb hedger runs).

### Runner changes
- `FLATTEN_AT` → `SESSION_END_AT` rename.
- Add `load_prior_state` / `write_state_file` (atomic).
- Replace `flatten_and_report` with `end_of_session(hedger, today, args)`:
  1. If `--force-flatten-on-exit`: call `_generate_close_all_proposals`
     and execute (old behaviour).
  2. Else if any open position's contract (option leg OR futures leg)
     expires today: same forced flatten path.
  3. Always: `generate_eod_report`, `_save_iv_history`, then persist state.
- `--force-flatten-on-exit` CLI flag (default False).
- Same path on `KeyboardInterrupt`.

### Expiry helper
- `legs_expire_on(today: date) -> bool` on the strategy. Walks
  `self.state.positions` (OptionContract.expiry, ISO string) AND the
  futures hedge's contract expiry. Returns True if anything expires today.

### What this DOESN'T change
- The option-expiry selection in `_get_options_chain()` still picks
  nearest expiry. On Tuesdays the nearest weekly NIFTY is often
  same-day expiry, in which case the new state-persist has no effect —
  positions go to settlement and get flushed before persisting. The
  rebuild's benefit only materialises on days when the strategy enters
  an option whose expiry is later than today. Worth flagging but NOT
  changing here (Rule 3 surgical).
- Autoresearch loop (`autoresearch_loop.py`), IV history persistence,
  parameter tuning. None of these depend on the EOD flatten.

### P&L attribution
HedgeState ALREADY tracks `_current_day_pnl` + `daily_pnl_history` via
`_record_pnl_snapshot` (line 1353), with day-boundary detection at
line 1361. No new "session_delta" fields needed — restoring state and
ticking again naturally archives yesterday's `_current_day_pnl` to
history on the first tick after midnight.

### Files touched
- `strategies/taleb_karpathy.py` — serialise/restore + expiry helper.
- `run_paper.py` — main behavioural change.
- `tests/test_taleb_karpathy.py` — roundtrip test, expiry helper test.
- `tasks/todo.md` — Review section after.
- `tasks/lessons.md` — short note if any new failure mode surfaces.

## Implementation checklist
- [ ] `serialize_state` / `restore_state` + dataclass conversion helpers
      for `OptionContract` and nested structures.
- [ ] `legs_expire_on(today)` helper (options + futures).
- [ ] `run_paper.py`: load prior state, rename constants, add CLI flag,
      replace `flatten_and_report` with `end_of_session`.
- [ ] Tests: serialise/restore roundtrip (with open straddle + futures
      hedge), expiry helper, session-end branching.
- [ ] Smoke: 4-session lifecycle.
- [ ] Run full test suite; confirm no regressions.

## Review (2026-05-19)

### What shipped
- **`strategies/taleb_karpathy.py`**: `serialize_state()` / `restore_state()`
  covering HedgeState's persistent fields (positions, futures hedge, P&L
  counters, daily-bucket fields, closed_trades). Skips computable
  derivatives (`portfolio_greeks`, bleed/stability/MC histories,
  `_attribution_baseline`) which rebuild next tick. `legs_expire_on(today)`
  checks **both** option legs (each carrying its own ISO `expiry` string)
  AND the futures hedge (looked up against `instruments("NFO")`).
- **`run_paper.py`**:
  - `FLATTEN_AT` → `SESSION_END_AT` rename.
  - `load_prior_state` (missing-/corrupt-file safe) + `write_state_file`
    (atomic via `.tmp` → `os.replace`) + `restore_state_if_any`.
  - `end_of_session(hedger, today, args)` replaces `flatten_and_report`:
    flatten only on `--force-flatten-on-exit` or `legs_expire_on(today)`,
    always write EOD report + IV history, always persist state.
  - `--force-flatten-on-exit` CLI flag.
  - Same path on `KeyboardInterrupt`.
- **Tests**: 8 new in `tests/test_taleb_karpathy.py` (roundtrip with open
  straddle + futures hedge, flat-state roundtrip, JSON-cleanliness,
  expiry helper across option-leg and futures-leg paths). Full suite:
  **329 tests pass** (up from 321).
- **Smoke**: 5-session lifecycle (enter → persist → restore → force-flatten
  → option-expiry-flatten → futures-expiry-flatten) all green.

### Observation worth surfacing (not a code change)
The strategy's `_get_options_chain()` picks the **nearest** expiry. On
Tuesdays that's often same-day weekly NIFTY (today's 2026-05-19 straddle
on `NIFTY2651923700CE` expired today). For those sessions the new
state-persist has no effect — the expiry-day flatten fires identically
to the old EOD flatten. The rebuild's real benefit materialises only on
sessions where the strategy enters a position whose contract is later
than today's close. If you want overnight holds to happen more
regularly, that's a strategy decision (prefer next weekly over same-day
weekly) — a separate change.

Today's session entered same-day-expiry options anyway, so today
specifically would not have benefited from the rebuild. Going forward,
on Mondays/Wednesdays/Thursdays the strategy will enter options that
DO live past today's close — those are the days the rebuild starts
mattering.

### What this does NOT change (Rule 3 surgical)
- `_get_options_chain()` expiry selection logic — unchanged.
- Autoresearch loop (`autoresearch_loop.py`) — unchanged.
- IV history persistence (`_save_iv_history` / `_load_iv_history`) —
  unchanged; runs same as before at session end.
- The spot-history rolling cache (`_spot_history`) — not serialised; it
  rebuilds in ~10 ticks from live spot.

### Open follow-ups (not blockers)
- **Strategy entry-expiry preference** — if persistent overnight holds
  are the goal, the strategy should prefer expiries ≥ next trading day.
  Open question whether that improves OOS — short-dated options have
  higher gamma per ₹ premium but worse holding cost.
- **Greeks-on-restore**: `portfolio_greeks` starts None after restore;
  computed on the next tick's call to `_record_pnl_snapshot` /
  `check_and_rehedge`. Brief race: if `_should_exit` is called before
  the first tick of the day, `greeks` is None and the vega-limit gate
  is skipped. Risk is small — restore happens before tick loop entry —
  but a defensive `_recompute_greeks()` call right after restore would
  remove the ambiguity.
