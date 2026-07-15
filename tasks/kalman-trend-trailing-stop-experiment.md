# Experiment spec — trailing stop vs EOD flatten (kalman_trend MA arm)

**Status:** **CLOSED — variant D REJECTED on honest OHLC fills (2026-07-14).**
All blockers cleared: #121 resolved (PR #124), fills fixed (PR #126), OHLC
re-fetched, fits+harness wired. Re-run on the full 10,050-bar tape: D beats A by
0.02–0.05 Sharpe (i.e. nothing) at 1.3–3.3x the churn; both arms ~0 at every
cost. The close-only 'win' was ~97% fill artifact — see findings §'Variant D on
HONEST OHLC FILLS'. §7 below records the superseded close-only run.

**Origin:** operator question 2026-07-14 ("why flatten EOD — can we trail
instead?"). The exploratory replay (findings §"EOD-flatten vs the fitting
environment") showed trailing-100pt at +₹444k / Sharpe 3.58 / lower drawdown vs
the current +₹74.7k / 0.95. **That is not a result** — it is a 3-value
selection, in-sample, one instrument, one seed, with overnight fills modelled
only close-to-close. This spec exists to test the idea properly, or kill it.

**Prime directive:** this is a *paper-only* strategy whose faithful backtest is
NO-GO vs MA. A promising trailing variant does not change that; it would create
a NEW candidate that must clear the loop's gates on its own evidence. Go in
expecting the edge to evaporate (SKILL.md: "A low rejection rate is a warning
sign, not a win").

---

## 0. Blocking prerequisites (do NOT run the experiment before these)

1. **Resolve #121 (fit/deploy mismatch).** `optimize_kalman_trend.simulate` has
   no EOD flatten; the runner flattens daily. Any trailing fit is meaningless
   until the fit environment and the execution environment agree. The trailing
   variant *removes* the flatten, so the fix direction matters: `simulate` must
   model whatever the runner does.
2. **Level-booked stops.** ~~Intraday that is fine; across an overnight gap it is
   wrong.~~ **CORRECTED 2026-07-14 — this was wrong and it cost us the D run.**
   Booking at the stop LEVEL is benign only when the stop is **wide relative to
   the bar's range** (a 250-pt fixed stop rarely triggers, and the overshoot is
   small next to the distance). For a **tight trail it manufactures money
   intraday too**: at trail=25 on BANKNIFTY 5-min the mean overshoot was
   **35.9 pts — larger than the trail itself** (NIFTY 13.2), flipping the tape
   from −₹506k to +₹719k. The bias scales with (bar range ÷ stop distance), NOT
   with holding overnight. It is the single biggest threat to ANY tight-stop
   variant, D included.
2b. **DATA PREREQUISITE — ✅ DONE 2026-07-14. Both blockers cleared.**
   - *Engine + fetcher* (PR #126): `simulate(highs=, lows=, opens=)` fires a
     stop/target when the bar's range actually TOUCHED the level and fills at the
     level (normal trade-through) or the bar's OPEN (gap). OHLC is all-or-nothing
     and integrity-checked (open/close within [low, high]) — a partial or
     misaligned set raises rather than silently reverting to the biased path.
     `fetch_index_daily.fetch_candles` persists OHLC.
   - *Re-fetch* (operator, 2026-07-14 21:12 IST — markets closed, no runner
     active, cached Kite session REUSED, backups in
     `data_cache/_pre_ohlc_backup/`): **both tapes now carry OHLC and are
     LONGER, not shorter** — 10,050 bars over 134 sessions,
     2025-12-26 → 2026-07-14 (was 9,000 / 2025-12-30 → 2026-06-25). Guards pass
     on the real Kite data; every close-only consumer is unaffected.
   - **First measurement on honest fills:** same params, same tape, NIFTY —
     close-only **1095.2 pts (130 trades)** vs honest OHLC **882.9 pts (132
     trades)**: **−212.4 pts = −₹15,926**. The close-only cache was flattering
     even the INCUMBENT wide-stop config, not just the tight trail.

3. **Margin model.** 78/153 trades in the exploratory run went overnight →
   NRML (~₹1.2–1.5L/lot) not MIS (~₹40–50k). Sharpe is capital-blind; the
   comparison MUST be return-on-margin, not points, or trailing wins on a
   metric that ignores using ~3× the capital.

---

## 1. Hypothesis (pre-registered — state before running)

> **H-TRAIL:** For the MA arm, replacing the 15:25 EOD flatten with a
> ratcheting trailing stop (trail distance T points from peak favourable
> excursion) improves risk-adjusted **return on margin** versus the current
> EOD-flatten config, on both NIFTY and BANKNIFTY, out-of-sample.

Economic story (must hold, or the result is a curve-fit): a 5-min SMA crossover
identifies a trend; the EOD flatten truncates that trend arbitrarily at 15:25
regardless of whether it is still intact. A trailing stop exits on
*trend failure* instead of *clock time*, so it should capture multi-session
trends the flatten cuts short — paying for that with gap risk and overnight
margin.

**Falsifier:** if trailing's edge over EOD-flatten is concentrated in the
overnight gap component (i.e. it is being paid for gap risk, not trend capture),
H-TRAIL is REJECTED even if net P&L is higher — that is a risk premium, not the
claimed mechanism. Decompose and check (§3.4).

## 2. Variants

| id | Exit rule | Notes |
|---|---|---|
| **A** | EOD flatten 15:25 + fixed stop (current) | the control; must reproduce the live book |
| **B** | Hold to fixed stop/target, no flatten | the current *fit* environment (known −₹322k) |
| **C(T)** | Trailing stop T pts, no flatten, no target | the candidate |
| **D(T)** | Trailing stop T pts **+ EOD flatten** | isolates trail-vs-fixed-stop WITHOUT overnight risk |

**D is the cheap win if it works** — it tests the trailing idea while keeping the
intraday risk profile (no gap, no NRML margin, no #121/#0.2 blockers). Run D
first; only escalate to C if D is inconclusive.

## 3. Protocol

### 3.1 Data & harness
- 5-min bars, **both** NIFTY + BANKNIFTY (`*_5minute.parquet`). Per the standing
  rule (`backtest_timeframe.py`), 5-min is the standard — no daily approximation.
- Reuse the existing harness (`backtest_kalman_trend.py` walk-forward folds) —
  do **not** write a new bespoke simulator (Rule 7; the exploratory scratch
  script is explicitly not the reference). Extend `simulate` with the exit modes
  above so fit and test share one code path.
- **Parity gate (mandatory):** variant A must reproduce the live
  `IntradayTrendStrategy` book to ≈₹0 on the same tape before any variant is
  believed. (The exploratory run caught a real re-entry bug this way.)

### 3.2 Fitting discipline
- T is a **fitted parameter**, not a hand-picked one. Fit on TRAIN folds only.
- Reduced-parameter walk-forward (SKILL.md lesson: the single-split 8-param
  CMA-ES fit overfits, train Sharpe 2–5 → OOS seed luck). Keep the free
  parameter count minimal: ideally T only, with short/long/offset held at the
  incumbent values so the comparison isolates the exit rule.
- **≥5 seeds**, aggregate before judging (SKILL.md: "Do not promote on a single
  seed"; the buy-on-gap scar: train 2.71 → test −0.83).
- Report pooled OOS across folds+seeds, not the best fold.

### 3.3 Costs
- 2.5 pts/side minimum (the modelled cost). Also report at **8 pts round-trip**
  (STT on the sell leg alone is ~4.8 pts at current index levels) — an edge that
  dies there is not an edge.
- Overnight variants: add the carry/margin cost of holding.

### 3.4 Required decompositions (the anti-fool checks)
1. **Gap vs trend attribution** — split C's per-trade P&L into the overnight gap
   component (prev close → next open) and the intraday component. If the edge is
   mostly gap, REJECT per the falsifier.
2. **Return on margin** — MIS vs NRML per variant (§0.3), not raw points.
3. **Regime split** — tag folds trend/chop (SKILL.md regime tags). The current
   config makes all its money in 2 of 7 months (Mar +₹107.8k, Apr +₹51.8k; every
   other month negative). Show trailing's behaviour in the chop months
   specifically — that is where the current config bleeds and where the claim
   must earn its keep.
4. **T-sensitivity curve** — the exploratory run flipped sign between 150 and
   250 pts. Plot OOS metric vs T; a sharp cliff = fragile = REJECT even if the
   peak is high. Require a plateau.

## 4. Promotion gates (loop checker, from SKILL.md `## Rules`)

A variant is a candidate only if, on **pooled OOS**:
- `sharpe_min: 1.5`
- `max_dd_max: 0.10` (fraction of deployed margin — see §0.3)
- `nw_tstat_min: 2.0`
- beats variant A on return-on-margin at the 8-pt cost, on **both** instruments
- T-sensitivity plateau (§3.4.4), and gap-attribution passes the falsifier

Failing ANY gate ⇒ killed, rejection logged to `state/kalman_trend/STATE.md`.
Even a PASS only earns a **forward paper A/B slot** (a third book alongside
Kalman/MA), never a live path — the strategy's live decision is governed
separately (KILL_DATE 2026-08-30, see project memory).

## 5. Deliverables
- `simulate` exit-mode extension + tests (parity gate as a test).
- A results section appended to `tasks/kalman-trend-findings.md` (the record),
  including REJECTED variants — negative results are the point of this loop.
- `SKILL.md` lesson entry if the experiment teaches a rule (newest on top).

## 6. Explicit non-goals
- Not re-fitting short/long/offset (that is a fish; isolate the exit rule).
- Not touching the Kalman arm — the A/B's original question (Kalman vs MA) is
  already answered decisively (Kalman −₹21.2k vs MA +₹40.3k forward, 14× the
  churn). This experiment concerns the MA arm's exit rule only.
- Not a live promotion path.

---

## 7. RESULT — variant D, run 2026-07-14 (`experiment_kalman_trail.py`)

Protocol as specified: walk-forward (20 folds × 5 seeds), T fit on TRAIN only
(grid argmax of train Sharpe), short/long/offset held at the incumbent
flatten-aware (#121) fit, both instruments, costs 2.5 and 8.0 pts/side.

**CORRECTED re-run (post-code-review).** Sharpe = per-seed pooled OOS, median
across seeds. Trades = **per seed**. The first run summed trades across seeds and
pooled the plateau on a different basis; both fixed and the experiment re-run
from scratch. Verdict unchanged.

| cost | symbol | A (fixed+flatten) | D (trail+flatten) | trades/seed A→D | T median |
|---|---|---|---|---|---|
| 2.5 | NIFTY | 0.16 | 0.69 | 163.8 → 543.8 | 25 |
| 2.5 | BANKNIFTY | 0.42 | 3.00 | 275.4 → 1,490.4 | 25 |
| 8.0 | NIFTY | −0.04 | −0.03 | 92.6 → 120.2 | 200 |
| 8.0 | BANKNIFTY | 0.27 | 1.68 | 194.6 → 1,451.0 | 25 |

T-sensitivity (median per-seed OOS Sharpe at FIXED T), NIFTY @2.5:
`T25 0.86 · T50 0.50 · T75 0.29 · T100 0.15 · T150 0.23 · T200 0.27 · T250 0.18 · T300 0.16`
BANKNIFTY @2.5: `T25 3.00 · T50 1.76 · T75 1.15 · T100 0.85 · T150 0.57 · … · T300 0.27`
NIFTY @8.0 **inverts**: `T25 -0.27 · … · T200 0.07` — the tight trail that "won"
at 2.5 is the worst cell once costs are real.

`n_unfittable = 0` on every cell: the T=25 pin is genuine argmax, not the old
silent floor-default (which the review removed).

### REJECTED — on two independent grounds

**1. The pre-registered plateau criterion (§3.4.4) fails.** In 3 of 4 cells the
OOS metric **decays monotonically from the grid's smallest T** (NIFTY@2.5
0.86→0.16; BANKNIFTY@2.5 3.00→0.27). That is a
boundary-pinned spike, not a plateau: the fit pins T at the floor (median 25 =
the grid minimum) and the "edge" is whatever the tightest allowed trail churns
out. The spec says: *"a sharp cliff = fragile = REJECT even if the peak is
high. Require a plateau."* → REJECT, no appeal.

**2. The numbers were fiction anyway (§0.2 corrected).** D takes ~3–5× A's trades
(1,490 vs 275 per seed), and every trail exit booked at the stop LEVEL — gifting a mean
**35.9 pts** on a **25-pt** trail (BANKNIFTY). Re-scored with realistic fills the
full tape flips **+₹719k → −₹506k** (NIFTY: −₹374k → −₹1.53M). D does not merely
fail to beat A; **it loses badly** once the fills are honest.

**Corroborating:** NIFTY at the realistic 8-pt cost collapses D 0.69 → −0.03 and
T jumps off the floor (25 → 200) as trades/seed fall 543.8 → 120.2 — the textbook
hyper-tight-stop churn pathology the runner's own docstring documents ("CMA-ES
prefers hyper-tight stops whose churn the live book then pays for"). The low-cost
"edge" was churn.

### What this closes and what it does not

- **The exit rule is not the problem.** The operator hypothesis — that the 15:25
  flatten arbitrarily truncates live trends and a trail would capture them —
  is **not supported**: the flatten remains the profitable exit (#121 findings),
  and replacing it with a trail is worse at every tested T on both instruments.
- **Variants B and C stay blocked**, now behind §0.2b (5-min OHLC) as well as
  the margin model. Since B (hold-to-stop) already lost ₹322k with realistic
  fills, and D loses with honest fills, the burden of proof on "hold longer" is
  now high. Do not re-open without OHLC data and a fresh hypothesis.
- **Re-running D is cheap once OHLC exists** — `experiment_kalman_trail.py` is
  written and parameterised; only the fill model and data need to change.
