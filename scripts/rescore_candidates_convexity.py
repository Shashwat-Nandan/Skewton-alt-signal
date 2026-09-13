#!/usr/bin/env python3
"""Re-score kept candidate_params_*.json under the convexity_edge objective.

Phase-4 regression check of the 2026-07-18 fitness redesign (tasks/todo.md):
the 8 candidates kept by the old net_pnl objective were, per the 8-week
no-convergence analysis, noise fits. Re-scoring them under convexity_edge
answers "would the new objective have kept them?" — the expectation is NO
(negative or vetoed fitness for all).

Dev tool: reads config.ini + captured tape, writes NOTHING (no results.tsv
rows, no best_params/candidate mutation). Run from the repo root:

    .venv/bin/python scripts/rescore_candidates_convexity.py [--eval-cycles 15]

Runtime ~1h for 8 candidates × 15 sessions (tape parsed once, cached).
"""
import argparse
import glob
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-8s %(message)s")
logger = logging.getLogger("rescore")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-cycles", type=int, default=15,
                        help="Replay-window size (default 15 = the weekly "
                             "sweep's window; config.ini's "
                             "eval_cycles_per_experiment is still 5 and is "
                             "NOT the weekly value — the sweep script passes "
                             "--eval-cycles explicitly).")
    parser.add_argument("--underlying", default="NIFTY")
    args = parser.parse_args()

    from runners.autoresearch_loop import HedgeResearchLoop
    from research.backtest import MockKite, generate_synthetic_data, list_captured_sessions
    from strategies import TalebKarpathyStrategy

    if not list_captured_sessions(args.underlying):
        raise SystemExit("No captured tape — convexity_edge needs real sessions.")

    dummy = MockKite(generate_synthetic_data(days=1, ticks_per_day=1),
                     args.underlying)
    hedger = TalebKarpathyStrategy(dummy, config_path="config.ini", mode="paper")
    loop = HedgeResearchLoop(hedger, config_path="config.ini")
    loop.primary_metric = "convexity_edge"
    loop.eval_cycles = args.eval_cycles

    candidates = sorted(glob.glob("candidate_params_*.json"))
    if not candidates:
        raise SystemExit("No candidate_params_*.json at repo root.")

    rows = []
    # Seed first — the comparison baseline, and it warms the shared tape
    # cache for the candidate evals. NOTE (#237 / #238 review): the overlay
    # is no longer unconditional — TalebKarpathyStrategy applies
    # best_params.json only when it carries validation.promote_ok AND
    # validation.promoted_by. So this seed is config.ini alone unless the
    # on-disk file is operator-promoted, and re-score numbers produced
    # BEFORE #237 (which always included the June overlay) are not
    # comparable to these. Report which baseline actually ran.
    overlay_applied = getattr(hedger, "_best_params_applied", 0)
    seed_label = ("SEED (config+best_params)" if overlay_applied
                  else "SEED (config only — no promoted overlay)")
    logger.info("Scoring seed params (%s) over %d sessions...",
                seed_label, loop.eval_cycles)
    seed_fit = loop._run_experiment(hedger.tunable_params)
    rows.append((seed_label, None, seed_fit))

    for path in candidates:
        data = json.load(open(path))
        params = data.get("best_params", {})
        old = (data.get("best_metric") or {}).get("net_pnl")
        label = os.path.basename(path)
        logger.info("Scoring %s ...", label)
        fit = loop._run_experiment(params)
        rows.append((label, old, fit))

    # Issue #159: "beats seed" alone is degenerate when the seed is vetoed
    # (−999999 → any non-vetoed fitness "wins"). Report BOTH columns — the
    # relative comparison and the absolute bar the loop now actually applies
    # under a vetoed seed (_evaluate_experiment / vetoed_baseline_abs_floor)
    # — so the headline can't be an artifact of the seed's status.
    from runners.autoresearch_loop import VETO_FITNESS
    floor = loop.vetoed_baseline_abs_floor
    seed_vetoed = seed_fit <= VETO_FITNESS

    print("\n" + "=" * 78)
    print("CONVEXITY_EDGE RE-SCORE — old-objective candidates "
          f"({loop.eval_cycles}-session window: "
          f"{loop._replay_sessions[0] if loop._replay_sessions else '?'}"
          f"..{loop._replay_sessions[-1] if loop._replay_sessions else '?'})")
    if seed_vetoed:
        print(f"⚠️  SEED VETOED — 'beats seed' is meaningless below; the "
              f"absolute bar (fitness > {floor:g}) is the operative column.")
    print("=" * 78)
    print(f"{'candidate':<36}{'old net_pnl':>12}{'convexity_edge':>16}"
          f"{'  vs seed':<12}{'abs bar':<10}")
    beats_seed = clears_bar = 0
    for label, old, fit in rows:
        is_seed = label == "SEED (config+best_params)"
        vetoed = fit <= VETO_FITNESS
        # Relative column mirrors the non-vetoed-seed acceptance rule: keep
        # iff metric_value > baseline_metric. NOT max(seed_fit, 0): clamping
        # hid negative-but-better-than-seed keeps (2026-07-18 review).
        if is_seed:
            rel = "baseline" + (" (VETOED)" if vetoed else "")
        elif vetoed:
            rel = "VETOED"
        elif fit > seed_fit:
            rel = "beats seed"
            beats_seed += 1
        else:
            rel = "reject"
        bar = "clears" if (not is_seed and not vetoed and fit > floor) else "-"
        if bar == "clears":
            clears_bar += 1
        old_s = f"{old:,.0f}" if isinstance(old, (int, float)) else "n/a"
        print(f"{label:<36}{old_s:>12}{fit:>16,.1f}  {rel:<12}{bar:<10}")
    print("-" * 78)
    n = len(rows) - 1
    print(f"Beats seed: {beats_seed}/{n}"
          + (" (MEANINGLESS — seed vetoed)" if seed_vetoed else "")
          + f" | clears absolute bar (fitness > {floor:g}): {clears_bar}/{n} "
          "(expectation from the 8-week no-convergence analysis: 0)")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
