# Kalman Trend-Following — Phase-0 gate findings (2026-06-27)

**Verdict: NO-GO under faithful paper replication.** An optimized Kalman trend
follower does **not** robustly beat an optimized moving-average crossover
out-of-sample on NIFTY or BANKNIFTY daily. The result is dominated by
overfitting / optimizer noise, not edge.

## Protocol (faithful to Benhamou §6)
- Model-1 (Newtonian) Kalman trend filter; signal = causal one-step forecast vs
  prior close with dead-band µ (Algorithm 4); fixed-tick profit-target/stop-loss.
- Joint CMA-ES fit of all params on the **train** half maximizing Sharpe, with an
  L1 penalty on the filter params; single **6-month train / 6-month test** split.
- Baseline: MA crossover (Algorithm 5), its params **also** CMA-ES-fit on train.
- Data: NIFTY (246 daily closes, the cached 1-yr F&O underlying) and BANKNIFTY
  (271 daily closes, fetched via `fetch_index_daily.py`). Cost 2.5 pts/side.
- Deviation forced by finding #2 (Phase 0): model 4's free Φ diverges, so the fit
  uses the stable model 1. The literal 18-param model-4 fit is not reproducible.

## Result (5 optimizer seeds; verdict on the MEDIAN seed)
| symbol | Kalman OOS median | MA OOS median | Kalman OOS [min,max] | win-rate |
|---|---|---|---|---|
| NIFTY | **0.20** | **0.93** | [−0.29, 0.56] | 20% |
| BANKNIFTY | **−0.07** | **0.07** | [−1.18, 1.03] | 40% |

- Kalman's MEDIAN OOS Sharpe **loses to MA on both** indices.
- The per-seed spread is enormous (NIFTY −0.29→0.56; BANKNIFTY −1.18→1.03) while
  train Sharpe is routinely 2–5: the fit overfits the train half and OOS outcome
  is **seed luck**, not a stable signal.
- A single seed *can* show "Kalman 0.66 vs MA 0.31" (NIFTY, seed 0) — which is
  exactly why a single-seed gate is misleading; the gate now aggregates seeds.

## Interpretation
This empirically confirms the plan's §7 risk #1 and the repo's recurring scar
(buy-on-gap train 2.71 → test −0.83; autoresearch objective drift): **a joint
8-param fit on one 6-month window maximizing in-sample Sharpe overfits.** It is
not (detectably) a bug — signals are causal, no look-ahead, exits book at the
stop/target level, and the MA baseline is fit the same way. The faithful protocol
itself is the source of the fragility.

## Caveats (what this does NOT prove)
- The paper's edge is on **S&P 500 index futures**; NIFTY/BANKNIFTY daily may
  simply trend differently. Not tested: the paper's intraday/futures setting.
- Daily-close approximation of intraday fixed-tick PT/SL (documented in
  `optimize_kalman_trend.simulate`).
- Model 1, not the paper's model 4 (which diverges; finding #2).
- ~123 train bars is thin for an 8-param Sharpe maximization.

## Decision needed (paths forward)
- **A — Stop at this research note (Rule-12 NO-GO).** Honor the go/no-go: the
  paper's method does not transfer to Indian index daily under faithful
  replication. Keep the (correct, tested) filter + gate as the artifact.
- **B — Re-introduce robustness discipline** (walk-forward folds, fewer free
  params, OOS-selected regularization). This was the *original* plan, set aside
  for the "base entirely on the paper" directive; it would test whether ANY
  stable edge exists rather than reproducing the paper's exact (fragile) protocol.
- **C — Match the paper's setting more closely** (intraday/futures bars, longer
  history) before judging transfer.

Recommendation: **A or B.** Do not promote to Phase 1 (paper runner) on this
result — there is no demonstrated OOS edge to trade.

---

# Option B — robustness discipline (2026-06-27)

