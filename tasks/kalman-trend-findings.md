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
  (271 daily closes, fetched via `market_data/fetch_index_daily.py`). Cost 2.5 pts/side.
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
- **Walk-forward** (`research/backtest_kalman_trend.py`): many rolling train→test folds;
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
Fetched multi-year NIFTY/BANKNIFTY daily via Kite (`python -m market_data.fetch_index_daily --days
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
BANKNIFTY; ~6 months) via `python -m market_data.fetch_index_daily --interval 5minute` and re-ran
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

---

# EOD-flatten vs the fitting environment (2026-07-14)

Triggered by an operator question ("why flatten EOD — can we trail instead?").
Replayed the **live NIFTY MA book** (`IntradayTrendStrategy`, the exact deployed
params from `kalman_trend_runner_state.json`: short=13 long=52 offset=51.4
stop=250 target=670, cost 2.5/side, lot 75) over `NIFTY_5minute.parquet` —
120 sessions, 2025-12-30 → 2026-06-25.

## The defect: the optimizer and the runner model DIFFERENT strategies

`optimize_kalman_trend.simulate` — the function that **fits these params** — has
**no EOD flatten**. It holds to stop/target continuously across bars and days
(replay: avg **4.3-day** holds, max **19 days**), force-closing only at the end
of the whole series. `run_paper_kalman_trend` force-closes **every day at
15:25**. So the params are fit for multi-day holds and deployed with a daily
flatten.

Consequence: the **670-pt target is unreachable inside one session** (NIFTY
daily range ~200–300 pts) and **fired 0 times in 120 sessions**. What is
actually deployed is *"cross the 13/52 SMA, hold to 15:25, with a 250-pt
disaster stop"* — the fitted target is decorative, and the fitted stop only
fires on disasters.

## Measured (parity-gated: variant A reproduces the class replay to ₹0)

| Variant | Net | Sharpe | maxDD | Trades | Win | Overnight |
|---|---|---|---|---|---|---|
| A — current (EOD flatten) | +₹74,708 | 0.95 | −₹85,934 | 119 | 53% | 0 |
| **B — hold to stop/target (= the FIT environment)** | **−₹321,953** | **−2.44** | −₹341,059 | 38 | 24% | 32 |
| C — trailing stop 100pt | +₹443,993 | 3.58 | −₹58,515 | 153 | 44% | 78 |
| C — trailing stop 150pt | +₹387,236 | 2.91 | −₹64,448 | 102 | 39% | 73 |
| C — trailing stop 250pt | −₹61,087 | −0.42 | −₹200,299 | 61 | 36% | 54 |

A's exits: force_close ×111 = **+₹227,734** (avg +₹2,051); stop ×8 =
**−₹153,026** (avg −₹19,128). **The EOD flatten is the profitable exit; the
fitted stop is the bleed.** The strategy is profitable *because of* the flatten,
not because of its fitted exits — and its own fitting environment (B) loses
₹322k with the params that environment produced.

Method notes (honesty): variant A books stops/targets at the LEVEL (parity with
the live class); B/C book **stops at the worse of (level, bar price)** — across
an overnight gap you do not get your stop price. Close-only 5-min data, so gaps
are close-to-close approximations. The scratch replay is not committed; the
spec below supersedes it.

## Read
1. **The fit/deploy mismatch is a defect** independent of the trailing question
   → issue #121. Either `simulate` must model the EOD flatten, or the runner
   must stop flattening — today they contradict, and the fit is measurably
   harmful in its own environment.
2. **The trailing-stop numbers are NOT a result.** 3 trail values tried and the
   best quoted (selection, not a fit); in-sample-ish on one instrument/one seed;
   sign-flips by 250pt; 78/153 trades go overnight → NRML margin (~3× MIS) and
   real gap risk; and the live `check_exit` books stops **at the level**, which
   would systematically overstate any overnight book. Proper test spec:
   `tasks/kalman-trend-trailing-stop-experiment.md` (issue #122).
3. Regime tag `gap/event` in SKILL.md already flags "backtest does not model
   intraday gap fills faithfully; low trust" — that caveat now has teeth: it is
   the blocking modelling gap for any overnight variant.

## Fix shipped (#121, 2026-07-14) — direction (a): the fit models the flatten

`simulate(..., session_ends=)` — optional bool mask marking each session's last
bar; where True, an open position is force-closed at that close. Bar order
(mark → stop/target → entry → flatten) mirrors `IntradayTrendStrategy.on_bar()`
then `eod_close()`. `session_ends_from_timestamps()` builds the mask. Threaded
through `fit_kalman_trend` / `fit_ma_crossover` / `fit_kalman_reduced` /
`evaluate`, the runner's warmup fit (from the Kite candle timestamps it was
already fetching and discarding), and `backtest_kalman_trend` (intraday `--csv`
only, reusing the existing `_is_5min` detector).

**`session_ends=None` is the daily default — the daily gates are byte-identical
to before** (one bar == one day; holding across bars IS the daily strategy).

**Parity gate (`tests/test_optimize_kalman_trend.py`):** `simulate(session_ends)`
must reproduce the live `IntradayTrendStrategy` on real tape. It does, exactly:
**996.1 pts / ₹74,708 / 119 trades** — the same figure the independent class
replay produced. This is the guard that stops fit and deploy diverging again.

### Re-fit evidence (MA arm, runner's 1500-bar warmup window, seed 0, n_gen 25)

| Fit | short/long | offset | stop | target | Full tape **as deployed** |
|---|---|---|---|---|---|
| OLD (no flatten — the bug) | 13/40 | 61.7 | 487.4 | 262.3 | +329.6 pts (+₹24,719), 105 trades |
| NEW (models the flatten) | 14/18 | 41.6 | 255.1 | 755.1 | **+571.9 pts (+₹42,891), 38 trades** |

The corrected fit is better *in the environment it actually runs in* — which is
the whole point of the fix, not a performance claim (one seed, one window).

### Follow-on finding: with the flatten modelled, `target_ticks` is UNIDENTIFIABLE

The new fit still picks a 755-pt target — larger than the old one. That is not a
regression: once the flatten is modelled the target **never binds**, so CMA-ES
has no gradient on it and the parameter drifts freely. It is vestigial for the
intraday config. Per the SKILL.md anti-overfit lesson (fewer free params = less
overfit), the intraday fit should **drop `target_ticks` entirely** (a 3-param MA
fit: short/long/offset + stop). Not done here — out of #121's scope.

**Filed as issue #125 with evidence (2026-07-14).** A 6-seed sweep on the
runner's warmup window fits targets spanning **636 → 1558 pts (spread 922)** and
hits **zero targets on the full tape in every seed**, while the other params do
real work. Why it cannot bind: the within-session max favourable excursion from
entry over 120 sessions peaks at **391 pts** (p99 = 379, p50 = 81) — **no session
ever moved 400 pts in favour from entry**. Every target above ~379 therefore
scores identically: an unbounded flat plateau CMA-ES samples at random. The
`target=755` serialized into `kalman_trend_runner_state.json` is noise being read
as a tuned parameter. Identification is **one-sided** — below ~379 the target
*does* bind (and hurts, by cutting winners), so a fold with an unusually large
move can silently drop the fit into the binding region.

### The fix now DOES reach the live book — no state reset needed (2026-07-14)

Originally this section said the deployed books would keep the OLD mis-fit params
until the operator cleared `kalman_trend_runner_state.json`, and that clearing it
would destroy the forward A/B feeding the 2026-08-28 MA decision. **That
trade-off is resolved: the runner now re-fits a stale book in place, preserving
its history.**

The mechanism mirrors the existing #77 precedent in the same function — that bug
was also a stale *serialized config* (`cost_per_unit=0.0` faithfully preserved by
`restore()`), fixed by re-asserting the correct value on restore:

- `serialize()` writes **`fit_flatten_aware: True`**; `fit_is_stale(blob)` treats
  an absent/False marker as a pre-#121 fit.
- On restore of a stale book the runner re-fits on fresh 5-min history
  (flatten-aware) and calls **`reparam()`**, which swaps the params but
  **preserves trades, realized P&L and bar count** — deleting 12 sessions of data
  would be worse than keeping it.
- `reparam()` **refuses while a position is open** (a mid-session crash): the
  stop/target came from the old fit, so managing a live position to new levels it
  was never entered against is wrong. It logs loudly and defers to the next flat
  restart.
- If a stale book cannot be re-fit (thin history) the instrument is **skipped**
  rather than traded on knowingly mis-fit params (Rule 12).

**The seam is recorded, not hidden.** Because trades are preserved, a re-fit book's
cumulative P&L spans two configs. `refit_at` is written to the state and surfaced
in the EOD sidecar/summary, so the A/B analysis **must segment on it** and count
only post-seam sessions as evidence for the corrected fit. Pooling across the seam
would blend two different strategies — the honest read of the pre-seam sessions is
that they measured a config nobody intended (#121: the exits were a clock, not the
fitted logic).

---

# Trailing stop (#122 variant D) — REJECTED (2026-07-14)

Ran the spec's "do this first" case: trailing stop **+ keep** the 15:25 flatten
(no overnight hold → no gap/margin blockers). Walk-forward 20 folds × 5 seeds, T
fit on TRAIN only, short/long/offset held at the incumbent #121 flatten-aware
fit, both instruments, costs 2.5 and 8.0. Script: `research/experiment_kalman_trail.py`.

> **Numbers below are the CORRECTED re-run (2026-07-14, post-code-review).** The
> first run had three harness defects: trade counts summed across seeds (~5×
> overstated), the plateau pooled across seeds while A/D used median-of-per-seed
> (incomparable bases), and the T-fit silently defaulted to the grid floor when
> nothing fitted. All fixed; the experiment was **re-run from scratch** rather
> than the table patched. **The verdict did not change** — see "what the review
> changed" below.

Sharpe = per-seed pooled OOS, median across seeds (the repo's standard basis).
Trades = **per seed** (the typical single strategy's churn, not N strategies').

| cost | symbol | A (fixed+flatten) | D (trail+flatten) | trades/seed A→D | T median |
|---|---|---|---|---|---|
| 2.5 | NIFTY | 0.16 | 0.69 | 163.8 → 543.8 | 25 |
| 2.5 | BANKNIFTY | 0.42 | 3.00 | 275.4 → 1,490.4 | 25 |
| 8.0 | NIFTY | −0.04 | −0.03 | 92.6 → 120.2 | 200 |
| 8.0 | BANKNIFTY | 0.27 | 1.68 | 194.6 → 1,451.0 | 25 |

**REJECTED on two independent grounds.**

**1. Plateau criterion (pre-registered §3.4.4) fails.** OOS Sharpe decays
monotonically from the grid's smallest T in 3 of 4 cells (NIFTY@2.5:
0.86→0.16; BANKNIFTY@2.5: 3.00→0.27). T pins at the grid FLOOR (median 25). A
boundary spike, not a plateau → reject regardless of the peak.

**The pin is genuine, not a harness artifact.** The corrected run reports
`n_unfittable = 0` on every cell — the old silent floor-default never fired, so
every median-25 came from a real train-Sharpe argmax. (This mattered: the old
code returned T_GRID[0]=25 when nothing fitted, which would have been
indistinguishable from a true boundary pin.)

**2. The numbers were an artifact.** Every trail exit booked at the stop LEVEL,
gifting a mean **35.9 pts on a 25-pt trail** (BANKNIFTY; NIFTY 13.2). Re-scored
with realistic fills the full tape flips **+₹719k → −₹506k** (NIFTY −₹374k →
−₹1.53M). D doesn't just fail to beat A — it loses badly once fills are honest.

**Corroboration:** NIFTY at the realistic 8-pt cost collapses D 0.69 → −0.03, T
jumps off the floor (25→200), trades/seed fall 543.8→120.2 — the hyper-tight-stop
churn pathology `run_paper_kalman_trend.fit_params` already documents. The
low-cost "edge" was churn. Note the plateau at 8 pts *inverts*: T25 is now the
WORST cell (−0.27) and the curve rises toward T200 (0.07) — the tight trail that
"won" at 2.5 is the biggest loser once costs are real.

## What the code review changed (and did not)

A high-effort review of this PR found six defects, three of which touched this
experiment. All fixed, and the experiment **re-run from scratch**:

| defect | effect on the published result |
|---|---|
| trade counts summed across 5 seeds | cosmetic-but-misleading: 819→163.8/seed, 2,719→543.8/seed. **The A→D ratio (~3.3×) is unchanged**, so the churn argument stands. |
| plateau pooled across seeds while A/D used median-of-per-seed (bases not comparable; the repo warns cross-seed pooling inflates Sharpe ~√N) | negligible in practice: NIFTY@2.5 plateau T25 0.84→0.86; BANKNIFTY 2.97→3.00. A/D Sharpes identical. |
| T-fit silently returned the grid floor (25) when no T fitted | **none — it never fired** (`n_unfittable = 0` everywhere). The median-25 pin is genuine optimization. This was the one that could have invalidated ground #1; it did not. |

**Verdict unchanged: REJECTED.** The review improved the evidence's integrity
without moving the answer — which is the outcome you want from a review of a
negative result. Ground #2 (the fill artifact) was measured independently of the
harness and was never in question.

## The methodological lesson (bigger than the experiment)

Booking a stop at its LEVEL is benign only when **stop distance >> bar range**.
The 250-pt fixed stop rarely triggers, so the convention never mattered. A
25-pt trail triggers constantly and the convention **manufactures money**. The
bias scales with (bar range ÷ stop distance) — it is NOT an overnight-only
concern, which is what the #122 spec originally (wrongly) assumed.

**Root cause is a data gap:** `data_cache/*_5minute.parquet` holds
**`datetime, close` only** — the cache discarded the high/low Kite returns.
Without OHLC we cannot know whether/where a stop was touched intrabar; the honest
fill is bounded by [triggering close, stop level] and that band (13–36 pts)
**exceeds the trail being tested**. Booking at the close is not a fix (pessimism
for optimism). **Tight-stop strategies are not evaluable on close-only data.**
Note the live book polls prices intrabar (`check_exit`), so live trailing would
differ from ANY close-only backtest.

Per `feedback_data_resolution_over_backtest`: re-fetch 5-min OHLC rather than
ship a caveated backtest. `research/experiment_kalman_trail.py` is parameterised and cheap
to re-run once the data exists.

## Read
The operator hypothesis — the 15:25 flatten arbitrarily truncates trends a trail
would ride — is **not supported**. The flatten remains the profitable exit
(#121); replacing it with a trail is worse at every tested T on both instruments,
and the apparent wins were a fill artifact. Variants B/C stay blocked (B already
lost ₹322k with realistic fills); the burden of proof on "hold longer" is now
high.


---

# Variant D on HONEST OHLC FILLS — the definitive run (2026-07-14)

The close-only re-run above was the best we could do on a close-only tape. With
the OHLC re-fetch (#122 blocker cleared) and the fits+harness wired to the honest
fill model (fit and eval share it — a #121-class mismatch otherwise), variant D
was re-run from scratch on the **full 10,050-bar / 134-session tape**.

Sharpe = per-seed pooled OOS, median across seeds. Trades = per seed. 22 folds x
5 seeds, T fit on TRAIN only, short/long/offset held at the incumbent.

| cost | symbol | A (fixed+flatten) | D (trail+flatten) | trades/seed A→D | T median |
|---|---|---|---|---|---|
| 2.5 | NIFTY | −0.03 | −0.01 | 96.8 → 121.6 | 200 |
| 2.5 | BANKNIFTY | 0.05 | 0.07 | 141.8 → 467.2 | 250 |
| 8.0 | NIFTY | −0.08 | −0.05 | 86.8 → 108.0 | 250 |
| 8.0 | BANKNIFTY | −0.07 | −0.02 | 105.6 → 277.0 | 300 |

## The close-only result was ENTIRELY a fill artifact

| | close-only | honest OHLC |
|---|---|---|
| NIFTY@2.5 A | +0.16 | **−0.03** |
| NIFTY@2.5 D | **+0.69** | **−0.01** |
| BANKNIFTY@2.5 D | **+3.00** | **+0.07** |
| BANKNIFTY@8.0 D | **+1.68** | **−0.02** |
| T chosen (all cells) | **25 = the grid FLOOR** | **200–300** |
| plateau at T25 | **+0.86 / +2.97 (BEST)** | **−0.88 / −0.50 (WORST)** |

Three independent signatures all invert:
1. **The grid-floor pin is gone.** T now fits at 200–300 across every cell. The
   optimizer only ever wanted T=25 because a 25-pt trail let level-booking gift
   ~36 pts per exit; with honest fills a tight trail is the worst thing you can do.
2. **T25 flips from best to worst** in every cell (BANKNIFTY@8.0: **−2.45**).
   The plateau now slopes UP toward wide trails — the exact opposite shape.
3. **BANKNIFTY's headline 3.00 collapses to 0.07.** That number was ~97% fiction.

## VERDICT — variant D REJECTED (third time, now on clean evidence)

D beats A by 0.02–0.05 Sharpe in every cell — i.e. **nothing**, at 1.3–3.3x the
trade count. Both arms sit at ~0 on honest fills at every cost. There is no edge
in either exit rule; the trail is not better, it is just churnier for the same
nil.

**Every previous kalman_trend number was optimistic.** Even variant A — the wide
250-pt incumbent stop, where level-booking was assumed benign — drops
NIFTY@2.5 **+0.16 → −0.03** and BANKNIFTY@8.0 **+0.27 → −0.07**. Measured
directly on the incumbent config: **−212.4 pts = −₹15,926** over the tape. Read
any pre-#126 kalman_trend figure as biased upward by an unknown amount.

## Read

The operator hypothesis ("the 15:25 flatten arbitrarily truncates trends a trail
would ride") is **not supported** and is now closed on data that can actually
answer it. The flatten remains the profitable exit (#121); the trail adds churn
and no edge at every T on both instruments at both costs.

This is what the whole #121→#126 chain bought: the question was answerable only
after the fit modelled the flatten, the fills stopped being fiction, and the data
carried high/low. The answer is still no — but it is now a real no.


---

# #125 — target_ticks dropped from the intraday fit (2026-07-14)

Re-verified on the HONEST-FILL tape before changing anything (the fill model had
changed underneath the original evidence):

| | close-only (original) | honest OHLC (re-verified) |
|---|---|---|
| fitted target across 6 seeds | 636–1558 | **512–1410** (spread 899) |
| target hits, every seed | 0 | **0** (even with honest bar-high touch detection) |
| max within-session favourable excursion | 391 pts | **440.8** (p100, using bar extremes) |

The **smallest** fitted target (512) still exceeds the **largest** favourable
excursion ever observed (441). Under the 15:25 flatten the target cannot bind:
it is not an estimated parameter, it is a flat plateau CMA-ES samples at random.

## Change

`fit_target: bool = True` on all three fits. When False the target dimension is
dropped from the CMA-ES vector entirely and `target_ticks` returns **None** (no
target — not a magic large constant, so nothing has to guess a threshold).

- **Intraday callers pass False**: the runner's warmup fit, the walk-forward
  harness (when `session_ends` is given), and the trail experiment.
- **The DAILY gates keep the target** (`session_ends=None`): a bar IS a day
  there, holds run for days, and the target genuinely binds. Byte-identical.
- **`simulate(target_ticks=None)`** and **`IntradayTrendStrategy.target_ticks:
  Optional`** both support no-target — the live book MUST match the fit or
  dropping it would be a fresh #121-class fit/deploy mismatch. The parity gate
  still passes.

MA fit: 5 params → **4**. Kalman reduced: 4 → **3**. Kalman full: 8 → **7**.

## Honest note on the verification

The issue predicted "intraday results unchanged (the target never fired)". That
is **not quite right**: the target never *fires* either way, but removing a
dimension changes the SEARCH, so CMA-ES lands on different params (seed 0:
7/98 → 23/93). Train Sharpe is flat-to-slightly-lower (mean 0.665 → 0.640
across 4 seeds) — which is the *expected and desired* direction for an
anti-overfit change: a smaller space cannot chase the train window as hard. No
trade changes for the same params; the param differences are CMA-ES search-path
noise, which the multi-seed aggregation exists to absorb.

## Why it mattered

`target=755` was serialized into `kalman_trend_runner_state.json` and read as a
tuned parameter. It was noise — and it was part of the config feeding the
2026-08-28 MA decision. Per SKILL.md's core lesson (fewer free params = less
overfit; `fit_kalman_reduced` exists precisely to cut 8→4), one of those params
was provably inert intraday.
