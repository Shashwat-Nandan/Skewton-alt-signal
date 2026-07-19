# taleb_karpathy — long-gamma ATM straddle with dynamic delta hedging

One-line: a long ATM straddle on NIFTY (defaultable to BANKNIFTY), with
delta-neutralised via NIFTY futures around an asymmetric, vol-aware,
cost-gated rehedge band. Weekly autoresearch tunes the parameters; live
runs in paper mode under a systemd timer.

## Contents
- [Overview](#overview)
- [Theoretical foundation](#theoretical-foundation)
- [Cron schedule & systemd unit](#cron-schedule--systemd-unit)
- [Process lifecycle (runners/run_paper.py)](#process-lifecycle-run_paperpy)
- [Entry signal pipeline](#entry-signal-pipeline)
- [Position structure](#position-structure)
- [Rehedging logic](#rehedging-logic)
- [Exit logic](#exit-logic)
- [Realized accounting + asymmetric bands (Phase 5 uplift)](#realized-accounting--asymmetric-bands-phase-5-uplift)
- [State model and persistence](#state-model-and-persistence)
- [Daily loss limit and circuit breaker](#daily-loss-limit-and-circuit-breaker)
- [Parameter reference](#parameter-reference)
- [Greeks computation](#greeks-computation)
- [Logging and monitoring](#logging-and-monitoring)
- [Known issues and quirks](#known-issues-and-quirks)
- [Files involved](#files-involved)

---

## Overview

The strategy implements the Taleb "long-gamma, short-vega-via-rehedge"
playbook on Indian index options. At entry, it buys an ATM straddle (one
ATM call + one ATM put on the same expiry) on a single underlying (NIFTY
by default, configurable to BANKNIFTY). The straddle is delta-neutral at
entry. As spot drifts, the position accumulates directional delta; the
rehedge loop offsets that delta by trading the corresponding index
futures contract. The edge — when it works — is that realized spot moves
let you "scalp" the rebuilt gamma while the time-decay rent (theta) is
paid at a slower rate than the scalp earns.

The implementation lives in `strategies/taleb_karpathy.py` (2028 lines).
The runner is `runners/run_paper.py`. The strategy is launched once per trading
day by `taleb-hedger.service` at 09:10 IST, runs through the bell, and
persists state at session-end for the next morning.

Mode is paper-only. Live mode is intentionally not wired here (the
broker-execution path lives in pair_trading; the Taleb hedger has not
been hardened to live-readiness — see
[`tasks/live-readiness-deferred.md`](../../tasks/live-readiness-deferred.md)).

## Theoretical foundation

The "why" of the strategy — shadow gamma, three-level neutrality, soft
vs hard delta, bleed forecasting, alpha as gamma cost — is documented in
[`taleb_framework.md`](./taleb_framework.md). This doc COMPLEMENTS it
with implementation specifics. When debugging strategy *behavior*, start
here. When asking "why is the strategy structured this way at all", read
the framework doc.

Key concepts you must know to navigate this doc:

| Concept | One-line summary | Defined in code |
|---|---|---|
| Discrete delta | Delta using actual spot bumps rather than the closed-form Greek | `core/greeks_engine.py` |
| Shadow gamma | Asymmetric gamma split into γ_up and γ_down (biased assets) | `core/greeks_engine.py` |
| Alpha | Gamma cost: `theta / gamma` — straddle's daily rent per unit of curvature | `core/greeks_engine.py` |
| Bleed | Forecast P&L drift if held overnight at current Greeks | `core/risk_analyzer.py` |
| Lock delta | Maximum directional exposure under extreme regime shift | `core/greeks_engine.py` |
| Regime classifier | Routes between straddle, calendar, risk reversal, etc. based on IV/RV/skew | `core/regime_classifier.py` |

## Cron schedule & systemd unit

Timer: `deploy/taleb-hedger.timer`

```ini
OnCalendar=Mon..Fri *-*-* 09:10:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=60
```

- **09:10 IST** — service fires 5 minutes before the bell so the
  authentication + IV-history warm-up can complete before the first tick.
- **Mon–Fri** — the runner self-gates on `holidays.csv` (raises if the
  file is stale, per commit `58af67a`).
- **Persistent=true** — if the VPS missed the window, run once on catch-up.
  `runners/run_paper.py` refuses to start after 15:30 IST.
- **RandomizedDelaySec=60** — jitter to avoid simultaneous Kite logins on
  multi-account VPSes.

Service: `deploy/taleb-hedger.service`

```ini
Type=oneshot
WorkingDirectory=/opt/taleb-karpathy-kite        # see Architecture note
Environment=TZ=Asia/Kolkata
EnvironmentFile=…/.env
ExecStart=…/.venv/bin/python -m runners.run_paper
OnFailure=notify-failure@%n.service
```

Failure-handling: any nonzero exit triggers `notify-failure@taleb-hedger.service.service`,
which posts a structured Telegram alert (per commit `561feba`).

> **Architecture note.** The example unit in `deploy/` shipped with
> `/opt/taleb-karpathy-kite` paths, but the live `/etc/systemd/system/`
> units point at `/root/algo-trading/taleb-karpathy-kite` (the dev
> checkout *is* the deploy checkout). Verify with
> `systemctl cat taleb-hedger.service` before assuming.

## Process lifecycle (runners/run_paper.py)

The runner is a single straight-line process per session:

| Phase | Wall clock | What happens | Source |
|---|---|---|---|
| Boot | 09:10 | Setup logging, parse args, load `holidays.csv` | `runners/run_paper.py:_setup_logging`, `:load_holidays` |
| Auth | 09:10 | TOTP auto-login via `kite_auth.KiteAuthManager` | `runners/run_paper.py` (auth block) |
| Restore | 09:10 | If `data_cache/taleb_paper_state.json` exists, call `restore_state(blob)` | `strategies/taleb_karpathy.py:935` |
| Backup | 09:10 | `_state_backup.archive_state_backup` rolls a snapshot ring | `runners/run_paper.py:36` |
| Wait | 09:10–09:15 | Sleep to the bell | `runners/run_paper.py` (wait loop) |
| Tick | 09:15–15:25 | 60-second loop: `scan_and_propose` → `check_and_rehedge` → mark-to-market | `runners/run_paper.py` |
| Session end | 15:25 | Persist state to disk via `serialize_state()`; write EOD report; flush IV/spot history | `runners/run_paper.py` |
| Exit | 15:25–15:30 | Process exits 0 (or 1 on error → triggers notify-failure) |  |

EOD does NOT force-flatten by default (2026-05-19 change documented in
`runners/run_paper.py` header). Open positions survive into the next session via
`taleb_paper_state.json`. The only session-end exits are
(a) `--force-flatten-on-exit` ops hatch and
(b) a held leg whose contract expires today (`legs_expire_on(today)` at
`strategies/taleb_karpathy.py:987`).

## Entry signal pipeline

`scan_and_propose` at `strategies/taleb_karpathy.py:345` runs a sequential
gate chain. Each gate can short-circuit with an empty list:

```
[layering check] → [pre-trade checks] → [spot fetch] → [chain fetch]
  → [IV percentile gate] → [skew percentile gate] → [RV/IV gate]
  → [regime dispatch (Phase 3)] → [proposer]
  → [alpha cap] → [vega cap + scale]
  → [stability test] → [Monte-Carlo worst-path sizing]
  → return TradeProposal list
```

### 1. Layering check (Phase 4, lines 361–374)
If positions already exist and `max_layered_structures > 1`, an
additional structure may layer on top — but only when
`enable_regime_dispatch=True`. Default `max_layered_structures=1`
preserves the legacy "one structure at a time" invariant.

### 2. Pre-trade checks (line 375)
Daily loss limit, circuit breaker, market-hours window, no-trade-last-
minutes guard.

### 3. IV percentile gate (line 386)
Current ATM IV is ranked against `_atm_iv_history` (max 500 samples,
persisted to `data_cache/iv_history_NIFTY.json`). Entry allowed only if
percentile ∈ `[entry_iv_percentile_min, entry_iv_percentile_max]`.

### 4. Skew percentile gate (line 393, Phase 1.3)
Skew = `IV(25Δ put) − IV(25Δ call)`. Percentile-ranked over rolling
`_skew_history`. Reject straddle entry when skew > `skew_pct_max`
(default 80) — at high skew, the right structure is a risk reversal,
not the body. When `enable_regime_dispatch=True`, skew becomes a routing
feature instead of a hard veto (lines 404–411).

### 5. RV/IV gate (line 413)
Long-straddle thesis: realized vol must exceed implied. Compute realized
vol over a rolling window (`rv_window_days`, default 5d) using
`_spot_history`. Reject entry if `RV/IV < min_rv_iv_ratio` (default 1.0).
Permissive during warm-up (returns None until history accumulates).

When regime dispatch is on, RV/IV is a feature; biased-asset /
backspread regimes legitimately trade at RV/IV < 1.

### 6. Regime dispatch (line 433, Phase 3.1)
When `enable_regime_dispatch=True`, the proposal type is chosen by
`regime_classifier.classify(features)` from {STRADDLE, CALENDAR,
RISK_REVERSAL_LONG_PUT, BACKSPREAD_LONG, ASYM_STRANGLE, NO_TRADE}.
The chosen `Structure` is passed to `proposer.propose_for_structure`.
When dispatch is off (default), always emits a straddle via
`proposer.propose_delta_neutral` (line 468).

### 7. Alpha cap (line 477)
After proposals are sized, build the test portfolio and compute
`pf.net_alpha`. Reject if `|alpha| > max_entry_alpha` (default 25000) —
the gamma is too expensive relative to its theta cost.

### 8. Vega cap with scaling (line 486)
Compute total vega exposure; if it exceeds `vega_limit_per_lot ×
n_long_lots`, scale all proposal quantities by the ratio. If the scale
factor is < 0.5, reject the entry entirely. After scaling, re-validate;
the per-lot budget shrinks with the position so scaling alone can't
rescue a structure whose single-lot vega already exceeds the limit
(line 510 re-check — prevents same-bar entry+exit round trip).

### 9. Stability test (line 515, Gap #14)
`risk.stability_test()` checks sensitivity of computed Greeks to small
IV / spot perturbations. Failures are logged as warnings but don't block
entry.

### 10. Monte-Carlo worst-path sizing (line 522, Gap #19)
Simulate 50 paths over `max(int(T*365), 5)` trading days using a
deterministic SHA-256 seed of `(timestamp, spot)`. If
`|worst_path_pnl| > total_capital × mc_worst_path_loss_pct / 100`, scale
proposals down. If scaling pushes any leg below 1 lot, reject — the cap
is otherwise unenforceable.

## Position structure

A straddle entry produces two `TradeProposal` rows (BUY 1×CE + BUY 1×PE
at ATM strike) plus, after entry, a futures hedge generated by
`check_and_rehedge`. State tracks:

- `state.positions: List[OptionContract]` — every open option leg
- `state.futures_lots: int` — signed net lot count of the futures hedge
- `state.futures_hedge_delta: float` — net delta contribution from futures
- `state.futures_entry_vwap: float` — volume-weighted avg entry price

Lot size is read via `_get_lot_size()` (cached) and futures symbol via
`_get_futures_symbol()` (also cached). Both query `kite.instruments("NFO")`
lazily.

Strike interval: `50` for NIFTY, `100` for BANKNIFTY (line 1081).
ATM strike = `round(spot / interval) × interval`.

## Rehedging logic

`check_and_rehedge` at line 553 is the load-bearing loop. Sequence:

### Same-bar guard (line 568)
Block rehedge on the same wall-clock timestamp as entry — prevents
backtest replay from collapsing entry+exit into one bar.

### Refresh state (lines 571–580)
Fetch spot → `_update_positions_prices(spot)` → `_update_portfolio_greeks()`.
If portfolio Greeks couldn't be computed, return.

### Exit check (line 583)
If `_should_exit(greeks, spot)` returns True → emit close-all proposals
(see [Exit logic](#exit-logic)).

### Asymmetric vol-aware rehedge band (lines 586–649)

```python
g_avg  = max(|net_shadow_gamma|, |net_gamma|, 1e-6)
g_up   = max(|net_shadow_gamma_up|,   g_avg × 0.1)
g_down = max(|net_shadow_gamma_down|, g_avg × 0.1)

if delta >= 0:                  # up-side drift → hedge would SELL
    band_lots = base × √(g_up   / g_avg)
else:                           # down-side drift → hedge would BUY
    band_lots = base × √(g_down / g_avg)
```

Rationale (line 586–598): on biased assets like NIFTY/BANKNIFTY, downside
gamma exceeds upside (vol expands on sell-offs). A single symmetric band
leaves too much downside delta on the book exactly when it matters most.
Square-root scaling is gentler than Whalley-Wilmott γ^(2/3).

### T-0 (expiry-day) tightening (lines 612–638, Phase 5)
On expiry day (`min_days_to_exp < 1.0`), multiply the band by
`t0_band_factor` (default 1.0 = no change; 0.33 = aggressive
sticky-strike harvest). Bounded by the cost gate below.

### Below-band check (line 641)
If `delta_in_lots < band_lots`, return (no hedge).

### Whalley-Wilmott cost gate (lines 651–676)
WW's optimal-band gives `required_move_to_rehedge ∝ (cost/γ)^(1/3)`.
Translated to scalp/cost: scalp must beat `cost × hurdle^(1/3)`. Cube
root softens the linear hurdle (a 2× cost only demands 1.26× larger
scalp). Defaults: `cost_hurdle_factor=1.5` → effective ~1.14×. Set
`=1.0` to disable.

If `expected_scalp < ww_required`, skip and log.

### Soft vs hard delta decision (line 678, Gaps #20, #21)
`risk.hedge_decision()` returns a `HedgeDecision` indicating whether
to:
- **Hard hedge** (`_generate_hard_delta_proposals`, line 1031): take
  `-net_discrete_delta` and round to integer lots of the front-month
  futures contract. Skip if `round() == 0` and log loudly.
- **Soft hedge** (`_generate_soft_delta_proposals`, line 1064): buy
  options instead — used when gamma flip risk would amplify tail
  exposure from a futures hedge.

### Realized gamma-scalp booking (lines 689–705, Phase 1.1)
Compute `0.5 × γ × ΔS²` where ΔS is actual spot move since the last
anchor (entry or prior rehedge). **Critical: the sign of γ matters** —
taking abs(γ) would silently invert short-gamma losses into fake gains
(line 695 comment). Anchor at `state._last_rehedge_spot`.

### Increment rehedge_count, return proposals.

## Exit logic

Implemented in `_should_exit(greeks, spot)` (not shown in the excerpt;
read at the line referenced from `_should_exit` call site at line 583).
Exit triggers in order:

| Trigger | Source | Action |
|---|---|---|
| Daily loss limit hit | `_pre_trade_checks` records `_daily_loss_stop_date`; loop logs "Daily loss limit hit — no new entries" | Exit + flatten |
| Max holding period | Compare `now - state.entry_time` against `max_holding_period_hours` | Generate close-all proposals |
| Vega cap breach | If `|net_vega|` blows past `vega_limit × n_long_lots` mid-session | Close-all |
| Gap exit | Open-gap > `gap_exit_threshold_pct` from prior close | Close-all |
| Contract expiry day | `legs_expire_on(today)` returns True → set in session-end branch | Force-flatten |

Close-all generates SELL proposals for each held option leg and an
offsetting futures trade to net the hedge.

## Realized accounting + asymmetric bands (Phase 5 uplift)

Landed 2026-05-23 (commit `a7e3005`, memory file
`project_taleb_profitability_uplift_2026_05_23.md`). Five phases:

| Phase | Default | Description |
|---|---|---|
| 1.1 Realized accounting | **always on** | gamma_scalp_pnl uses actual ΔS² not estimated band; theta_decay_paid integrated from `_last_theta_anchor_time` |
| 1.2 Asymmetric rehedge bands | **always on** | √(γ_side/γ_avg) scaling per direction |
| 1.3 Skew percentile gate | always on | reject straddle when 25Δ skew > 80th pct |
| 3.1 Regime dispatch | **off by default** | `enable_regime_dispatch=False`; route to non-straddle structures when on |
| 4 Layering | off | `max_layered_structures=1`; >1 only valid with regime dispatch |
| 5 T-0 band tightening | off | `t0_band_factor=1.0`; <1.0 enables expiry-day sticky-strike harvest |

The "always on" pieces changed the metric semantics — `gamma_scalp_pnl`
and `theta_decay_paid` are now realized accruals, not estimates. The
`gamma_theta_ratio` metric (used by autoresearch since commit `0dac251`)
is `gamma_scalp_pnl / theta_decay_paid` and only meaningful once
`theta_paid > 1.0` (line 842 guard).

## State model and persistence

`HedgeState` at line 150. Persistent state shape (matches
`taleb_paper_state.json`):

```json
{
  "saved_at": "<ISO timestamp>",
  "state": {
    "positions": [{"tradingsymbol", "instrument_token", "strike",
                   "expiry", "option_type", "lot_size", "quantity",
                   "entry_price", "current_price", "iv"}, ...],
    "entry_time": "<ISO|null>",
    "total_pnl": 0.0,
    "realized_pnl": 0.0,
    "unrealized_pnl": 0.0,
    "rehedge_count": 0,
    "gamma_scalp_pnl": 0.0,
    "theta_decay_paid": 0.0,
    "max_drawdown": 0.0,
    "peak_pnl": 0.0,
    "total_transaction_costs": 0.0,
    "closed_trades": [{"entry_time", "exit_time", "holding_minutes",
                       "n_legs", "n_rehedges", "entry_atm_iv",
                       "gross_pnl", "costs", "gamma_scalp",
                       "residual"}, ...],
    "_prev_snapshot_pnl": 0.0,
    "_current_day_pnl": 0.0,
    "_current_trading_date": "<YYYY-MM-DD|null>",
    "daily_pnl_history": [...],
    "futures_hedge_delta": 0.0,
    "futures_entry_vwap": 0.0,
    "futures_lots": 0,
    "_last_theta_anchor_time": "<ISO|null>",
    "_last_rehedge_spot": null
  }
}
```

Serialization at `serialize_state()` line 868; deserialization at
`restore_state()` line 935. Restore is fail-loud on shape mismatch (line
938 — Rule 12). Optional fields tolerated for backward compat: anchors
predating Phase 1.1.

**Skipped from persistence** (line 874 docstring):
- `portfolio_greeks` — recomputed every tick
- `bleed_history` / `stability_history` / `monte_carlo_report` / `last_hedge_decision`
  — rolling diagnostic outputs, rebuilt next tick
- `_attribution_baseline` — re-anchored on next entry
- `_atm_iv_history` / `_spot_history` — IV has its own persistence
  (`_save_iv_history`); spot rebuilds in ~10 ticks

State is written ONCE at session end (15:25). Mid-session crashes lose
the day's progress — see EQ-FU-1-equivalent gap noted for pair_trading
in `tasks/live-readiness-deferred.md` H1. Backup ring rolled by
`_state_backup.archive_state_backup` (3 kept per
`data_cache/state_backups/taleb_paper_state.<timestamp>.json`).

## Daily loss limit and circuit breaker

Two distinct mechanisms:

### Daily loss limit (`max_daily_loss_pct`, immutable)
`_pre_trade_checks` snapshots day P&L and compares against
`total_capital × max_daily_loss_pct / 100`. If breached, sets
`_daily_loss_stop_date = today`. From then until end-of-day:
- `scan_and_propose` returns `[]`
- The tick loop logs `Daily loss limit hit — no new entries until next trading day.`
  repeatedly (one line per tick — useful as a heartbeat in journalctl).
- Existing positions are NOT auto-flattened by this gate; they still
  respond to other exits.

Resets at next trading day's first tick.

### Circuit breaker (`circuit_breaker_consecutive_losses`, immutable)
After N consecutive losing trades, set `_circuit_breaker_until = now +
circuit_breaker_pause_minutes minutes`. `_pre_trade_checks` blocks
new entries until expiry.

## Parameter reference

All read from `[strategy]` section of `config.ini`, with autoresearch
overlay from `best_params.json` (line 282).

### Tunable (autoresearch-controlled)

| Param | Default | Units | Controls |
|---|---|---|---|
| `rehedge_delta_threshold` | varies | lots | Base rehedge band; multiplied by √(γ_side/γ_avg) |
| `gamma_scalp_band_pct` | varies | % of spot | Expected gamma scalp move used in cost gate |
| `position_size_pct` | varies | % of capital | Fraction of capital deployed per straddle |
| `vega_limit` | varies | per lot | Cap on `|net_vega|`; scales with n_long_lots |
| `max_holding_period_hours` | varies | hours | Time-stop trigger |
| `entry_iv_percentile_min` | varies | 0–100 | Lower band of IV-percentile entry window |
| `entry_iv_percentile_max` | varies | 0–100 | Upper band |
| `max_entry_alpha` | 25000 | INR | Cap on `|net_alpha|` at entry (Gap #22) |
| `mc_worst_path_loss_pct` | 3.0 | % of capital | MC worst-path budget (Gap #19) |
| `cost_hurdle_factor` | 1.5 | dimensionless | Linear-equivalent rehedge cost hurdle; cube-root applied internally |
| `min_rv_iv_ratio` | 1.0 | ratio | Min realized-vol / implied-vol for entry |
| `rv_window_days` | 5.0 | days | Rolling window for realized-vol estimate |
| `skew_pct_max` | 80.0 | 0–100 | Max put-skew percentile for straddle entry (100 = disabled) |
| `enable_regime_dispatch` | False | bool | Route structures via regime_classifier (Phase 3.1) |
| `max_layered_structures` | 1 | count | Parallel structures cap (Phase 4) |
| `t0_band_factor` | 1.0 | factor | T-0 band tightening (Phase 5; 0.33 = aggressive) |

### Immutable (safety rails)

| Param | Purpose |
|---|---|
| `max_daily_loss_pct` | Daily loss limit % of capital |
| `max_position_margin_pct` | Cap on margin used per position |
| `no_naked_shorts` | Forbid uncovered short legs |
| `liquidity_min_spread_pct` | Min bid-ask spread tolerated |
| `no_trade_last_minutes` | Window before close where new entries are blocked |
| `gap_exit_threshold_pct` | Overnight gap that triggers exit |
| `circuit_breaker_consecutive_losses` | Loss streak that activates the breaker |
| `circuit_breaker_pause_minutes` | Pause duration after breaker trip |
| `total_capital` | Capital base for all % calculations |
| `max_positions` | Hard cap on total option legs |

## Greeks computation

Provided by `core/greeks_engine.py`:
- Black-Scholes via `OptionContract.greeks(spot, T)` for analytic Greeks
- Discrete delta via spot-bump differencing (`compute_portfolio_greeks(positions, spot, T)`)
- Shadow gamma (γ_up, γ_down) for asymmetric assets
- Alpha = `theta / gamma` (Gamma cost per day)
- Lock delta = directional exposure under extreme regime shift
- Per-leg / multi-expiry support via `per_leg_T` dict (added in Phase 3
  for calendars / diagonals)

IV calibration: `implied_volatility_bisect` finds IV from observed
premium. Risk-free rate fixed at `0.065` (line 217) — Indian 91-day
T-bill ballpark; update if rates move materially.

ATM IV history persisted to `data_cache/iv_history_NIFTY.json` (max 500
samples; line 324). Loaded at `_load_iv_history()` and saved at session
end. Skew history persisted alongside (line 337).

Spot history kept in-memory for the RV/IV gate; seeded from daily EOD
CSV at startup (line 333 comment) since cron sessions are oneshot and
the in-memory history would otherwise be empty until ~10 minutes of
intraday ticks accumulate.

## Logging and monitoring

Per-day logfile: `logs/paper-YYYY-MM-DD.log`. Grep cookbook:

| Symptom | Grep |
|---|---|
| Entry attempted | `grep "Generated.*proposals" paper-*.log` |
| IV gate blocked | `grep "IV percentile.*outside" paper-*.log` |
| Skew gate blocked | `grep "Skew percentile" paper-*.log` |
| RV/IV gate blocked | `grep "RV/IV ratio.*< min" paper-*.log` |
| Alpha cap | `grep "Alpha.*exceeds max_entry_alpha" paper-*.log` |
| MC worst-path scaling | `grep "MC worst path" paper-*.log` |
| Rehedge triggered | `grep "Delta drift:" paper-*.log` |
| Rehedge cost-gated | `grep "Skipping rehedge:.*scalp" paper-*.log` |
| Regime dispatch | `grep "Regime classifier" paper-*.log` |
| Daily loss limit | `grep "Daily loss limit hit" paper-*.log` |
| Session end | `grep "Session-end window reached" paper-*.log` |
| State persisted | `grep "State persisted" paper-*.log` |

Failure alerts go to Telegram via `notify-failure@taleb-hedger.service`
on nonzero exit.

## Known issues and quirks

1. **Lifetime `gamma_scalp_pnl == 0`** with non-zero `theta_decay_paid`
   is the diagnostic for a regime where the strategy is bleeding pure
   theta. Observed during recent sessions (see analysis from
   2026-05-25). Either the IV-RV gate is too permissive, the rehedge
   cadence is too slow to monetise moves, or entry IV is mispriced vs
   realized.

2. **State persisted ONCE at session end**. A mid-session crash loses
   the day's progress (positions, rehedge anchors, gamma scalp accrual).
   Equivalent issue tracked for pair_trading as H1 in
   `tasks/live-readiness-deferred.md`.

3. **Risk-free rate hardcoded** at 0.065 (line 217). RBI policy moves
   beyond ±100bp warrant an update.

4. **Spot symbol map** is hand-maintained in `_INDEX_SPOT_SYMBOLS` (line
   47). NIFTY and BANKNIFTY are present; adding a new index requires
   editing this dict (the bare-except that masked this for two weeks is
   called out in the comment).

5. **Regime dispatch / layering / T-0 disabled by default** (Phase 3.1
   / 4 / 5). They are tested but require config flag flip to activate.
   See `project_taleb_profitability_uplift_2026_05_23.md` memory for
   the rationale ("ship the always-on uplift first, gate the rest").

6. **Live mode is not wired**. The strategy raises in `__init__` if
   `mode=live`. Migration to live requires the same hardening pair_trading
   went through 2026-05-21 (see deferred.md and pair-trading docs).

7. **Per-tick spot-fetch failures escalate** to ERROR after 5
   consecutive misses (line 318 counter `_consecutive_spot_failures`) —
   useful to detect a wrong symbol or session issue rather than burying
   it in unrelated stack traces.

## Files involved

| File | Role |
|---|---|
| `strategies/taleb_karpathy.py` | Strategy class, scan/rehedge/exit logic |
| `strategies/base.py` | `BaseStrategy` contract, `validate_order`, ExecutionMode |
| `runners/run_paper.py` | Runner: auth, restore, tick loop, persist |
| `core/greeks_engine.py` | Greeks, IV solver, shadow gamma, alpha |
| `core/risk_analyzer.py` | Monte Carlo, stability, bleed forecast, hedge decision |
| `core/trade_proposer.py` | `TradeProposal` dataclass, `propose_delta_neutral` / `propose_for_structure` |
| `core/regime_classifier.py` | Phase 3.1 structure routing |
| `core/variance_pnl_gate.py` | Auxiliary variance/PnL gating |
| `core/kite_auth.py` | TOTP auto-login |
| `config.ini` | All defaults under `[strategy]` |
| `best_params.json` | Autoresearch overlay (applied at boot) |
| `holidays.csv` | Self-gates the runner |
| `data_cache/taleb_paper_state.json` | Persisted session state |
| `data_cache/iv_history_NIFTY.json` | ATM IV percentile reference |
| `data_cache/state_backups/taleb_paper_state.*.json` | Backup ring (3 kept) |
| `core/_state_backup.py` | Backup ring helpers |
| `logs/paper-YYYY-MM-DD.log` | Per-day session log |
| `deploy/taleb-hedger.service` | systemd service |
| `deploy/taleb-hedger.timer` | systemd timer (09:10 IST Mon–Fri) |
| `deploy/notify-failure@.service` | Telegram alert on exit ≠ 0 |
| `docs/strategies/taleb_framework.md` | Theoretical foundation (read first) |
| `docs/research/autoresearch.md` | How parameters get tuned |
