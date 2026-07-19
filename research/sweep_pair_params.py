"""Sweep the three pair-trading risk knobs added 2026-05-15 (commit 0da82b1):

    safety_buffer     — per-trade widening of stop_z past |entry_z|
    max_entry_z       — hard ceiling above which entries are refused as
                        regime breaks
    min_edge_multiplier — cost-hurdle multiple of round-trip cost

For each grid combo, runs backtest_one over the top-N candidates and reports
aggregate net P&L, round-trip count, and per-pair stats. Mirrors the OOS
plumbing of research/backtest_pairs.py (--train-fraction re-screens on train slice).

Usage:
    # In-sample on cached pair_candidates.csv (lookahead bias):
    python -m research.sweep_pair_params --top 5

    # OOS (honest):
    python -m research.sweep_pair_params --top 8 --train-fraction 0.7

    # Override grid:
    python -m research.sweep_pair_params --top 5 \
        --safety-buffers 0.5,0.75,1.0 \
        --max-entry-zs 4.5,5.0,5.5 \
        --min-edge-multipliers 1.0,1.5,2.0
"""
from __future__ import annotations

import argparse
import logging
import sys
from itertools import product
from pathlib import Path
from typing import List, Optional

import pandas as pd


from research.backtest_pairs import (
    CANDIDATES_PATH,
    backtest_one,
    load_lot_sizes,
    load_top_pairs,
)
from core.screen_pairs import NIFTY_50, load_front_month_panel, screen_pairs

logger = logging.getLogger(__name__)


