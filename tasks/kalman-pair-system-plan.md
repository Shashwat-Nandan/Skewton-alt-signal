# Kalman-Filter Pairs Trading System — PLAN (2026-06-24)

A **separate, independent** pairs-trading system built around a Kalman-filter
time-varying hedge ratio, per Palomar, *Portfolio Optimization* (2025), Ch. 15
§15.6. It runs in parallel with — and does not modify — the existing static-β
`pair_trading.py` system. Reference equations cited as (15.x) are from that
chapter.

## Why a new system (not a retrofit)

The existing `strategies/pair_trading.py` (1,948 lines) estimates the hedge
ratio γ **once** via OLS at screening time (`screen_pairs._hedge_ratio`) and
holds it fixed for the life of a position. Ch. 15 §15.6.4 shows this is the
weakest tracking method: on EWA–EWC the rolling-LS hedge ratio swings 0.6–1.2
while Kalman stays 0.55–0.65, and cumulative return goes **0.6 (rolling LS) →
2.0 (basic Kalman) → 3.2 (momentum Kalman)** with far better drawdown
("Kalman filtering is a must in pairs trading", p. 438).

That file also carries heavy incident-driven complexity (e.g. the deliberate
daily-only z-window after the 2026-05-13 intraday-std-collapse). Forking the
*tracking* logic into a clean system is lower-risk than surgery on it, and
matches the repo's existing parallel-runner pattern (arbitrage mirrors pairs).

## Success criteria (Rule 4)

1. **Filter is provably correct** before it touches our pairs: a numpy Kalman
   implementation reproduces a known-good daily result (target: EWA–EWC-style
   stable γ in ~0.55–0.65 band and a monotone-ish cumulative-return shape
   matching Fig. 15.23 qualitatively). Fail loud if it doesn't.
2. **Time-varying γ_t, μ_t** tracked per pair, strictly causal (uses the
   *predicted* state γ_{t|t-1}, μ_{t|t-1} — no look-ahead, per (15.3)).
3. **Backtest on our NIFTY F&O pairs** shows the Kalman spread is *more*
   stationary (lower half-life, fewer cointegration breaks) than the static-β
   spread on the same pairs over the same window.
4. **Paper runner** runs unattended on its own systemd timer, own state file,
   own EOD report and logfile — zero shared mutable state with the static
   system except the read-only candidate universe and the shared kill-switches.
5. **Emits the §4 signal contract** (ENTRY/EXIT envelopes to
   `logs/signals-*.jsonl`) from day one — Phase-0 telemetry, no subscribers,
   exactly as the static pair system does. (Aligns with the signal→OMS pivot.)
6. Unit tests encode *why* (Rule 9): a known linear-Gaussian system is
   recovered; a regime change in γ is tracked within N steps; degenerate input
   fails loud.

## Key design decisions to confirm before coding (Rule 1 / Rule 7)

These are the choices where I'm picking a default and want a yes/no, because
they change what gets built:

- **D1 — Update frequency: DAILY state updates (recommended).** The Kalman
  state (γ_t, μ_t) updates once per day on the official F&O close (bhavcopy),
  exactly as the book's experiments do. Intraday, the runner only compares the
  *live* spread to the day's Kalman band for entry/exit. This matches the
  book, matches our daily-distribution tuning, and sidesteps the documented
  intraday-std-collapse incident. (Alternative: per-tick state updates — richer
  but re-opens that incident and has no book support. Not recommended for v1.)

- **D2 — Model: momentum Kalman (15.4) as default, basic (15.3) behind a flag.**
  The book shows (15.4) is strictly better (γ less noisy). State
  α_t = (μ_t, γ_t, γ̇_t), transition [[1,0,0],[0,1,1],[0,0,1]]. Keep basic
  (15.3) selectable for A/B comparison in the backtest. Partial-cointegration
  (15.5, AR(1) residual) is a **stretch goal**, not v1.

- **D3 — Trading signal: rolling z-score on the Kalman spread (recommended),
  log the standardized innovation too.** The book's *signal* is still a rolling
  Bollinger z on the (Kalman-derived) spread z_t = (y₁−γ_{t|t-1}y₂−μ_{t|t-1})/(1+γ).
  The Kalman filter *also* hands us a causal z for free — the standardized
  innovation vₜ/√Fₜ. v1 trades the rolling-z (fidelity + comparability with the
  static system); we compute and log the innovation-z in parallel to evaluate
  switching later.

- **D4 — No new dependency.** Implement the filter in ~40 lines of numpy, not
  `filterpy`/`pykalman` (neither is installed; the VPS stays dependency-light,
  Rule 2). The book's models are small linear-Gaussian filters — trivial to
  hand-roll and unit-test.

- **D5 — Capital/risk isolation during paper.** Own capital cap, own state
  file, own notional book. It does **not** aggregate into the static system's
  `_aggregate_book_notional` while paper-validating. Shared kill-switches
  (`HALT_ALL`, `HALT_NEW_ENTRIES`) still halt it (shared `data_cache`), which is
  correct. Revisit cross-system risk netting only before any live cutover.

- **D6 — Universe reuse, β re-estimated.** Reuse `pair_candidates.csv` for the
  *discovery* universe (cointegration screening is unchanged — what changes is
  *tracking*, not *discovery*), but the Kalman system **ignores the static β**
  and re-fits γ itself from the training window. Discovery stays in
  `screen_pairs.py` (reused, not forked).

## What is reused vs. forked

**Reused (no fork):** `runner_common.py` (locks, holidays, market hours, signal
handlers, heartbeat), `screen_pairs.py` (cointegration discovery + candidate
universe), `strategies/base.py` (`_publish_signal`, `proposal_to_leg`, `uuid7`,
TradeProposal), `kite_auth` / `kite_throttle`, data layer (bhavcopy /
`fetch_bars` / `tick_capture`), dashboard auth.

