"""
Sweep --top to find the optimal book size for the pair-trading rule.

The leg-concentration walk in `classify_pair_candidates` is monotonic in
`top`: pair P admitted at rank K under top=N is admitted at rank K under
any top >= K, and not admitted under top < K. So one expensive walk-
forward at top=MAX gives us every candidate top's admit set via a simple
filter on `processing_rank`. We avoid re-running 15 separate backtests.

Reuses every building block from research/backtest_pairs_rule.py. Output is a
table of {top, net P&L, Sharpe, max DD, win-weeks, round trips, tx costs,
avg P&L per admit, avg trades per week} so the trade-off across book
sizes is visible at a glance.

Usage:
    python -m research.sweep_top
    python -m research.sweep_top --max-top 15 --screen-window 130 --test-horizon 10
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from research.backtest_pairs import backtest_one, load_lot_sizes
from research.backtest_pairs_rule import (
    aggregate_per_pair,
    portfolio_metrics,
    portfolio_weekly_pnl,
    weekly_checkpoints,
)
from core.screen_pairs import classify_pair_candidates
from core.screen_pairs import NIFTY_50, load_front_month_panel, screen_pairs

logger = logging.getLogger("sweep_top")

RAW_DIR = Path("./data_cache/bhavcopy_raw")


def setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose == 0 else (
        logging.INFO if verbose == 1 else logging.DEBUG
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def collect_admissions_at_max_top(args) -> list[dict]:
    """Run the walk-forward once at top=args.max_top. Return per-admission
    dicts with processing_rank attached, so a downstream filter on rank
    reconstructs any smaller top's portfolio."""
    panel = load_front_month_panel(
        universe=NIFTY_50, raw_dir=RAW_DIR, min_coverage=0.80,
    )
    # Optional date-range slice — lets us point one run at a specific
    # validation window without removing files. .loc with timestamps is
    # half-open on neither side; both bounds are inclusive when present.
    if args.panel_start:
        panel = panel.loc[pd.Timestamp(args.panel_start):]
    if args.panel_end:
        panel = panel.loc[:pd.Timestamp(args.panel_end)]
    logger.info("Panel: %d trading days × %d symbols (%s → %s)",
                len(panel), panel.shape[1],
                panel.index[0].date(), panel.index[-1].date())
    lot_sizes = load_lot_sizes(panel.columns.tolist(), raw_dir=RAW_DIR)
    checkpoints = weekly_checkpoints(
        panel, screen_window=args.screen_window,
        test_horizon=args.test_horizon, stride=args.checkpoint_stride,
    )
    logger.info("Planned %d checkpoints; one walk-forward at top=%d feeds the sweep.",
                len(checkpoints), args.max_top)

    per_admission: list[dict] = []
    for ck_idx, ck in enumerate(checkpoints, 1):
        train = panel.loc[:ck].tail(args.screen_window)
        ck_pos = panel.index.get_loc(ck)
        test = panel.iloc[ck_pos + 1: ck_pos + 1 + args.test_horizon]
        if test.empty:
            continue
        screened = screen_pairs(
            train, p_threshold=0.05, min_correlation=0.5,
            min_hedge_ratio=0.1, max_hedge_ratio=10.0,
        )
        if screened.empty:
            continue
        annotated = classify_pair_candidates(
            screened, top=args.max_top,
            exclude_symbols=args.exclude_symbols or None,
            max_hedge_ratio=args.max_hedge_ratio,
        )
        admitted = (
            annotated[annotated["processing_rank"].notna()]
            .sort_values("processing_rank")
        )
        logger.info("  [%d/%d] %s — admitted %d/%d",
                    ck_idx, len(checkpoints), ck.date(),
                    len(admitted), len(screened))
        for _, row in admitted.iterrows():
            res = backtest_one(
                row, replay_panel=test, lot_sizes=lot_sizes,
                entry_z=args.entry_z, exit_z=args.exit_z, stop_z=args.stop_z,
                lookback_days=args.lookback_days,
                max_holding_days=args.max_holding_days,
                lots_per_leg=args.lots_per_leg,
                max_leg_notional=args.max_leg_notional,
                min_edge_multiplier=args.min_edge_multiplier,
                max_entry_z=args.max_entry_z,
                safety_buffer=args.safety_buffer,
                seed_panel=train,
            )
            if res is None:
                continue
            res["_checkpoint"] = ck
            res["_processing_rank"] = int(row["processing_rank"])
            per_admission.append(res)
    logger.info("Collected %d total admissions across all checkpoints.",
                len(per_admission))
    return per_admission