Re-ran with the anti-overfit discipline the single-split protocol lacked:
- **Reduced 4-param Kalman fit** (`fit_kalman_reduced`): ONE filter knob (velocity
  process std) + µ/stop/target; R and P₀ seeded from the data. Fewer params = the
  regularization. (Down from 8 params; built on the stable Newtonian model 2.)
- **Walk-forward** (`backtest_kalman_trend.py`): many rolling train→test folds;
  fit on each train, score the immediately-following OOS test, multi-seed.
- **Pooled OOS metric:** concatenate the non-overlapping test-slice daily P&L
  across folds → ONE Sharpe. (A per-20-bar-window Sharpe is dominated by the
  no-trade penalty — that first cut was degenerate and not interpretable.)

## Result on the cached ~1-year data (pooled OOS Sharpe, Kalman vs MA)
| config train/test/step (seeds) | NIFTY Kal | NIFTY MA | NIFTY win | BANKNIFTY Kal | BN MA | BN win |
|---|---|---|---|---|---|---|
| 120/20/20 (3) | 2.25 | 1.46 | 67% | 0.70 | −0.01 | 57% |
| 100/30/30 (5) | 2.48 | 1.66 | 75% | 2.63 | −0.21 | 60% |
| 140/21/21 (5) | **−0.96** | 1.11 | 20% | 3.44 | −0.13 | 50% |

## Read
- **Clear improvement over the faithful protocol** (which was NO-GO on both).
- **BANKNIFTY: robust** — Kalman beats MA OOS in all 3 fold geometries.
- **NIFTY: fragile** — strong in 2/3 but flips negative in the 140/21 geometry
  (wins only 20% of folds). The result is **config-sensitive**.
- **Cause = data depth.** ~1 yr → only 4–6 folds; geometry-sensitivity is the
  expected small-sample symptom. **Suggestive, not conclusive.**

## Deep history — the deciding test (8.2 years, 2018-04 → 2026-06, 2035 bars)
Fetched multi-year NIFTY/BANKNIFTY daily via Kite (`fetch_index_daily.py --days
3000`) and re-ran the walk-forward with many folds (pooled OOS Sharpe, 3 seeds):

| config train/test/step | folds | NIFTY Kal | NIFTY MA | NIFTY win | BN Kal | BN MA | BN win |
|---|---|---|---|---|---|---|---|
| 252/63/63 (1yr→1q) | 28 | 0.28 | **0.80** | 32% | 0.35 | **0.64** | 46% |
| 504/126/126 (2yr→6mo) | 12 | 0.10 | **0.88** | 25% | **0.44** | 0.16 | 58% |

### VERDICT — NO-GO. Kalman does not beat MA on Indian index daily.
- **NIFTY: decisive loss.** Kalman loses to MA in BOTH geometries (Sharpe 0.28 vs
  0.80, 0.10 vs 0.88; wins only 32% / 25% of folds; ~⅓ the P&L). MA captures the
  2018–2026 bull trend far better.
- **BANKNIFTY: a wash.** Kalman loses the 28-fold cfg1 (0.35 vs 0.64) and wins the
  12-fold cfg2 (0.44 vs 0.16) — i.e. coin-flip, not a robust edge. On the
  higher-fold (more reliable) cfg1 it loses.
- **The 1-year Option-B "edge" was a small-sample artifact.** With 4–6 folds it
  looked promising; with 28 folds it disappears. Exactly the failure mode this
  whole investigation was guarding against.

### Bottom line across all three attempts
1. Faithful paper protocol (8-param, single split) → overfit, NO-GO.
2. Option B (4-param + walk-forward), 1 year → promising but data-limited.
3. **Option B on 8 years → Kalman clearly loses to a simple MA crossover.**

The paper's headline (Kalman ≫ MA OOS, on S&P 500 index futures) **does not transfer
to NIFTY/BANKNIFTY daily.** Both strategies are profitable in the bull market, but
MA is the better (and far simpler) trend follower here.

---

