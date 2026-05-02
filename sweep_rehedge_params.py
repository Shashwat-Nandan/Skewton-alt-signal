"""Grid sweep over rehedge mechanics: delta threshold, scalp band, cost hurdle.

Holds the RV/IV gate at a permissive value (ratio 0.6) since the variance test
showed gating subtracts edge in this regime.
"""

import argparse
import logging

import pandas as pd

from backtest import run_backtest


logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Sweep rehedge mechanics params")
    parser.add_argument("--data", type=str, required=True)
    parser.add_argument("--underlying", type=str, default="NIFTY")
    args = parser.parse_args()

    data = pd.read_csv(args.data, parse_dates=["timestamp"])
    if data["timestamp"].dt.tz is not None:
        data["timestamp"] = data["timestamp"].dt.tz_localize(None)

    rehedge_thresholds = [0.10, 0.15, 0.20, 0.25, 0.30]
    cost_hurdles = [1.0, 1.3, 1.6, 2.0]
    scalp_bands = [1.0, 1.5, 2.0]

    print(f"\nGrid sweep on {args.data}")
    header = (
        f"{'reh_dt':>7} {'cost_h':>7} {'scalp_b':>8} "
        f"{'trades':>7} {'rehedges':>9} {'win%':>6} "
        f"{'net_pnl':>12} {'gross':>12} {'costs':>10} {'scalp':>14} {'sharpe':>8}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for rt in rehedge_thresholds:
        for ch in cost_hurdles:
            for sb in scalp_bands:
                params = {
                    "min_rv_iv_ratio": 0.6,
                    "rv_window_days": 5.0,
                    "rehedge_delta_threshold": rt,
                    "cost_hurdle_factor": ch,
                    "gamma_scalp_band_pct": sb,
                }
                results = run_backtest(data, underlying=args.underlying, tunable_params=params)
                metrics = results["metrics"]
                trades_df = results.get("closed_trades")
                if trades_df is None or trades_df.empty:
                    n = 0
                    win_pct = gross = costs = scalp = 0.0
                else:
                    n = len(trades_df)
                    wins = int((trades_df["gross_pnl"] > 0).sum())
                    win_pct = (wins / n * 100) if n else 0.0
                    gross = float(trades_df["gross_pnl"].sum())
                    costs = float(trades_df["costs"].sum())
                    scalp = float(trades_df["gamma_scalp"].sum())
                net = metrics.get("net_pnl", 0.0)
                rehedges = int(metrics.get("rehedge_count", 0))
                sh = metrics.get("sharpe_ratio", 0.0)
                print(
                    f"{rt:>7.2f} {ch:>7.2f} {sb:>8.2f} "
                    f"{n:>7d} {rehedges:>9d} {win_pct:>5.1f}% "
                    f"{net:>12,.0f} {gross:>12,.0f} {costs:>10,.0f} "
                    f"{scalp:>14,.0f} {sh:>8.2f}"
                )
                rows.append({
                    "rehedge_dt": rt, "cost_hurdle": ch, "scalp_band": sb,
                    "trades": n, "rehedges": rehedges, "win_pct": win_pct,
                    "net_pnl": net, "gross": gross, "costs": costs,
                    "scalp": scalp, "sharpe": sh,
                })

    df = pd.DataFrame(rows)
    if not df.empty:
        best = df.sort_values("net_pnl", ascending=False).head(5)
        print("\nTop 5 by net_pnl:")
        print(best.to_string(index=False))
        worst = df.sort_values("net_pnl", ascending=True).head(3)
        print("\nBottom 3:")
        print(worst.to_string(index=False))


if __name__ == "__main__":
    main()
