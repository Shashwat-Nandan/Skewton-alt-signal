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

## Next: deeper history (in progress)
Fetching multi-year NIFTY/BANKNIFTY daily (Kite session, same NSE source) →
30–80 folds, to settle whether NIFTY's edge is real or a small-sample artifact.
Result + verdict to be appended here.
