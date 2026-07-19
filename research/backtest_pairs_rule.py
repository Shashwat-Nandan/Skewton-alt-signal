"""
Walk-Forward Backtest of the Top-12 Pair Selection Rule
========================================================

Validates the runner's selection logic (β filter → quality floor → composite
select_score → leg-concentration cap walk) end-to-end on historical bhavcopy
data, with no look-ahead.

How it works
------------
At each weekly checkpoint t over the cached bhavcopy panel:
  1. Train slice = panel.loc[:t].tail(SCREEN_WINDOW)
  2. Re-screen pairs on the train slice via `screen_pairs.screen_pairs`
     (same Engle-Granger + half-life + spread-vol pipeline as the live cron).
  3. Run `classify_pair_candidates(df, top=N)` to apply the runner's β →
     quality → composite select_score → leg-cap walk.
  4. For each admitted pair, replay the next TEST_HORIZON trading days
     through `backtest_pairs.backtest_one`, seeded with the train-slice
     spread history so z-scores are immediately computable.
  5. Aggregate per-pair results (a pair admitted in multiple weeks is summed
     across all admissions).

Differences vs the existing `python -m research.backtest_pairs --train-fraction`:
  - Uses the runner's selection (not `head(n)` on rank_score).
  - Walks forward in weekly steps instead of one train/test split, so the
    report reflects how the rule actually behaves under regime change.

Usage
-----
    python -m research.backtest_pairs_rule
    python -m research.backtest_pairs_rule --top 12 --test-horizon 10
    python -m research.backtest_pairs_rule --checkpoint-stride 5 --screen-window 130
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

# Re-use existing building blocks — Rule 7: don't fork the selection logic
# and don't reimplement screening.
from research.backtest_pairs import backtest_one, load_lot_sizes
from core.screen_pairs import classify_pair_candidates
from core.screen_pairs import (
    NIFTY_50, load_front_month_panel, screen_pairs, screen_pairs_persistent,
)

logger = logging.getLogger("backtest_pairs_rule")

CACHE_DIR = Path("./data_cache")
RAW_DIR = CACHE_DIR / "bhavcopy_raw"


def setup_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity == 0 else (
        logging.INFO if verbosity == 1 else logging.DEBUG
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
        force=True,
    )


def weekly_checkpoints(
    panel: pd.DataFrame,
    screen_window: int,
    test_horizon: int,
    stride: int,
) -> List[pd.Timestamp]:
    """Return checkpoint timestamps such that each has at least
    `screen_window` trailing days and `test_horizon` forward days."""
    dates = panel.index
    if len(dates) < screen_window + test_horizon + 1:
        raise RuntimeError(
            f"Panel has {len(dates)} days; need at least "
            f"{screen_window + test_horizon + 1} for one checkpoint."
        )
    # First valid checkpoint: index = screen_window - 1 (so we have
    # screen_window days trailing including the checkpoint itself).
    first = screen_window - 1
    last = len(dates) - test_horizon - 1
    return [dates[i] for i in range(first, last + 1, stride)]


def aggregate_per_pair(per_admission: List[dict]) -> pd.DataFrame:
    """Collapse the (checkpoint, pair) result list into one row per pair."""
    rows: Dict[str, dict] = defaultdict(lambda: {
        "n_admissions": 0,
        "n_round_trips": 0,
        "n_orders": 0,
        "realized_pnl": 0.0,
        "transaction_costs": 0.0,
        "win_trades": 0,
        "loss_trades": 0,
    })
    for r in per_admission:
        if r is None:
            continue
        key = r["pair"]
        d = rows[key]
        d["n_admissions"] += 1
        d["n_round_trips"] += r["n_round_trips"]
        d["n_orders"] += r["n_orders"]
        d["realized_pnl"] += r["realized_pnl"]
        d["transaction_costs"] += r["transaction_costs"]
        # closed_trades carries cumulative realized_pnl at close — diff for
        # per-trade ₹, then bucket.
        prev = 0.0
        for tr in r["closed_trades"]:
            pnl = tr.get("realized_pnl", 0.0) - prev
            prev = tr.get("realized_pnl", 0.0)
            if pnl > 0:
                d["win_trades"] += 1
            elif pnl < 0:
                d["loss_trades"] += 1
    out = []
    for pair, d in rows.items():
        total = d["win_trades"] + d["loss_trades"]
        win_rate = (d["win_trades"] / total * 100) if total else None
        out.append({
            "pair": pair,
            "n_admissions": d["n_admissions"],
            "round_trips": d["n_round_trips"],
            "orders": d["n_orders"],
            "realized_pnl": d["realized_pnl"],
            "tx_costs": d["transaction_costs"],
            "win_rate_pct": win_rate,
        })
    return (
        pd.DataFrame(out)
        .sort_values("realized_pnl", ascending=False)
        .reset_index(drop=True)
    )


def portfolio_weekly_pnl(per_admission: List[dict]) -> pd.DataFrame:
    """Sum P&L per (checkpoint week) across all admitted pairs."""
    rows = []
    for r in per_admission:
        if r is None:
            continue
        rows.append({
            "checkpoint": r["_checkpoint"],
            "pair": r["pair"],
            "pnl": r["realized_pnl"],
        })
    if not rows:
        return pd.DataFrame(columns=["checkpoint", "weekly_pnl"])
    df = pd.DataFrame(rows)
    weekly = df.groupby("checkpoint")["pnl"].sum().reset_index(
        name="weekly_pnl"
    ).sort_values("checkpoint")
    weekly["cum_pnl"] = weekly["weekly_pnl"].cumsum()
    return weekly


def portfolio_metrics(weekly: pd.DataFrame) -> dict:
    if weekly.empty:
        return {"net_pnl": 0.0, "sharpe": None, "max_dd": 0.0,
                "win_weeks_pct": None, "n_weeks": 0}
    pnl = weekly["weekly_pnl"].values
    cum = weekly["cum_pnl"].values
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = float(dd.max())
    sharpe = None
    if len(pnl) > 1 and pnl.std(ddof=1) > 0:
        # Annualize: 52 trading weeks per year.
        sharpe = float(pnl.mean() / pnl.std(ddof=1) * np.sqrt(52))
    win_weeks = (pnl > 0).sum()
    win_pct = float(win_weeks / len(pnl) * 100)
    return {
        "net_pnl": float(pnl.sum()),
        "sharpe": sharpe,
        "max_dd": max_dd,
        "win_weeks_pct": win_pct,
        "n_weeks": int(len(pnl)),
    }


def run(args) -> int:
    setup_logging(args.verbose)

    logger.info("Loading bhavcopy panel from %s", RAW_DIR)
    panel = load_front_month_panel(
        universe=NIFTY_50, raw_dir=RAW_DIR, min_coverage=0.80,
    )
    if args.panel_start:
        panel = panel.loc[pd.Timestamp(args.panel_start):]
    if args.panel_end:
        panel = panel.loc[:pd.Timestamp(args.panel_end)]
    logger.info("Panel: %d trading days × %d symbols (%s → %s)",
                len(panel), panel.shape[1],
                panel.index[0].date(), panel.index[-1].date())

    lot_sizes = load_lot_sizes(panel.columns.tolist(), raw_dir=RAW_DIR)

    checkpoints = weekly_checkpoints(
        panel,
        screen_window=args.screen_window,
        test_horizon=args.test_horizon,
        stride=args.checkpoint_stride,
    )
    logger.info("Planned %d checkpoints (stride=%d, screen=%d, horizon=%d days)",
                len(checkpoints), args.checkpoint_stride,
                args.screen_window, args.test_horizon)

    per_admission: List[dict] = []
    skip_tally = defaultdict(int)

    for ck_idx, ck in enumerate(checkpoints):
        train = panel.loc[:ck].tail(args.screen_window)
        # Walk forward test_horizon days after the checkpoint. Use
        # iloc-based slice so we always get exactly test_horizon bars.
        ck_pos = panel.index.get_loc(ck)
        test = panel.iloc[ck_pos + 1: ck_pos + 1 + args.test_horizon]
        if test.empty:
            continue

        if args.persistence_min is not None:
            screened = screen_pairs_persistent(
                train,
                min_persistence=args.persistence_min,
                window_days=args.persistence_window_days,
                step_days=args.persistence_step_days,
                p_threshold=0.05,
                min_correlation=0.5,
                min_hedge_ratio=0.1,
                max_hedge_ratio=10.0,
            )
        else:
            screened = screen_pairs(
                train,
                p_threshold=0.05,
                min_correlation=0.5,
                min_hedge_ratio=0.1,
                max_hedge_ratio=10.0,
            )
        if screened.empty:
            logger.warning("Checkpoint %s: screen returned 0 pairs", ck.date())
            continue

        annotated = classify_pair_candidates(
            screened, top=args.top,
            exclude_symbols=args.exclude_symbols or None,
            max_hedge_ratio=args.max_hedge_ratio,
            max_pvalue=args.quality_max_pvalue,
        )
        admitted = (
            annotated[annotated["processing_rank"].notna()]
            .sort_values("processing_rank")
        )
        for reason, n in annotated["skip_reason"].value_counts().items():
            if reason:
                skip_tally[reason] += int(n)
        logger.info(
            "Checkpoint %s: admitted %d/%d pairs",
            ck.date(), len(admitted), len(screened),
        )

        for _, row in admitted.iterrows():
            selection_stats = {
                "checkpoint": ck,
                "symbol_a": row["symbol_a"],
                "symbol_b": row["symbol_b"],
                "processing_rank": int(row["processing_rank"]),
                "hedge_ratio": float(row["hedge_ratio"]),
                "abs_beta": abs(float(row["hedge_ratio"])),
                "beta_sign": "+" if row["hedge_ratio"] >= 0 else "-",
                "correlation": float(row["correlation"]),
                "coint_pvalue": float(row["coint_pvalue"]),
                "half_life_days": float(row["half_life_days"]),
                "spread_vol_pct": float(row["spread_vol_pct"]),
                "select_score": float(row["select_score"]),
                "latest_z_at_admit": float(row.get("latest_z_score", float("nan"))),
            }
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
            res["_selection_stats"] = selection_stats
            per_admission.append(res)

    # Optional CSV dump of per-admission diagnostics — selection stats joined
    # with the realized outcome. Lets a downstream analyzer compare winners
    # vs losers across the screening metrics.
    if args.dump_admissions:
        rows = []
        for r in per_admission:
            base = dict(r["_selection_stats"])
            base.update({
                "n_round_trips": r["n_round_trips"],
                "realized_pnl": r["realized_pnl"],
                "tx_costs": r["transaction_costs"],
                "max_drawdown": r["max_drawdown"],
                "win_rate_pct": r["win_rate_pct"],
            })
            rows.append(base)
        out_path = Path(args.dump_admissions)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(out_path, index=False)
        logger.info("Wrote %d admission rows to %s", len(rows), out_path)

    # ── Report ─────────────────────────────────────────────────────────
    if not per_admission:
        print("No admitted-pair backtests completed — nothing to report.")
        return 1

    weekly = portfolio_weekly_pnl(per_admission)
    metrics = portfolio_metrics(weekly)
    by_pair = aggregate_per_pair(per_admission)

    bar = "═" * 76
    print(bar)
    print(f"  TOP-{args.top} RULE WALK-FORWARD OOS")
    print(bar)
    print(f"  Panel               : {panel.index[0].date()} → {panel.index[-1].date()}  "
          f"({len(panel)} days)")
    print(f"  Checkpoints         : {metrics['n_weeks']}  "
          f"(stride {args.checkpoint_stride}d, horizon {args.test_horizon}d, "
          f"screen window {args.screen_window}d)")
    print(f"  Strategy params     : entry_z={args.entry_z} exit_z={args.exit_z} "
          f"stop_z={args.stop_z} max_entry_z={args.max_entry_z} "
          f"safety_buffer={args.safety_buffer}")
    print(f"                        lookback={args.lookback_days}d "
          f"max_hold={args.max_holding_days}d "
          f"min_edge_mult={args.min_edge_multiplier} "
          f"max_leg_notional=₹{args.max_leg_notional:,.0f}")
    print(bar)
    print("  PORTFOLIO METRICS")
    print(f"    Net realized P&L  : ₹{metrics['net_pnl']:>14,.0f}")
    print(f"    Sharpe (ann., 52w): {metrics['sharpe']:>14.3f}"
          if metrics['sharpe'] is not None else
          f"    Sharpe            : {'n/a':>15s}")
    print(f"    Max drawdown      : ₹{metrics['max_dd']:>14,.0f}")
    print(f"    Win-weeks         : {metrics['win_weeks_pct']:>13.1f}%  "
          f"({metrics['n_weeks']} total weeks)")
    print(f"    Round trips       : {by_pair['round_trips'].sum():>14d}  "
          f"across {by_pair['n_admissions'].sum()} pair-admissions")
    print(f"    Tx costs          : ₹{by_pair['tx_costs'].sum():>14,.0f}")

    print(bar)
    print("  SKIP TALLY (across all checkpoints)")
    for reason in ("beta", "quality", "leg_cap", "cutoff"):
        print(f"    {reason:<10s}        : {skip_tally.get(reason, 0):>14d}")

    print(bar)
    print("  PER-PAIR BREAKDOWN (sorted by net P&L)")
    print(f"  {'pair':<24s} {'admits':>6s} {'RTs':>5s} {'orders':>6s} "
          f"{'P&L ₹':>12s} {'tx ₹':>8s} {'win %':>6s}")
    for r in by_pair.itertuples():
        win = f"{r.win_rate_pct:>5.1f}" if r.win_rate_pct is not None else "  n/a"
        print(f"  {r.pair:<24s} {r.n_admissions:>6d} {r.round_trips:>5d} "
              f"{r.orders:>6d} {r.realized_pnl:>12,.0f} {r.tx_costs:>8,.0f} "
              f"{win:>6s}")
    print(bar)

    if args.dump_weekly:
        print()
        print("  WEEKLY P&L STREAM")
        for r in weekly.itertuples():
            print(f"    {r.checkpoint.date()}  weekly={r.weekly_pnl:>10,.0f}  "
                  f"cum={r.cum_pnl:>12,.0f}")

    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--top", type=int, default=12,
                   help="Top-N cutoff for the runner's selection (default 12).")
    p.add_argument("--screen-window", type=int, default=130,
                   help="Trailing trading days fed to the screener at each "
                        "checkpoint (default 130).")
    p.add_argument("--test-horizon", type=int, default=10,
                   help="Forward trading days per checkpoint (default 10 — "
                        "comfortably covers max_holding_days=7 entries).")
    p.add_argument("--checkpoint-stride", type=int, default=5,
                   help="Trading days between checkpoints (default 5 = weekly).")
    # Strategy params — mirror config.ini [pair_trading].
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
                   help="Comma-separated symbol blacklist (e.g. ADANIENT,ADANIPORTS).")
    p.add_argument("--max-hedge-ratio", type=float, default=None,
                   help="Override the |β| upper bound (default uses "
                        "HEDGE_RATIO_MAX=10.0).")
    p.add_argument("--persistence-min", type=int, default=None,
                   help="Mirror the PERSISTENT runner: replace the single-"
                        "window screen at each checkpoint with the rolling-"
                        "window persistence screen, admitting only pairs that "
                        "pass p<0.05 in ≥this-many windows AND the latest one. "
                        "Use a large --screen-window so multiple windows fit "
                        "(needs ≥ window_days + (N-1)*step_days history).")
    p.add_argument("--persistence-window-days", type=int, default=130,
                   help="Rolling-window length for --persistence-min (default "
                        "130, matches the live screen).")
    p.add_argument("--persistence-step-days", type=int, default=45,
                   help="Step between rolling windows for --persistence-min "
                        "(default 45, matches the live screen).")
    p.add_argument("--quality-max-pvalue", type=float, default=None,
                   help="Override the runner quality floor's p-value ceiling "
                        "(default QUALITY_MAX_PVALUE=0.025). The persistent "
                        "runner uses 0.05; pass it here to mirror that.")
    p.add_argument("--panel-start", type=str, default=None,
                   help="YYYY-MM-DD lower bound on the bhavcopy panel.")
    p.add_argument("--panel-end", type=str, default=None,
                   help="YYYY-MM-DD upper bound on the bhavcopy panel.")
    p.add_argument("--dump-weekly", action="store_true",
                   help="Also print the weekly P&L stream at the end.")
    p.add_argument("--dump-admissions", type=str, default=None,
                   help="CSV path to dump per-admission selection stats + outcome.")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="-v for INFO, -vv for DEBUG.")
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
