# Fix the inert IV/skew gates

## Diagnosis (empirical, from the 2026-06-07 fitness-eval log)

The 2026-05-31 memory's causes are already fixed:
- IV-pct pinning at 50 → fixed by `load_iv_skew_seed` seeding. Fitness eval shows
  real `iv_pct ∈ [14,22]`, `skew_pct ∈ [61,93]`, `rv_iv ∈ [0.3,4.0]`.
  (The `iv_pct=50/skew=50` log lines were only the synthetic validation — now
  also fixed in PR #29.)
- RV off-by-one → fixed (`_compute_realized_vol`, `n < 4`).

What is STILL inert, under the live config (`enable_regime_dispatch=true`):

1. **Regime classifier thresholds are hardcoded.** taleb_karpathy.py:513 calls
   `classify(features)` with NO `Thresholds` arg, so it always uses the dataclass
   defaults. `Thresholds as RegimeThresholds` is imported (line 36) but unused.
   The 9 cutoffs that actually route structures are NOT in tunable_params /
   TUNABLE_RANGES. → the autoresearch cannot tune the gates that fire.
2. **`skew_pct_max` and `min_rv_iv_ratio` are bypassed under regime dispatch**
   (taleb_karpathy.py:467, 489 guarded by `not regime_enabled`). The sweep
   mutates them but they no-op in the live config.
3. **`entry_iv_percentile_*` range is mis-centered.** Active (line 452) but the
   swept range (min∈[10,50], max∈[50,95]) never brackets the observed 14–22
   regime, so it never flips an entry decision.

## Plan

CORE (always):
- [x] taleb_karpathy.py: add the 9 `regime_*` fields to `tunable_params`.
- [x] taleb_karpathy.py: build `RegimeThresholds(...)`, pass to `classify()`.
- [x] autoresearch_loop.py TUNABLE_RANGES: add the 9 `regime_*` params.
- [x] config_template.ini + config.ini: document/seed the 9 new defaults.

OPTIONAL (decided: include first two, skip experiment bump):
- [x] Re-center `entry_iv_percentile_min/max` → min (5,30), max (20,90).
- [x] Drop dead `skew_pct_max` / `min_rv_iv_ratio` from TUNABLE_RANGES.
- [ ] ~~Bump AUTORESEARCH_EXPERIMENTS~~ (user declined).

## Verify
- [x] Bind test (06-02 tape, candidate params, seeded): mutating
      `regime_backspread_vvol_min` 0.01→0.99 changed total_trades 0→24 — the
      threshold now drives routing (was hardcoded/ignored before). PROVEN.
- [ ] Short autoresearch run: confirm `regime_*` rows in results.tsv move fitness
      (deferred — full sweep is ~3h; bind test already proves the wiring).

## Review
- Root cause was NOT the stale memory's "IV-pct pinned 50" (already fixed by
  seeding) but that `classify()` was called with no `Thresholds`, so the 9
  routing cutoffs were hardcoded and untunable despite the imported-but-unused
  `RegimeThresholds`.
- Fix: surfaced the 9 cutoffs as `regime_*` tunables, built `RegimeThresholds`
  from them at the call site, added them to TUNABLE_RANGES, dropped the 2 dead
  legacy tunables, re-centred `entry_iv_percentile_*` to the observed regime.
- Side finding (out of scope): backspread builder can't find 10Δ OTM call
  strikes on the captured chain → routing to backspread yields 0 trades. Pre-
  existing; worth a follow-up (widen captured strike span or relax the builder).
- Net tunable count: +9 −2 = +7. Experiment count left at 40 (user choice);
  convergence will be slower per-param but the gates are now reachable.
