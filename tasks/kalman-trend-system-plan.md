# Kalman-Filter Trend-Following System — PLAN (2026-06-27, paper-faithful)

A **new, independent** single-instrument trend-following system that reproduces
the algorithm of **Benhamou, "Kalman filter demystified: from intuition to
probabilistic graphical model to real case in financial markets"** (HAL
`hal-02012471` / arXiv `1811.11618`, 2018), §6 "Numerical experiments", and
applies it to **any stock or index** on daily bars. Equations cited as (B-x) are
from that paper.

**Design stance (user directive 2026-06-27): base the algorithm ENTIRELY on the
paper.** Where the paper and this repo's usual conventions conflict (exit style,
parameter-fitting method, single-split vs walk-forward), the **paper wins**. Repo
*infrastructure* is still reused for how it lives here (runner scaffolding, signal
contract, dashboard) but the **trend model, signal, exits, and optimization are
the paper's, unchanged.**

Runs in parallel with — and does **not** modify — the existing Kalman **pairs**
system (`strategies/kalman_pair_trading.py`) or any other strategy. This is a
*different application of the same math*: the pairs system tracks a time-varying
**hedge ratio between two instruments**; this system tracks the **trend (level +
velocity) of one instrument** and trades it outright, long or short.

---

## 1. The paper's algorithm (faithful, §6 + Table 1/2 + Algorithm 4)

### 1.1 State-space model (B-5.1, B-5.2)
A 2-D state `x_t = [position, velocity]` with control terms:

    x_{t+1} = Φ x_t + c_t + w_t,   w_t ~ N(0, Q)      (B-5.1)
    z_t     = H x_t + d_t + v_t,   v_t ~ N(0, R)      (B-5.2)

The observation `z_t` is the daily close. Table 1 gives four model
specifications of increasing richness, parameterized by `p1…p15`:

| Model | Φ | H | Q | R | P₀ | c_t |
|---|---|---|---|---|---|---|
| 1 | `[[1,dt],[0,1]]` | `[1,0]` | `[[p1²,p1p2],[p1p2,p3²]]` | `p4` | `[[p5,0],[0,p5]]` | `0` |
| 2 | `[[1,dt],[0,1]]` | `[1,0]` | `[[p1²,p1p2],[p1p2,p3²]]` | `p4` | `[[p5,0],[0,p6]]` | `0` |
| 3 | `[[p1,p2],[0,p3]]` | `[p4,p5]` | `[[p6²,p6p7],[p7p6,p8²]]` | `p9` | `[[p10,0],[0,p11]]` | `0` |
| 4 | `[[p1,p2],[0,p3]]` | `[p4,p5]` | `[[p6²,p6p7],[p7p6,p8²]]` | `p9` | `[[p10,0],[0,p11]]` | `[p12(p13−Kₜ), p14(p15−Kₜ)]` |

- **Model 4 is the one used in the experiment** (15 filter params `p1…p15`).
- `Φ=[[1,dt],[0,1]]` (models 1–2) is the classic Newtonian constant-velocity
  trend; models 3–4 let the optimizer free **all** transition/observation
  coefficients rather than fixing them.
- `c_t` is the paper's control/drift term (B-4.2: *"this control term … makes a
  big difference in practice"*). `Kₜ` in `c_t` is a reference quantity the OCR'd
  table does not fully disambiguate — **read Table 1 directly from the PDF before
  implementing model 4's `c_t`** (Rule 8/12). v1 may run model 4 with `c_t=0`
  (i.e. the model-3 subset), because the optimum drives it there anyway (below).

### 1.2 Trend signal (B Algorithm 4)
Each daily bar, run the filter; take the **one-step-ahead prediction of the next
close**, `Predict = H·(Φ x_{t|t} + c_t)` (strictly causal — uses data ≤ t).
With a dead-band offset `µ` against the **previous close**:

    Predict ≥ Close_prev + µ  → enter LONG  at next open (market-on-open)
    Predict ≤ Close_prev − µ  → enter SHORT at next open (market-on-open)

