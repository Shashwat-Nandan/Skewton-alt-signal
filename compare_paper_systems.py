#!/usr/bin/env python3
"""
Compare paper-trading systems
=============================

Reads the per-day EOD sidecar JSONs that `run_paper_pairs.py` writes
(``data_cache/pair_paper{,_<system>}_eod_<date>.json``) and prints a
side-by-side P&L comparison across the systems you're running.

Two systems are wired in by default — ``baseline`` (the original screener,
single-window cointegration) and ``persistent`` (rolling-window admission;
see tasks/todo.md 2026-05-17). Pass ``--systems`` to compare a different
list.

Usage:
    python compare_paper_systems.py                       # last 5 trading days
    python compare_paper_systems.py --days 10
    python compare_paper_systems.py --systems baseline,persistent
    python compare_paper_systems.py --end 2026-05-23 --days 5
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
DATA_CACHE = HERE / "data_cache"


def eod_path(system: str, d: date, cache: Path = DATA_CACHE) -> Path:
    """Match the filename convention in run_paper_pairs.write_eod_sidecar()."""
    if system == "baseline":
        return cache / f"pair_paper_eod_{d.isoformat()}.json"
    return cache / f"pair_paper_{system}_eod_{d.isoformat()}.json"


def load_eod(system: str, d: date, cache: Path = DATA_CACHE) -> Optional[dict]:
    p = eod_path(system, d, cache)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as e:
        print(f"  WARN: failed to read {p}: {e}", file=sys.stderr)
        return None


def pair_label(pair_field) -> str:
    # Schema stores pair as a 2-element list [a, b].
    if isinstance(pair_field, (list, tuple)) and len(pair_field) == 2:
        return f"{pair_field[0]}/{pair_field[1]}"
    return str(pair_field)


def total_pnl(report: dict) -> float:
    """realized_pnl is already net of transaction costs (see
    pair_trading._apply_fill line 595). unrealized at EOD should be 0
    because the runner flattens at 15:25, but we add it anyway in case
    a flatten failed on a quote outage."""
    return float(report.get("realized_pnl", 0.0)) + float(report.get("unrealized_pnl", 0.0))


def collect_days(end: date, n_days: int) -> List[date]:
    """Walk backward from `end` and return the last `n_days` trading dates
    (Mon-Fri). Doesn't consult holidays.csv — if there's no EOD file for a
    holiday, it just shows '—' and moves on, which is the right behaviour."""
    out: List[date] = []
    cur = end
    while len(out) < n_days:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
        # Safety stop in case n_days is wildly large
        if (end - cur).days > n_days * 3:
            break
    return list(reversed(out))


def fmt_inr(x: float) -> str:
    return f"₹{x:>12,.0f}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=5, help="Trading days back from --end (default: 5)")
    p.add_argument("--end", type=str, default=None,
                   help="End date YYYY-MM-DD (default: today)")
    p.add_argument("--systems", type=str, default="baseline,persistent",
                   help="Comma-separated system names (default: baseline,persistent)")
    p.add_argument("--data-cache", type=str, default=str(DATA_CACHE),
                   help=f"Path to data_cache dir (default: {DATA_CACHE})")
    args = p.parse_args()

    cache = Path(args.data_cache)
    end_date = (date.fromisoformat(args.end) if args.end else date.today())
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    if len(systems) < 2:
        print(f"Need at least 2 systems to compare (got: {systems})", file=sys.stderr)
        return 1

    days = collect_days(end_date, args.days)
    print(f"Comparing systems: {', '.join(systems)}")
    print(f"Date range: {days[0]} → {days[-1]} ({len(days)} trading days)")
    print(f"Cache dir: {cache}\n")

    # Per-day per-system totals
    daily: Dict[date, Dict[str, dict]] = {}
    # per-pair aggregate across the whole window
    pair_pnl: Dict[str, Dict[str, float]] = {}

    for d in days:
        daily[d] = {}
        for sys_name in systems:
            payload = load_eod(sys_name, d, cache)
            if payload is None:
                daily[d][sys_name] = None
                continue
            pairs = payload.get("pairs", [])
            total = sum(total_pnl(r) for r in pairs)
            n_trades = sum(int(r.get("n_closed_trades", 0)) for r in pairs)
            costs = sum(float(r.get("transaction_costs", 0.0)) for r in pairs)
            daily[d][sys_name] = {
                "total_pnl": total,
                "n_pairs": len(pairs),
                "n_trades": n_trades,
                "costs": costs,
            }
            for r in pairs:
                lbl = pair_label(r.get("pair"))
                pair_pnl.setdefault(lbl, {}).setdefault(sys_name, 0.0)
                pair_pnl[lbl][sys_name] += total_pnl(r)

    # ── Daily table ──
    print("Daily net P&L by system:")
    header = f"  {'date':<12}" + "".join(f"  {s:>22}" for s in systems)
    print(header)
    print("  " + "-" * (len(header) - 2))
    totals = {s: 0.0 for s in systems}
    n_obs = {s: 0 for s in systems}
    for d in days:
        cells = [f"  {d.isoformat():<12}"]
        for sys_name in systems:
            row = daily[d].get(sys_name)
            if row is None:
                cells.append(f"  {'— (no EOD file)':>22}")
            else:
                cells.append(f"  {fmt_inr(row['total_pnl']):>10}  "
                              f"({row['n_pairs']}p/{row['n_trades']}t)")
                totals[sys_name] += row["total_pnl"]
                n_obs[sys_name] += 1
        print("".join(cells))
    print("  " + "-" * (len(header) - 2))
    cells = [f"  {'TOTAL':<12}"]
    for sys_name in systems:
        cells.append(f"  {fmt_inr(totals[sys_name]):>10}  ({n_obs[sys_name]}d obs)")
    print("".join(cells))
    print()

    # ── Per-pair breakdown ──
    print("Per-pair net P&L by system (sum over the window):")
    header = f"  {'pair':<26}" + "".join(f"  {s:>16}" for s in systems) + "  shared"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for lbl in sorted(pair_pnl.keys(),
                       key=lambda k: -max(pair_pnl[k].values(), default=0.0)):
        cells = [f"  {lbl:<26}"]
        present = [s for s in systems if s in pair_pnl[lbl]]
        for sys_name in systems:
            v = pair_pnl[lbl].get(sys_name)
            cells.append(f"  {fmt_inr(v) if v is not None else '             —':>16}")
        shared_mark = "BOTH" if len(present) == len(systems) else f"only {','.join(present)}"
        cells.append(f"  {shared_mark}")
        print("".join(cells))
    print()

    # ── Summary ──
    print("Summary:")
    for sys_name in systems:
        n_pairs_total = sum(1 for p in pair_pnl if sys_name in pair_pnl[p])
        net = totals[sys_name]
        avg = net / n_obs[sys_name] if n_obs[sys_name] else 0.0
        print(f"  {sys_name:>14}: net {fmt_inr(net)}  avg/day {fmt_inr(avg)}  "
              f"pairs traded: {n_pairs_total}  days: {n_obs[sys_name]}/{len(days)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
