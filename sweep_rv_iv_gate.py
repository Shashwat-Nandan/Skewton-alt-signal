"""Focused grid sweep over the RV/IV gate's two tunables.

Runs `run_backtest` against a single CSV for every (min_rv_iv_ratio, rv_window_days)
pair and reports net PnL plus attribution components. The MC seeding fix is now
in place, so each cell is reproducible.
"""

import argparse
import logging

import pandas as pd

from backtest import run_backtest
from data_cache_io import read_table


logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Grid sweep over RV/IV gate parameters")
    parser.add_argument("--data", type=str, required=True, help="Path to historical data CSV")
    parser.add_argument("--underlying", type=str, default="NIFTY")
    args = parser.parse_args()

    data = read_table(args.data, parse_dates=["timestamp"])
    if data["timestamp"].dt.tz is not None:
        data["timestamp"] = data["timestamp"].dt.tz_localize(None)

    ratios = [0.6, 0.8, 1.0, 1.2, 1.4]
    windows = [3.0, 5.0, 8.0, 12.0]

    print(f"\nGrid sweep on {args.data}")
    print(f"  ratios:  {ratios}")
    print(f"  windows: {windows}")
    print()
    header = (
        f"{'ratio':>6} {'window':>8} {'trades':>7} {'win%':>6} "
        f"{'net_pnl':>12} {'gross':>12} {'costs':>10} {'scalp':>12} {'residual':>12}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for r in ratios:
        for w in windows:
            params = {"min_rv_iv_ratio": r, "rv_window_days": w}
            results = run_backtest(data, underlying=args.underlying, tunable_params=params)
            metrics = results["metrics"]
            trades_df = results.get("closed_trades")
            if trades_df is None or trades_df.empty:
                n = 0
                win_pct = gross = costs = scalp = residual = 0.0
            else:
                n = len(trades_df)
                wins = int((trades_df["gross_pnl"] > 0).sum())
                win_pct = (wins / n * 100) if n else 0.0
                gross = float(trades_df["gross_pnl"].sum())
                costs = float(trades_df["costs"].sum())
                scalp = float(trades_df["gamma_scalp"].sum())
                residual = float(trades_df["residual"].sum())
            net = metrics.get("net_pnl", 0.0)
            print(
                f"{r:>6.2f} {w:>8.1f} {n:>7d} {win_pct:>5.1f}% "
                f"{net:>12,.0f} {gross:>12,.0f} {costs:>10,.0f} "
                f"{scalp:>12,.0f} {residual:>12,.0f}"
            )
            rows.append({
                "ratio": r, "window": w, "trades": n, "win_pct": win_pct,
                "net_pnl": net, "gross": gross, "costs": costs,
                "scalp": scalp, "residual": residual,
            })

    df = pd.DataFrame(rows)
    if not df.empty:
        best = df.sort_values("net_pnl", ascending=False).head(3)
        print("\nTop 3 by net_pnl:")
        print(best.to_string(index=False))


if __name__ == "__main__":
    main()