(The paper prints `+µ` on both branches — a typo; it is a symmetric dead-band
`[Close−µ, Close+µ]`. We implement the dead-band.)

### 1.3 Exits (B §6) — fixed ticks
A **fixed profit target** and **fixed stop loss, both in ticks**, set per trade.
No ATR, no trailing, no time-stop — the average ~6-bar hold is an *emergent*
property of the PT/SL, not a separate rule.

### 1.4 Parameter fitting (B §6 + §4.7) — joint CMA-ES on train Sharpe + L1
All **18 parameters** — `p1…p15`, offset `µ`, stop, target — are optimized
**jointly** (not staged) to **maximize the Sharpe ratio over the train period**,
using **CMA-ES** (the paper's §4.7 describes the algorithm in full), with an
**L1 penalty** that drives non-meaningful filter params to zero. The paper's
rationale (B §6): joint optimization lets `µ`/stop/target absorb model
mis-specification instead of corrupting the filter.

### 1.5 Data & protocol (B §6)
- Daily data, **single split: first 6 months train, next 6 months test.**
- Paper instrument: S&P 500 index futures (CQG `EP`), 01Jan2017→01Jan2018.
- Baseline for comparison: a **moving-average crossover** (B Algorithm 5) —
  long when `SMA(short) > SMA(long) + offset`, short when below, same fixed PT/SL.

### 1.6 Reference results (the correctness target — Tables 2–7)
- **Optimal params (Table 2):** p1=24.8, p2=0, p3=11.8, p4=46.2, p5=77.5,
  p6=67, p7=100, p8=0, p9=0, p10=0, p11=100, p12=p13=p14=p15=0; **µ=5, stop=80,
  target=150.** (The L1 penalty zeroed `c_t` and several Q/R/P₀ entries.)
- **Kalman train:** net 5,086€, Sharpe 1.62, 15 trades, 46.67% win, PF 1.75,
  max DD −2,941€. **Kalman test:** net 4,266€, Sharpe 1.40, PF 1.62, max DD
  −1,721€, avg ~6.2 bars in trade.
- **MA crossover test:** net 935€, Sharpe 0.41, PF 1.13 — i.e. Kalman's OOS edge
  over the MA baseline (Sharpe 1.40 vs 0.41) is the paper's whole claim.

**Honest read (Rule 12):** this is *one instrument, one 6-month OOS window, 15
trades, 18 fitted parameters*. The mechanism (a causal one-step Kalman trend
prediction lags less and whipsaws less than an MA crossover) is sound and worth
building; the specific numbers are not a promise. Per the directive, we replicate
the paper's design exactly; the overfitting caveat is recorded in §7, not
engineered away.

---

## 2. How this differs from the existing Kalman PAIRS system (do not retrofit)

`strategies/kalman_filter.py` is hard-wired to the **pairs** observation model:
state `(μ, γ[, γ̇])`, observation row `Z_t = [1, y2_t]`, and a `from_training`
**OLS seeding heuristic** (Palomar §15.6.3). **None of that applies here.** The
paper does *not* seed via OLS — it optimizes `p1…p15` from scratch with CMA-ES,
and its state is `[position, velocity]` with a general `Φ/H`. So the trend filter
is a **new** module that *mirrors the dataclass-step / `update()` / `serialize`
shape* of `kalman_filter.py` but takes the model matrices `(Φ, H, Q, R, P₀, c)`
**directly** (supplied by the optimizer) — no OLS heuristic, no pairs coupling.
Do not bend the pairs filter into this; it is load-bearing for the near-live
pairs system (Rule 3/8).

---

## 3. Reuse map (repo infrastructure only — algorithm stays the paper's)

**Reused (no fork):**
- `core/runner_common.py` — locks, holiday/weekend gate, market-hours loop, IST
  assert, disk/heartbeat, signal handlers, atomic state persist.
- `strategies/base.py` — `BaseStrategy`, `_emit_signal`/signal-contract seam,
  `TradeProposal`, `proposal_to_leg`, `uuid7`, `estimate_transaction_cost`.
- **`equity_pending_entries` next-day-open fill** (`runners/run_equity_swing.py`) — the
  paper enters with a **market order for the open** *after* a close-computed
  signal; this is exactly market-on-open at the next session. Reuse this
  mechanism (queue at close, fill at open, gap handling). Do not reinvent.
- Data layer — bhavcopy / `fetch_bars` / `fetch_historical_data` for daily bars;
  `kite_auth` / `kite_throttle`; shared `HALT_*` kill-switches; dashboard auth.
- The `pair_paper_{system}` EOD convention for the dashboard (Phase 4).

**New dependency (required by the directive):** a CMA-ES implementation. `cma`
and `cmaes` are NOT installed. The paper's optimizer **is** CMA-ES, so to "base
it entirely on the paper" we add **`cmaes`** (pure-python, light) to
`requirements.in` and lock it (per `reference_lockfile_refresh`). This reverses
the pairs system's "no new dep" stance *on purpose* — flagged, not blended
(Rule 7). (Fallback if the dep is rejected: `scipy.optimize.differential_evolution`,
already installed, as a global-optimizer stand-in — but that is a deviation, so
it is the fallback, not the plan.)

**New (this system):** the trend filter, the strategy, the
CMA-ES+L1 optimizer, the train/test backtest, the MA-crossover baseline, the
paper runner, the correctness gate, the tests, the systemd units, the dashboard
surface.

---

## 4. Success criteria (Rule 4)

1. **Reproduces the paper.** `research/validate_kalman_trend.py` runs the full pipeline
   (filter + CMA-ES/L1 fit + fixed-tick exits + 6mo/6mo split) on a daily index-
   futures series and **recovers the paper's qualitative result: optimized Kalman
   OOS Sharpe materially beats the MA-crossover baseline OOS Sharpe**, with the
   optimum sparse (L1 zeroing several params, as in Table 2). On the paper's own
   S&P 500 series (if obtainable) it should land near Table 2/3 numbers. **Fail
   loud (exit ≠ 0)** otherwise; later phases gate on this.
2. **Strictly causal** — `Predict` for bar `t` uses only data ≤ `t-1` (predicted
   state, never filtered/smoothed of `t`). A test pins this; the look-ahead
   variant must fail it.
3. **Runs on any stock or index** given a daily close series — the filter and
   optimizer are instrument-agnostic; only the tick size and the
   long/short-execution capability differ per instrument (see §5 D1).
4. **Paper runner** runs unattended on its own systemd timer, own state file, own
   EOD report, own logfile — zero shared mutable state with other systems except
   the read-only universe and shared kill-switches.
5. **Emits the signal contract** (ENTRY/EXIT envelopes) from day one via
   `base._emit_signal`.
6. **Tests encode WHY** (Rule 9): a synthetic trend+reversal is detected within N
   bars; the dead-band suppresses chop; the CMA-ES+L1 fit recovers a known sparse
   optimum on a synthetic objective; a frozen-estimate variant fails the reversal
   test; serialize→restore is byte-identical; degenerate input fails loud.

---

## 5. Design decisions (paper-faithful; confirm before coding)

- **D1 — Instrument: any single stock or index, traded long & short on daily
  bars** (the paper's design). In this repo's Zerodha context: indices and stocks
  that can be shorted (NIFTY/BANKNIFTY and stock **futures**) implement the paper
  unchanged. **Cash equity cannot be held short overnight on Zerodha** — that is
  an *execution* limit, not an algorithm change: for a cash-equity instrument the
  runner takes long signals only and **logs every skipped short** (Rule 12). The
  filter/optimizer/backtest are identical for every instrument.
- **D2 — Model 4 of Table 1** (general `Φ/H`, full `Q`, scalar `R`, diagonal
  `P₀`, control `c_t`), the spec the paper optimized. Models 1–3 are selectable
  for the A/B the paper implies. v1 may run model 4 with `c_t=0` until Table 1's
  `Kₜ` term is confirmed from the PDF (the optimum has `c_t=0` regardless).
- **D3 — Signal: one-step prediction vs prior close with dead-band `µ`** (B Alg
  4), exactly the paper.
- **D4 — Exits: fixed profit target + fixed stop loss in ticks** (B §6), exactly
  the paper. No ATR / trailing / time-stop. Tick = the instrument's min price
  increment (define per symbol; e.g. NIFTY index-future tick = 0.05).
- **D5 — Fitting: joint 18-param CMA-ES maximizing TRAIN Sharpe, with L1 penalty
  driving filter params to zero; single 6mo train / 6mo test split** (B §6).
  This is the directive's core reversal of my prior plan — no walk-forward, no
  small-subset heuristic; replicate the paper.
- **D6 — Update frequency: DAILY** (B §6). State updates once per day on the
  official close; entries are market-on-open the next session.
- **D7 — Capital/risk isolation during paper.** Own cap, own state file (kept off
  the `*paper_state*` glob so it isn't summed into any live notional cap), own
  book. Shared `HALT_*` still halt it.

---

## 6. Phases (checkable)

**Sequencing:** fresh branch off `main`. Phases gate: 0 → 1 → 2, and **Phase 2
(reproduce the paper's train/test result) is the go/no-go for 3 and 4.**

### Phase 0 — Filter core + optimizer + correctness gate
- [x] `strategies/kalman_trend.py` — pure, no I/O. `KalmanTrendFilter` built from
      explicit `(F, H, Q, R, P₀, c)`; `from_params(p1…p15, model=1..4)` maps the
      Table-1 layout to matrices. `update(close)` returns the causal one-step
      **prediction of the next close** `H·(F x_{t|t}+c)`, predicted/filtered
      (level, velocity), innovation `vₜ`+variance `Fₜ`, std innovation. Forward
      pass per D&K §4.2. `serialize`/`deserialize` of the FULL state `(x, P)`.
      Fails loud on NaN / non-PSD `Q`/`P₀` / `R<0` / divergent `Fₜ≤0`.
  - **Finding 1 — Q parameterization (Rule 1).** Table-1's literal `Q` makes the
    paper's *own* optimum **indefinite** (not a valid covariance). Implemented Q
    as a **Cholesky product** `Q=LLᵀ` — the only reading under which Table-2's
    optimum is a clean rank-1 PSD matrix (det 0 exactly). Faithful + valid.
  - **Finding 2 — Table-2 optimum diverges (Rule 12).** The reported optimal
    `Φ=[[24.8,0],[0,11.8]]` is an **explosive** transition (position ×24.8/bar) →
    the filter diverges, `Fₜ`≤0. The filter fails loud on it (test pins this).
    The OCR'd 15-d vector is almost certainly mis-transcribed / the thin
    experiment isn't reproducible at face value. **Consequence:** the correctness
    gate targets the paper's *qualitative* claim (optimized Kalman beats MA
    crossover OOS), **not** the literal Table-2 vector.
- [x] `research/optimize_kalman_trend.py` — pure: the fixed-tick trend `simulate` (daily-
      close stop/target approximation, documented), `kalman_direction` (Alg 4) +
      `ma_direction` (Alg 5) causal signals, `run_cmaes` (the paper's optimizer,
      `cmaes` dep added to requirements.in/lock), and `fit_kalman_trend` /
      `fit_ma_crossover` maximizing train Sharpe (Kalman adds the L1 penalty).
      Catches the filter's fail-loud (divergent params) → worst fitness so CMA-ES
      steers away. **Conditioning fixes (Rule 1):** decision vector is in std-dev/
      price-point space (data-driven bounds scaled to the daily move), CMA-ES runs
      in normalized [0,1] coords (one well-scaled `sigma`), and the L1 is taken in
      normalized space (scale-invariant sparsity). **D5 deviation (Rule 12):** the
      fit optimizes **model 1** (Newtonian, Φ fixed) — model 3/4's free Φ diverges
      (finding #2), so the literal 18-param model-4 fit is not reproducible; this
      is the stable Table-1 spec.
- [x] `tests/test_kalman_trend.py` (Rule 9), 11 pass: recovers a known constant
      slope; **one-step forecast beats a same-lag SMA** (the paper's claim; a
      lagging predictor fails); **adapts to a trend reversal within N bars**
      (frozen-velocity would not); forecast is causal (truncation-identical);
      serialize→deserialize byte-identical through subsequent steps; indefinite-Q
      / R<0 / NaN / too-few-params / model-4-nonzero-control fail loud;
      model-4-zero-control ≡ model-3; Table-2 optimum diverges (fail loud).
- [x] `tests/test_optimize_kalman_trend.py` (Rule 9), 10 pass: stop/target book at
      the LEVEL not the close (long/short, target/stop); costs reduce realized;
      Kalman signal is net-long on an uptrend / net-short on a downtrend; MA flips
      on reversal; **CMA-ES+L1 shrinks irrelevant params to ~0** (the paper's
      sparsity mechanism); the Kalman fit finds a profitable, multi-trade strategy
      on a regime-switching series; the fit rejects unstable models 3/4.
- [x] **Correctness gate:** `research/validate_kalman_trend.py` — 6mo train / 6mo test,
      fits Kalman + MA on train, compares **TEST** Sharpe across **multiple seeds**
      (a single-seed pass is cherry-picking — finding #3). Missing data → clean
      exit 2 with remediation; genuine Kalman<MA → exit 1.
  - [x] **BANKNIFTY fetch wired + run** (`market_data/fetch_index_daily.py`, 5 tests): resolves
        the F&O symbol → NSE spot name (BANKNIFTY→"NIFTY BANK"), reuses `.env`
        (`load_dotenv`) + the Kite session, writes `data_cache/BANKNIFTY_daily.csv`
        (gitignored). Fetched 271 daily closes (2025-05-23→2026-06-25).
  - **Finding 3 — NO-GO under faithful replication (Rule 12).** Full writeup in
    `tasks/kalman-trend-findings.md`. 5-seed gate: **NIFTY** Kalman median OOS
    Sharpe **0.20 vs MA 0.93** (win-rate 20%); **BANKNIFTY** Kalman **−0.07 vs MA
    0.07** (40%). Kalman's median OOS **loses to MA on both**; per-seed OOS swings
    −1.18→+1.03 while train Sharpe is 2–5 → the fit **overfits**, the result is
    seed luck. The earlier "NIFTY 0.66 vs 0.31" was seed-0 luck. This confirms
    §7 risk #1 (single-split multi-param overfit), not a bug (signals causal, MA
    fit identically). **GATE FAILED.** Do not promote to Phase 1. Decision (A stop
    / B walk-forward robustness / C match the paper's intraday-futures setting) is
    in the findings doc — recommend A or B.

### Backtest verdict NO-GO → building a FORWARD paper A/B anyway (2026-06-27)
**Update:** after the daily + intraday NO-GO, the user opted to build the
intraday **Kalman-vs-MA paper A/B as a forward test** (paper = zero risk; live
fills are the one thing backtests can't model). Built **fully intraday**:
- `strategies/kalman_trend_following.py` — `IntradayTrendStrategy`, online, two
  interchangeable engines (kalman one-step forecast / MA crossover), fixed-tick
  stop/target with `check_exit` for intraday fills between 5-min bars, warmup,
  full serialize/restore. 13 tests.
- `runners/run_paper_kalman_trend.py` — two books per instrument (NIFTY+BANKNIFTY futures)
  stepped on identical 5-min bars; pure core (BarAggregator, InstrumentBooks,
  eod_report, warmup `fit_params`) is unit-tested (6 tests); Kite-wired main() is
  host-smoke-test-only. EOD sidecar `kalman_trend_eod_<date>.json` reports
  kalman−ma ₹. `deploy/kalman-trend-paper.{service,timer}` authored, NOT installed.
- [ ] **Host smoke-test (operator):** run once in paper on the VPS (reuse the
  cached Kite session — no fresh login while a live runner is active).
- Go in **expecting parity**, to MEASURE forward fills — not assuming a Kalman win.

**The backtest verdict below stands (this is a forward A/B, not a refutation):**
On 8.2 years of NIFTY/BANKNIFTY daily (2018–2026, 2035 bars, walk-forward, pooled OOS):
- **Option B** (reduced 4-param fit + walk-forward, `research/backtest_kalman_trend.py`)
  removed the single-split overfit. On ~1yr it looked promising (BANKNIFTY 3/3
  configs beat MA), but that was a 4–6-fold small-sample artifact.
- **Deep history (28 folds): Kalman LOSES to a plain MA crossover.** NIFTY Kalman
  Sharpe 0.28/0.10 vs MA 0.80/0.88 (wins 32%/25%); BANKNIFTY a wash (loses the
  28-fold cfg, wins the 12-fold). Both profitable in the bull market, but MA is
  the better and far simpler trend follower. Full table in
  `tasks/kalman-trend-findings.md`.
- The paper's Kalman≫MA edge (S&P 500 index futures) **does not transfer** to NSE
  index daily. Artifacts (filter, optimizer, harness, gate — 29 tests, all green)
  are kept for reference / a possible future re-test on a different instrument
  class (intraday, or the paper's own futures), but the strategy is **not** worth
  trading over MA. The Phase 1–4 spec below is retained only as a record of intent.

### Phase 1 — Strategy class  (NOT BUILT — see HALT above)
- [ ] `strategies/kalman_trend_following.py` — `KalmanTrendStrategy(BaseStrategy)`.
      One `KalmanTrendFilter` per instrument with **optimized params loaded from
      the Phase-2 fit** (a `kalman_trend_params.json`, per-instrument). Daily
      `step_daily_close(close)` (D6); dead-band signal (D3) → ENTRY at market-on-
      open; **fixed-tick stop/target** (D4) → EXIT; long & short for shortable
      instruments, long-only skip + log for cash equity (D1). Kite-light (injected
      `quote_fn`/`clock`) for unit-testing without a broker. Reuses
      `estimate_transaction_cost`, `proposal_to_leg`, the signal seam.
- [ ] `serialize_state`/`restore_state` persist the full filter state + the
      open-position context (entry price, side, stop/target in ticks).
- [ ] `tests/test_kalman_trend_following.py` (Rule 9): long fires above the band,
      short below, dead-band suppresses chop; entry→target and entry→stop paper
      cycles book correct fixed-tick P&L; daily update advances the filter by one;
      serialize→restore byte-identical subsequent signals; cash-equity short is
      skipped+logged; paper mode requires a notional cap.

### Phase 2 — Backtest = reproduce the paper (THE GATE)
- [ ] `research/backtest_kalman_trend.py` — **single 6mo train / 6mo test** split (B §6) on
      daily bars for a chosen instrument (NIFTY/BANKNIFTY futures first, then any
      requested stock/index). Runs the **CMA-ES+L1 fit on train**, evaluates on
      test, and computes the paper's metric set: net/gross P&L (after
      `estimate_transaction_cost`), Sharpe, profit factor, % profitable, max DD,
      # trades, avg bars in trade. Runs the **MA-crossover baseline** (B Alg 5)
      through the identical execution/cost path.
- [ ] Findings → `tasks/kalman-trend-findings.md`: per-instrument train/test
      table mirroring the paper's Tables 3–7, Kalman-vs-MA OOS delta, the fitted
      sparse params, and an **honest overfitting assessment** (param count vs
      trade count, sensitivity).
- [ ] **Go/no-go:** the optimized Kalman must beat the MA crossover on the test
      half after costs (the paper's claim). If not → research note, stop (Rule 12).

### Phase 3 — Paper runner (only if Phase 2 = GO)
- [ ] `runners/run_paper_kalman_trend.py` — mirrors `runners/run_paper_kalman_pairs.py` /
      `runners/run_equity_swing.py` via `runner_common`: TOTP auth (**reuse cached Kite
      session — never fresh-login while a live runner is active**), holiday/
      weekend gate, daily close state update, **signal → market-on-open via the
      `equity_pending_entries` mechanism**, fixed-tick stop/target management,
      atomic crash-safe persist, heartbeat, shared `HALT_*`. Loads optimized
      per-instrument params from `kalman_trend_params.json`. Own state
      `kalman_trend_runner_state.json`, EOD `kalman_trend_eod_<date>.json`, log
      `paper-kalman-trend-*.log`. paper/signals only; live raises
      `NotImplementedError` until the forward paper test validates.
- [ ] `tests/test_run_paper_kalman_trend.py` (Rule 9): front-month resolution;
      build loads params + skips unresolvable; **daily-step + state round-trip
      byte-identical**; signal→pending-entry→market-on-open fill path; EOD shape.
- [ ] `deploy/kalman-trend-paper.{service,timer}` — authored, **NOT installed**
      (pairs/arbitrage precedent); staggered after the other runners.
- [ ] **Host smoke-test (operator):** run once on the VPS in paper, reusing the
      cached Kite session.

### Phase 4 — Dashboard (only if Phase 3 running)
- [ ] Prefer reuse: write a `pair_paper_{system}`-style EOD so it slots into an
      existing compare tab; else a small dedicated `/api/kalman-trend` router+tab
      (decide at build time, Rule 2). Surface per-instrument position, close vs
      one-step prediction, velocity sign, open-trade fixed-tick P&L.
- [ ] Note: **dashboard-backend has no auto-deploy** — new routers 404 until
      `systemctl restart dashboard-backend.service`.

---

## 7. Risks / open questions

- **Overfitting (acknowledged, per directive not mitigated by design).** 18
  params fit on one 6-month window is exactly the trap this repo has hit before
  (buy-on-gap train 2.71 → test −0.83; autoresearch objective drift). We replicate
  the paper faithfully and **report the fragility honestly** in findings (param-
  vs-trade count, OOS sensitivity). If the live/paper edge later proves a fitted
  curve, that is a finding to surface, not a silent failure (Rule 12).
- **Edge transfer.** The paper's win is on S&P 500 index futures. NIFTY/BANKNIFTY
  / NSE stock futures may not trend the same at daily resolution. Phase 2 is the
  honest test on each instrument.
- **`Kₜ` in model 4's `c_t`** is not fully disambiguated by the OCR'd Table 1 —
  **read it from the PDF before implementing the control term.** v1 can run
  `c_t=0` (the optimum's value).
- **Tick semantics.** "stop=80 ticks, target=150 ticks" depends on the
  instrument's min tick; define per symbol (e.g. NIFTY future tick 0.05) so the
  ₹ stop/target match the paper's intent.
- **Shorting constraint** (D1): cash equities can't be shorted overnight on
  Zerodha → cash-equity instruments are long-only; shortable instruments (index/
  stock futures) implement the paper unchanged. Log every skipped short.
- **New dependency.** Adds `cmaes` (Rule 7 — flagged reversal of the pairs
  system's no-new-dep stance, justified by "base it entirely on the paper").
- **Costs vs edge.** A fixed-tick flipper can be eaten by per-trade STT/charges;
  all Phase 2 numbers are **after** `estimate_transaction_cost`.
- **Ops:** never run a fresh Kite login while any live runner is active (token
  invalidation). The runner reuses the cached session.

## 8. Paper artifact
Source PDF in scratchpad (HAL is behind an Anubis anti-bot wall; the identical
arXiv `1811.11618` preprint was used). To track it, drop it in `docs/references/`.
The OCR of Table 1 is imperfect — **§1.1's matrix table is the implementer's
spec, but verify Table 1 and the `Kₜ` control term against the PDF.**