# Intraday (5-min) re-test (2026-06-27)
User hypothesis: the daily-close backtest hides intraday entry/exit dynamics
where the lower-lag Kalman could win. Fetched 9000 5-min bars each (NIFTY,
BANKNIFTY; ~6 months) via `fetch_index_daily.py --interval 5minute` and re-ran
the walk-forward on 5-min bars (windows now in bars; absolute Sharpe mis-
annualized but the Kalman-vs-MA comparison is unaffected).

| run | NIFTY Kal vs MA | BANKNIFTY Kal vs MA |
|---|---|---|
| light (1 seed, 11 folds) | +0.17 vs −0.09 | +0.68 vs +0.36 |
| **heavy (5 seeds, 22 folds)** | **+0.03 vs −0.04 (wash)** | **+0.27 vs +0.43 (MA wins)** |

**The light single-seed run looked like a reversal (Kalman > MA on both); the
multi-seed run dissolved it** — NIFTY collapses to a wash (both ≈ flat) and
BANKNIFTY flips to MA (Kalman wins only 27% of folds). Same small-sample mirage as
every other favourable cut in this investigation.

### Overall verdict — consistent NO-GO, daily AND intraday
Every time robustness is added (seeds, folds, history), the Kalman edge evaporates:
daily single-split overfit → daily 1yr looked good → daily 8yr/28-fold MA wins;
intraday 1-seed looked good → intraday 5-seed wash/MA-wins. **No robust Kalman>MA
edge exists in NIFTY/BANKNIFTY at either resolution.**

### CORRECTION (2026-06-27, post code-review): the deep-history NO-GO was a metric bug
A high-effort code review found `walk_forward` was AVERAGING per-seed daily P&L
before computing the pooled Sharpe — variance reduction that inflated Sharpe
~√N, **asymmetrically**. Fixed to per-seed pooled Sharpe → median over seeds
(commit c63053b), plus warmup parity and NaN (not −10/0) degenerate handling.
Re-ran the same deep geometries:

| geometry (folds) | symbol | OLD (buggy) kal vs MA | CORRECTED kal vs MA | win |
|---|---|---|---|---|
| cfg1 1yr→1q (28) | NIFTY | 0.28 vs 0.80 | 0.62 vs 0.56 | 39% FAIL |
| cfg1 (28) | BANKNIFTY | 0.35 vs 0.64 | 0.12 vs 0.22 | 49% FAIL |
| cfg2 2yr→6mo (12) | NIFTY | 0.10 vs 0.88 | **1.05 vs 0.15** | 56% PASS |
| cfg2 (12) | BANKNIFTY | 0.44 vs 0.16 | 0.45 vs −0.27 | 58% PASS |

**Re-confirmed 2026-06-28** after the 2nd code-review tightened the verdict
(fold-win now requires Kalman actually traded; majority-of-seeds-traded guard):
identical Sharpes, win-rates slightly lower (BANKNIFTY cfg1 49%→39%, cfg2 58%→56%
— the closed loophole removing spurious no-trade "wins"), and **no PASS/FAIL
flipped**. The mixed/geometry-dependent conclusion is robust to the fix.

The bug **inflated MA** (its dispersed seeds averaged to low variance) and
**deflated Kalman** (its dispersed seeds' P&L partly cancelled). So the earlier
"deep history → MA decisively wins, clean NO-GO" was **largely the metric bug**
— that conclusion is RETRACTED. Corrected picture: **mixed / geometry-dependent**.
Kalman wins the longer-train cfg2 (both symbols) and ≈-ties MA on the more-folds
cfg1 (win-rate <50%). No robust geometry-independent edge, but Kalman is far more
competitive than the buggy numbers showed — which makes the forward paper A/B the
right call, not a foregone NO-GO.

### Decision (user, 2026-06-27): build the paper A/B anyway as a FORWARD test
Backtests can't model live fills; paper is zero-risk. Building the intraday
MA-vs-Kalman paper runner to **measure** forward parity, not assume a winner.
Phase 1 strategy (`strategies/kalman_trend_following.py`, 13 tests) done; runner
next. Go in expecting parity.
