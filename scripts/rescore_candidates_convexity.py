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

    from autoresearch_loop import HedgeResearchLoop
    from backtest import MockKite, generate_synthetic_data, list_captured_sessions
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
    # cache for the candidate evals. NOTE: TalebKarpathyStrategy overlays
    # best_params.json onto config.ini at construction, so this seed is the
    # same config.ini+overlay baseline the weekly sweep starts from.
    logger.info("Scoring seed params (config.ini + best_params overlay) "
                "over %d sessions...", loop.eval_cycles)
    seed_fit = loop._run_experiment(hedger.tunable_params)
    rows.append(("SEED (config+best_params)", None, seed_fit))

    for path in candidates:
        data = json.load(open(path))
        params = data.get("best_params", {})
        old = (data.get("best_metric") or {}).get("net_pnl")
        label = os.path.basename(path)
        logger.info("Scoring %s ...", label)
        fit = loop._run_experiment(params)
        rows.append((label, old, fit))

    print("\n" + "=" * 78)
    print("CONVEXITY_EDGE RE-SCORE — old-objective candidates "
          f"({loop.eval_cycles}-session window: "
          f"{loop._replay_sessions[0] if loop._replay_sessions else '?'}"
          f"..{loop._replay_sessions[-1] if loop._replay_sessions else '?'})")
    print("=" * 78)
    print(f"{'candidate':<36}{'old net_pnl':>14}{'convexity_edge':>16}  verdict")
    kept = 0
    for label, old, fit in rows:
        # "would keep" mirrors the loop's actual acceptance rule
        # (_evaluate_experiment: keep iff metric_value > baseline_metric),
        # where baseline_metric IS the seed's score. Compare against seed_fit
        # directly — NOT max(seed_fit, 0): a candidate that beats a negative
        # seed but is itself negative would still be accepted as the new best,
        # and clamping to 0 hid exactly those from the count (2026-07-18 review).
        verdict = ("VETOED" if fit == -999999.0
                   else "would keep" if fit > seed_fit and label != "SEED (config+best_params)"
                   else "reject")
        if label == "SEED (config+best_params)":
            verdict = "baseline"
        elif verdict == "would keep":
            kept += 1
        old_s = f"{old:,.0f}" if isinstance(old, (int, float)) else "n/a"
        print(f"{label:<36}{old_s:>14}{fit:>16,.1f}  {verdict}")
    print("-" * 78)
    print(f"Candidates the new objective would keep over the seed: {kept}"
          f"/{len(rows) - 1} (expectation from the 8-week no-convergence "
          "analysis: 0)")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
