"""
Kalman Pair Sweep — is there a profitable (α, entry_z, exit_z)?
==============================================================
The Phase-2 backtest showed Kalman is far more stationary than static β and
loses much less, but is still net-negative out-of-sample at book-default
params. This sweep answers the follow-up: does any reasonable config turn a
robust profit, or is the edge simply not there on Indian F&O pairs?

Methodology (anti-overfit):
  • Screen cointegrated pairs ONCE on the train slice; backtest every grid
    point on the SAME holdout slice — selection and filter seed are both
    out-of-sample.
  • Small grid (book-anchored), so we are not p-hacking a 100-cell surface.
  • Model fixed to momentum (the book's and our backtest's strongest tracker).
  • Reuses run_replay from backtest_kalman_pairs — identical fills/costs/logic.

Read the result with suspicion: a single config that edges positive on one
holdout is NOT a green light (Rule 12). Look for a *region* that is positive,
and treat one lucky cell as noise.

Usage:
    python -m research.sweep_kalman_pairs
    python -m research.sweep_kalman_pairs --top 12 --train-fraction 0.5
"""
from __future__ import annotations

import argparse
import logging
import sys
from itertools import product
from typing import List

import pandas as pd


from research.backtest_kalman_pairs import _write_temp_config, run_replay
from research.backtest_pairs import load_lot_sizes
from core.screen_pairs import NIFTY_50, load_front_month_panel, screen_pairs

logger = logging.getLogger(__name__)

# Book-anchored grid. α around the book's 1e-6 (momentum); thresholds around
# the static system's tuned entry_z=2.0 / exit_z=0.75.
ALPHAS = [1e-7, 1e-6, 1e-5]
ENTRY_ZS = [1.5, 2.0, 2.5]
EXIT_ZS = [0.5, 0.75]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--train-fraction", type=float, default=0.5)
    ap.add_argument("--stop-z", dest="stop_z", type=float, default=4.0)
    ap.add_argument("--lookback-days", dest="lookback_days", type=int, default=60)
    ap.add_argument("--max-holding-days", dest="max_holding_days", type=int, default=7)
    ap.add_argument("--max-leg-notional", dest="max_leg_notional", type=float,
                    default=2_000_000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(message)s")

    # Screen + slice ONCE.
    panel = load_front_month_panel(NIFTY_50, min_coverage=0.50)
    lot_sizes = load_lot_sizes(NIFTY_50)
    cut = int(len(panel) * args.train_fraction)
    train_panel, test_panel = panel.iloc[:cut], panel.iloc[cut:]
    print(f"Screening {len(train_panel)} train days (holdout {len(test_panel)})...")
    screened = screen_pairs(train_panel, p_threshold=0.05,
                            min_correlation=0.5).head(args.top)
    if screened.empty:
        print("No cointegrated pairs on train slice.")
        return 0

    # Precompute each pair's train/test arrays once.
    pairs = []
    for _, row in screened.iterrows():
        a, b = row["symbol_a"], row["symbol_b"]
        if a not in lot_sizes or b not in lot_sizes:
            continue
        tr = train_panel[[a, b]].dropna()
        te = test_panel[[a, b]].dropna()
        if len(tr) < 60 or len(te) < 30:
            continue
        pairs.append((a, b, int(lot_sizes[a]), int(lot_sizes[b]),
                      tr[a].values, tr[b].values, te[a].values, te[b].values,
                      [d.date() for d in te.index]))
    print(f"Sweeping momentum Kalman over {len(ALPHAS)}×{len(ENTRY_ZS)}×"
          f"{len(EXIT_ZS)} = {len(ALPHAS)*len(ENTRY_ZS)*len(EXIT_ZS)} cells "
          f"on {len(pairs)} pairs...\n")

    grid: List[dict] = []
    for alpha, ez, xz in product(ALPHAS, ENTRY_ZS, EXIT_ZS):
        cfg = _write_temp_config({
            "entry_z": ez, "exit_z": xz, "stop_z": args.stop_z,
            "lookback_days": args.lookback_days,
            "max_holding_days": args.max_holding_days, "lots_per_leg": 1,
            "max_leg_notional": args.max_leg_notional, "exit_debounce_ticks": 1,
            # Gate OFF: this sweep isolates the effect of entry/exit/alpha, so the
            # always-on strategy default (adf_gate_p=0.05) must NOT confound the
            # grid — every cell would otherwise also have entries gate-suppressed.
            "adf_gate_p": 0,
        })
        rows = []
        for (a, b, la, lb, tra, trb, tea, teb, dates) in pairs:
            r = run_replay(a, b, la, lb, tra, trb, tea, teb, dates,
                           label="momentum", model="momentum", alpha=alpha,
                           config_path=cfg)
            if r:
                rows.append(r)
        if not rows:
            continue
        df = pd.DataFrame(rows)
        wins = df["win_rate_pct"].dropna()
        n_profitable = int((df["net_pnl"] > 0).sum())
        grid.append({
            "alpha": alpha, "entry_z": ez, "exit_z": xz,
            "pairs": len(df), "trips": int(df["n_round_trips"].sum()),
            "net_pnl": df["net_pnl"].sum(),
            "median_pair_pnl": df["net_pnl"].median(),
            "profitable_pairs": f"{n_profitable}/{len(df)}",
            "avg_win_pct": wins.mean() if not wins.empty else float("nan"),
        })

    g = pd.DataFrame(grid).sort_values("net_pnl", ascending=False)
    print(f"{'alpha':>7} {'entry':>6} {'exit':>5} {'pairs':>5} {'trips':>6} "
          f"{'net P&L':>13} {'median/pair':>13} {'profit pairs':>13} {'win%':>6}")
    print("-" * 86)
    for _, r in g.iterrows():
        print(f"{r['alpha']:>7.0e} {r['entry_z']:>6.2f} {r['exit_z']:>5.2f} "
              f"{int(r['pairs']):>5} {int(r['trips']):>6} {r['net_pnl']:>13,.0f} "
              f"{r['median_pair_pnl']:>13,.0f} {r['profitable_pairs']:>13} "
              f"{r['avg_win_pct']:>6.1f}")

    best = g.iloc[0]
    print(f"\nBest cell: α={best['alpha']:.0e} entry={best['entry_z']} "
          f"exit={best['exit_z']} → net ₹{best['net_pnl']:,.0f}, "
          f"{best['profitable_pairs']} pairs profitable.")
    print("Reminder: a single positive cell is noise, not an edge. Look for a "
          "contiguous positive region and a majority of profitable pairs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
