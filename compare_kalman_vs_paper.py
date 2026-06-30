"""
Kalman vs the live paper book — same pairs, same window
=======================================================
Counterfactual: how would a momentum-Kalman hedge ratio have done on the
*exact* pairs the live pair-paper runner carried, over the *exact* recent
window — vs the static-β it actually ran? This is the in-regime Test A behind
tasks/kalman-pairs-findings.md, and the tool to re-run weekly during the
Phase-3 forward test.

For each pair the live `<system>` book carried since --since:
  • ACTUAL  — the realized P&L the live runner booked (sum of per-pair
    session_realized/unrealized deltas across its EOD reports);
  • daily static  — frozen-β (Kalman α≈0) daily replay on the same window;
  • daily Kalman   — momentum Kalman daily replay on the same window.

Both replays are seeded on bhavcopy closes BEFORE --since and run at daily
resolution, so they will not reproduce the live runner's intraday timing — the
ACTUAL column is context; the static-vs-Kalman columns are the apples-to-apples
comparison (identical replay, only the hedge-ratio tracker differs).

Usage:
    python compare_kalman_vs_paper.py
    python compare_kalman_vs_paper.py --system baseline --since 2026-05-13
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest_kalman_pairs import _write_temp_config, run_replay
from backtest_pairs import load_lot_sizes
from screen_pairs import load_front_month_panel

CACHE = Path("data_cache")


def actual_pnl_by_pair(system: str, since: date) -> dict:
    """Sum per-pair session realized+unrealized deltas across the system's EOD
    reports on/after `since` — the P&L the live runner actually booked."""
    # baseline writes pair_paper_eod_*.json (no system token); every other
    # system is pair_paper_{system}_eod_*.json. Match run_paper_pairs' naming.
    pattern = ("pair_paper_eod_*.json" if system == "baseline"
               else f"pair_paper_{system}_eod_*.json")
    out: dict = {}
    for f in sorted(glob.glob(str(CACHE / pattern))):
        d = f.split("eod_")[-1].replace(".json", "")
        if date.fromisoformat(d) < since:
            continue
        blob = json.load(open(f))
        for p in blob.get("pairs", []):
            key = tuple(p["pair"])
            out[key] = out.get(key, 0.0) + p.get("session_realized_delta", 0.0) \
                + p.get("session_unrealized_delta", 0.0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", default="persistent", choices=["persistent", "baseline"])
    ap.add_argument("--since", default="2026-05-13",
                    help="ISO date; window start (default ~1 month)")
    ap.add_argument("--alpha", type=float, default=1e-6)
    args = ap.parse_args()
    since = date.fromisoformat(args.since)

    actual = actual_pnl_by_pair(args.system, since)
    if not actual:
        print(f"No EOD reports for '{args.system}' since {since}.")
        return 0
    traded = list(actual)
    print(f"Live '{args.system}' book, {since} → today: {len(traded)} pairs")
    print(f"ACTUAL net realized (intraday, static β): ₹{sum(actual.values()):,.0f}\n")

    syms = sorted({s for k in traded for s in k})
    panel = load_front_month_panel(syms, min_coverage=0.30)
    lots = load_lot_sizes(syms)
    cfg = _write_temp_config({
        "entry_z": 2.0, "exit_z": 0.75, "stop_z": 4.0, "lookback_days": 60,
        "max_holding_days": 7, "lots_per_leg": 1, "max_leg_notional": 2_000_000,
        "exit_debounce_ticks": 1, "min_edge_multiplier": 1.5,
        # Gate OFF: this comparison pre-dates the regime gate and reconciles the
        # static-vs-Kalman book on the OLD thresholds; the always-on strategy
        # default (adf_gate_p=0.05) would silently confound it.
        "adf_gate_p": 0,
    })

    print(f"{'pair':<22}{'actual':>12}{'daily static':>13}{'daily Kalman':>13}")
    print("-" * 60)
    tot_a = tot_s = tot_k = 0.0
    n_skip = 0
    for a, b in traded:
        act = actual[(a, b)]
        tot_a += act
        if a not in panel.columns or b not in panel.columns \
                or a not in lots or b not in lots:
            print(f"{a}/{b:<14}{act:>12,.0f}{'no data':>26}"); continue
        pair = panel[[a, b]].dropna()
        ci = pair.index.searchsorted(pd.Timestamp(since))
        tr_a, tr_b = pair[a].values[:ci], pair[b].values[:ci]
        te_a, te_b = pair[a].values[ci:], pair[b].values[ci:]
        dates = [d.date() for d in pair.index[ci:]]
        if len(tr_a) < 60 or len(te_a) < 5:
            print(f"{a}/{b:<14}{act:>12,.0f}{'short':>26}"); continue
        rs = run_replay(a, b, int(lots[a]), int(lots[b]), tr_a, tr_b, te_a, te_b,
                        dates, label="static", model="basic", alpha=1e-12,
                        config_path=cfg)
        rk = run_replay(a, b, int(lots[a]), int(lots[b]), tr_a, tr_b, te_a, te_b,
                        dates, label="momentum", model="momentum",
                        alpha=args.alpha, config_path=cfg)
        if rs is None or rk is None:   # Kalman refused (e.g. non-cointegrated)
            n_skip += 1
            print(f"{a}/{b:<14}{act:>12,.0f}{'(γ rejected)':>26}"); continue
        tot_s += rs["net_pnl"]; tot_k += rk["net_pnl"]
        print(f"{a}/{b:<14}{act:>12,.0f}{rs['net_pnl']:>13,.0f}{rk['net_pnl']:>13,.0f}")
    print("-" * 60)
    print(f"{'TOTAL (replayed)':<22}{tot_a:>12,.0f}{tot_s:>13,.0f}{tot_k:>13,.0f}")
    if n_skip:
        print(f"\n{n_skip} pair(s) rejected by the log-elasticity guard "
              f"(not cointegrated in log-space) — replayed on the rest only.")
    print("\nDaily replay (≠ live intraday timing); static-vs-Kalman is the "
          "apples-to-apples read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
