# SKILL.md — kalman_trend

Procedure manual for the Kalman-trend loop, read at the start of every session
(paper §II-B). Hard rules and accumulated lessons live here; the per-signal
checker reads its gate thresholds from `## Rules` so the Phase-6 recalibration
audit can tighten them in one place. Seeded 2026-06-28 from
`tasks/kalman-trend-findings.md` and `run_paper_kalman_trend.py`.

## Goal
Run an intraday Kalman-vs-MA trend A/B (forward parity) on NIFTY + BANKNIFTY
front-month futures, paper-only, and use it as the maker–checker testbed for the
loop-engineering orchestrator. The strategy is NOT a proven edge: faithful
backtest replication is NO-GO vs an MA crossover. The point of this loop is to
measure forward fills at zero risk and to exercise independent verification — not
to assert alpha.

## Rules
- Paper-only. No live order path; never call broker.send for this strategy.
- Position size capped per the runner's existing sizing; do not raise it here.
- Reuse the cached Kite session — never fresh-login while a live runner is active
  (Zerodha invalidates the prior token and breaks the live pair runner).
- Honour the shared HALT_ALL / HALT_NEW_ENTRIES kill switches in data_cache/.
- Checker gate thresholds (Phase 2, deterministic — Rule 5):
  - sharpe_min: 1.5
  - max_dd_max: 0.10
  - nw_tstat_min: 2.0
  - oos_months_min: 24
- A candidate that fails ANY gate is killed and the rejection is logged to
  STATE.md. A low rejection rate is a warning sign (verifier looseness), not a
  win (paper §VI-A).
- Risk monitor (Phase 4, isolated process): trips HALT_NEW_ENTRIES when the paper
  book's realized-P&L drawdown-from-peak breaches (NOT the paper's flatten-all —
  this repo has no such primitive and HALT_ALL would trap open positions):
  - kill_switch_drawdown_rupees: 20000

## Lessons
- 2026-07-14 (#125): the intraday fit no longer fits `target_ticks` — under the
  15:25 flatten it CANNOT bind (fitted 512-1410 pts across seeds, ZERO hits, vs a
  max session excursion of 441), so CMA-ES was sampling a flat plateau at random.
  MA 5->4 params, Kalman reduced 4->3. Intraday params now carry
  target_ticks=None; the live book supports it (fit==deploy). DAILY gates keep
  the target — a bar IS a day and it genuinely binds. Watch for this shape: a
  param whose fitted value scatters wildly across seeds while nothing it controls
  ever triggers is unidentifiable, not tuned.
- 2026-07-14 (#122 FINAL, on honest OHLC fills): trailing stop REJECTED on clean
  evidence. D beats A by 0.02-0.05 Sharpe (nothing) at 1.3-3.3x the churn; both
  arms ~0 at every cost. The close-only "win" was ~97% artifact: BANKNIFTY@2.5 D
  3.00 -> 0.07, the grid-floor T pin vanished (T now fits 200-300), and T25
  flipped from BEST to WORST. **Every pre-#126 kalman_trend number is optimistic**
  — even the wide 250-pt incumbent stop drops +0.16 -> -0.03 (-Rs 15,926 on the
  tape). Fills must use OHLC; fit and eval must share the fill model.
- Booking a stop at its LEVEL is only safe when stop distance >> bar range: a
  25-pt trail gifted 35.9 pts/exit on BANKNIFTY (flipping a tape +₹719k -> -₹506k).
  The bias scales with (bar range / stop distance) — NOT with holding overnight.
- Reporting a multi-seed experiment: trades must be PER-SEED and every metric on
  ONE basis (per-seed pooled, median across seeds). A first cut summed trades ~5x
  and pooled the plateau cross-seed while A/D used per-seed-median.
- A grid fit that pins at a boundary is a red flag: check whether the fallback
  (or a fill artifact) is manufacturing the pin before believing it.
- 2026-07-14 (#121): the fit MUST model the runner's 15:25 flatten
  (`simulate(session_ends=)`, built from bar timestamps). It did not, so params
  were fit for multi-day holds and deployed with a daily flatten — the 670-pt
  target fired 0/120 sessions and the fit environment lost ₹322k with the params
  it produced. A parity test now pins `simulate(session_ends)` to the live
  `IntradayTrendStrategy`; keep it green or fit/deploy will silently diverge
  again. Corollary: with the flatten modelled, `target_ticks` never binds and is
  unidentifiable — drop it from the intraday fit rather than let CMA-ES drift it.
- Restored books keep their serialized params: a fit fix does NOT reach the live
  book until the runner state is cleared (which also resets the forward A/B).
- Faithful single-split 8-param CMA-ES fit overfits: train Sharpe 2–5, OOS is
  seed luck (NIFTY median 0.20 vs MA 0.93). Do not promote on a single seed.
- Same scar as buy-on-gap (train 2.71 → test −0.83): in-sample Sharpe
  maximization on a thin window is not edge. Aggregate seeds/folds before judging.
- Reduced 4-param walk-forward fit (the anti-overfit discipline) is the only
  config that showed any Kalman>MA win-rate; treat it as the candidate generator,
  not the single-split protocol.

## Regime tags
- trend: the strategy is built to make money here; this is its design regime.
- chop: trend follower bleeds in range-bound/whipsaw regimes — expect rejections.
- gap/event: gap fills ARE modelled now (OHLC re-fetch + #126: a stop fires when
  the bar's range touched it; fills at the level, or the OPEN on a gap). Trust
  restored — but ONLY for tapes carrying o/h/l; a close-only table fails loud.
