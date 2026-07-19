"""Focused sweep on entry-side levers that actually move net_pnl on this dataset.

Rehedge sweep showed net_pnl is invariant to rehedge mechanics (rehedge P&L is
an attribution decomposition, not a separable income stream when futures fill at
spot). Net_pnl moves only when entry decisions change.
"""

import argparse
import logging

import pandas as pd

from research.backtest import run_backtest
from core.data_cache_io import read_table


logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, required=True)
    parser.add_argument("--underlying", type=str, default="NIFTY")
    args = parser.parse_args()

    data = read_table(args.data, parse_dates=["timestamp"])
    if data["timestamp"].dt.tz is not None:
        data["timestamp"] = data["timestamp"].dt.tz_localize(None)

    print(f"\nEntry-side sweep on {args.data}")
    header = (
        f"{'iv_min':>7} {'iv_max':>7} {'max_α':>9} {'hold_h':>7} "
        f"{'trades':>7} {'win%':>6} {'net_pnl':>12} {'gross':>12} {'sharpe':>8}"
    )
    print(header)
    print("-" * len(header))

    iv_mins = [0, 5, 10, 20]
    iv_maxs = [85, 95, 100]
    max_alphas = [50_000, 100_000, 200_000, 500_000]
    holds = [12, 22, 36, 72]

    rows = []
    for iv_lo in iv_mins:
        for iv_hi in iv_maxs:
            for ma in max_alphas:
                for hh in holds:
                    params = {
                        "min_rv_iv_ratio": 0.6,
                        "rv_window_days": 5.0,
                        "entry_iv_percentile_min": float(iv_lo),
                        "entry_iv_percentile_max": float(iv_hi),
                        "max_entry_alpha": float(ma),
                        "max_holding_period_hours": float(hh),
                    }
                    results = run_backtest(data, underlying=args.underlying, tunable_params=params)
                    metrics = results["metrics"]
                    trades_df = results.get("closed_trades")
                    if trades_df is None or trades_df.empty:
                        n = win = gross = 0
                    else:
                        n = len(trades_df)
                        win = (trades_df["gross_pnl"] > 0).sum()
                        gross = float(trades_df["gross_pnl"].sum())
                    win_pct = (win / n * 100) if n else 0
                    net = metrics.get("net_pnl", 0.0)
                    sh = metrics.get("sharpe_ratio", 0.0)
                    print(
                        f"{iv_lo:>7d} {iv_hi:>7d} {ma:>9,.0f} {hh:>7d} "
                        f"{n:>7d} {win_pct:>5.1f}% {net:>12,.0f} {gross:>12,.0f} {sh:>8.2f}"
                    )
                    rows.append({
                        "iv_min": iv_lo, "iv_max": iv_hi, "max_alpha": ma,
                        "max_holding_h": hh, "trades": n, "win_pct": win_pct,
                        "net_pnl": net, "gross": gross, "sharpe": sh,
                    })

    df = pd.DataFrame(rows)
    if not df.empty:
        print("\nTop 8 by net_pnl:")
        print(df.sort_values("net_pnl", ascending=False).head(8).to_string(index=False))
        print("\nTop 5 by sharpe (where trades >= 10):")
        sub = df[df["trades"] >= 10]
        if len(sub):
            print(sub.sort_values("sharpe", ascending=False).head(5).to_string(index=False))


if __name__ == "__main__":
    main()
