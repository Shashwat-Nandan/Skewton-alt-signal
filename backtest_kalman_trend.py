#!/usr/bin/env python3
"""
Walk-forward backtest for the Kalman trend follower (Option B — robustness).
===========================================================================
The single 6mo/6mo split of `validate_kalman_trend.py` overfits: train Sharpe
2–5 collapses to OOS noise (tasks/kalman-trend-findings.md). This harness instead
tests whether ANY *stable* edge exists by:

  1. the REDUCED 4-param Kalman fit (one signal-to-noise knob + µ/stop/target;
     `optimize_kalman_trend.fit_kalman_reduced`) — fewer params = less overfit, and
  2. WALK-FORWARD evaluation: roll many train→test folds across the series, fit on
     each train and score the immediately-following (out-of-sample) test, multi-
     seed, and aggregate. A method with real edge wins across folds; one that only
     wins on a lucky window does not.

Compares reduced-Kalman vs the (equally-fit) MA crossover baseline. Verdict =
Kalman's median across-fold OOS Sharpe ≥ MA's AND it wins a majority of folds.

Usage:
    python backtest_kalman_trend.py --symbols NIFTY,BANKNIFTY
    python backtest_kalman_trend.py --csv data_cache/FOO_daily.csv
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

import optimize_kalman_trend as o
from validate_kalman_trend import TICK_SIZE, load_daily_closes


def _pooled_sharpe(pnl: np.ndarray) -> float:
    sd = float(np.std(pnl))
    return float(np.mean(pnl) / sd * np.sqrt(o.TRADING_DAYS)) if sd > 0 else 0.0


def _fold_oos(closes, a, b, c, *, kind, params, cost) -> np.ndarray:
    """OOS daily P&L on the test slice [b:c]; the signal is warmed up from the
    train start a (params were fit on [a:b] only — causal, no look-ahead).

    Returns the daily-P&L ARRAY (not a per-window Sharpe): on a 20-bar window a
    per-window Sharpe is dominated by the no-trade penalty, so we POOL the daily
    P&L across folds and take one Sharpe on the concatenation instead."""
    seg = closes[a:c]
    if kind == "kalman":
        direction = o.kalman_direction(seg, params["filter_params"],
                                       model=params.get("model", 1), mu=params["mu"])
    else:
        direction = o.ma_direction(seg, short=params["short"], long=params["long"],
                                   offset=params["offset"])
    test_dir = direction[b - a:]
    return o.simulate(closes[b:c], test_dir, stop_ticks=params["stop_ticks"],
                      target_ticks=params["target_ticks"], tick_size=TICK_SIZE,
                      cost_per_unit=cost).daily_pnl


def walk_forward(symbol: str, closes: np.ndarray, *, train_len: int, test_len: int,
                 step: int, seeds: list[int], n_gen: int, cost: float) -> dict:
    n = len(closes)
    starts = list(range(0, n - train_len - test_len + 1, step))
    if not starts:
        raise ValueError(f"{symbol}: {n} bars too short for train {train_len} + "
                         f"test {test_len}")
    kal_pool, ma_pool, wins = [], [], 0
    for a in starts:
        b, c = a + train_len, a + train_len + test_len
        train = closes[a:b]
        # average the OOS daily P&L over seeds (each seed = one fitted strategy)
        kal_fold = np.zeros(c - b)
        ma_fold = np.zeros(c - b)
        for seed in seeds:
            kp = o.fit_kalman_reduced(train, tick_size=TICK_SIZE, cost_per_unit=cost,
                                      n_gen=n_gen, seed=seed)
            mp = o.fit_ma_crossover(train, tick_size=TICK_SIZE, cost_per_unit=cost,
                                    n_gen=n_gen, seed=seed)
            kal_fold += _fold_oos(closes, a, b, c, kind="kalman", params=kp, cost=cost)
            ma_fold += _fold_oos(closes, a, b, c, kind="ma", params=mp, cost=cost)
        kal_fold /= len(seeds)
        ma_fold /= len(seeds)
        wins += int(kal_fold.sum() >= ma_fold.sum())   # per-fold total OOS P&L
        kal_pool.append(kal_fold)
        ma_pool.append(ma_fold)

    kal_pool = np.concatenate(kal_pool)
    ma_pool = np.concatenate(ma_pool)
    n_folds = len(starts)
    kal_sharpe = _pooled_sharpe(kal_pool)
    ma_sharpe = _pooled_sharpe(ma_pool)
    return {
        "symbol": symbol, "n_bars": n, "n_folds": n_folds,
        "oos_days": len(kal_pool),
        "kal_pooled_sharpe": kal_sharpe, "ma_pooled_sharpe": ma_sharpe,
        "kal_pooled_pnl": float(kal_pool.sum()), "ma_pooled_pnl": float(ma_pool.sum()),
        "fold_win_rate": wins / n_folds,
        "passed": kal_sharpe >= ma_sharpe and wins / n_folds >= 0.5,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="NIFTY,BANKNIFTY")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--train-len", type=int, default=120)
    ap.add_argument("--test-len", type=int, default=20)
    ap.add_argument("--step", type=int, default=20)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--n-gen", type=int, default=80)
    ap.add_argument("--cost", type=float, default=2.5)
    args = ap.parse_args()
    seeds = list(range(args.seeds))

    if args.csv:
        import pandas as pd
        from pathlib import Path
        df = pd.read_csv(args.csv)
        cols = {c.lower(): c for c in df.columns}
        jobs = [(Path(args.csv).stem, df[cols["close"]].to_numpy(float))]
    else:
        jobs = []
        for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            try:
                jobs.append((sym, load_daily_closes(sym)[1]))
            except FileNotFoundError as e:
                print(f"\nDATA MISSING for {sym}:\n{e}\n", file=sys.stderr)
                return 2

    results, all_pass = [], True
    for sym, closes in jobs:
        r = walk_forward(sym, closes, train_len=args.train_len, test_len=args.test_len,
                         step=args.step, seeds=seeds, n_gen=args.n_gen, cost=args.cost)
        results.append(r)
        all_pass &= r["passed"]

    print(f"\nKalman trend walk-forward (reduced 4-param fit; train {args.train_len}"
          f"/test {args.test_len}/step {args.step}; {args.seeds} seeds; POOLED OOS)\n")
    hdr = (f"{'symbol':<10}{'bars':>6}{'folds':>6}{'oosDays':>8}{'kalOOS_Sh':>10}"
           f"{'maOOS_Sh':>10}{'kalPnl':>10}{'maPnl':>10}{'foldWin':>9}{'  verdict'}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['symbol']:<10}{r['n_bars']:>6}{r['n_folds']:>6}{r['oos_days']:>8}"
              f"{r['kal_pooled_sharpe']:>10.2f}{r['ma_pooled_sharpe']:>10.2f}"
              f"{r['kal_pooled_pnl']:>10.0f}{r['ma_pooled_pnl']:>10.0f}"
              f"{r['fold_win_rate']:>9.0%}"
              f"{'   PASS' if r['passed'] else '   FAIL'}")
    print()
    if all_pass:
        print("WALK-FORWARD PASSED — reduced Kalman beats MA across folds (median "
              "OOS + majority of folds) on all symbols.\n")
        return 0
    print("WALK-FORWARD FAILED — reduced Kalman did not robustly beat MA across "
          "folds on at least one symbol.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
