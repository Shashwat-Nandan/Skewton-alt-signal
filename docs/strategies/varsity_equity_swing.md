# varsity_equity_swing — trend-following equity longs with next-day-open fills

One-line: positional-swing long-only equity strategy on the NSE
NIFTY200-ish universe. SMA50/200 trend filter, ATR-stop sizing,
Chandelier trail. Signals fire post-bhavcopy at 18:30 IST and queue for
fill at the NEXT trading day's official open (since 2026-05-25).

## Contents
- [Overview](#overview)
- [Cron schedule & systemd units (two scans)](#cron-schedule--systemd-units-two-scans)
- [Universe and data anchor](#universe-and-data-anchor)
- [Indicator stack](#indicator-stack)
- [Entry gates](#entry-gates)
- [Sizing](#sizing)
- [Exit rules](#exit-rules)
- [Pending-entry queue + next-day-open fill (2026-05-25)](#pending-entry-queue--next-day-open-fill-2026-05-25)
- [State model](#state-model)
- [Parameter reference](#parameter-reference)
- [Logging and monitoring](#logging-and-monitoring)
- [Backtest harness and contract drift](#backtest-harness-and-contract-drift)
- [Tests](#tests)
- [Known issues](#known-issues)
- [Files involved](#files-involved)

---

## Overview

Strategy: `strategies/varsity_equity_swing.py` (~700 lines). Runner:
`runners/run_equity_swing.py`. Two systemd timers fire per trading day: an
**open scan** at 09:30 IST (exits-only, v1 design) and a **close scan**
at 18:30 IST (entries + exits + persistence).

The strategy holds equity longs from a curated NSE universe with
positional swing horizon (typically 5–20 trading days). Selection is
trend-driven (SMA50>SMA200), entry is on EMA20 pullback OR Donchian
breakout, sizing is ATR-stop-based with per-trade risk capped at 1% of
notional capital. Exits are SL_HIT, TARGET_HIT, TIME_STOP, or
TRAIL_STOP (Chandelier).

Mode: paper only. Live mode raises `NotImplementedError` (see
`runners/run_equity_swing.py:188`). Migration to live is Phase 5, blocked on
the same hardening pair_trading went through (see
`tasks/live-readiness-deferred.md` EQ-FU-1 .. EQ-FU-6).

## Cron schedule & systemd units (two scans)

| Scan | Timer | Wall clock | Purpose | Persistent= |
|---|---|---|---|---|
| open | `equity-swing-open.timer` | Mon–Fri 09:30 IST | Exits only; reads live Kite quote for today's intraday view | true |
| close | `equity-swing-close.timer` | Mon–Fri 18:30 IST | Entries + exits + fills + state persist | true |

Open-scan rationale (line 280 of `runners/run_equity_swing.py`): 09:30 is 15
minutes after the bell, giving Kite spot quotes time to settle. Open
scan only does exits because bhavcopy hasn't published yet — entries
must wait for the close scan's full feature compute.

Close-scan rationale: 18:30 is post-bhavcopy (NSE UDiFF publishes
~17:30–18:00; `fetch-bhavcopy-eq.timer` runs at 18:00; the close scan's
30-min buffer allows for fetcher retries). It runs after
`fetch-bars.service`, `fetch-bhavcopy-eq.service`, and
`fetch-fii-dii.service` per the `After=` dependencies in
`equity-swing-close.service`.

Both timers are `Persistent=true` — catch-up runs allowed. Open scan's
catch-up is safe (exits only). Close scan's catch-up is also accepted
because PENDING entries from previous days are tolerated (with the
5-day max-age safety, see below).

## Universe and data anchor

Universe constructed from `data_cache/nifty200.csv` (~209 symbols, EQ
series only). Loaded by `strategies/_eq_data.load_equity_panel()` which
returns a per-symbol panel of (date, open, high, low, close, volume).

Source: `data_cache/equity_ohlcv/` (per-symbol per-date OHLCV CSVs
populated by `market_data/fetch_bhavcopy_eq.py`). Schema: `date, symbol, open,
high, low, close, volume`. Sort: `(symbol, date)` ascending.

Staleness guard (`runners/run_equity_swing.py:270`): at close-scan, panel max
date MUST equal today. If less, refuse with error — fetch-bhavcopy-eq
likely failed. Open-scan tolerates up to 4 calendar days behind (long
weekend) with a warning at >1 day behind. This is the fail-loud guard
from commit `7177dde`.

## Indicator stack

All in `strategies/_indicators.py`, applied per-symbol in
`_ensure_features()` (line 237 of strategy file):

| Indicator | Parameters | Code |
|---|---|---|
| SMA short / long | 50 / 200 (Varsity Method 1) | `ind.sma(close, window)` |
| EMA pullback | 20 | `ind.ema(close, 20)` |
| ATR | 14 (Wilder) | `ind.atr(high, low, close, 14)` |
| ADX | 14 (Wilder) | `ind.adx(high, low, close, 14)` |
| Donchian high | 20 (excludes today's bar) | `ind.donchian_high(high, 20)` |
| Chandelier stop | ATR-14 × 3.0, lookback 22 | `ind.chandelier_stop_long(...)` |
| 20-day avg volume | for surge multiplier | `g["volume"].rolling(20).mean()` |
| 20-day median turnover (₹ cr) | min-liquidity filter | rolling median |
| Previous close + gap % | gap filter input | shift(1) |

Optional gate inputs (lazy-merged):
- Market Profile features (mp_vah, mp_poc, mp_val) from
  `_market_profile_eq.panel_value_area()`
- OI confluence signal from `_oi_signal.build_oi_panel()`
- FII/DII 5-day net cash from `_fii_dii.build_fii_signal()`

Features are dirty-flagged and recomputed when the panel changes.

## Entry gates

`_signal_at(sym, dt)` (line 333) is the single per-symbol gate. Order:

1. **Liquidity** — `turnover_med20_cr ≥ min_avg_turnover_cr` (default 50 ₹cr)
2. **Trend** — `SMA50 > SMA200`
3. **ADX** — `ADX14 ≥ adx_threshold` (default 20)
4. **Trigger** — EITHER:
   - Pullback to EMA20: `|close − ema_pull| ≤ pullback_atr_multiple × ATR` AND `close > ema_pull`
   - Donchian breakout: `close > donch_hi` AND `volume > volume_surge_multiple × vol_avg20`
5. **Gap filter** — `|gap_pct| ≤ gap_filter_pct` (default 2.0%)
6. **Optional gates** (each default-allow on missing data):
   - Market Profile veto: reject if `mp_enabled` and `close < mp_val`
   - OI veto: reject if `oi_enabled` and `oi_signal == "SHORT_BUILDUP"` and `oi_veto_short_buildup`
7. **Score** — base 1.0 + 1 (breakout) + 1 (pullback) + min(ADX/20 − 1, 2.0) for trend
   strength. Boosts: +1 MP-above-VAH, +1 OI-long-buildup, +1 FII-net-5d-positive

Sized via `_size_position(entry, sl)` (line 437):
```
risk_rs       = total_capital × risk_per_trade_pct / 100      # default ₹10,000/trade
per_share_loss = entry − sl
qty           = risk_rs // per_share_loss
# gross-exposure cap
gross_used    = sum(p.entry_px × p.qty for p in open_positions)
max_gross     = total_capital × max_gross_exposure_pct / 100  # default 30%
qty           = min(qty, (max_gross − gross_used) // entry)
```

Slot cap: `max_positions` (default 6). `scan_and_propose` (line 457)
walks the universe, scores all qualifying signals, sorts by score
desc, takes top `slots_left = max_positions − len(positions)`.

## Sizing

Position size is FROZEN at signal time (signal-day close as anchor for
the ATR-stop distance), even though the actual fill happens at the
next-day open. The `qty` stored in `equity_pending_entries.qty` carries
through to the fill — sizing is NOT recomputed at fill time. This
preserves the screener's risk budget logic; the SL/target re-anchor
(see below) covers the gap between signal close and fill open.

## Exit rules

`check_and_rehedge()` (line 515) walks open positions, checks today's
bar `[low, high]`:

| Priority | Trigger | Condition | Exit price |
|---|---|---|---|
| 1 | SL_HIT | `low ≤ initial_sl ≤ high` | `initial_sl` |
| 2 | TARGET_HIT | `low ≤ target ≤ high` | `target` |
| 3 | TRAIL_STOP | `low ≤ current_sl ≤ high` AND `current_sl > initial_sl` | `current_sl` |
| 4 (gap) | SL_HIT | `low ≤ initial_sl` (gapped through SL) | `min(initial_sl, today_open)` (more conservative) |
| 5 (gap) | TARGET_HIT | `high ≥ target` (gapped through target) | `max(target, today_open)` |
| 6 | TIME_STOP | `days_held ≥ time_stop_days` (default 20) | today's close |

Chandelier trail update (lines 530–548):
- `pos.high_watermark = max(pos.high_watermark, today_high)`
- If `unrealised ≥ trail_activate_R × risk_at_entry` AND
  `chandelier > pos.current_sl` AND `chandelier < close`:
  - `pos.current_sl = chandelier`
- The "chandelier > today's close" sanity bound rejects ratcheted-into-
  the-future trail values (typical cause: corp-action split where
  pre-split highs remain in proxy data; same shape as the dividend
  lesson in `tasks/lessons.md`).

## Pending-entry queue + next-day-open fill (2026-05-25)

**This is the load-bearing recent change.** Old behavior: close scan
emitted entry proposals AND immediately opened paper positions at the
signal-day close. New behavior: emit → queue in
`equity_pending_entries` table → NEXT close scan fills at THAT day's
official open from bhavcopy.

### Why

Close-scan fires at 18:30 IST, after the close auction is settled.
Filling at the signal-day close was an unachievable price — paper P&L
systematically overstated what live execution would deliver. See
`tasks/todo.md` top section dated 2026-05-25 for the full motivation.

### State machine

`equity_pending_entries.status`:

| Status | Set by | Meaning |
|---|---|---|
| PENDING | `_queue_pending_entries` after scan | Awaiting next session's open |
| FILLED | `_fill_pending_entries` after position insert | Successfully materialised at next-day open |
| SKIPPED_GAP | `_fill_pending_entries` | `gap > 1.5 × ATR` between signal close and fill open — setup invalid at new price |
| SKIPPED_STALE | `_fill_pending_entries` | Panel missing today's bar, signal aged > 5d, non-finite stored field, or unhandled per-row error |
| SKIPPED_OPEN | `_fill_pending_entries` | Symbol already has an open position (distinct from STALE for analytics) |

### Constants

```python
_PENDING_MAX_AGE_DAYS = 5             # calendar days
_PENDING_GAP_ATR_THRESHOLD = 1.5      # |open - signal_close| / atr
_REQUIRED_SNAPSHOT_KEYS = ("atr", "entry", "sl", "target")
```

### Queue path (`_queue_pending_entries`, `runners/run_equity_swing.py:266`)

Runs at close-scan after `scan_and_propose`. Per proposal:
1. Dedupe against existing PENDING for same symbol; skip if present
2. **Require** all four `greeks_snapshot` keys; raise `KeyError` if any
   missing (CLAUDE.md Rule 12 — silent .get() fallbacks would hide
   strategy-contract drift)
3. Compute distances: `sl_distance = signal_close − snap["sl"]`,
   `target_distance = snap["target"] − signal_close`
4. Reject NaN / inf / non-positive distances with
   `math.isfinite(x) and x > 0` (`NaN <= 0` is False — naive guard
   would let NaN through)
5. INSERT into `equity_pending_entries` with status PENDING

### Fill path (`_fill_pending_entries`, `runners/run_equity_swing.py:153`)

Runs FIRST in close-scan, before rehedge. Per-row try/except so one bad
row can't abort the batch. Per pending row:

1. Age check: `(today - signal_dt).days > 5` → SKIPPED_STALE
2. Already-open check: `sym in strategy.positions` → SKIPPED_OPEN
3. Panel lookup: `f.loc[today_ts, "open"]` via `_scalar_open_from_panel`
   helper — raises ValueError on duplicate-date Series (caught by
   per-row except → SKIPPED_STALE)
4. Open-price sanity: `math.isfinite(open_px) and open_px > 0`
5. Stored-field sanity (defense in depth): atr / signal_close /
   sl_distance / target_distance must all be finite and positive
6. Gap check: `|open_px - signal_close| / atr > 1.5` → SKIPPED_GAP
7. Re-anchor SL/target:
   - `sl = open_px − sl_distance`
   - `target = open_px + target_distance`
   - Distances preserved in ATR terms; absolute levels shift with the gap
8. Build `EquityPosition`, seed `pos.last_mtm_dt = today_ts` (prevents
   `_persist_proposals` from later writing wall-clock now() into a
   column meant to hold the bar date)
9. INSERT into `equity_positions`; mutate `strategy._db_id_by_symbol[sym] = pid`
10. UPDATE pending row to FILLED

### Same-day fill+exit

If the just-filled position's today bar `[low, high]` crosses SL or
target, `check_and_rehedge` (run immediately after `_fill_pending_entries`)
will close it the same day. This is by design — a synthetic stop fires.
**Auditability gap (EQ-FU-6):** the exit_reason is bare SL_HIT /
TARGET_HIT with no marker that this trade never had a real overnight
stop order. See `tasks/live-readiness-deferred.md`.

### Migration on 2026-05-25

4 positions opened earlier that day under the old logic (DIVISLAB,
MARICO, SUNPHARMA, ABB; equity_positions ids 6,7,8,9) were rolled back:
`status='CLOSED'`, `exit_reason='REQUEUED'`, `pnl=0`, `exit_px=entry_px`.
Equivalent PENDING rows inserted with `signal_dt=2026-05-25`. DB backed
up to `data_cache/dashboard.db.before-pending-rollback-2026-05-25`.

### Deferred follow-ups

From `tasks/live-readiness-deferred.md` EQ-FU-1 .. EQ-FU-6:

| ID | Severity | What |
|---|---|---|
| EQ-FU-1 | High | No `/equity/pending-entries` API endpoint — dashboard can't show today's signals until tomorrow |
| EQ-FU-2 | High | `research/backtest_varsity_equity.py` doesn't apply gap-skip or staleness filter → contract drift with live; autoresearch optimises against wrong trade rate |
| EQ-FU-3 | Medium | Autocommit gap between INSERT position + UPDATE pending → process kill leaves OPEN position + PENDING row. Self-heals via SKIPPED_OPEN on next run. |
| EQ-FU-4 | Low | `opened_by_scan='close'` hardcoded in `_fill_pending_entries`; fragile if open-scan ever pre-fills |
| EQ-FU-5 | Low | Signals-mode never drains PENDING rows — they age to SKIPPED_STALE at day 6 |
| EQ-FU-6 | Low | Same-day fill+exit gets bare SL_HIT / TARGET_HIT exit_reason; no marker for synthetic stop |

## State model

Three tables in `data_cache/dashboard.db`:

### `equity_positions`

Per-position row, OPEN or CLOSED.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | autoincrement |
| symbol | TEXT | NSE EQ symbol |
| side | TEXT | always 'LONG' in v1 |
| entry_dt | TEXT | ISO date — bar date of entry (NOT wall-clock fill time) |
| entry_px | REAL | the actual fill price (next-day open under new flow) |
| qty | INTEGER | shares |
| initial_sl | REAL | re-anchored to entry_px under new flow |
| current_sl | REAL | Chandelier-trailed |
| target | REAL | re-anchored to entry_px under new flow |
| atr_at_entry | REAL | for trail multiplier |
| rationale | TEXT | scoring + gates + fill metadata |
| last_mtm_dt | TEXT | bar date of last MTM (seeded to entry_dt at fill) |
| last_mtm_px | REAL | last MTM close |
| high_watermark | REAL | rolling-max high for Chandelier |
| status | TEXT | OPEN \| CLOSED |
| exit_dt, exit_px, exit_reason, pnl | TEXT/REAL | populated at close |
| opened_by_scan | TEXT | 'open' \| 'close' (which scan kind opened it) |

### `equity_pending_entries` (NEW 2026-05-25)

Per-pending-signal row.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| signal_dt | TEXT | ISO DATE of close-scan that emitted |
| symbol | TEXT | |
| side | TEXT | default 'LONG' |
| signal_close | REAL | signal-day close (for gap calc) |
| sl_distance | REAL | atr × stop_multiplier; preserved across re-anchor |
| target_distance | REAL | atr × stop_multiplier × RR |
| atr | REAL | |
| qty | INTEGER | sized at signal time |
| rationale | TEXT | from scan |
| status | TEXT | PENDING \| FILLED \| SKIPPED_GAP \| SKIPPED_STALE \| SKIPPED_OPEN |
| created_at | TEXT | ISO timestamp of queue |
| resolved_at | TEXT | ISO timestamp of status transition |
| resolution_note | TEXT | human-readable why |

Indexes on `(status, signal_dt DESC)` and `(symbol, signal_dt DESC)`.

NOT NULL constraints on `atr`, `sl_distance`, `target_distance`,
`signal_close` happen to reject NaN at insert (SQLite quirk — exercised
in `tests/test_equity_pending_entries.py::TestRobustness::test_sqlite_schema_rejects_nan_in_not_null_columns`).
This makes the queue-side NaN validation the only realistic defense;
the fill-side stored-field sanity check is belt-and-braces.

### `equity_scans`

One row per scan invocation (audit / dashboard tile).

| Column | Notes |
|---|---|
| scan_dt | ISO timestamp |
| scan_kind | 'open' \| 'close' |
| mode | 'signals' \| 'paper' |
| n_signals | proposals returned by scan |
| n_trades | `n_closed_today + max(0, len(positions) − n_open_before_fill)` — net-delta to correctly count same-day fill+exit as 1, not 2 |
| n_open_positions | count after scan |
| n_closed_today | new closes this scan |
| notes | "queued N pending entry(ies) for next session" if N>0 |

## Parameter reference

All from `[equity_swing]` section of `config.ini`, with `DEFAULTS` in
`strategies/varsity_equity_swing.py:129`. Most-used:

| Param | Default | Units | Controls |
|---|---|---|---|
| `total_capital` | 1,000,000 | INR | risk-pct base |
| `risk_per_trade_pct` | 1.0 | % | risk_rs = capital × 1% = ₹10k/trade |
| `max_positions` | 6 | count | slot cap |
| `max_gross_exposure_pct` | 30.0 | % | gross-notional cap |
| `trend_short_window` | 50 | bars | SMA short |
| `trend_long_window` | 200 | bars | SMA long |
| `adx_window` | 14 | bars | Wilder ADX |
| `adx_threshold` | 20.0 | 0–100 | min ADX |
| `atr_window` | 14 | bars | Wilder ATR |
| `atr_stop_multiplier` | 2.5 | × ATR | `sl = entry − k × ATR` |
| `chandelier_multiplier` | 3.0 | × ATR | trail = high − k × ATR |
| `chandelier_lookback` | 22 | bars | rolling-max window |
| `risk_reward` | 2.0 | dimensionless | `target_distance = R × sl_distance` |
| `time_stop_days` | 20 | trading days | TIME_STOP trigger |
| `gap_filter_pct` | 2.0 | % | reject signal if \|gap\| > this |
| `pullback_atr_multiple` | 0.5 | × ATR | pullback-to-EMA trigger band |
| `ema_pullback_window` | 20 | bars | EMA for pullback |
| `breakout_window` | 20 | bars | Donchian-high lookback |
| `volume_surge_multiple` | 1.5 | × | min volume / 20-day-avg for breakout |
| `min_avg_turnover_cr` | 50.0 | ₹ crore | min 20d median turnover |
| `trail_activate_R` | 1.0 | × risk | Chandelier activates when unrealised ≥ R × risk |

Optional gates (default OFF unless noted):

| Param | Default | Purpose |
|---|---|---|
| `mp_enabled` | 0 | Market Profile gate (default OFF — neutral-to-negative Sharpe in backtest) |
| `mp_veto_below_val` | 1 | If enabled: reject close < VAL |
| `mp_boost_above_vah` | 1 | If enabled: +1 score if close > VAH |
| `oi_enabled` | 0 | OI confluence gate (default OFF — -₹52k/₹10L of edge over 535d window) |
| `oi_veto_short_buildup` | 1 | If enabled: veto on SHORT_BUILDUP |
| `oi_boost_long_buildup` | 1 | If enabled: +1 score on LONG_BUILDUP |
| `fii_enabled` | 1 | FII/DII confluence (default ON — boost only) |
| `fii_boost_when_positive` | 1 | +1 score when 5d FII net cash positive |

CLI on `runners/run_equity_swing.py`:

| Flag | Purpose |
|---|---|
| `--scan {open,close}` | Required |
| `--mode {signals,paper}` | Default paper |
| `--force` | Run on weekend/holiday (testing) |

## Logging and monitoring

Log file: `logs/equity-YYYY-MM-DD.log` (single file per day, both scans).

Grep cookbook:

| Symptom | Grep |
|---|---|
| Scan startup | `grep "EQUITY SWING SCAN" equity-*.log` |
| Resumed open positions | `grep "Resumed.*open paper positions" equity-*.log` |
| Pending entries filled | `grep "\[PENDING FILL\]" equity-*.log` |
| Pending skipped (gap) | `grep "\[PENDING SKIP\].*exceeds" equity-*.log` |
| Pending skipped (stale) | `grep "\[PENDING SKIP\].*aged\|\[PENDING SKIP\].*no panel" equity-*.log` |
| Pending dedupe | `grep "\[PENDING DEDUPE\]" equity-*.log` |
| New pending queued | `grep "\[PENDING QUEUE\]" equity-*.log` |
| New position opened | `grep "\[DB OPEN\]" equity-*.log` |
| Position closed | `grep "\[DB CLOSE\]" equity-*.log` |
| Stale panel warning | `grep "EQ panel is.*days behind" equity-*.log` |
| Stale panel ERROR (refuses) | `grep "EQ panel latest date" equity-*.log` |
| Scan summary | `grep "DB writes" equity-*.log` |
| EOD report | `grep "Report:" equity-*.log` |

Failure alerts: nonzero exit → `notify-failure@equity-swing-close.service`
fires the Telegram template.

## Backtest harness and contract drift

`research/backtest_varsity_equity.py` (`EquityBacktester`) is the harness used
by autoresearch sweeps. It loads the panel, walks it day-by-day, and
simulates entries / exits.

**Contract drift (EQ-FU-2, High):** the backtester fills every signal
at next-day open with NO gap-skip filter and NO max-age check. Live
`_fill_pending_entries` rejects gaps > 1.5×ATR and signals older than
5d. Backtest trade count is therefore systematically higher than live;
high-gap days are exactly the asymmetric tails that drive most of the
PnL variance. Autoresearch picks parameters that look good on backtest
trade counts that live will never produce.

Fix path: port the gap filter into `EquityBacktester._open_price` (and
factor the constants into a shared module so they stay in sync). Per
CLAUDE.md Rule 7 (don't average two patterns — pick one).

## Tests

53 tests covering this strategy, split across two files:

### `tests/test_varsity_equity_swing.py` (38 tests)
- Indicator math (SMA, EMA, ATR, ADX, Donchian, Chandelier warm-up + correctness)
- Strategy gates (gap, trend, sizing, slot cap, gross-exposure cap)
- Position state machine (SL hit, target hit, time stop, trail activation)
- Backtest integration (≥1 trade on synthetic uptrend; zero-trade sentinel)
- Market Profile / OI / gate behaviour

### `tests/test_equity_pending_entries.py` (15 tests, 2026-05-25)
- QUEUE: distance storage, dedupe, invalid distances rejected, NaN
  rejected at queue (4-axis: sl/target/atr keys), KeyError on missing
  required key
- FILL: entry_px = next-day open, SL/target re-anchored,
  last_mtm_dt seeded to bar date, gap > 1.5×ATR → SKIPPED_GAP,
  boundary 1.45×ATR fills, signal age > 5d → SKIPPED_STALE,
  panel missing → SKIPPED_STALE, symbol already open → SKIPPED_OPEN
- ROBUSTNESS: per-row exception isolation, duplicate-date panel rows
  caught, SQLite NOT NULL blocks NaN at schema level
- ROUND-TRIP: queue today → fill tomorrow end-to-end

All 53 pass.

## Known issues

1. **EQ-FU-1..6** (per
   [`tasks/live-readiness-deferred.md`](../../tasks/live-readiness-deferred.md))
   — see [Pending-entry queue](#pending-entry-queue--next-day-open-fill-2026-05-25)
   section.

2. **DIVISLAB cross-strategy overlap** — equity-swing and pair-baseline
   both held long DIVISLAB exposure on 2026-05-25. Not a bug per se (both
   in paper) but for live cutover a portfolio-level netting view is
   needed.

3. **SONACOMS / DRREDDY left unchanged on migration** (2026-05-25) —
   they entered 2026-05-08 under the old logic. Re-queueing would have
   required replaying 17 days of management state; not worth it.

4. **MP and OI gates default OFF** — backtest evidence is inconclusive
   to negative (lines 151–169 of strategy file have the data). FII gate
   default ON (boost-only) since it's score-additive, not vetoing.

5. **`time_stop_days = 20`** is generous. Worth re-sweeping once a
   broader OOS window is available — the backtest 2026-05-08 oos_trades
   set has ~30 trades; weak statistical power.

## Files involved

| File | Role |
|---|---|
| `strategies/varsity_equity_swing.py` | Strategy class, signal/sizing/exit |
| `strategies/_indicators.py` | SMA, EMA, ATR, ADX, Donchian, Chandelier |
| `strategies/_eq_data.py` | EQ panel loader from `equity_ohlcv/` cache |
| `strategies/_market_profile_eq.py` | MP value area computation |
| `strategies/_oi_signal.py` | OI confluence classifier |
| `strategies/_fii_dii.py` | FII/DII 5d signal builder |
| `runners/run_equity_swing.py` | Runner: scan kind dispatch, pending queue/fill, persistence |
| `research/backtest_varsity_equity.py` | Backtester (contract drift flagged in EQ-FU-2) |
| `backend/db.py` | `equity_positions`, `equity_pending_entries`, `equity_scans` tables + helpers |
| `config.ini` | `[equity_swing]` defaults |
| `holidays.csv` | Holiday gate |
| `data_cache/equity_ohlcv/` | Per-symbol per-day OHLCV cache (output of fetch-bhavcopy-eq) |
| `data_cache/nifty200.csv` | Universe definition |
| `data_cache/dashboard.db` | All persisted state (positions, pending, scans) |
| `data_cache/equity_swing_trades.tsv` | Historical closed-trade log (pre-DB era) |
| `logs/equity-YYYY-MM-DD.log` | Per-day log (both scans) |
| `deploy/equity-swing-open.{service,timer}` | 09:30 IST exits-only scan |
| `deploy/equity-swing-close.{service,timer}` | 18:30 IST entries+exits+fills+persist |
| `tests/test_varsity_equity_swing.py` | 38 tests |
| `tests/test_equity_pending_entries.py` | 15 tests (new flow) |
| `tasks/todo.md` | Top section documents 2026-05-25 fill change |
| `tasks/live-readiness-deferred.md` | EQ-FU-1..6 follow-ups |
