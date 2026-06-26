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

### Phase 0 — Filter core + correctness gate  ✅ DONE (2026-06-24)
- [x] `strategies/kalman_filter.py` — pure, no I/O. `KalmanPairFilter` with
      `update(y1, y2)` returning predicted/filtered state, innovation vₜ and
      its variance Fₜ, the causal spread z_t, and standardized innovation
      vₜ/√Fₜ. Both models (15.3 basic, 15.4 momentum) selectable. `from_training`
      implements the §15.6.3 OLS heuristic (σ²_ε, σ²_μ=α·σ²_ε, σ²_γ=α·σ²_ε/Var[y₂],
      init states/cov). Forward pass per §4.2 (D&K). Includes serialize/
      deserialize of the FULL state (a, P) for Phase-1 restart fidelity.
      Momentum Q decomposition (book is silent on it): random walk on the
      velocity γ̇ only; γ floored. Verified the robust, α-invariant benefit is
      lower tracking lag on a drifting γ (not the book's α-dependent "smoother γ"
      claim) — see test below.
- [x] `tests/test_kalman_filter.py` (Rule 9), 11 tests pass: recovers constant
      γ; spread more stationary than naive diff; **tracks a step change in γ**
      (the test a frozen estimate fails); momentum tracks a drifting γ with
      lower lag (α-matched); spread uses predicted state (no look-ahead, pinned
      to v/(1+γ_pred)); serialize→deserialize identity; NaN/constant/collinear/
      too-short → fail loud.
- [x] **Correctness gate:** `validate_kalman_filter.py` (deterministic synthetic
      Eq-15.1 pair; `--csv` for ad-hoc real pairs) reproduces the book's
      ordering and FAILS LOUD otherwise. Result: γ adapts 0.60→0.75, band tight
      [0.59,0.79]; spread variance static 2.31 ≫ basic 0.19 > momentum 0.16
      (~13× more stationary, Fig 15.22); cumulative P&L static 190 < basic 250 <
      momentum 266 (Fig 15.23 ordering). EXIT 0 = gate passed; later phases
      unblocked.

### Phase 1 — Strategy class  ✅ DONE (2026-06-24)
- [x] `strategies/kalman_pair_trading.py` — `KalmanPairStrategy(BaseStrategy)`.
      One `KalmanPairFilter` per pair, seeded from the training window; once-per-
      day state update via `step_daily_close` (D1); ENTRY/EXIT proposals off the
      rolling-z band on the causal Kalman spread (D3); structure-scoped risk
      band (`_structure_risk_band` → stop/target in z and ₹, the static system's
      STRUCTURE-scope reasoning). Kite-light by design (injected `quote_fn`/
      `clock`) so it's testable without a broker; reuses `PairLeg`,
      `_trading_days_between`, and the established `estimate_transaction_cost`.
- [x] `serialize_state` / `restore_state` persist the **full filter state**
      (vector + covariance) AND the daily-spread window — neither is
      recoverable from bhavcopy alone. β-lock: entry-time (μ, γ) frozen into the
      position and used to manage it while the filter keeps tracking.
- [x] `tests/test_kalman_pair_trading.py` (Rule 9), 9 tests pass: entry fires on
      the correct side of the band; >max_entry_z refuses; full entry→mean-revert
      paper cycle books a trade; spread uses the predicted state (no look-ahead);
      daily update advances filter + window by one; **serialize→restore is
      identity proven via byte-identical subsequent spreads** (covariance
      preserved, not just γ); wrong-pair restore fails loud; β-lock holds the
      open hedge ratio while the filter drifts; paper mode requires a notional
      cap.
- **Decisions / deviations (Rule 12):** D5 signal emission uses base
  `_emit_signal` because the §4 `_publish_signal` contract is **not on `main`**
  (it lives on the unmerged `signal-contract-phase0-pair-emit` branch). `_publish`
  is the single seam to wire it in when that branch merges — no duplication, no
  fabricated contract. Live execution raises `NotImplementedError` (Phase 3).
  Spread uses LOG prices (per user decision 2026-06-24, matching the book):
  spread = (log p_a − γ·log p_b − μ)/(1+γ); γ is an elasticity, leg sizing is
  dollar-weighted, fills/P&L use raw prices. A `min_edge_multiplier` cost-hurdle
  (parity with pair_trading) was added during Phase 2.

### Phase 2 — Backtest + validation on our pairs  ✅ DONE (2026-06-24)
**Verdict: CONDITIONAL GO → forward paper test (Phase 3). Full writeup in
`tasks/kalman-pairs-findings.md`.**
- [x] `backtest_kalman_pairs.py` — screen-on-train / test-on-holdout (no
      look-ahead). static (Kalman α≈0) vs basic vs momentum through the
      *identical* code path. Reports trips, net/gross P&L, win%, spread-var,
      half-life; `--min-edge-multiplier` tunable.
