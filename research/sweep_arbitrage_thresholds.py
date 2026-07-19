"""Grid sweep over the arbitrage strategy's calendar thresholds.

Mirrors the sweep_*.py pattern in this repo: drives backtest_arbitrage.run_backtest
across a 2-D grid of (calendar_entry_annual, calendar_exit_annual) and prints
net P&L / trade count / max drawdown per cell. Optional date filtering lets
you sweep on a train slice and validate the winner separately on a hold-out.

Usage:
    python -m research.sweep_arbitrage_thresholds
    python -m research.sweep_arbitrage_thresholds --from 2026-03-01 --to 2026-04-17
    python -m research.sweep_arbitrage_thresholds --entries 0.01,0.02,0.03,0.05 \\
                                          --exits   0.0,0.005,0.01
    python -m research.sweep_arbitrage_thresholds --universe RELIANCE,INFY,HDFCBANK \\
                                          --save-tsv arb_sweep.tsv
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import List, Optional

import pandas as pd


from research.backtest_arbitrage import load_stf_panel, run_backtest

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sweep_arbitrage")


def _parse_floats(s: str) -> List[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> int:
    p = argparse.ArgumentParser(description="Grid sweep over arbitrage calendar thresholds")
    p.add_argument("--universe", type=str, default=None,
                   help="Comma-separated underlyings. Default: every STF in archive.")
    p.add_argument("--from", type=str, default=None, dest="date_from")
    p.add_argument("--to", type=str, default=None, dest="date_to")
    p.add_argument("--entries", type=str, default="0.01,0.015,0.02,0.03,0.05",
                   help="Comma-separated calendar_entry_annual values to sweep.")
    p.add_argument("--exits", type=str, default="0.0,0.0025,0.005,0.01",
                   help="Comma-separated calendar_exit_annual values to sweep.")
    p.add_argument("--risk-free-rate", type=float, default=0.07, dest="risk_free_rate")
    p.add_argument("--dividend-yield", type=float, default=0.0, dest="dividend_yield")
    p.add_argument("--max-hold", type=int, default=15, dest="max_hold")
    p.add_argument("--min-dte-near", type=int, default=4, dest="min_dte_near")
    p.add_argument("--max-leg-basis", type=float, default=0.10, dest="max_leg_basis",
                   help="Cleanliness gate (annualized leg basis ceiling). "
                        "Set 9.99 to disable.")
    p.add_argument("--lots-per-leg", type=int, default=1, dest="lots_per_leg")
    p.add_argument("--max-open-calendars", type=int, default=5, dest="max_open_calendars")
    p.add_argument("--max-leg-notional", type=float, default=500_000, dest="max_leg_notional")
    p.add_argument("--save-tsv", type=str, default=None,
                   help="Optional: write the full grid to this TSV file.")
    p.add_argument("--top", type=int, default=5,
                   help="Print the top-K cells by net P&L.")
    args = p.parse_args()

    entries = _parse_floats(args.entries)
    exits = _parse_floats(args.exits)
    if not entries or not exits:
        logger.error("--entries and --exits must be non-empty")
        return 1

    universe: Optional[List[str]] = (
        [s.strip().upper() for s in args.universe.split(",") if s.strip()]
        if args.universe else None
    )

    print("Loading STF panel from bhavcopy archive…")
    panel = load_stf_panel(universe=universe)
    if args.date_from:
        d0 = datetime.strptime(args.date_from, "%Y-%m-%d").date()
        panel = panel[panel["date"] >= d0]
    if args.date_to:
        d1 = datetime.strptime(args.date_to, "%Y-%m-%d").date()
        panel = panel[panel["date"] <= d1]
    if panel.empty:
        logger.error("Panel is empty after date filtering")
        return 1
    print(f"  {len(panel):,} rows over {panel['date'].nunique()} days, "
          f"{panel['symbol'].nunique()} underlyings")
    print(f"  carry: r={args.risk_free_rate} q={args.dividend_yield}, "
          f"max-hold={args.max_hold}d, lots={args.lots_per_leg}, "
          f"cap=₹{args.max_leg_notional:,.0f}")
    print(f"  entries: {entries}")
    print(f"  exits:   {exits}")
    print()

    header = (
        f"{'entry':>7} {'exit':>7} {'trades':>7} {'orders':>7} "
        f"{'basis_n':>8} {'net_pnl':>13} {'gross':>13} {'costs':>11} "
        f"{'max_dd':>13}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for entry in entries:
        for exit_t in exits:
            if exit_t >= entry:
                # Exit threshold ≥ entry threshold makes no sense — every entry
                # would also be a same-bar exit. Skip the cell.
                continue
            res = run_backtest(
                panel,
                risk_free_rate=args.risk_free_rate,
                dividend_yield=args.dividend_yield,
                basis_entry_annual=0.015,         # static; we're not sweeping basis
                basis_min_dte=3,
                calendar_entry_annual=entry,
                calendar_exit_annual=exit_t,
                calendar_max_holding_days=args.max_hold,
                calendar_min_dte_near=args.min_dte_near,
                calendar_max_leg_basis=args.max_leg_basis,
                lots_per_leg=args.lots_per_leg,
                max_open_calendars=args.max_open_calendars,
                max_leg_notional=args.max_leg_notional,
            )
            net = res["total_pnl"]
            gross = net + res["transaction_costs"]
            print(
                f"{entry:>7.4f} {exit_t:>7.4f} {res['n_round_trips']:>7d} "
                f"{res['n_orders']:>7d} {res['n_basis_events']:>8d} "
                f"{net:>13,.0f} {gross:>13,.0f} "
                f"{res['transaction_costs']:>11,.0f} "
                f"{res['max_drawdown']:>13,.0f}"
            )
            rows.append({
                "calendar_entry_annual": entry,
                "calendar_exit_annual": exit_t,
                "n_round_trips": res["n_round_trips"],
                "n_orders": res["n_orders"],
                "n_basis_events": res["n_basis_events"],
                "net_pnl": net,
                "gross_pnl": gross,
                "transaction_costs": res["transaction_costs"],
                "max_drawdown": res["max_drawdown"],
            })

    if not rows:
        print("\nNo cells were evaluable (check that entries > exits).")
        return 1

    df = pd.DataFrame(rows)
    print()
    print(f"Top {args.top} cells by net P&L:")
    print(df.sort_values("net_pnl", ascending=False).head(args.top).to_string(index=False))

    if args.save_tsv:
        df.to_csv(args.save_tsv, sep="\t", index=False)
        print(f"\nWrote {len(df)} rows to {args.save_tsv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
