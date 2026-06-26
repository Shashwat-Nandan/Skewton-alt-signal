# Kalman Pairs — Phase 2 Backtest Findings (2026-06-24)

**Verdict: Conditional GO for a forward paper test.** Kalman tracking robustly
beats static β (more stationary spreads, better P&L) in *both* a cross-regime
2-year holdout and an in-regime test on the live system's actual pairs/month.
Absolute profitability is regime-dependent, and ~30 daily bars of in-regime data
is too thin to bank on — so the right next step is a live side-by-side paper
test (Phase 3), not a live-money cutover.

Two tests, deliberately measuring different things:

## Test A — Real data: the live `persistent` pairs over the last month
The decisive in-regime test. Take the exact pairs the live paper runner carried
2026-05-13 → 06-24 and replay static vs Kalman on that same window (filter
seeded on pre-window bhavcopy). Daily resolution.

| | net P&L |
|---|---|
| Actual (intraday, static β — what really happened) | +₹136,504 |
| Daily static (my harness, same pairs/window) | +₹157,960 |
| **Daily momentum-Kalman** | **+₹225,137** |

- **Kalman would have beaten static by ~43%**, and both are profitable. In the
  current regime, swapping static β → Kalman γ improves the book.
- Kalman accepts 13 of 18 pairs and **refuses 5 as non-cointegrated in log-space**
  (seed log-elasticity γ ≈ −0.04…−0.002, i.e. no proportional relationship over
  500 days). The live system trades those on raw β (e.g. BAJAJFINSV/BPCL raw
  β=3.56 but log-γ≈−0.07) and made ~₹46k on them this month — scale/regime luck
  on non-hedge pairs, not a spread edge. Kalman declining them is arguably
  correct.
- Caveat: this is the same favorable recent regime + curated pairs that make the
  *live book itself* look profitable; it is optimistic by selection.

## Test B — Cross-regime: 2-year out-of-sample (screen-train / test-holdout)
The pessimistic generalization test. NIFTY 50 STF, ~530 daily bars
(2024-05 → 2026-06), pairs screened on the train slice, P&L booked only on the
holdout. Three trackers through the *identical* code path (same sizing, costs,
z-band, cost-hurdle): `static` (Kalman α≈0 = frozen β), `basic` (α=1e-5),
`momentum` (α=1e-6).

| config | trips | net P&L | win% | med spread-var | med half-life |
|---|---|---|---|---|---|
| static   | 84 | −₹1.05M | 36.9 | 0.00602 | 24.8d |
| basic    | 84 | −₹0.40M | 46.7 | 0.00210 | 6.7d |
| momentum | 79 | −₹0.32M | 48.8 | 0.00095 | 2.8d |

- Out-of-sample, **everything loses** — but **Kalman loses ~70% less than
  static**, lifts win-rate 37%→49%, and is **more stationary on 8/8 pairs**
  (Fig 15.21/15.22 reproduced). Relative result transfers; absolute does not.
- **It is not cost-bleed.** Pushing the cost-hurdle `min_edge_multiplier`
  1.5→100 only throttles activity (79→1 trips) and shrinks loss toward zero —
  never to profit; even the single highest-edge entry loses. The losses are
  **adverse non-reversion**: screened spreads break down out-of-sample. A more-
  stationary spread can't help when the spread stops reverting (the book warns,
  p.437, that shrinking spread variance erases profit after costs).

## Reconciliation (why A and B disagree — and it's not a bug)
- Test A is **in-regime on recently-curated pairs** → favorable (this is the
  same reason the live book looks profitable).
- Test B is **cross-regime on train-screened pairs** → relationships break →
  losses.
- Kalman tracking **beats static in both**; absolute profitability is
  regime/selection-dependent. The live baseline's apparent edge is concentration-
  and regime-driven (in the 2-year run the trusted `backtest_pairs.py` is +₹950k
  but *entirely* one degenerate negative-β pair, LT/BAJFINANCE, +₹5.89M with a
  −₹6.2M drawdown; strip it and it's ≈ −₹4.9M).

## Recommendation
1. **Phase 3 = forward paper test (GO).** Run the Kalman runner in paper
   *alongside* the live static system; compare on identical forward data over
   the coming weeks. This is the only way to settle absolute profitability
   without regime/selection bias (your own rule: prefer forward capture over an
   insufficient-data backtest — ~30 daily bars is too thin to conclude).
2. **No live-money cutover** until Kalman shows a forward edge over static on a
   majority of pairs across enough sessions.
3. **Phase 0/1 stand on their own** — the filter is correct (gate + 11 tests),
   the strategy is sound (21 tests), and the log-elasticity guard usefully
   refuses non-cointegrated pairs the static system trades on luck.
4. **The real lever is pair selection, not the tracker:** demand persistent
   log-cointegration (stable γ and half-life across train AND a validation
   slice). Kalman then helps on the survivors.

## Reproduce
```
# Test A (real month, live pairs) — see scratchpad/real_month_compare logic
python backtest_kalman_pairs.py --top 10 --train-fraction 0.5            # Test B
python backtest_kalman_pairs.py --top 10 --train-fraction 0.5 --min-edge-multiplier 25
python sweep_kalman_pairs.py     --top 12 --train-fraction 0.5
```
(Each screens ~1,225 pairs on the train slice — ~60s — so run once, not in a loop.)
