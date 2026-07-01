"""
Run the autoresearch optimization loop for a fixed number of experiments.

Usage:
  # Synthetic data (default):
  python run_autoresearch.py --experiments 50

  # Real historical data from Kite:
  python run_autoresearch.py --data data_cache/NIFTY_20260301_20260330.csv --experiments 100

  # Different metric:
  python run_autoresearch.py --metric net_pnl --data historical.csv
"""
import argparse
import json
import logging
import sys
import os

import numpy as np
import pandas as pd

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(__file__))

from backtest import generate_synthetic_data, MockKite, run_backtest
from strategies import TalebKarpathyStrategy
from autoresearch_loop import HedgeResearchLoop, ZERO_TRADE_PENALTY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)


def _split_data_into_windows(
    data: pd.DataFrame, window_days: int = 5, holdout_days: int = 0,
) -> tuple:
    """
    Split a historical DataFrame into non-overlapping training windows
    and a held-out validation set.

    Training windows are non-overlapping (step = window_days) to prevent
    data leakage between experiments. The last `holdout_days` trading days
    are reserved exclusively for validation and never appear in any
    training window.

    Returns:
        (train_windows, holdout_df)  — holdout_df is None if holdout_days == 0
    """
    data = data.sort_values("timestamp").reset_index(drop=True)
    timestamps = pd.to_datetime(data["timestamp"])
    dates = sorted(timestamps.dt.date.unique())

    # Reserve hold-out
    holdout_df = None
    if holdout_days > 0 and len(dates) > holdout_days + window_days:
        holdout_dates = dates[-holdout_days:]
        train_dates = dates[:-holdout_days]
        holdout_mask = timestamps.dt.date.isin(holdout_dates)
        holdout_df = data[holdout_mask].reset_index(drop=True)
    else:
        train_dates = dates

    if len(train_dates) <= window_days:
        train_mask = timestamps.dt.date.isin(train_dates)
        return [data[train_mask].reset_index(drop=True)], holdout_df

    # Non-overlapping windows (step = window_days)
    windows = []
    for i in range(0, len(train_dates) - window_days + 1, window_days):
        window_dates = train_dates[i : i + window_days]
        mask = timestamps.dt.date.isin(window_dates)
        window = data[mask].reset_index(drop=True)
        if len(window) > 0:
            windows.append(window)

    return windows, holdout_df