- [x] `sweep_kalman_pairs.py` — screens once, sweeps α×entry×exit (18 cells)
      on the holdout; small book-anchored grid, OOS by construction.
- [x] `compare_kalman_vs_paper.py` — **real-data Test A**: replays the live
      paper book's actual pairs over the last month, static vs Kalman.
- [x] Findings → `tasks/kalman-pairs-findings.md`. Headlines:
      • Test A (in-regime, live pairs, last month): **Kalman +₹225k vs static
        +₹158k (+43%)**, both profitable; Kalman refuses 5 non-cointegrated
        pairs the live book trades on raw β.
      • Test B (2-year OOS): everything loses, but **Kalman loses ~70% less**
        than static and is more stationary on **8/8 pairs**. Not cost-bleed
        (proven by pushing the hurdle to 100×) — it's adverse non-reversion;
        absolute profitability is regime-dependent.
      • Kalman beats static in BOTH regimes (relative); absolute edge is
        regime/selection-driven → settle it with a forward paper test.

### Phase 3 — Paper runner  ✅ DONE (build) 2026-06-24 — needs host smoke-test
- [x] `run_paper_kalman_pairs.py` — mirrors `run_paper_pairs.py` scaffolding via
      `runner_common` (TOTP auth, holiday/weekend gate, 09:15→15:25 loop, shared
      `HALT_*` kill switches, atomic crash-safe state persist, silent-fail
      heartbeat). Own system: state `kalman_pairs_runner_state.json`, EOD
      `pair_paper_kalman_eod_<date>.json`, log `paper-kalman-pairs-*.log`.
      Kalman-specific: seeds each filter from bhavcopy (log prices), advances
      ONE daily step at the close (D1), persists the FULL filter state +
      z-window, skips non-cointegrated pairs at build. paper/signals only
      (live raises NotImplementedError until the forward test validates).
- [x] `tests/test_run_paper_kalman_pairs.py` (Rule 9), 4 tests: front-month
      resolution, build seeds + skips unresolvable, **daily-step + state
      round-trip is byte-identical** (full filter state persisted), EOD shape.
- [x] `deploy/kalman-pairs-paper.{service,timer}` — authored, **NOT installed**
      (arbitrage precedent); timer 09:14 IST, staggered after the other runners.
- [ ] **Host smoke-test (operator):** cannot verify live in CI (no Kite
      session); run once on the VPS in paper. NOTE: reuse the cached Kite
      session — do not fresh-login while the live static runner is active.
- [ ] `verify_kalman_pair_paper.py` — deferred to deploy-time; the EOD sidecar
      shares the static system's shape, so the existing verifier is adaptable.
      Lower value until forward data accumulates.
- **Forward A/B test is now runnable:** `compare_kalman_vs_paper.py` (Phase 2)
  already compares Kalman vs the live book on accumulating EOD data — re-run it
  weekly; the paper runner adds an independent forward book once smoke-tested.

### Phase 4 — Dashboard  ✅ DONE (2026-06-25) — via the existing compare tab
Chosen approach (Rule 2): instead of a dedicated router, the Kalman runner now
writes the `pair_paper_{system="kalman"}` filenames, so "kalman" is a first-class
system in the EXISTING `/pair-paper-compare` router + `PaperSystemCompare` tab +
`compare_paper_systems.py` CLI — zero new backend code. That head-to-head
(baseline vs persistent vs kalman) IS the forward A/B view.
- [x] Runner emits EOD `pair_paper_kalman_eod_<date>.json` (the compare tooling's
      `pair_paper_{system}` convention); state is `kalman_pairs_runner_state.json`
      (kept off the `*paper_state*` glob so the paper book isn't summed into the
      live runner's notional cap).
- [x] `PaperSystemCompare.tsx` default systems → `baseline,persistent,kalman`;
      title/blurb updated (the `systems` field is free-text, so it was already
      possible — this surfaces it by default). Empty cells render until the
      runner is live.
- [x] `tests/test_pair_paper_compare.py::test_kalman_system_included_three_way`
      pins that a kalman-tagged EOD is read/aggregated in the 3-way compare.
- [ ] Deferred (nice-to-have): a Kalman-specific γ_t / spread-band visualization
      (the compare tab shows P&L, not the filter internals). Build only if the
      forward test warrants it.
- [ ] Note: dashboard-backend has no auto-deploy — changes 404/stale until
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