def evaluate_top(per_admission: list[dict], top: int) -> dict:
    """Filter to admits with processing_rank <= top, compute portfolio
    metrics. Returns a single-row dict."""
    subset = [r for r in per_admission if r["_processing_rank"] <= top]
    if not subset:
        return {"top": top}
    weekly = portfolio_weekly_pnl(subset)
    metrics = portfolio_metrics(weekly)
    by_pair = aggregate_per_pair(subset)
    n_admits = len(subset)
    n_active = sum(1 for r in subset if r["n_round_trips"] > 0)
    n_round_trips = int(by_pair["round_trips"].sum())
    tx_costs = float(by_pair["tx_costs"].sum())
    avg_pnl_per_admit = metrics["net_pnl"] / n_admits if n_admits else 0.0
    avg_trades_per_week = (
        n_round_trips / metrics["n_weeks"] if metrics["n_weeks"] else 0.0
    )
    return {
        "top": top,
        "n_admits": n_admits,
        "n_active": n_active,
        "active_pct": n_active / n_admits * 100 if n_admits else 0.0,
        "net_pnl": metrics["net_pnl"],
        "sharpe": metrics["sharpe"],
        "max_dd": metrics["max_dd"],
        "win_weeks_pct": metrics["win_weeks_pct"],
        "n_weeks": metrics["n_weeks"],
        "round_trips": n_round_trips,
        "tx_costs": tx_costs,
        "avg_pnl_per_admit": avg_pnl_per_admit,
        "avg_trades_per_week": avg_trades_per_week,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--max-top", type=int, default=15,
                   help="Largest top value to sweep (default 15).")
    p.add_argument("--screen-window", type=int, default=130)
    p.add_argument("--test-horizon", type=int, default=10)
    p.add_argument("--checkpoint-stride", type=int, default=5)
    p.add_argument("--entry-z", type=float, default=2.0)
    p.add_argument("--exit-z", type=float, default=0.75)
    p.add_argument("--stop-z", type=float, default=4.0)
    p.add_argument("--max-entry-z", type=float, default=5.0)
    p.add_argument("--safety-buffer", type=float, default=0.75)
    p.add_argument("--min-edge-multiplier", type=float, default=1.5)
    p.add_argument("--lookback-days", type=int, default=60)
    p.add_argument("--max-holding-days", type=int, default=7)
    p.add_argument("--lots-per-leg", type=int, default=1)
    p.add_argument("--max-leg-notional", type=float, default=1_000_000)
    p.add_argument("--exclude-symbols", type=lambda s: set(s.split(",")),
                   default=set(),
                   help="Comma-separated symbol blacklist (e.g. ADANIENT,ADANIPORTS). "
                        "Any pair touching one of these symbols is dropped before "
                        "the β/quality/walk passes.")
    p.add_argument("--max-hedge-ratio", type=float, default=None,
                   help="Override the strategy's |β| upper bound (default uses "
                        "HEDGE_RATIO_MAX=10.0). Tightens the tradeable hedge-ratio "
                        "band — e.g. 2.0 to exclude highly asymmetric pairs.")
    p.add_argument("--panel-start", type=str, default=None,
                   help="YYYY-MM-DD lower bound on the bhavcopy panel; trims "
                        "the loaded panel before walk-forward planning. Use to "
                        "evaluate a specific year for OOS validation.")
    p.add_argument("--panel-end", type=str, default=None,
                   help="YYYY-MM-DD upper bound on the bhavcopy panel.")
    p.add_argument("--dump-csv", type=str, default=None,
                   help="Write the sweep results table to this CSV.")
    p.add_argument("-v", "--verbose", action="count", default=0)
    args = p.parse_args()

    setup_logging(args.verbose)
    per_admission = collect_admissions_at_max_top(args)
    if not per_admission:
        print("No admissions collected — nothing to sweep.")
        return 1

    rows = [evaluate_top(per_admission, t) for t in range(1, args.max_top + 1)]
    df = pd.DataFrame(rows)

    bar = "═" * 110
    print(bar)
    print(f"  --top SWEEP   (one walk-forward at top={args.max_top}, filtered to N)")
    print(bar)
    print(f"  {'top':>3s}  {'admits':>6s} {'active':>6s} {'act%':>5s}  "
          f"{'net P&L ₹':>13s}  {'Sharpe':>7s}  {'maxDD ₹':>11s}  "
          f"{'win wk%':>7s}  {'RTs':>4s}  {'tx ₹':>8s}  "
          f"{'P&L/admit ₹':>11s}  {'RT/wk':>5s}")
    print('-' * 110)
    for r in rows:
        sharpe_s = f"{r.get('sharpe', 0):>7.3f}" if r.get('sharpe') is not None else "    n/a"
        print(f"  {r['top']:>3d}  {r['n_admits']:>6d} {r['n_active']:>6d} "
              f"{r['active_pct']:>4.1f}%  "
              f"{r['net_pnl']:>13,.0f}  {sharpe_s}  "
              f"{r['max_dd']:>11,.0f}  {r['win_weeks_pct']:>6.1f}%  "
              f"{r['round_trips']:>4d}  {r['tx_costs']:>8,.0f}  "
              f"{r['avg_pnl_per_admit']:>11,.0f}  "
              f"{r['avg_trades_per_week']:>5.1f}")
    print(bar)

    # Highlight the best by net P&L and by Sharpe.
    best_pnl = df.loc[df["net_pnl"].idxmax()]
    best_sh = df.dropna(subset=["sharpe"]).loc[
        df.dropna(subset=["sharpe"])["sharpe"].idxmax()
    ] if df["sharpe"].notna().any() else None
    print(f"\n  Best net P&L : top={int(best_pnl['top'])}  "
          f"₹{best_pnl['net_pnl']:,.0f}  "
          f"Sharpe {best_pnl['sharpe']:.2f}  "
          f"MaxDD ₹{best_pnl['max_dd']:,.0f}")
    if best_sh is not None:
        print(f"  Best Sharpe  : top={int(best_sh['top'])}  "
              f"₹{best_sh['net_pnl']:,.0f}  "
              f"Sharpe {best_sh['sharpe']:.2f}  "
              f"MaxDD ₹{best_sh['max_dd']:,.0f}")

    if args.dump_csv:
        Path(args.dump_csv).parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.dump_csv, index=False)
        print(f"\n  Sweep table written to {args.dump_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
