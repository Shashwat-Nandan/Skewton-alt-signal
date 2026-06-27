#!/usr/bin/env python3
"""
Correctness gate for the Kalman trend-following system (Phase 0).
================================================================
Reproduces the paper's *qualitative* claim (Benhamou, hal-02012471, §6): an
optimized Kalman trend follower beats an optimized moving-average crossover
OUT OF SAMPLE. Protocol mirrors the paper — split a daily series into a 6-month
train and a 6-month test, fit each strategy's parameters on train by CMA-ES
maximizing the train Sharpe (the Kalman fit adds the L1 penalty), then compare
their TEST-period Sharpe.

We do NOT target the paper's literal Table-2 vector: that optimum is numerically
explosive and does not reproduce (see tasks/kalman-trend-system-plan.md, Phase-0
finding #2). The gate asserts the claim that actually matters — Kalman ≥ MA on
the held-out half — and FAILS LOUD (exit 1) otherwise.

Usage:
    python validate_kalman_trend.py                       # NIFTY + BANKNIFTY
    python validate_kalman_trend.py --symbols NIFTY
    python validate_kalman_trend.py --csv data_cache/foo_daily.csv  # date,close
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import optimize_kalman_trend as o

CACHE = Path("data_cache")
# Index level treated as continuous: one "tick" = one index point.
TICK_SIZE = 1.0


def load_daily_closes(symbol: str) -> tuple[list, np.ndarray]:
    """Return (dates, closes) of daily closes for `symbol`.

    BANKNIFTY (and any non-NIFTY symbol) loads from data_cache/<symbol>_daily.csv
    with columns (date, close). NIFTY is extracted from the cached F&O EOD
    snapshot's per-day underlying price. Fails loud if the data is missing."""
    simple = CACHE / f"{symbol}_daily.csv"
    if simple.exists():
        df = pd.read_csv(simple)
        cols = {c.lower(): c for c in df.columns}
        if "date" not in cols or "close" not in cols:
            raise ValueError(f"{simple} must have 'date' and 'close' columns")
        df = df[[cols["date"], cols["close"]]].dropna()
        return df[cols["date"]].tolist(), df[cols["close"]].to_numpy(float)

    if symbol == "NIFTY":
        # Widest-span NIFTY_*_eod.csv → daily underlying close.
        eod = sorted(CACHE.glob("NIFTY_*_eod.csv"),
                     key=lambda p: p.stat().st_size, reverse=True)
        if not eod:
            raise FileNotFoundError("no NIFTY_*_eod.csv in data_cache")
        df = pd.read_csv(eod[0], usecols=["timestamp", "underlying_price"])
        df["d"] = pd.to_datetime(df["timestamp"]).dt.date
        g = df.groupby("d")["underlying_price"].last()
        return list(g.index), g.to_numpy(float)

    raise FileNotFoundError(
        f"no daily data for {symbol}: expected {simple}. On the HOST (where a "
        f"live Kite session exists) fetch it, e.g.\n"
        f"    python -c \"from kite_auth import KiteAuthManager; import pandas as pd; \"\n"
        f"      # kite.historical_data(<{symbol} index token>, from, to, 'day') -> "
        f"date,close -> {simple}\"\n"
        f"then re-run this gate. (A fresh login must not run while a live runner "
        f"is active — reuse the cached session.)")


def evaluate_oos(prices: np.ndarray, n_train: int, *, kind: str, params: dict,
                 cost: float) -> o.SimResult:
    """Warm the signal up on the full series (filter state at test-start reflects
    train data — legitimate, no look-ahead since params were fit on train only),
    then book trades on the TEST slice only."""
    if kind == "kalman":
        direction = o.kalman_direction(prices, params["filter_params"], model=1,
                                       mu=params["mu"])
    else:
        direction = o.ma_direction(prices, short=params["short"],
                                   long=params["long"], offset=params["offset"])
    return o.simulate(prices[n_train:], direction[n_train:],
                      stop_ticks=params["stop_ticks"],
                      target_ticks=params["target_ticks"],
                      tick_size=TICK_SIZE, cost_per_unit=cost)


