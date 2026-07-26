# Phase B — Level-Significance Event Study (kill-shot gate) — 2026-07-25

Issue #180. Phase B of `auction-orderflow-reversal-engine-2026-07-22.md` (§7,
validation steps 2–3). Depends on A2 (level registry, merged #202).

**Verdict: STOP for the profile-only (TPO-level) engine.** A swing that sits at a
registry Market-Profile level reverses **no more** than a swing anywhere else.
Per the plan's §8 kill criterion ("the profile-only version must clear Phase B on
its own merits"), the profile-only reversal engine does not advance to a strategy
build.

## Method — swing-split (`research/level_significance.py`)

The measurement went through a code review that corrected the control; the
history is worth stating because it is the whole point of a kill-shot.

A level is only ever *tested* at a **swing** (price reaches it and turns). So the
study enumerates every real swing pivot in each session (point-in-time) and
**splits** it: is the pivot within `tol` of an active registry level, or not? Both
arms get the identical forward reversal-vs-continuation measurement over N×5-min
bars. Because *both are swings*, the generic "price mean-reverts after any pivot"
effect cancels, and the **level-minus-non-level** difference isolates whether
sitting at a level adds reaction.

Bias guards (this repo has a documented record of manufactured edges):
- **Point-in-time** — a level created on session i is only tested on sessions > i
  (`_active_as_of`, unit-tested; no hindsight levels).
- **The right control.** An earlier draft used a matched *price* control and then
  a *random-time* control. Random-time **flipped the verdict to PROCEED (+8 bp)** —
  but that is an artifact: it compares a fade at a swing to a fade at an arbitrary
  bar, so it credits the generic swing mean-reversion to the level. The swing-split
  is the fair null (both arms are swings) and was adopted after the review.
- **Cluster bootstrap** — the level-minus-non-level difference is bootstrapped
  across whole sessions, not across swings.

Data: `{NIFTY,BANKNIFTY}_5minute.parquet`, 134 sessions (2025-12-26 → 2026-07-14),
5-min OHLC. **No volume**, so this covers the TPO-derived levels
(POC/VAH/VAL, IB, excess/poor H-L, single-print, weekly composite). Volume nodes
(HVN/LVN) are **untested** — see below.

## Result (default: touch ±5 bps, horizon 6 bars = 30 min)

| underlying | level-swing net | non-level-swing net | level−nonlevel edge (95% CI) | range coverage |
|---|---|---|---|---|
| NIFTY | +16.11 bps | +19.52 bps | **−1.62 [−4.31, +1.03]** | 69% |
| BANKNIFTY | +19.59 bps | +23.27 bps | **−3.98 [−9.40, +0.24]** | 68% |

The level-minus-non-level edge is **negative in all 18 cells** (2 underlyings ×
touch ∈ {3,5,8} bps × horizon ∈ {3,6,12} bars); several are significantly negative
(CI upper bound < 0). Level swings never beat non-level swings.

Three corollaries:

1. **Swings mean-revert hard — but it isn't about levels.** Both arms hold ~75–95%
   and net +15–25 bps. That is generic mean-reversion-after-a-pivot, present at any
   swing, level or not. The reversal *effect* is real; it is not a *level* effect.
2. **The registry blankets ~70% of the range.** With that saturation a "level
   touch" is barely distinguishable from generic price presence — a non-selectivity
   signal in its own right (and why a matched-price control can't even be placed).
3. **No source stands out**, and the first-test premium is small/absent.

## Honest scope — what is NOT killed

- **Volume nodes (HVN/LVN) are untested.** The 5-min history has no volume, so the
  rejection-type levels the engine most wanted could not be studied. That is a
  forward-capture question (only ~11 depth-bearing tape sessions as of 2026-07-25).
  Reopen this same swing-split on volume-node levels once tape ≳ 40–60 sessions —
  a cloud reminder is scheduled for 2026-09-26. A0/A1/A2 infra is reused.
- This does not invalidate A0/A1/A2 (merged).

## Decision

- **Do not build Phase D (the strategy) on TPO levels.** The profile-only engine
  failed its gate on the corrected, sound methodology.
- **Reopen only via volume nodes** when enough depth-bearing tape exists.
- Artifacts: `docs/research/phase-b-results/level_significance_{NIFTY,BANKNIFTY}.json`.
  Reproduce: `python -m research.level_significance --underlying NIFTY`.