def _parse_floats(arg: Optional[str], default: List[float]) -> List[float]:
    if not arg:
        return default
    return [float(x.strip()) for x in arg.split(",") if x.strip()]


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--candidates", type=str, default=str(CANDIDATES_PATH))
    # Fixed knobs (not swept here — sweep these in a separate script if needed)
    p.add_argument("--entry-z", type=float, default=2.0)
    p.add_argument("--exit-z", type=float, default=0.75)
    p.add_argument("--stop-z", type=float, default=4.0)
    p.add_argument("--lookback", type=int, default=30, dest="lookback_days")
    p.add_argument("--max-hold", type=int, default=7, dest="max_holding_days")
    p.add_argument("--lots-per-leg", type=int, default=1)
    p.add_argument("--max-leg-notional", type=float, default=None)
    # Swept knobs
    p.add_argument("--safety-buffers", type=str, default=None,
                   help="Comma-separated. Default: 0.5,0.75,1.0,1.25")
    p.add_argument("--max-entry-zs", type=str, default=None,
                   help="Comma-separated. Default: 4.5,5.0,5.5,6.0")
    p.add_argument("--min-edge-multipliers", type=str, default=None,
                   help="Comma-separated. Default: 1.0,1.3,1.5,1.8,2.5")
    # OOS mode (mirrors backtest_pairs.main())
    p.add_argument("--train-fraction", type=float, default=None,
                   help="Re-screen on first N fraction of bhavcopy days, "
                        "backtest on remainder. Avoids the lookahead bias "
                        "of using cached pair_candidates.csv.")
    p.add_argument("--universe", type=str, default=None,
                   help="With --train-fraction: newline-separated symbol file. "
                        "Default NIFTY 50.")
    p.add_argument("--output", type=str, default=None,
                   help="Optional CSV path for the full sweep table.")
    p.add_argument("--top-rows", type=int, default=10,
                   help="Top rows to print, sorted by net P&L.")
    args = p.parse_args()

    safety_buffers = _parse_floats(args.safety_buffers, [0.5, 0.75, 1.0, 1.25])
    max_entry_zs = _parse_floats(args.max_entry_zs, [4.5, 5.0, 5.5, 6.0])
    min_edge_multipliers = _parse_floats(args.min_edge_multipliers, [1.0, 1.3, 1.5, 1.8, 2.5])
    n_combos = len(safety_buffers) * len(max_entry_zs) * len(min_edge_multipliers)

    # Resolve pair book + replay/seed panels exactly once.
    if args.train_fraction is not None:
        if not (0.1 < args.train_fraction < 0.95):
            logger.error("--train-fraction must be in (0.1, 0.95)")
            return 1
        if args.universe:
            universe = [s.strip() for s in Path(args.universe).read_text().splitlines() if s.strip()]
        else:
            universe = NIFTY_50
        full_panel = load_front_month_panel(universe, min_coverage=0.50)
        lot_sizes = load_lot_sizes(universe)
        cutoff = int(len(full_panel) * args.train_fraction)
        train_panel = full_panel.iloc[:cutoff]
        test_panel = full_panel.iloc[cutoff:]
        screened = screen_pairs(train_panel, p_threshold=0.05, min_correlation=0.5)
        if screened.empty:
            logger.error("No pairs passed cointegration on train slice")
            return 1
        pairs = screened.head(args.top).reset_index(drop=True)
        replay_panel = test_panel
        seed_panel = train_panel
        mode_label = (
            f"OOS train_fraction={args.train_fraction} "
            f"(train {train_panel.index[0].date()}→{train_panel.index[-1].date()}, "
            f"test {test_panel.index[0].date()}→{test_panel.index[-1].date()})"
        )
    else:
        pairs = load_top_pairs(args.top, Path(args.candidates))
        universe = sorted(set(pairs["symbol_a"]) | set(pairs["symbol_b"]))
        replay_panel = load_front_month_panel(universe, min_coverage=0.50)
        lot_sizes = load_lot_sizes(universe)
        seed_panel = None
        mode_label = "IN-SAMPLE (lookahead bias — use --train-fraction for honest results)"

    print("\nPair-trading risk-knob sweep")
    print(f"  mode: {mode_label}")
    print(f"  top={args.top}, lookback={args.lookback_days}d, max-hold={args.max_holding_days}d, "
          f"entry={args.entry_z} exit={args.exit_z} stop={args.stop_z}, "
          f"lots-per-leg={args.lots_per_leg}")
    print(f"  grid: safety_buffer {safety_buffers} × max_entry_z {max_entry_zs} "
          f"× min_edge_multiplier {min_edge_multipliers} = {n_combos} combos × {len(pairs)} pairs")
    header = (
        f"\n{'buf':>5} {'max_z':>5} {'edge×':>5} {'trips':>6} {'wins':>5} {'win%':>5} "
        f"{'net':>13} {'costs':>11} {'gross':>13} {'maxDD':>13}"
    )
    print(header)
    print("-" * (len(header) - 1))

    rows = []
    combo_i = 0
    for sb, mez, mem in product(safety_buffers, max_entry_zs, min_edge_multipliers):
        combo_i += 1
        agg_trips = agg_wins = 0
        agg_net = agg_costs = agg_gross = agg_dd = 0.0
        per_pair: List[dict] = []
        for _, row in pairs.iterrows():
            r = backtest_one(
                row, replay_panel, lot_sizes,
                entry_z=args.entry_z, exit_z=args.exit_z, stop_z=args.stop_z,
                lookback_days=args.lookback_days,
                max_holding_days=args.max_holding_days,
                lots_per_leg=args.lots_per_leg,
                max_leg_notional=args.max_leg_notional,
                min_edge_multiplier=mem,
                max_entry_z=mez,
                safety_buffer=sb,
                seed_panel=seed_panel,
            )
            if r is None:
                continue
            agg_trips += r["n_round_trips"]
            agg_net += r["total_pnl"]
            agg_costs += r["transaction_costs"]
            agg_gross += r["total_pnl"] + r["transaction_costs"]
            agg_dd = min(agg_dd, r["max_drawdown"])
            # Per-trade P&L from cumulative realized at close → diff
            prev = 0.0
            for t in r["closed_trades"]:
                rp = t.get("realized_pnl", 0.0)
                if rp - prev > 0:
                    agg_wins += 1
                prev = rp
            per_pair.append({"pair": r["pair"], "trips": r["n_round_trips"],
                             "net": r["total_pnl"]})
        win_pct = (agg_wins / agg_trips * 100) if agg_trips else 0.0
        print(
            f"{sb:>5.2f} {mez:>5.2f} {mem:>5.2f} {agg_trips:>6d} {agg_wins:>5d} "
            f"{win_pct:>4.1f}% {agg_net:>13,.0f} {agg_costs:>11,.0f} "
            f"{agg_gross:>13,.0f} {agg_dd:>13,.0f}"
        )
        rows.append({
            "safety_buffer": sb, "max_entry_z": mez, "min_edge_multiplier": mem,
            "n_round_trips": agg_trips, "n_wins": agg_wins, "win_pct": win_pct,
            "net_pnl": agg_net, "transaction_costs": agg_costs,
            "gross_pnl": agg_gross, "agg_max_drawdown": agg_dd,
            "per_pair": per_pair,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNo rows produced — check pair book / panel coverage.")
        return 1

    print(f"\nTop {args.top_rows} by net P&L:")
    print(df.drop(columns=["per_pair"])
            .sort_values("net_pnl", ascending=False)
            .head(args.top_rows)
            .to_string(index=False))

    sub = df[df["n_round_trips"] >= max(5, len(pairs))]
    if len(sub):
        print(f"\nTop {min(args.top_rows, len(sub))} by net P&L (filtered trips>={max(5, len(pairs))}):")
        print(sub.drop(columns=["per_pair"])
                .sort_values("net_pnl", ascending=False)
                .head(args.top_rows)
                .to_string(index=False))

    if args.output:
        df.drop(columns=["per_pair"]).to_csv(args.output, index=False)
        print(f"\nWrote {len(df)} rows to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