def run_symbol(symbol: str, closes: np.ndarray, *, n_gen: int, l1_lambda: float,
               cost: float, seed: int) -> dict:
    n = len(closes)
    if n < 60:
        raise ValueError(f"{symbol}: need >= 60 daily bars, got {n}")
    n_train = n // 2
    train = closes[:n_train]

    kal = o.fit_kalman_trend(train, model=1, tick_size=TICK_SIZE,
                             cost_per_unit=cost, l1_lambda=l1_lambda,
                             n_gen=n_gen, seed=seed)
    ma = o.fit_ma_crossover(train, tick_size=TICK_SIZE, cost_per_unit=cost,
                            n_gen=n_gen, seed=seed)
    kal_test = evaluate_oos(closes, n_train, kind="kalman", params=kal, cost=cost)
    ma_test = evaluate_oos(closes, n_train, kind="ma", params=ma, cost=cost)
    return {
        "symbol": symbol, "n_bars": n, "n_train": n_train,
        "kal_train_sharpe": kal["train_sharpe"],
        "ma_train_sharpe": ma["train_sharpe"],
        "kal_test_sharpe": kal_test.sharpe, "ma_test_sharpe": ma_test.sharpe,
        "kal_test_trades": kal_test.n_trades, "ma_test_trades": ma_test.n_trades,
        "kal_sparsity": kal["l1_norm_normalized"],
        "passed": kal_test.sharpe >= ma_test.sharpe,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="NIFTY,BANKNIFTY")
    ap.add_argument("--csv", default=None, help="date,close CSV (overrides --symbols)")
    ap.add_argument("--n-gen", type=int, default=150)
    ap.add_argument("--l1-lambda", type=float, default=0.1)
    ap.add_argument("--cost", type=float, default=2.5,
                    help="per-side cost in index points (round trip = 2x)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.csv:
        df = pd.read_csv(args.csv)
        cols = {c.lower(): c for c in df.columns}
        jobs = [(Path(args.csv).stem, df[cols["close"]].to_numpy(float))]
    else:
        jobs = []
        for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            try:
                jobs.append((sym, load_daily_closes(sym)[1]))
            except FileNotFoundError as e:
                print(f"\nDATA MISSING for {sym} — gate cannot run on it:\n{e}\n",
                      file=sys.stderr)
                return 2   # distinct from a genuine gate failure (1)

    results, all_pass = [], True
    for sym, closes in jobs:
        r = run_symbol(sym, closes, n_gen=args.n_gen, l1_lambda=args.l1_lambda,
                       cost=args.cost, seed=args.seed)
        results.append(r)
        all_pass &= r["passed"]

    print("\nKalman trend-following correctness gate (paper §6 claim: "
          "optimized Kalman ≥ MA crossover, out of sample)\n")
    hdr = (f"{'symbol':<10}{'bars':>6}{'kal_train':>11}{'ma_train':>10}"
           f"{'kal_TEST':>10}{'ma_TEST':>9}{'kal_trd':>8}{'sparsity':>10}{'  verdict'}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['symbol']:<10}{r['n_bars']:>6}{r['kal_train_sharpe']:>11.2f}"
              f"{r['ma_train_sharpe']:>10.2f}{r['kal_test_sharpe']:>10.2f}"
              f"{r['ma_test_sharpe']:>9.2f}{r['kal_test_trades']:>8}"
              f"{r['kal_sparsity']:>10.2f}"
              f"{'   PASS' if r['passed'] else '   FAIL'}")
    print()
    if all_pass:
        print("GATE PASSED — Kalman beats the MA baseline out of sample on all "
              "symbols.\n")
        return 0
    print("GATE FAILED — Kalman did NOT beat the MA baseline OOS on at least one "
          "symbol. Do not promote past Phase 0 (Rule 12).\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
