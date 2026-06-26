"""
Kalman Pair-Trading Backtester — the Phase-2 go/no-go
=====================================================
Replays cached F&O bhavcopy STF closes through KalmanPairStrategy and compares
three hedge-ratio trackers on the SAME pairs, window, sizing, costs, and z-band:

  • static    — Kalman with α≈0, i.e. γ frozen at the training-window OLS value
                (the incumbent static-β behaviour, run through the identical
                code path so the comparison is apples-to-apples — no duplicated
                decision logic);
  • basic     — basic Kalman, Eq.(15.3), α=1e-5 (book default);
  • momentum  — momentum Kalman, Eq.(15.4), α=1e-6 (book default).

Each "tick" is one trading day (bhavcopy is EOD). The pair universe comes from
data_cache/pair_candidates.csv (discovery is unchanged — decision D6); the
Kalman system IGNORES the static screener β and re-fits γ itself on the log-
price training window. Decisions on day t use the predicted state α_{t|t-1}
(causal) and the day-t close; step_daily_close then folds day t in.

Out-of-sample by construction: the filter is seeded on the train slice; P&L is
booked only on the test slice. Open positions are force-closed at the last test
bar so reported P&L is fully realized.

The go/no-go question (CLAUDE.md Rule 12 — be explicit if the answer is "no"):
does Kalman produce a more stationary spread AND better net-of-cost P&L than
static β on Indian F&O pairs? The book's edge is on US ETFs; it may not transfer.

Usage:
    python backtest_kalman_pairs.py
    python backtest_kalman_pairs.py --top 15 --train-fraction 0.5
    python backtest_kalman_pairs.py --csv-out data_cache/kalman_bt.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest_pairs import load_lot_sizes
from screen_pairs import NIFTY_50, _half_life, load_front_month_panel, screen_pairs
from strategies.kalman_pair_trading import KalmanPairStrategy

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
CANDIDATES_PATH = CACHE_DIR / "pair_candidates.csv"

# (label, model, alpha). α≈0 is the frozen-γ (static-β) limit.
CONFIGS = [
    ("static", "basic", 1e-12),
    ("basic", "basic", 1e-5),
    ("momentum", "momentum", 1e-6),
]


def _write_temp_config(knobs: dict) -> str:
    """KalmanPairStrategy reads [kalman_pair_trading] from its config_path and
    refuses paper mode without max_leg_notional. Write a throwaway ini with the
    backtest knobs so we construct it normally — no __init__ bypass (Rule 3)."""
    fd = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
    fd.write("[strategy]\ntotal_capital = 500000\n\n[kalman_pair_trading]\n")
    for k, v in knobs.items():
        fd.write(f"{k} = {v}\n")
    fd.close()
    return fd.name


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float((equity - peak).min())


def _win_rate(closed_trades: List[dict]) -> Optional[float]:
    if not closed_trades:
        return None
    wins = sum(1 for t in closed_trades if t.get("realized_pnl", 0.0) > 0)
    return wins / len(closed_trades) * 100


def run_replay(symbol_a, symbol_b, lot_a, lot_b,
               train_a, train_b, test_a, test_b, test_dates,
               *, label, model, alpha, config_path) -> Optional[dict]:
    """One pair, one config. Returns metrics or None (skipped, with a reason
    logged) when the filter can't be seeded (degenerate/out-of-band γ).

    `label` (static/basic/momentum) — NOT `model` — is the config key, because
    the static and basic configs share model='basic' (they differ only in α)."""
    ts_a, ts_b = f"{symbol_a}_FUT", f"{symbol_b}_FUT"
    quote = {ts_a: None, ts_b: None}
    cur = [test_dates[0]]
    try:
        strat = KalmanPairStrategy(
            kite=None, config_path=config_path, mode="paper",
            symbol_a=symbol_a, symbol_b=symbol_b,
            tradingsymbol_a=ts_a, tradingsymbol_b=ts_b,
            lot_size_a=lot_a, lot_size_b=lot_b,
            training_a=train_a, training_b=train_b,
            model=model, alpha=alpha,
            quote_fn=lambda t: quote.get(t),
            clock=lambda: datetime.combine(cur[0], datetime.min.time()),
        )
    except ValueError as e:
        logger.warning("%s/%s [%s] skipped: %s", symbol_a, symbol_b, label, e)
        return None

    spreads, equity = [], []
    for ca, cb, d in zip(test_a, test_b, test_dates):
        quote[ts_a], quote[ts_b] = float(ca), float(cb)
        cur[0] = d
        try:
            entries = strat.scan_and_propose()
            if entries:
                strat.execute_proposals(entries)
            rehedges = strat.check_and_rehedge()
            if rehedges:
                strat.execute_proposals(rehedges)
        except Exception as e:  # a bad bar must not abort the whole replay
            logger.warning("%s/%s [%s] tick %s failed: %s",
                           symbol_a, symbol_b, label, d, e)
        spreads.append(strat.step_daily_close(float(ca), float(cb)))
        equity.append(strat.state.realized_pnl + strat.state.unrealized_pnl)

    # Force-close any open position at the last test bar.
    if strat.state.position != "FLAT":
        prices = {symbol_a: float(test_a[-1]), symbol_b: float(test_b[-1])}
        strat._update_unrealized(prices)
        props = strat._build_exit_proposals("EOD_CLOSE", 0.0, prices)
        if props:
            strat.execute_proposals(props)
        equity.append(strat.state.realized_pnl + strat.state.unrealized_pnl)

    sp = np.asarray(spreads, dtype=float)
    return {
        "pair": f"{symbol_a}/{symbol_b}", "config": label,
        "n_days": len(test_dates),
        "n_round_trips": len(strat.state.closed_trades),
        "net_pnl": strat.state.realized_pnl + strat.state.unrealized_pnl,
        "costs": strat.state.total_transaction_costs,
        "gross_pnl": (strat.state.realized_pnl + strat.state.unrealized_pnl
                      + strat.state.total_transaction_costs),
        "win_rate_pct": _win_rate(strat.state.closed_trades),
        "max_drawdown": _max_drawdown(np.asarray(equity)),
        "spread_var": float(np.var(sp)) if sp.size else float("nan"),
        "half_life": _half_life(sp) if sp.size > 10 else float("nan"),
        "final_gamma": strat._gamma_today,
    }


def backtest_pair(a, b, train_panel, test_panel, lot_sizes, *, configs,
                  config_path) -> List[dict]:
    """Seed the filter on the TRAIN-slice prices, book P&L only on the
    TEST slice. Pair SELECTION happens upstream by screening the train panel,
    so both selection and the filter seed are out-of-sample (no look-ahead)."""
    if a not in lot_sizes or b not in lot_sizes:
        logger.warning("%s/%s missing lot size; skipping", a, b)
        return []
    train = train_panel[[a, b]].dropna()
    test = test_panel[[a, b]].dropna()
    if len(train) < 60:
        logger.warning("%s/%s train slice too short (%d); skipping", a, b, len(train))
        return []
    if len(test) < 30:
        logger.warning("%s/%s test slice too short (%d); skipping", a, b, len(test))
        return []
    train_a, train_b = train[a].values, train[b].values
    test_a, test_b = test[a].values, test[b].values
    test_dates = [d.date() for d in test.index]

    out = []
    for label, model, alpha in configs:
        r = run_replay(a, b, int(lot_sizes[a]), int(lot_sizes[b]),
                       train_a, train_b, test_a, test_b, test_dates,
                       label=label, model=model, alpha=alpha,
                       config_path=config_path)
        if r:
            out.append(r)
    return out


def print_report(rows: List[dict], args):
    df = pd.DataFrame(rows)
    if df.empty:
        print("No results — no pair could be backtested.")
        return
    print(f"\nKalman pair backtest — top={args.top}, "
          f"train_fraction={args.train_fraction} (OUT-OF-SAMPLE), "
          f"entry={args.entry_z} exit={args.exit_z} stop={args.stop_z} "
          f"lookback={args.lookback_days}d max-hold={args.max_holding_days}d")
    print("=" * 96)

    # Per-config aggregate — the headline go/no-go comparison. Spread-var is
    # reported as the MEDIAN: a pair whose γ drifts toward −1 makes the
    # normalized spread (÷(1+γ)) blow up, so the mean is outlier-dominated.
    print(f"\n{'config':<10} {'pairs':>5} {'trips':>6} {'net P&L':>14} "
          f"{'gross P&L':>14} {'costs':>12} {'avg win%':>9} "
          f"{'med spread-var':>15} {'med half-life':>14}")
    print("-" * 96)
    order = {c[0]: i for i, c in enumerate(CONFIGS)}
    for cfg in sorted(df["config"].unique(), key=lambda c: order.get(c, 99)):
        g = df[df["config"] == cfg]
        wins = g["win_rate_pct"].dropna()
        hl = g["half_life"].replace([np.inf, -np.inf], np.nan)
        print(f"{cfg:<10} {len(g):>5} {int(g['n_round_trips'].sum()):>6} "
              f"{g['net_pnl'].sum():>14,.0f} {g['gross_pnl'].sum():>14,.0f} "
              f"{g['costs'].sum():>12,.0f} "
              f"{(wins.mean() if not wins.empty else float('nan')):>9.1f} "
              f"{g['spread_var'].median():>15.5f} "
              f"{hl.median():>14.2f}")

    # Spread-stationarity: does Kalman lower the spread variance vs static,
    # pair by pair? Report the fraction of pairs where it does.
    piv = df.pivot_table(index="pair", columns="config", values="spread_var")
    if {"static", "basic", "momentum"}.issubset(piv.columns):
        print("\nSpread variance vs static (lower = more stationary):")
        for cfg in ("basic", "momentum"):
            better = (piv[cfg] < piv["static"]).sum()
            print(f"  {cfg:<9}: more stationary than static on "
                  f"{better}/{len(piv)} pairs")
    if args.csv_out:
        df.to_csv(args.csv_out, index=False)
        print(f"\nPer-pair rows written to {args.csv_out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--train-fraction", type=float, default=0.5)
    ap.add_argument("--entry-z", dest="entry_z", type=float, default=2.0)
    ap.add_argument("--exit-z", dest="exit_z", type=float, default=0.75)
    ap.add_argument("--stop-z", dest="stop_z", type=float, default=4.0)
    ap.add_argument("--lookback-days", dest="lookback_days", type=int, default=60)
    ap.add_argument("--max-holding-days", dest="max_holding_days", type=int, default=7)
    ap.add_argument("--lots-per-leg", dest="lots_per_leg", type=int, default=1)
    ap.add_argument("--max-leg-notional", dest="max_leg_notional", type=float,
                    default=2_000_000)
    ap.add_argument("--min-edge-multiplier", dest="min_edge_multiplier",
                    type=float, default=1.5)
    ap.add_argument("--csv-out", dest="csv_out", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if not (0.1 < args.train_fraction < 0.95):
        ap.error("--train-fraction must be in (0.1, 0.95)")

    config_path = _write_temp_config({
        "entry_z": args.entry_z, "exit_z": args.exit_z, "stop_z": args.stop_z,
        "lookback_days": args.lookback_days,
        "max_holding_days": args.max_holding_days,
        "lots_per_leg": args.lots_per_leg,
        "max_leg_notional": args.max_leg_notional,
        "min_edge_multiplier": args.min_edge_multiplier,
        # Daily bar = one tick, so exit on the first in-band bar (the 2-tick
        # debounce is an intraday-noise filter; see backtest_pairs rationale).
        "exit_debounce_ticks": 1,
    })

    # Screen on the TRAIN slice, backtest on the holdout — pair selection is
    # out-of-sample (using the pre-screened pair_candidates.csv, which was
    # screened on RECENT data, would leak future cointegration into the test).
    panel = load_front_month_panel(NIFTY_50, min_coverage=0.50)
    lot_sizes = load_lot_sizes(NIFTY_50)
    cut = int(len(panel) * args.train_fraction)
    train_panel, test_panel = panel.iloc[:cut], panel.iloc[cut:]
    print(f"Screening {len(train_panel)} train days for cointegrated pairs "
          f"(holdout = {len(test_panel)} days)...")
    screened = screen_pairs(train_panel, p_threshold=0.05, min_correlation=0.5)
    if screened.empty:
        print("No cointegrated pairs found on the train slice.")
        return 0
    screened = screened.head(args.top)

    rows: List[dict] = []
    for _, row in screened.iterrows():
        rows.extend(backtest_pair(
            row["symbol_a"], row["symbol_b"], train_panel, test_panel,
            lot_sizes, configs=CONFIGS, config_path=config_path,
        ))
    print_report(rows, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