**New (forked):** the filter, the strategy, its backtest, its paper runner, its
sweep, its verifier, its tests, its systemd units, its dashboard tab.

---

## Phases (checkable)

### Phase 0 — Filter core + correctness gate
- [ ] `strategies/kalman_filter.py` — pure, no I/O. A `KalmanPairFilter` with
      `predict()` / `update(y1, y2)` returning the filtered/predicted state,
      innovation vₜ and its variance Fₜ, and the causal spread z_t. Both
      models (15.3 basic, 15.4 momentum) selectable. Parameter init from the
      training-window OLS heuristic in §15.6.3:
      σ²_ε=Var[ε^LS]; σ²_μ=α·Var[ε^LS]; σ²_γ=α·Var[ε^LS]/Var[y₂]; initial
      states from μ^LS, γ^LS (α the process-noise hyper-param, ~1e-5…1e-6).
- [ ] `tests/test_kalman_filter.py` (Rule 9): (a) recover known constant γ
      from synthetic cointegrated series (15.1 generator); (b) track a step
      change in γ within N updates; (c) momentum model has lower γ variance
      than basic on the same data; (d) NaN/degenerate/too-short → fail loud.
- [ ] **Correctness gate:** a small script reproduces a stable-γ, rising-
      cumulative-return result on a public daily pair (EWA–EWC if fetchable,
      else a synthetic 15.1 series). Block all later phases until this passes.

### Phase 1 — Strategy class
- [ ] `strategies/kalman_pair_trading.py` — `KalmanPairStrategy(BaseStrategy)`.
      Holds one `KalmanPairFilter` per pair; seeds it from the training window;
      one state update per close (D1); builds ENTRY/EXIT proposals off the
      rolling-z band on the Kalman spread (D3); structure-scoped §4 risk
      directives (reuse the static system's STRUCTURE_PNL_INR pattern); emits
      the signal contract via `_publish_signal` (D5/contract).
- [ ] `serialize_state` / `restore_state` must persist the **full filter
      state** (state vector + covariance P), not just γ — restoring only γ
      would reset the filter's uncertainty and corrupt tracking after a
      restart. Lock filter state to the saved value for an open position.
- [ ] Unit tests for the strategy: entry/exit thresholds, no-look-ahead z,
      state round-trips (serialize→restore is identity), open-position β-lock.

### Phase 2 — Backtest + validation on our pairs
- [ ] `backtest_kalman_pairs.py` — mirror `backtest_pairs.py`'s harness/CLI.
      Run static-β vs basic-Kalman vs momentum-Kalman on the same candidate
      pairs + window. Report per pair: spread half-life, # cointegration
      breaks, # round-trips, gross/net P&L, Sharpe, max drawdown.
- [ ] Validation report → `tasks/kalman-pairs-findings.md`: does Kalman
      produce more stationary spreads / better net P&L *after costs* than
      static β on Indian F&O pairs? Be explicit if it does **not** (Rule 12) —
      the book's edge is on US ETFs; it may not transfer.
- [ ] `sweep_kalman_pairs.py` — sweep α (process-noise ratio), entry/exit z,
      rolling-z lookback. Mirror `sweep_pair_params.py`. Guard against
      overfitting (train/test split; small grid).

### Phase 3 — Paper runner (only if Phase 2 validates)
- [ ] `run_paper_kalman_pairs.py` — mirror `run_paper_pairs.py`: TOTP auth,
      holiday/weekend guard, 09:15→15:25 loop, own state file
      `data_cache/kalman_pair_paper_state.json`, own EOD
      `data_cache/kalman_pair_paper_eod_<date>.json`, own logfile, own daily-
      loss kill-switch, hourly broker reconcile. Honors shared `HALT_*` flags.
- [ ] `verify_kalman_pair_paper.py` — mirror `verify_pair_paper.py`
      (state/EOD/signal-log consistency checks).
- [ ] `deploy/kalman-pairs-paper.{service,timer}` — systemd units, **authored
      but not installed on host** (precedent: arbitrage units shipped
      uninstalled). Document install steps in the runbook.

### Phase 4 — Dashboard
- [ ] `backend/routers/kalman_pairs_paper.py` + wire into `backend/main.py`
      (mirror `backend/routers/arbitrage_paper.py`). New tab reads the Kalman
      EOD JSON: per-pair γ_t track, Kalman spread + band, z, open positions,
      cumulative return.
- [ ] `tests/test_kalman_pairs_paper_router.py`.
- [ ] Note: dashboard-backend has no auto-deploy — the new router 404s until
      `systemctl restart dashboard-backend.service` on the host.

## Risks / open questions
- **Edge transfer.** The book's Kalman wins are on US ETFs (EWA–EWC, KO–PEP).
  No guarantee NIFTY single-stock-futures pairs behave the same. Phase 2 is the
  go/no-go; if Kalman doesn't beat static β after costs, we stop at a research
  note (Rule 12).
- **α (process noise) is the whole ballgame.** Too large → γ chases noise,
  spread variance collapses, profit dies after costs (book warns of this on
  p. 437). Too small → γ can't adapt, cointegration breaks. Phase 2 sweep must
  find a robust α, not an overfit one.
- **Daily data depth.** Kalman on daily bars needs a healthy training window;
  confirm bhavcopy history per pair is long enough before trusting γ.
- **Ops:** never run a fresh Kite login while either live runner is active
  (token invalidation) — the runner reuses the cached session.

## Sequencing note
Implement on a fresh branch off `main` (current branch is the unrelated
signal-contract Phase 0 work). Phases gate on each other: 0 → 1 → 2, and **2 is
the go/no-go for 3 and 4.**
