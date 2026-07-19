# pair_trading — futures pair trading on cointegrated NSE stock pairs

One-line: long-short z-score-driven pair trading on NSE stock futures.
A screener identifies cointegrated pairs weekly; the runner takes one
position per pair when z-score crosses ±entry_z and exits on
mean-reversion (|z|≤exit_z), stop-out (|z|≥stop_z), or time-stop. Runs
in two parallel variants — `baseline` (broader, 12 pairs) and
`persistent` (high-quality filter, fewer pairs).

## Contents
- [Overview](#overview)
- [Two systems: baseline vs persistent](#two-systems-baseline-vs-persistent)
- [Cron schedule & systemd units](#cron-schedule--systemd-units)
- [Process lifecycle (runners/run_paper_pairs.py)](#process-lifecycle-run_paper_pairspy)
- [Cointegration & hedge ratio](#cointegration--hedge-ratio)
- [Z-score signal and rolling spread history](#z-score-signal-and-rolling-spread-history)
- [Entry path](#entry-path)
- [Exit path](#exit-path)
- [Orphan position handling](#orphan-position-handling)
- [Sizing, leg notional cap, and lots](#sizing-leg-notional-cap-and-lots)
- [Tick loop and broker reconciliation](#tick-loop-and-broker-reconciliation)
- [Hedge-ratio drift](#hedge-ratio-drift)
- [State model and persistence](#state-model-and-persistence)
- [Live-mode cutover (2026-05-21)](#live-mode-cutover-2026-05-21)
- [Kill switches and circuit breakers](#kill-switches-and-circuit-breakers)
- [Parameter reference](#parameter-reference)
- [Logging and monitoring](#logging-and-monitoring)
- [Known issues](#known-issues)
- [Files involved](#files-involved)

---

## Overview

Strategy lives in `strategies/pair_trading.py` (1100+ lines). Runner is
`runners/run_paper_pairs.py`. Two systemd timer pairs invoke the same script
with different candidate-CSV inputs and config files, producing the
`baseline` and `persistent` variants.

The edge: NSE-listed stock-futures pairs that have been historically
cointegrated tend to mean-revert when their normalised spread drifts to
~±2σ from the rolling mean. The pair is selected weekly by
`core/screen_pairs.py`; per-pair β (hedge ratio) is locked at screening time
and stored in `pair_candidates_*.csv`. A `pair_verify.py` cron re-runs
the screening logic mid-week to catch β drift on held positions.

Mode dispatch (line 22-26):
- `signals` — emit structured JSONL via `_emit_signal`; no state mutation
- `paper` — mock fills, update `state.positions` and P&L
- `live` — place real Kite orders (since 2026-05-21 cutover with triple-lock safety)

## Two systems: baseline vs persistent

Both share the same strategy code (`strategies/pair_trading.py`) and
runner (`runners/run_paper_pairs.py`). They differ in input candidate-CSV and
quality-floor knobs.

| Aspect | baseline | persistent |
|---|---|---|
| Timer | `pair-paper.timer` @ 09:11 IST | `pair-paper-persistent.timer` @ 09:12 IST |
| Service | `pair-paper.service` | `pair-paper-persistent.service` |
| Candidates input | `data_cache/pair_candidates.csv` (baseline screener) | `data_cache/pair_candidates_persistent.csv` |
| State file | `data_cache/pair_paper_state_baseline.json` | `data_cache/pair_paper_state_persistent.json` |
| EOD sidecar | `pair_paper_eod_<date>.json` | `pair_paper_persistent_eod_<date>.json` |
| Log | `logs/paper-pairs-YYYY-MM-DD.log` | `logs/paper-pairs-persistent-YYYY-MM-DD.log` |
| Quality floor | Looser — accepts up to 12 pairs | Tighter (corr≥0.65, HL≤5d, p≤0.025); usually 1–3 pairs |
| Intent | Broad coverage; tolerates marginal pairs for signal volume | High-conviction subset; volume-light by design |

The two runners share `data_cache/` and `.kite_session.json` — the
1-minute timer offsets (09:11 vs 09:12) avoid TOTP races on the same
session token.

Both run paper mode by default. The `--mode live` cutover that landed
2026-05-21 (10 Criticals closed) applies to both — see [Live-mode
cutover](#live-mode-cutover-2026-05-21).

## Cron schedule & systemd units

Timers:

```ini
# pair-paper.timer
OnCalendar=Mon..Fri *-*-* 09:11:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=60
```

```ini
# pair-paper-persistent.timer
OnCalendar=Mon..Fri *-*-* 09:12:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=60
```

Sequencing rationale (from the timer comments):
- `taleb-hedger.timer` at 09:10 — first auth.
- `pair-paper.timer` at 09:11 — 1-minute offset to avoid TOTP write
  race on `.kite_session.json`.
- `pair-paper-persistent.timer` at 09:12 — another 1-minute offset.
- All three self-gate on 09:15 IST bell anyway.

Service hardening identical to taleb-hedger: `Type=oneshot`,
`UMask=0027`, `ProtectSystem=strict`, `ReadWritePaths=<logs, data_cache,
repo root>`, `OnFailure=notify-failure@%n.service`.

## Process lifecycle (runners/run_paper_pairs.py)

| Phase | What happens | Source |
|---|---|---|
| Boot | Logging setup, holiday gate, kill-switch check | `runners/run_paper_pairs.py:_setup_logging` and HALT_* path checks |
| Config injection | `ensure_pair_config()` — if `[pair_trading]` section missing or no `max_leg_notional`, write derived config to `data_cache/.pair_paper_config.ini` | line 68-97 |
| Candidates load | Read `pair_candidates_*.csv`, apply quality floor, log "Selected N of M requested pairs" | (search `Selected.*pair` in source) |
| Auth | TOTP via `kite_auth` | shared with taleb-hedger |
| Strategy init | One `PairTradingStrategy` instance per selected pair | line 87 of strategy file |
| Restore | If `pair_paper_state_<system>.json` exists, call `restore_state()` for each pair | (see state model below) |
| Orphan loading | For pairs in the state file but NOT in today's candidates → load as ORPHAN ("management-to-exit") | (search `ORPHAN` in source) |
| Backup ring | `_state_backup.archive_state_backup` rolls a snapshot | shared with taleb |
| Wait | Sleep until 09:15 IST | runner |
| Tick | 09:15 → 15:25, every 60s | runner main loop |
| Session end | Persist state per tick (commit `8bbd606`), write EOD sidecar, exit | runner |

EOD does NOT force-flatten by default (since 2026-05-19). Open positions
survive into next session via the state file. Force-flatten available
via `--force-flatten-on-exit` ops hatch.

## Cointegration & hedge ratio

Screener (`core/screen_pairs.py` — see
[`docs/data_pipeline/pair_screening.md`](../data_pipeline/pair_screening.md)
for the full pipeline) computes for each candidate pair (A, B):
- Engle-Granger cointegration test on log-prices over 508 trading days
  of bhavcopy history
- Hedge ratio β via OLS: `price_A ≈ α + β × price_B`
- Half-life of mean reversion (Ornstein-Uhlenbeck)
- Pearson correlation of returns

Selected pairs are written to `pair_candidates_*.csv` with columns:
`symbol_a, symbol_b, hedge_ratio, halflife_d, pvalue, corr, rank`.

`PairTradingStrategy.__init__` reads `hedge_ratio` from candidates CSV
(or config / explicit arg). β is validated against the tradeable range
(line 126):

```python
HEDGE_RATIO_MIN = 0.1   # leg B too small → spread is just leg A
HEDGE_RATIO_MAX = 10.0  # leg B notional explodes
```

A β outside `[0.1, 10.0]` is rejected at construction — refusing to
trade is safer than letting bad β size leg B unbounded.

## Z-score signal and rolling spread history

Spread definition (line 6):
```
spread_t = price_A,t  -  β × price_B,t
```

Z-score normalisation:
```
z_t = (spread_t  -  mean(spread, lookback))  /  std(spread, lookback)
```

`lookback_days = 60` (default, line 153). At init, the strategy seeds
`_spread_history` with the last `lookback_days` of front-month STF
closes via `screen_pairs.load_front_month_panel()` so the first tick of
the session already has a usable z-score (no warm-up dead window).

Each subsequent scan tick appends one fresh observation to
`_spread_history`. Old samples roll off by the rolling-window
computation, not by trimming the list itself.

## Entry path

When the strategy is FLAT and `|z| ≥ entry_z`:

```python
if z < -entry_z:                                # spread is unusually LOW
    # → LONG_SPREAD: buy A, sell hedge B (expect spread to revert UP)
elif z >  entry_z:                              # spread is unusually HIGH
    # → SHORT_SPREAD: sell A, buy hedge B (expect spread to revert DOWN)
```

Defaults: `entry_z=2.0`, `exit_z=0.75`, `stop_z=4.0`. The `exit_z=0.75`
deviation from Varsity Ch. 12 (which says exit at z=0) is deliberate
per the line 133–137 comment: "Spreads stall before reaching exactly
zero on this universe."

### Pre-entry safety checks

| Check | Source line | Rationale |
|---|---|---|
| `max_entry_z` ceiling | line 151 (`max_entry_z=5.0`) | A z<-5 entry is regime break, not signal. Refuses entry past this. |
| Safety buffer | line 152 (`safety_buffer=0.75`) | `effective_stop_z = max(stop_z, |entry_z| + safety_buffer)`. A z=-3.8 entry stops at 4.55, not 4.0 — prevents insta-stop by sub-σ jitter. |
| `min_edge_multiplier` cost hurdle | line 162 (`=1.5`) | Reject entries whose expected ₹ move from current z back to exit band < `1.5 × round_trip_cost`. Filters marginal entries. |

Motivation for `max_entry_z` + `safety_buffer` (line 142): 2026-05-15
RELIANCE/CIPLA pair had a deep z=-4.22 entry past the stop band,
producing 346 same-tick entry+stop-out round-trips. Both knobs are
per-pair config-overridable.

Motivation for `min_edge_multiplier` (line 158): 2026-05-13 paper
session ate ~₹39k in friction across 28 round-trips while the backtest
baseline expected only ~₹1k/day gross edge — the strategy fired on
z-crossings whose expected ₹ move was smaller than the round-trip cost.

### Position construction

On entry, two `TradeProposal` rows are emitted (BUY/SELL of leg A and
inverse on leg B), with quantities derived from `lots_per_leg` (default
1) and the `max_leg_notional` cap (see [Sizing](#sizing-leg-notional-cap-and-lots)).

## Exit path

Three exit triggers, evaluated in order each tick (when book is open):

1. **Mean-revert exit** — `|z| ≤ exit_z` (default 0.75) → close both legs
2. **Stop-out** — `|z| ≥ effective_stop_z` → close both legs
3. **Time-stop** — `(today - entry_time).days ≥ max_holding_days` (default 7) → close both legs

`max_holding_days=7` chosen as win-rate peak (82.6%) from sweeps in
`data_cache/backtest_2026-05-08/sweep_maxhold_*.csv` — see line 164–170
comment. Tighter time stop frees the book to re-enter on natural
winners.

A held leg's contract expiring today also triggers a close (rollover —
the runner won't carry through expiry).

## Orphan position handling

When the screener re-runs and a pair no longer meets the quality floor,
but the runner state shows an open position on that pair, the position
is loaded as ORPHAN. Behavior:
- Load with `state.position` restored
- Log: `[<sym_a>/<sym_b>] ORPHAN — held position is not in today's candidates; loaded for management-to-exit`
- Continue ticking — exits (mean-revert, stop, time, expiry) still fire
- Do NOT add to entry pool; the pair is in "management-to-exit" mode

Without this path, a position whose screener entry has dropped out
would either be force-flattened (lost edge) or silently dropped from
in-memory state (orphan position in the broker's book with no
strategy tracking it). The ORPHAN log line is the only externally
visible indicator — grep `logs/paper-pairs-*.log` for it.

## Sizing, leg notional cap, and lots

Per-leg notional cap (`max_leg_notional`, ₹) is the only hard limit on
deployed capital per entry. Required in paper / live modes (line 183 —
refuses to construct without it); informational in signals-only mode.

Sizing logic:
1. Start with `lots_per_leg` lots of A (default 1)
2. Compute `B_lots = round(|β| × A_lots)` — preserves hedge ratio
3. Compute leg notionals: `A_notional = price_A × lot_size_A × A_lots`,
   same for B
4. If `max(A_notional, B_notional) > max_leg_notional`, scale BOTH legs
   down by the binding ratio. Hedge ratio preserved.
5. If even 1 lot of the larger leg breaks the cap → skip entry.

This prevents high-β pairs from silently deploying 10× the intended
notional on leg B.

## Tick loop and broker reconciliation

Tick interval: 60s, from 09:15 to 15:25 IST. Per tick:

1. Fetch LTPs for all configured pair tradingsymbols in one batched
   `kite.quote(...)` call
2. For each `PairTradingStrategy` instance:
   - `update_prices(ltp_map)` updates `_spread_history` and last-tick prices
   - `scan_and_propose()` checks entry conditions
   - `check_and_rehedge()` checks exit conditions on open positions
   - In paper mode, mock-fill any returned proposals via `_paper_execute`
3. Persist state to DB / state file (per `8bbd606`, every tick — earlier
   was EOD-only)

Live mode adds broker reconciliation: `kite.positions()` is fetched
each tick and the strategy's `state.positions` is reconciled against
it (commit `1e17712`). Phantom legs (in broker but not in strategy
state) → fail loud (`f028329`).

Per-tick state persistence (since `8bbd606`) skips the backup-ring
churn on every write — backups are still rolled per session, not per
tick.

## Hedge-ratio drift

Screener re-computes β weekly; held positions might have an entry-time
β that differs from today's screener β. Behavior:
- Compare `screener_β` against `state.entry_β` for held positions
- If different → log `screener β=X differs from saved entry β=Y;
  honouring saved β for held position`
- The held position keeps using its entry-time β until exit. New entries
  use today's β.

Rationale: changing β mid-position would silently alter the hedge ratio
of an open trade, invalidating the entry thesis.

`pair-verify.timer` runs `scripts/verify_pair_paper.py` daily to detect β drift
on held positions and emit warnings to `logs/pair-verify-*.json` and
`logs/pair-verify-*.log`. See
[`docs/data_pipeline/pair_screening.md`](../data_pipeline/pair_screening.md).

## State model and persistence

`PairState` dataclass at line 67. Persistent shape (matches
`pair_paper_state_<system>.json`):

```json
{
  "system": "baseline",                 // or "persistent"
  "updated_at": "<ISO timestamp>",
  "pairs": [
    {
      "pair": ["<symbol_a>", "<symbol_b>"],
      "hedge_ratio": 0.1725,
      "state": {
        "position": "FLAT|LONG_SPREAD|SHORT_SPREAD",
        "entry_z": -2.0127,
        "entry_time": "<ISO|null>",
        "entry_spread": 1643.05,
        "effective_stop_z": 4.0,
        "legs": [
          {
            "symbol": "BRITANNIA",
            "tradingsymbol": "BRITANNIA26MAYFUT",
            "lot_size": 125,
            "quantity": 1,       // signed: positive = long
            "entry_price": 5314.5,
            "current_price": 5342.0
          },
          { ... leg B ... }
        ],
        "realized_pnl": -752.71,
        "unrealized_pnl": -4250.0,
        "total_transaction_costs": 752.71,
        "closed_trades": [
          {
            "exit_time": "<ISO>",
            "entry_time": "<ISO>",
            "entry_z": 2.07,
            "entry_spread": 577.04,
            "realized_pnl": 27436.01,
            "transaction_costs": 1663.99,
            "position": "SHORT_SPREAD"
          }
        ]
      }
    }
  ]
}
```

Persistence cadence:
- **Per tick** (since `8bbd606`) — full state file rewritten on every
  60s tick. Backup ring not touched.
- **Per session end** — backup ring rolled (`pair_paper_state_<system>.<timestamp>.json`,
  4 kept) via `_state_backup.archive_state_backup`.

EOD sidecar `pair_paper_eod_<date>.json` written at session end with
per-pair `generate_eod_report()` output — consumed by the dashboard and
by `scripts/verify_pair_paper.py`.

## Live-mode cutover (2026-05-21)

Tracked in
[`tasks/live-readiness-deferred.md`](../../tasks/live-readiness-deferred.md)
"Closed Criticals" section. Ten Critical hardening items landed:

| Commit | Critical | What it fixed |
|---|---|---|
| `b9fe452` | C1, C2 | Live order confirmation + entry-batch atomicity |
| `1e17712` | C3 | Reconcile state against `kite.positions()` |
| `cd711c3` | C4 | Daily-loss circuit breaker |
| `907fc4b` | C5 | Flag-file kill switch (HALT_ALL, HALT_NEW_ENTRIES) |
| `f82451e` | C6 | `--mode live` with triple-lock safety gate |
| `58af67a` | C7 | Fail loud if `holidays.csv` is stale/partial |
| `561feba` | C8 | Failure alerting via `notify-failure@` template |
| `1b8fb34` | C9 | State-file backups with refuse-to-start-on-orphan |
| `f028329` | C10, H11 | Exit on held contract, fail-loud on phantom legs |

Pre-flight checklist in `deploy/VPS_DEPLOYMENT.md §7.9`. Remaining Highs
in `tasks/live-readiness-deferred.md` (Mid-session state persistence H1,
others).

Triple-lock safety gate for live: requires (a) `--mode live` CLI flag,
(b) `LIVE_TRADING=true` env var, (c) `data_cache/LIVE_TRADING_ACK`
flag-file present. All three must agree, or the runner refuses to
start.

## Kill switches and circuit breakers

Operator-managed flag files in `data_cache/`:

| File | Effect |
|---|---|
| `HALT_ALL` | Freeze the book: no entries, no exits. Use sparingly — positions cannot exit while set. |
| `HALT_NEW_ENTRIES` | Stop adding new entries; existing positions exit normally via stop / mean-revert / max-hold. |
| `HALT_DAILY_LOSS` | Runner-set when `--max-daily-loss-inr` breached. Persists across restarts; operator must `rm` to acknowledge. |

To halt: `touch <path>`. To resume: `rm <path>`. Both runners share
`data_cache/`, so flags halt both baseline and persistent
simultaneously.

Per-pair daily-loss circuit breaker (`cd711c3`): if a pair's
intra-session realized P&L breaches `--max-daily-loss-inr`, the runner
creates its own daily-loss flag and halts new entries for the rest of the
day (existing positions still exit normally). The flag is per-runner
(`halt_daily_loss_path`): the persistent/live runner uses the canonical
`HALT_DAILY_LOSS`; other runners (e.g. `--system baseline`) use
`HALT_DAILY_LOSS_<system>`, so one runner's breach can't halt another.

## Parameter reference

All in `[pair_trading]` section of `config.ini` unless noted.

| Param | Default | Units | Controls |
|---|---|---|---|
| `entry_z` | 2.0 | σ | Entry threshold; \|z\| ≥ this → enter |
| `exit_z` | 0.75 | σ | Mean-revert exit; \|z\| ≤ this → exit |
| `stop_z` | 4.0 | σ | Stop-out; \|z\| ≥ this → exit (after safety_buffer) |
| `max_entry_z` | 5.0 | σ | Hard ceiling for entries; past this is regime break |
| `safety_buffer` | 0.75 | σ | `effective_stop_z = max(stop_z, |entry_z|+buffer)` |
| `lookback_days` | 60 | days | Rolling window for z-score normalisation |
| `lots_per_leg` | 1 | lots | Base sizing of leg A; B scaled by β |
| `min_edge_multiplier` | 1.5 | × cost | Reject if expected ₹ move < this × round-trip cost |
| `max_holding_days` | 7 | trading days | Time-stop trigger |
| `max_leg_notional` | (required) | ₹ | Per-leg cap. Required in paper/live; refuse if missing |
| `symbol_a`, `symbol_b`, `hedge_ratio` | (none) | — | Optional pair override; defaults to screener top |

From `[strategy]` (shared):

| Param | Default | Purpose |
|---|---|---|
| `total_capital` | 500000 | Reference base for percentage calcs |

CLI flags on `runners/run_paper_pairs.py`:

| Flag | Purpose |
|---|---|
| `--system {baseline,persistent}` | Selects candidate CSV + state file |
| `--mode {signals,paper,live}` | Execution mode |
| `--max-daily-loss-inr` | Trips HALT_DAILY_LOSS flag on breach |
| `--force-flatten-on-exit` | Ops hatch: flatten at session end |
| `--force` | Run on weekend/holiday (testing) |

## Logging and monitoring

Logs:
- `logs/paper-pairs-YYYY-MM-DD.log` (baseline)
- `logs/paper-pairs-persistent-YYYY-MM-DD.log` (persistent)
- `logs/pair-verify-YYYY-MM-DD.{log,json}` (daily β drift check)
- `logs/pair-screen-YYYY-MM-DD.log` (weekly screener)

Grep cookbook:

| Symptom | Grep |
|---|---|
| Pair init | `grep "Init.*β=" paper-pairs-*.log` |
| State restored | `grep "restored: position=" paper-pairs-*.log` |
| Orphan pair loaded | `grep "ORPHAN — held position" paper-pairs-*.log` |
| β drift detected | `grep "differs from saved entry β" paper-pairs-*.log` |
| Quality floor dropped pairs | `grep "Selected only.*of.*requested" paper-pairs-*.log` |
| Entry fired | `grep "PAPER.*BUY\|PAPER.*SELL" paper-pairs-*.log` |
| Mean-revert exit | `grep "EXIT_MEAN_REVERT" paper-pairs-*.log` |
| Stop-out | `grep "EXIT_STOP" paper-pairs-*.log` |
| Cost-hurdle rejected entry | `grep "min_edge_multiplier" paper-pairs-*.log` |
| Halt flag active | `grep "HALT_" paper-pairs-*.log` |
| Phantom leg | `grep "phantom leg" paper-pairs-*.log` |
| State persisted | `grep "State persisted" paper-pairs-*.log` |
| Session end | `grep "Session-end window reached" paper-pairs-*.log` |

EOD sidecar at `data_cache/pair_paper_eod_<date>.json` has structured
per-pair summary — load into dashboards or post-mortems.

## Known issues

From `tasks/live-readiness-deferred.md` Highs section:

- **H1 Mid-session state persistence** — addressed in part by per-tick
  persistence (`8bbd606`), but the backup ring still only rolls at
  session end. A mid-session crash followed by EOD restart loses the
  most recent backup. Backup ring expansion to per-tick is a deferred
  follow-up.

- **Persistent candidates often select only 1 pair** — quality floor
  (corr≥0.65, HL≤5d, p≤0.025) is strict. On many sessions, only 1 of
  12 requested pairs passes (concentration risk). Recent example: on
  2026-05-25 the persistent runner selected only M&M/MARUTI.

- **BRITANNIA/BPCL orphan losing ~₹43k** — flagged in the 2026-05-25
  position review. Held since 2026-05-20 at z=-2.02; market gapped
  against and stop hasn't fired despite `effective_stop_z=4.0`. The
  orphan code path is correct (management-to-exit) but the position is
  unhedged in the screener's universe — worth checking whether ORPHAN
  positions should get a tighter stop than fresh entries.

- **Quality-floor warning is informational** — when "Selected only 1
  of 12" fires, the runner continues with the survivors. Operator may
  want a hard refusal threshold below which the runner pages an alert.

## Files involved

| File | Role |
|---|---|
| `strategies/pair_trading.py` | `PairTradingStrategy`, `PairState`, `PairLeg` |
| `strategies/base.py` | `BaseStrategy`, `validate_order` |
| `runners/run_paper_pairs.py` | Runner: multi-pair tick loop, persistence, ORPHAN load |
| `core/screen_pairs.py` | Weekly screener (β / HL / p-value / corr) |
| `scripts/verify_pair_paper.py` | Daily β-drift verification |
| `core/trade_proposer.py` | `TradeProposal` dataclass |
| `core/kite_auth.py` | TOTP auto-login |
| `core/_state_backup.py` | Backup ring helpers |
| `config.ini` | `[pair_trading]` section + shared `[strategy]` |
| `data_cache/pair_candidates.csv` | Baseline screener output |
| `data_cache/pair_candidates_persistent.csv` | Persistent screener output |
| `data_cache/pair_paper_state_baseline.json` | Baseline persisted state |
| `data_cache/pair_paper_state_persistent.json` | Persistent persisted state |
| `data_cache/state_backups/pair_paper_state_*.json` | Backup ring (4 kept) |
| `data_cache/pair_paper_eod_<date>.json` | Per-session EOD sidecar |
| `data_cache/HALT_ALL` / `HALT_NEW_ENTRIES` / `HALT_DAILY_LOSS` | Operator kill switches |
| `data_cache/.pair_paper_config.ini` | Runner-derived config (if `[pair_trading]` missing from operator config) |
| `logs/paper-pairs-*.log` / `paper-pairs-persistent-*.log` | Per-day logs |
| `deploy/pair-paper.service` / `.timer` | Baseline systemd units |
| `deploy/pair-paper-persistent.service` / `.timer` | Persistent systemd units |
| `deploy/pair-verify.service` / `.timer` | Daily β-drift check |
| `deploy/screen-pairs.service` / `.timer` | Weekly screener cron |
| `deploy/notify-failure@.service` | Telegram alert on exit ≠ 0 |
| `docs/data_pipeline/pair_screening.md` | Screener internals (companion doc) |
