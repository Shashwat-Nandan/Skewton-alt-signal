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
    python -m research.validate_kalman_trend                       # NIFTY + BANKNIFTY
    python -m research.validate_kalman_trend --symbols NIFTY
    python -m research.validate_kalman_trend --csv data_cache/foo_daily.csv  # date,close
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from research import optimize_kalman_trend as o
from core.data_cache_io import find_tables, read_table, table_exists

CACHE = Path("data_cache")
# Index level treated as continuous: one "tick" = one index point.
TICK_SIZE = 1.0


def load_daily_closes(symbol: str) -> tuple[list, np.ndarray]:
    """Return (dates, closes) of daily closes for `symbol`.

    BANKNIFTY (and any non-NIFTY symbol) loads from data_cache/<symbol>_daily.{parquet,csv}
    with columns (date, close). NIFTY is extracted from the cached F&O EOD
    snapshot's per-day underlying price. Fails loud if the data is missing."""
    simple = CACHE / f"{symbol}_daily.parquet"
    if table_exists(simple):
        df = read_table(simple)
        cols = {c.lower(): c for c in df.columns}
        if "date" not in cols or "close" not in cols:
            raise ValueError(f"{simple} must have 'date' and 'close' columns")
        df = df[[cols["date"], cols["close"]]].dropna()
        # Fresh parquet stores real dates; CSVs stored strings. Normalise so
        # callers keep getting the 'YYYY-MM-DD' labels they always did.
        dates = df[cols["date"]].astype(str).tolist()
        return dates, df[cols["close"]].to_numpy(float)

    if symbol == "NIFTY":
        # Widest-span NIFTY_*_eod table → daily underlying close.
        eod = sorted(find_tables(CACHE, "NIFTY_*_eod"),
                     key=lambda p: p.stat().st_size, reverse=True)
        if not eod:
            raise FileNotFoundError("no NIFTY_*_eod.{parquet,csv} in data_cache")
        df = read_table(eod[0], usecols=["timestamp", "underlying_price"])
        df["d"] = pd.to_datetime(df["timestamp"]).dt.date
        g = df.groupby("d")["underlying_price"].last()
        return list(g.index), g.to_numpy(float)

    raise FileNotFoundError(
        f"no daily data for {symbol}: expected {simple}. On the HOST (where a "
        f"broker session exists) fetch it, e.g.\n"
        f"    python -m market_data.fetch_index_daily --symbol {symbol}\n"
        f"      # historical_data(<{symbol} index token>, from, to, 'day') -> "
        f"date,close -> {simple}\"\n"
        f"then re-run this gate. (A fresh login must not run while a live runner "
        f"is active — reuse the cached session.)")


def evaluate_oos(prices: np.ndarray, n_train: int, *, kind: str, params: dict,
                 cost: float) -> o.SimResult:
    """Warm the signal up on the full series (filter state at test-start reflects
    train data — legitimate, no look-ahead since params were fit on train only),
    then book trades on the TEST slice only."""
    if kind == "kalman":
        direction = o.kalman_direction(prices, params["filter_params"],
                                       model=params.get("model", 1), mu=params["mu"])
    else:
        direction = o.ma_direction(prices, short=params["short"],
                                   long=params["long"], offset=params["offset"])
    return o.simulate(prices[n_train:], direction[n_train:],
                      stop_ticks=params["stop_ticks"],
                      target_ticks=params["target_ticks"],
                      tick_size=TICK_SIZE, cost_per_unit=cost)


