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
import copy
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
from autoresearch_loop import HedgeResearchLoop

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
    parser.add_argument("--metric", type=str, default="sharpe_ratio",
                        choices=["sharpe_ratio", "net_pnl", "calmar_ratio", "sortino_ratio"],
                        help="Primary metric to optimize (default: sharpe_ratio)")
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

    # Override autoresearch config for this run
    loop = HedgeResearchLoop(hedger, config_path="config.ini")
    loop.primary_metric = args.metric
    loop.eval_cycles = args.eval_cycles

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
                    cycle_metrics.append(results["metrics"])
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
                    cycle_metrics.append(results["metrics"])
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
    data_desc = f"historical ({args.data})" if args.data else f"synthetic ({args.days} days)"
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

    # Experiments
    for i in range(1, args.experiments + 1):
        result = loop.run_single_experiment()
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
    print(f"\n  Best parameters:")
    for k, v in sorted(loop.best_params.items()):
        if k in loop.TUNABLE_RANGES:
            print(f"    {k:<30s} = {v}")
    print(f"\n  Results log:    {loop.results_file}")

    # Save best params
    loop._save_best_params()
    print(f"  Best params:    best_params.json")
    print("=" * 60)

    # Validation run on truly unseen data
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
        np.random.seed(42)
        val_data = generate_synthetic_data(days=10, ticks_per_day=12)
        val_label = "synthetic (seed=42, 10 days)"

    print(f"\n  Validation run ({val_label})...")
    val_results = run_backtest(val_data, underlying=args.underlying, tunable_params=loop.best_params)
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
