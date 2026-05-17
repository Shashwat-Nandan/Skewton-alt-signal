#!/usr/bin/env python3
"""
Pair-Trading EOD Verifier
=========================
Runs after market close. Reconciles today's paper-trading session against:
  1. A trailing-N-day backtest on the same pairs (drift signal)
  2. Recent days' signal counts from logs/signals-*.jsonl (volume signal)

Inputs (read-only):
  - data_cache/pair_paper_eod_<today>.json     written by run_paper_pairs.py
  - data_cache/pair_candidates.csv             screener output (for hedge ratios)
  - data_cache/bhavcopy_raw/                   STF closes for backtest replay
  - logs/signals-YYYY-MM-DD.jsonl              signal-mode emissions

Output:
  - logs/pair-verify-YYYY-MM-DD.log            human-readable report
  - logs/pair-verify-YYYY-MM-DD.json           same data, machine-readable

This script never places orders, never mutates strategy state, and never
touches dashboard.db. A failure on one pair is logged but does not abort the
rest of the report.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
CANDIDATES_PATH = DATA_CACHE / "pair_candidates.csv"


def setup_logging(today: date, system: str = "baseline") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if system == "baseline" else f"-{system}"
    logfile = LOG_DIR / f"pair-verify{suffix}-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile),
        ],
        force=True,
    )
    return logging.getLogger("verify_pair_paper")


def load_eod_sidecar(today: date, system: str = "baseline") -> Optional[dict]:
    """Match run_paper_pairs.write_eod_sidecar() filename convention."""
    if system == "baseline":
        filename = f"pair_paper_eod_{today.isoformat()}.json"
    else:
        filename = f"pair_paper_{system}_eod_{today.isoformat()}.json"
    path = DATA_CACHE / filename
    if not path.exists():
        return None
    return json.loads(path.read_text())


def signal_counts(window_days: int, today: date) -> Dict[str, int]:
    """For each of the last N calendar days, count pair_trading rows in
    logs/signals-YYYY-MM-DD.jsonl. Missing files contribute 0 (signals-mode
    didn't run that day, which is fine)."""
    counts: Dict[str, int] = {}
    for offset in range(window_days):
        d = today - timedelta(days=offset)
        path = LOG_DIR / f"signals-{d.isoformat()}.jsonl"
        n = 0
        if path.exists():
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("strategy") == "pair_trading":
                    n += 1
        counts[d.isoformat()] = n
    return counts


def backtest_pair(
    symbol_a: str, symbol_b: str, hedge_ratio: float,
    *, lookback_days: int, entry_z: float, exit_z: float, stop_z: float,
    max_holding_days: int, lots_per_leg: int, max_leg_notional: float,
    history_days: int, log: logging.Logger,
) -> Optional[dict]:
    """Run backtest_pairs.backtest_one over the trailing `history_days` of
    bhavcopy STF closes. Returns the same dict shape backtest_one emits."""
    from backtest_pairs import backtest_one, load_lot_sizes
    from screen_pairs import load_front_month_panel

    universe = sorted({symbol_a, symbol_b})
    try:
        full_panel = load_front_month_panel(universe, min_coverage=0.50)
    except Exception as e:
        log.warning("Panel load failed for %s/%s: %s", symbol_a, symbol_b, e)
        return None

    if full_panel.empty or symbol_a not in full_panel.columns or symbol_b not in full_panel.columns:
        log.warning("%s/%s missing from bhavcopy panel; skipping backtest",
                    symbol_a, symbol_b)
        return None

    # Trim to the trailing window.
    panel = full_panel.iloc[-history_days:] if history_days else full_panel
    if len(panel) < lookback_days + 5:
        log.warning("%s/%s — only %d trailing days; need %d. Using full panel.",
                    symbol_a, symbol_b, len(panel), lookback_days + 5)
        panel = full_panel

    try:
        lot_sizes = load_lot_sizes(universe)
    except Exception as e:
        log.warning("lot_sizes lookup failed for %s/%s: %s",
                    symbol_a, symbol_b, e)
        return None

    pair_row = pd.Series({
        "symbol_a": symbol_a, "symbol_b": symbol_b, "hedge_ratio": hedge_ratio,
    })
    return backtest_one(
        pair_row, panel, lot_sizes,
        entry_z=entry_z, exit_z=exit_z, stop_z=stop_z,
        lookback_days=lookback_days, max_holding_days=max_holding_days,
        lots_per_leg=lots_per_leg, max_leg_notional=max_leg_notional,
        seed_panel=None,
    )


def render_report(
    today: date, paper_pairs: List[dict], bt_results: Dict[str, Optional[dict]],
    signal_history: Dict[str, int], log: logging.Logger,
) -> dict:
    """Build the verification report dict and log a human-readable table."""
    rows = []
    drift_total = 0.0
    today_paper_total = 0.0
    today_bt_total = 0.0

    for paper in paper_pairs:
        sa, sb = paper["pair"][0], paper["pair"][1]
        key = f"{sa}/{sb}"
        bt = bt_results.get(key)
        paper_today = float(paper.get("realized_pnl", 0.0)) + float(paper.get("unrealized_pnl", 0.0))

        if bt is None:
            row = {
                "pair": key, "hedge_ratio": paper.get("hedge_ratio"),
                "paper_today_pnl": paper_today,
                "paper_position": paper.get("position"),
                "paper_n_closed": paper.get("n_closed_trades"),
                "backtest_status": "skipped",
                "backtest_total_pnl": None, "backtest_n_days": None,
                "backtest_per_day": None, "drift": None,
            }
        else:
            n_days = int(bt["n_days"])
            bt_total = float(bt["total_pnl"])
            per_day = bt_total / n_days if n_days else 0.0
            drift = paper_today - per_day
            drift_total += abs(drift)
            today_paper_total += paper_today
            today_bt_total += per_day
            row = {
                "pair": key, "hedge_ratio": paper.get("hedge_ratio"),
                "paper_today_pnl": paper_today,
                "paper_position": paper.get("position"),
                "paper_n_closed": paper.get("n_closed_trades"),
                "backtest_status": "ok",
                "backtest_total_pnl": bt_total,
                "backtest_n_days": n_days,
                "backtest_n_round_trips": int(bt["n_round_trips"]),
                "backtest_per_day": per_day,
                "drift": drift,
            }
        rows.append(row)

    days_sorted = sorted(signal_history.keys(), reverse=True)
    today_signals = signal_history.get(today.isoformat(), 0)
    prior = [signal_history[d] for d in days_sorted if d != today.isoformat()]
    prior_avg = sum(prior) / len(prior) if prior else 0.0

    log.info("=" * 96)
    log.info("PAIR-TRADING VERIFICATION REPORT — %s", today)
    log.info("=" * 96)
    log.info(
        f"{'Pair':<22} {'β':>9} {'Paper P&L':>12} {'BT total P&L':>14} "
        f"{'BT per-day':>14} {'Drift':>12} Pos"
    )
    log.info("-" * 96)
    for r in rows:
        beta = r.get("hedge_ratio")
        beta_s = f"{beta:.3f}" if beta is not None else "  —"
        pos = r["paper_position"] or "—"
        if r["backtest_status"] == "ok":
            log.info(
                f"{r['pair']:<22} {beta_s:>9} {r['paper_today_pnl']:>12,.0f} "
                f"{r['backtest_total_pnl']:>14,.0f} {r['backtest_per_day']:>14,.2f} "
                f"{r['drift']:>12,.2f} {pos}"
            )
        else:
            log.info(
                f"{r['pair']:<22} {beta_s:>9} {r['paper_today_pnl']:>12,.0f} "
                f"{'—':>14} {'—':>14} {'—':>12} {pos}"
            )
    log.info("-" * 96)
    log.info(
        f"Aggregate: paper_today=₹{today_paper_total:,.0f}  "
        f"bt_per_day=₹{today_bt_total:,.0f}  "
        f"|drift|_total=₹{drift_total:,.0f}"
    )
    log.info("")
    log.info("Signal history (strategy=pair_trading, last %d days):", len(days_sorted))
    for d in days_sorted:
        marker = "  ← today" if d == today.isoformat() else ""
        log.info("  %s : %4d signals%s", d, signal_history[d], marker)
    log.info("Today vs prior-day avg: %d vs %.1f", today_signals, prior_avg)

    return {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "rows": rows,
        "aggregate": {
            "paper_today_pnl": today_paper_total,
            "bt_per_day_pnl": today_bt_total,
            "abs_drift_total": drift_total,
        },
        "signal_history": signal_history,
        "signals_today": today_signals,
        "signals_prior_avg": prior_avg,
    }


def main():
    parser = argparse.ArgumentParser(
        description="EOD verification: paper P&L vs trailing backtest"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="ISO date to verify (default: today)")
    parser.add_argument("--history-days", type=int, default=60,
                        help="Trailing-day window for the backtest replay (default 60)")
    parser.add_argument("--signal-window", type=int, default=10,
                        help="Days of signal history to summarise (default 10)")
    parser.add_argument("--lookback", type=int, default=60, dest="lookback_days")
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--exit-z", type=float, default=0.75)
    parser.add_argument("--stop-z", type=float, default=4.0)
    parser.add_argument("--max-hold", type=int, default=7, dest="max_holding_days")
    parser.add_argument("--lots-per-leg", type=int, default=1)
    parser.add_argument("--max-leg-notional", type=float, default=1_000_000)
    parser.add_argument("--system", type=str, default="baseline",
                        help="System tag — selects which EOD sidecar to verify "
                             "and suffixes the output filenames. Defaults to "
                             "'baseline' so the baseline pair-verify.service "
                             "wiring is byte-identical to today's.")
    args = parser.parse_args()

    today = date.fromisoformat(args.date) if args.date else datetime.now().date()
    log = setup_logging(today, args.system)

    sidecar = load_eod_sidecar(today, args.system)
    if sidecar is None:
        sidecar_label = ("pair_paper_eod_" if args.system == "baseline"
                          else f"pair_paper_{args.system}_eod_")
        log.error("No EOD sidecar at data_cache/%s%s.json — has "
                  "run_paper_pairs.py (system=%s) run today?",
                  sidecar_label, today.isoformat(), args.system)
        return 1
    paper_pairs = sidecar.get("pairs", [])
    log.info("Loaded EOD sidecar [system=%s]: %d pair(s) traded today",
             args.system, len(paper_pairs))

    bt_results: Dict[str, Optional[dict]] = {}
    for paper in paper_pairs:
        sa, sb = paper["pair"][0], paper["pair"][1]
        beta = float(paper["hedge_ratio"])
        key = f"{sa}/{sb}"
        log.info("Backtesting %s β=%.4f over trailing %dd...", key, beta, args.history_days)
        try:
            bt_results[key] = backtest_pair(
                sa, sb, beta,
                lookback_days=args.lookback_days,
                entry_z=args.entry_z, exit_z=args.exit_z, stop_z=args.stop_z,
                max_holding_days=args.max_holding_days,
                lots_per_leg=args.lots_per_leg,
                max_leg_notional=args.max_leg_notional,
                history_days=args.history_days, log=log,
            )
        except Exception as e:
            log.exception("Backtest failed for %s: %s", key, e)
            bt_results[key] = None

    sig_hist = signal_counts(args.signal_window, today)
    report = render_report(today, paper_pairs, bt_results, sig_hist, log)
    report["system"] = args.system  # label for downstream tooling

    suffix = "" if args.system == "baseline" else f"-{args.system}"
    json_path = LOG_DIR / f"pair-verify{suffix}-{today.isoformat()}.json"
    json_path.write_text(json.dumps(report, default=str, indent=2))
    log.info("Wrote machine-readable report: %s", json_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