def run_symbol(symbol: str, closes: np.ndarray, *, n_gen: int, l1_lambda: float,
               cost: float, seeds: list[int]) -> dict:
    """Fit + evaluate across MULTIPLE optimizer seeds. CMA-ES on a single
    train/test split overfits, so any one seed's OOS Sharpe is noise (a single
    seed can swing from +0.7 to −10). We report the per-seed distribution and
    base the verdict on the MEDIAN seed — a single-seed pass would be
    cherry-picking (Rule 12)."""
    n = len(closes)
    if n < 60:
        raise ValueError(f"{symbol}: need >= 60 daily bars, got {n}")
    n_train = n // 2
    train = closes[:n_train]

    kal_tests, ma_tests, wins, kal_trades_total = [], [], 0, 0
    for seed in seeds:
        kal = o.fit_kalman_trend(train, model=1, tick_size=TICK_SIZE,
                                 cost_per_unit=cost, l1_lambda=l1_lambda,
                                 n_gen=n_gen, seed=seed)
        ma = o.fit_ma_crossover(train, tick_size=TICK_SIZE, cost_per_unit=cost,
                                n_gen=n_gen, seed=seed)
        kr = evaluate_oos(closes, n_train, kind="kalman", params=kal, cost=cost)
        mr = evaluate_oos(closes, n_train, kind="ma", params=ma, cost=cost)
        kal_tests.append(kr.sharpe)
        ma_tests.append(mr.sharpe)
        kal_trades_total += kr.n_trades
        wins += int(o.beats(kr.sharpe, mr.sharpe))   # requires kal traded + strict
    kal_med = (float(np.nanmedian(kal_tests))
               if np.any(np.isfinite(kal_tests)) else float("nan"))
    ma_med = (float(np.nanmedian(ma_tests))
              if np.any(np.isfinite(ma_tests)) else float("nan"))
    win_rate = wins / len(seeds)
    passed = o.verdict_passed(kal_med, ma_med, kal_trades_total, win_rate)
    return {
        "symbol": symbol, "n_bars": n, "n_seeds": len(seeds),
        "kal_test_median": kal_med, "ma_test_median": ma_med,
        "kal_test_min": float(np.nanmin(kal_tests)) if any(np.isfinite(kal_tests)) else float("nan"),
        "kal_test_max": float(np.nanmax(kal_tests)) if any(np.isfinite(kal_tests)) else float("nan"),
        "kal_trades": kal_trades_total,
        "win_rate": win_rate,
        "passed": bool(passed),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="NIFTY,BANKNIFTY")
    ap.add_argument("--csv", default=None, help="date,close CSV (overrides --symbols)")
    ap.add_argument("--n-gen", type=int, default=100)
    ap.add_argument("--l1-lambda", type=float, default=0.1)
    ap.add_argument("--cost", type=float, default=2.5,
                    help="per-side cost in index points (round trip = 2x)")
    ap.add_argument("--seeds", type=int, default=5,
                    help="number of optimizer seeds to aggregate (0..N-1)")
    args = ap.parse_args()
    seeds = list(range(args.seeds))

    if args.csv:
        df = read_table(args.csv)
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
        try:
            r = run_symbol(sym, closes, n_gen=args.n_gen, l1_lambda=args.l1_lambda,
                           cost=args.cost, seeds=seeds)
        except ValueError as e:
            # e.g. a too-short --csv (run_symbol needs >= 60 bars). Exit cleanly
            # with the remediation, not an uncaught traceback.
            print(f"\nUNUSABLE DATA for {sym}: {e}\n", file=sys.stderr)
            return 2
        results.append(r)
        all_pass &= r["passed"]

    print(f"\nKalman trend-following correctness gate (paper §6 claim: optimized "
          f"Kalman ≥ MA crossover, out of sample) — {len(seeds)} seeds\n")
    hdr = (f"{'symbol':<10}{'bars':>6}{'kalOOS_med':>11}{'maOOS_med':>11}"
           f"{'kalOOS_min':>11}{'kalOOS_max':>11}{'win_rate':>10}{'  verdict'}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['symbol']:<10}{r['n_bars']:>6}{r['kal_test_median']:>11.2f}"
              f"{r['ma_test_median']:>11.2f}{r['kal_test_min']:>11.2f}"
              f"{r['kal_test_max']:>11.2f}{r['win_rate']:>10.0%}"
              f"{'   PASS' if r['passed'] else '   FAIL'}")
    print()
    if all_pass:
        print("GATE PASSED — Kalman's MEDIAN OOS Sharpe beats the MA baseline on "
              "all symbols.\n")
        return 0
    print("GATE FAILED — Kalman's median OOS Sharpe did NOT beat the MA baseline "
          "on at least one symbol. Do not promote past Phase 0 (Rule 12).\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