def main():
    parser = argparse.ArgumentParser(description="Run autoresearch optimization")
    parser.add_argument("--experiments", type=int, default=50,
                        help="Number of experiments to run (default: 50)")
    parser.add_argument("--metric", type=str, default=None,
                        choices=["sharpe_ratio", "net_pnl", "calmar_ratio",
                                 "sortino_ratio", "gamma_theta_ratio"],
                        help="Primary metric to optimize. Default: the "
                             "[autoresearch] metric from config.ini (net_pnl on "
                             "this host) — a hardcoded sharpe_ratio default here "
                             "used to silently override the config for manual "
                             "runs. gamma_theta_ratio is the Phase 2.4 Taleb-"
                             "framework efficiency metric, DECOUPLED from money "
                             "(2026-06-14) — don't optimize it alone.")
    parser.add_argument("--eval-cycles", type=int, default=3,
                        help="Backtest replays per experiment (default: 3)")
    parser.add_argument("--days", type=int, default=5,
                        help="Days per synthetic replay (default: 5)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility")
    parser.add_argument("--data", type=str, default=None,
                        help="Path to historical data CSV (from fetch_historical_data.py). "
                             "If omitted, uses synthetic data.")
    parser.add_argument("--underlying", type=str, default="NIFTY",
                        help="Underlying symbol (default: NIFTY)")
    parser.add_argument("--window-days", type=int, default=5,
                        help="Window size in days for historical data cross-validation (default: 5)")
    parser.add_argument("--validation-data", type=str, default=None,
                        help="Path to separate validation CSV (overrides hold-out split)")
    parser.add_argument("--out", type=str, default="best_params.json",
                        help="Where to write the winning params (default: "
                             "best_params.json). The weekly regen points "
                             "this at a dated candidate so the canonical "
                             "file is never touched (audit 2.3).")
    args = parser.parse_args()

    if args.seed is not None:
        np.random.seed(args.seed)

    # Load historical data if provided
    historical_data = None
    historical_windows = None
    holdout_data = None
    if args.data:
        logger.info("Loading historical data from %s", args.data)
        historical_data = pd.read_csv(args.data, parse_dates=["timestamp"])
        holdout_days = args.window_days  # Reserve 1 window worth of days for holdout
        historical_windows, holdout_data = _split_data_into_windows(
            historical_data, args.window_days, holdout_days=holdout_days,
        )
        logger.info("Split into %d non-overlapping %d-day training windows + %d-day hold-out",
                    len(historical_windows), args.window_days, holdout_days)
        if holdout_data is not None:
            logger.info("Hold-out: %s — %s (%d rows, never seen during training)",
                        holdout_data["timestamp"].min().date(),
                        holdout_data["timestamp"].max().date(),
                        len(holdout_data))

    # Bootstrap a hedger instance (autoresearch only uses it for param storage)
    dummy_data = generate_synthetic_data(days=1, ticks_per_day=1)
    dummy_kite = MockKite(dummy_data, args.underlying)
    hedger = TalebKarpathyStrategy(dummy_kite, config_path="config.ini", mode="paper")

    # Override autoresearch config for this run. --metric omitted keeps
    # the loop's config-derived metric ([autoresearch] metric).
    loop = HedgeResearchLoop(hedger, config_path="config.ini")
    if args.metric is not None:
        loop.primary_metric = args.metric
    args.metric = loop.primary_metric
    loop.eval_cycles = args.eval_cycles

    # Pre-screen training windows. A window where the seed params produce 0
    # trades gives the optimizer no gradient — every mutation will tie at the
    # zero-trade penalty floor, so the random walk can't escape. Drop those
    # windows up front; if none survive, fail loudly (the alternative is the
    # silent flat-fitness sweep that hid this bug for a week).
    if historical_windows:
        seed_params = hedger.tunable_params
        kept_windows, dropped_idx = [], []
        for i, w in enumerate(historical_windows):
            try:
                n_trades = run_backtest(
                    w, underlying=args.underlying, tunable_params=seed_params,
                )["metrics"].get("total_trades", 0)
            except Exception as e:
                logger.warning("Pre-screen window %d failed: %s — dropping", i, e)
                n_trades = 0
            if n_trades > 0:
                kept_windows.append(w)
            else:
                dropped_idx.append(i)
        if not kept_windows:
            raise RuntimeError(
                f"All {len(historical_windows)} training windows produced 0 trades "
                f"with seed params. The data likely contains no regime where the "
                f"entry gates trigger. Suggested fixes: widen --days (longer history "
                f"more likely to span a tradable regime), loosen min_rv_iv_ratio in "
                f"best_params.json, or inspect the data CSV for regime."
            )
        logger.info(
            "Pre-screen: kept %d/%d training windows (dropped indices %s for zero-trade)",
            len(kept_windows), len(historical_windows), dropped_idx,
        )
        historical_windows = kept_windows

        # Holdout: warn but never fail — validation is informational, not gating.
        if holdout_data is not None and len(holdout_data) > 0:
            try:
                holdout_trades = run_backtest(
                    holdout_data, underlying=args.underlying, tunable_params=seed_params,
                )["metrics"].get("total_trades", 0)
                if holdout_trades == 0:
                    logger.warning(
                        "Pre-screen: holdout produced 0 trades with seed params — "
                        "validation report at end will be uninformative."
                    )
            except Exception as e:
                logger.warning("Pre-screen holdout backtest failed: %s — proceeding", e)

    # When --data is not provided AND captured tape sessions exist,
    # use autoresearch_loop._run_experiment as-is — it prefers
    # captured tape over synthetic (Phase 2.3) and applies the
    # variance penalty (Phase 2.4) on cycle-averaged metrics. The
    # monkey-patch below was written before those landed and only
    # handles historical CSV / synthetic GBM; skipping it lets the
    # captured-tape path run.
    from backtest import list_captured_sessions
    if args.data is None and list_captured_sessions(args.underlying):
        logger.info(
            "No --data flag; using captured-tape replay path "
            "(autoresearch_loop._run_experiment Phase 2.3)."
        )
    else:
        # Patch _run_experiment to use historical or synthetic data
        def patched_run(params):
            import copy as cp
            original_params = cp.deepcopy(loop.hedger.tunable_params)
            loop.hedger.tunable_params = cp.deepcopy(params)
            cycle_metrics = []

            if historical_windows:
                # Historical mode: sample random windows for each cycle
                for cycle in range(loop.eval_cycles):
                    try:
                        window = historical_windows[
                            np.random.randint(0, len(historical_windows))
                        ]
                        results = run_backtest(
                            window, underlying=args.underlying, tunable_params=params
                        )
                        m = results["metrics"]
                        if m.get("total_trades", 0) == 0:
                            logger.debug("  Cycle %d: 0 trades — penalty %.0f",
                                         cycle + 1, ZERO_TRADE_PENALTY)
                            m = {**m, loop.primary_metric: ZERO_TRADE_PENALTY}
                        cycle_metrics.append(m)
                    except Exception as e:
                        logger.warning("Cycle %d failed: %s", cycle + 1, e)
                        return -999999.0
            else:
                # Synthetic mode: generate fresh data per cycle
                for cycle in range(loop.eval_cycles):
                    try:
                        data = generate_synthetic_data(
                            underlying=args.underlying, days=args.days, ticks_per_day=12
                        )
                        results = run_backtest(
                            data, underlying=args.underlying, tunable_params=params
                        )
                        m = results["metrics"]
                        if m.get("total_trades", 0) == 0:
                            logger.debug("  Cycle %d: 0 trades — penalty %.0f",
                                         cycle + 1, ZERO_TRADE_PENALTY)
                            m = {**m, loop.primary_metric: ZERO_TRADE_PENALTY}
                        cycle_metrics.append(m)
                    except Exception as e:
                        logger.warning("Cycle %d failed: %s", cycle + 1, e)
                        return -999999.0

            loop.hedger.tunable_params = original_params
            if not cycle_metrics:
                return -999999.0
            values = [m.get(loop.primary_metric, 0) for m in cycle_metrics]
            avg = np.mean(values)
            total_capital = loop.hedger.immutable_params.get("total_capital", 500000)
            max_dd = max(m.get("max_drawdown", 0) for m in cycle_metrics)
            max_dd_pct = (max_dd / total_capital) * 100 if total_capital > 0 else 0
            if max_dd_pct > loop.max_dd_threshold:
                logger.info("  DD %.2f%% exceeds threshold — penalizing", max_dd_pct)
                avg = -999999.0
            return avg
        loop._run_experiment = patched_run

    # ── Run ──
    # Describe the data the eval will ACTUALLY use. When --data is given the
    # loop replays that CSV; otherwise _run_experiment prefers captured tape
    # (the most recent eval_cycles sessions) and only falls back to synthetic
    # GBM when no tape exists. Mirror that selection here so the headline log
    # and the COMPLETE summary don't mislabel a tape run as "synthetic".
    if args.data:
        data_desc = f"historical ({args.data})"
    else:
        from backtest import list_captured_sessions
        captured = list_captured_sessions(args.underlying)
        replay = captured[-args.eval_cycles:] if captured else []
        if replay:
            data_desc = (f"captured tape ({len(replay)} sessions: "
                         f"{replay[0]}..{replay[-1]})")
        else:
            data_desc = f"synthetic ({args.days} days, no captured tape)"
    logger.info("=" * 60)
    logger.info("AUTORESEARCH: %d experiments, metric=%s, %d cycles, data=%s",
                args.experiments, args.metric, args.eval_cycles, data_desc)
    logger.info("Starting params: %s", json.dumps(hedger.tunable_params, indent=2))
    logger.info("=" * 60)

    # Baseline
    logger.info("[0/%d] Baseline...", args.experiments)
    loop.baseline_metric = loop._run_experiment(loop.baseline_params)
    loop.best_metric_value = loop.baseline_metric
    loop._log_experiment(0, "BASELINE", 0, 0, loop.baseline_metric, True, loop.baseline_params)
    logger.info("[0/%d] Baseline %s = %.6f", args.experiments, args.metric, loop.baseline_metric)
    # loop.baseline_metric drifts upward as mutations are accepted; keep
    # the seed's score for the sweep-quality verdict below.
    seed_baseline = loop.baseline_metric

    # Experiments
    experiment_results = []
    for i in range(1, args.experiments + 1):
        result = loop.run_single_experiment()
        experiment_results.append(result)
        status = "ACCEPTED" if result["accepted"] else "rejected"
        logger.info("[%d/%d] %s %s=%.6f (mutated %s: %.4f -> %.4f) best=%.6f",
                    i, args.experiments, status,
                    args.metric, result["metric_value"],
                    result["param_mutated"], result["old_value"], result["new_value"],
                    result["best_so_far"])

    # ── Report ──
    print("\n" + "=" * 60)
    print("AUTORESEARCH COMPLETE")
    print("=" * 60)
    print(f"  Experiments:    {args.experiments}")
    print(f"  Metric:         {args.metric}")
    print(f"  Data:           {data_desc}")
    print(f"  Baseline:       {loop.baseline_metric:.6f}" if loop.baseline_metric != -999999.0
          else f"  Baseline:       {loop.baseline_metric}")
    print(f"  Best:           {loop.best_metric_value:.6f}")
    print("\n  Best parameters:")
    for k, v in sorted(loop.best_params.items()):
        if k in loop.TUNABLE_RANGES:
            print(f"    {k:<30s} = {v}")
    print(f"\n  Results log:    {loop.results_file}")

    # ── Sweep quality (Rule 12: an uninformative sweep must say so) ──
    # The 2026-06-20 run accepted 0/40 mutations and 06-27 scored 29/40
    # experiments at one identical fitness — yet both wrote candidate
    # files indistinguishable from a real optimization result. Score the
    # sweep itself and stamp the verdict into the candidate JSON so the
    # manual promotion step can't mistake an echo of the seed params for
    # an optimized winner.
    from collections import Counter
    fitness_counts = Counter(
        round(r["metric_value"], 6) for r in experiment_results
    )
    plateau_value, plateau_n = (
        fitness_counts.most_common(1)[0] if fitness_counts else (0.0, 0)
    )
    n_accepted = sum(1 for r in experiment_results if r["accepted"])
    plateau_share = plateau_n / len(experiment_results) if experiment_results else 0.0
    warnings = []
    if n_accepted == 0:
        warnings.append("0 mutations accepted — candidate is the seed params")
    if loop.best_metric_value <= seed_baseline:
        warnings.append("best never beat the seed baseline")
    if plateau_share > 0.5:
        warnings.append(
            f"{plateau_share:.0%} of experiments scored an identical fitness "
            f"({plateau_value:.6f}) — landscape flat on this replay window"
        )
    sweep_quality = {
        "experiments": len(experiment_results),
        "accepted": n_accepted,
        "distinct_fitness": len(fitness_counts),
        "plateau_share": round(plateau_share, 3),
        "seed_baseline": seed_baseline,
        "best": loop.best_metric_value,
        "informative": not warnings,
        "warnings": warnings,
    }
    print("\n  Sweep quality:")
    print(f"    accepted:         {n_accepted}/{len(experiment_results)}")
    print(f"    distinct fitness: {len(fitness_counts)}")
    print(f"    plateau share:    {plateau_share:.0%}")
    if warnings:
        print("\n  ⚠️  SWEEP UNINFORMATIVE — do NOT promote this candidate:")
        for w in warnings:
            print(f"      - {w}")
        logger.warning("SWEEP UNINFORMATIVE: %s", "; ".join(warnings))

    # Save best params
    loop._save_best_params(out_file=args.out, sweep_quality=sweep_quality)
    print(f"  Best params:    {args.out}")
    print("=" * 60)

    # Validation run on truly unseen data. seed_iv/skew default to None (IV
    # history wiped) and are only set for the captured-tape branch, which must
    # prime the rolling windows the same way the fitness eval does — otherwise
    # _compute_iv_percentile sees <30 obs, returns the neutral 50.0, and the
    # validation is as degenerate as the metric it's checking.
    val_seed_iv, val_seed_skew = None, None
    if args.validation_data:
        val_data = pd.read_csv(args.validation_data, parse_dates=["timestamp"])
        val_label = (f"separate validation set ({args.validation_data}, "
                     f"{val_data['timestamp'].min().date()} — "
                     f"{val_data['timestamp'].max().date()}, "
                     f"never seen during training)")
    elif holdout_data is not None and len(holdout_data) > 0:
        val_data = holdout_data
        val_label = (f"hold-out ({val_data['timestamp'].min().date()} — "
                     f"{val_data['timestamp'].max().date()}, "
                     f"never seen during training)")
    elif historical_data is not None:
        # Fallback: not enough data for holdout, use full dataset (warn user)
        val_data = historical_data
        val_label = "full dataset (WARNING: no true hold-out, insufficient data)"
    else:
        # No CSV: prefer a real captured-tape session over synthetic GBM. The
        # fitness eval trains on the last `eval_cycles` sessions, so the most
        # recent session OUTSIDE that window is a genuine hold-out. Falls back
        # to synthetic only when there isn't enough tape for one.
        from backtest import list_captured_sessions, load_captured_tape, load_iv_skew_seed
        captured = list_captured_sessions(args.underlying)
        if len(captured) > loop.eval_cycles:
            val_date = captured[-(loop.eval_cycles + 1)]
            val_data = load_captured_tape(val_date, args.underlying)
            # Reuse the seed the fitness eval built (same drop_recent); compute
            # it if the run never took the tape path. NOTE (Rule 12): the seed
            # is NOT timestamp-filtered against val_date, so it can include
            # post-val_date IV — adequate for a relative sanity check, not a
            # look-ahead-clean absolute claim. Same caveat as load_iv_skew_seed.
            val_seed_iv = getattr(loop, "_iv_seed", None)
            val_seed_skew = getattr(loop, "_skew_seed", None)
            if val_seed_iv is None:
                drop = loop.config.getint("autoresearch", "iv_seed_drop_recent", fallback=0)
                val_seed_iv, val_seed_skew = load_iv_skew_seed(args.underlying, drop_recent=drop)
            val_label = (f"captured-tape hold-out ({val_date}, not in the "
                         f"{loop.eval_cycles}-session fitness window)")
        else:
            np.random.seed(42)
            val_data = generate_synthetic_data(days=10, ticks_per_day=12)
            val_label = (f"synthetic (seed=42, 10 days) — only {len(captured)} "
                         f"tape session(s), need >{loop.eval_cycles} for a hold-out")

    print(f"\n  Validation run ({val_label})...")
    val_results = run_backtest(
        val_data, underlying=args.underlying, tunable_params=loop.best_params,
        seed_iv_history=val_seed_iv, seed_skew_history=val_seed_skew,
    )
    vm = val_results["metrics"]
    print(f"    Net P/L:      {vm['net_pnl']:>12,.2f}")
    print(f"    Sharpe:       {vm['sharpe_ratio']:>12.4f}")
    print(f"    Calmar:       {vm['calmar_ratio']:>12.4f}")
    print(f"    Sortino:      {vm['sortino_ratio']:>12.4f}")
    print(f"    Max DD:       {vm['max_drawdown']:>12,.2f}")
    print(f"    Trades:       {vm['total_trades']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
