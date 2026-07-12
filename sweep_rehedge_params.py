"""Grid sweep over rehedge mechanics: delta threshold, scalp band, cost hurdle.

Two data modes:
  --data FILE : legacy CSV-bars replay (synthetic or fetched bars).
  --tape N    : replay the last N CAPTURED tape sessions (the same
                list_captured_sessions/load_captured_tape/load_iv_skew_seed
                path the weekly autoresearch sweep uses), aggregating net_pnl
                across sessions per grid point. Added for the efficiency
                review 2026-07-05 §2.2 item 2 — the review's ask is a rehedge
                sweep ON TAPE, not on synthetic bars.

Holds the RV/IV gate at a permissive value (ratio 0.6) since the variance test
showed gating subtracts edge in this regime (legacy CSV mode only; the tape
mode leaves regime dispatch as configured, matching production).
"""

import argparse
import logging

import pandas as pd

from backtest import (
    list_captured_sessions,
    load_captured_tape,
    load_iv_skew_seed,
    run_backtest,
)
from data_cache_io import read_table

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")


def _replay_frames(args) -> list:
    """Return [(label, DataFrame), ...] to replay for every grid point."""
    if args.tape:
        # list_captured_sessions excludes today's in-progress capture by
        # default (see its docstring) — the sweep never races the writer.
        sessions = list_captured_sessions(args.underlying)[-args.tape:]
        if not sessions:
            raise SystemExit(
                f"--tape requested but no captured sessions exist for "
                f"{args.underlying} — nothing to sweep on (fail loud, not "
                "silently fall back to synthetic).")
        print(f"Replaying {len(sessions)} captured sessions: "
              f"{sessions[0]} → {sessions[-1]}")
        return [(s, load_captured_tape(s, args.underlying)) for s in sessions]
    data = read_table(args.data, parse_dates=["timestamp"])
    if data["timestamp"].dt.tz is not None:
        data["timestamp"] = data["timestamp"].dt.tz_localize(None)
    return [(args.data, data)]


def main():
    parser = argparse.ArgumentParser(description="Sweep rehedge mechanics params")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", type=str, help="CSV bars file (legacy mode)")
    src.add_argument("--tape", type=int, metavar="N",
                     help="replay the last N captured tape sessions")
    parser.add_argument("--underlying", type=str, default="NIFTY")
    parser.add_argument("--grid", type=str, default=None,
                        choices=["full", "frontier"],
                        help="'frontier' = 3x3x1 around the current "
                             "best_params; 'full' = the legacy 5x4x3 grid, "
                             "whose 0.10-0.30 thresholds predate the "
                             "current TUNABLE_RANGES (0.5-1.5) scale. "
                             "Default: frontier with --tape, full with "
                             "--data — a bare tape run must sweep the "
                             "PRODUCTION scale, not the legacy one.")
    args = parser.parse_args()
    if args.grid is None:
        args.grid = "frontier" if args.tape else "full"

    frames = _replay_frames(args)
    iv_seed = skew_seed = None
    if args.tape:
        # Same warmup seeding as the autoresearch loop: a tape session fires
        # one entry scan; without seeded IV/skew history the percentile
        # gates never leave warmup and the sweep is uninformative.
        iv_seed, skew_seed = load_iv_skew_seed(args.underlying)
        print(f"IV seed: {len(iv_seed)} ATM-IV + {len(skew_seed)} skew obs")

    if args.grid == "frontier":
        # Bracket the current best_params (2026-07: dt 0.90 / hurdle 2.53 /
        # band 1.64) one step each way inside TUNABLE_RANGES.
        rehedge_thresholds = [0.6, 0.9, 1.2]
        cost_hurdles = [1.5, 2.5, 5.0]
        scalp_bands = [1.64]
    else:
        rehedge_thresholds = [0.10, 0.15, 0.20, 0.25, 0.30]
        cost_hurdles = [1.0, 1.3, 1.6, 2.0]
        scalp_bands = [1.0, 1.5, 2.0]

    print(f"\nGrid sweep ({args.grid}) on "
          f"{'tape' if args.tape else args.data}")
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
                    "rehedge_delta_threshold": rt,
                    "cost_hurdle_factor": ch,
                    "gamma_scalp_band_pct": sb,
                }
                if not args.tape:
                    params["min_rv_iv_ratio"] = 0.6
                    params["rv_window_days"] = 5.0
                # Aggregate across replay frames (sessions in tape mode).
                n = rehedges = 0
                win_n = 0
                net = gross = costs = scalp = 0.0
                sharpes = []
                for _label, frame in frames:
                    results = run_backtest(
                        frame.copy(), underlying=args.underlying,
                        tunable_params=params,
                        seed_iv_history=iv_seed, seed_skew_history=skew_seed,
                    )
                    metrics = results["metrics"]
                    trades_df = results.get("closed_trades")
                    traded = trades_df is not None and not trades_df.empty
                    if traded:
                        n += len(trades_df)
                        win_n += int((trades_df["gross_pnl"] > 0).sum())
                        gross += float(trades_df["gross_pnl"].sum())
                        costs += float(trades_df["costs"].sum())
                        scalp += float(trades_df["gamma_scalp"].sum())
                        # Sharpe only over sessions that TRADED: averaging a
                        # 0.0 in for no-trade sessions conflates "didn't
                        # trade" with "zero-return trade" and penalizes
                        # selective grid points (net_pnl stays the ranking
                        # metric either way).
                        sharpes.append(metrics.get("sharpe_ratio", 0.0))
                    net += metrics.get("net_pnl", 0.0)
                    rehedges += int(metrics.get("rehedge_count", 0))
                win_pct = (win_n / n * 100) if n else 0.0
                sh = sum(sharpes) / len(sharpes) if sharpes else 0.0
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
