#!/usr/bin/env python3
"""
Walk-forward backtest for the Kalman trend follower (Option B — robustness).
===========================================================================
The single 6mo/6mo split of `research/validate_kalman_trend.py` overfits: train Sharpe
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
    python -m research.backtest_kalman_trend --symbols NIFTY,BANKNIFTY
    python -m research.backtest_kalman_trend --csv data_cache/FOO_daily.csv
"""
from __future__ import annotations

import argparse
import sys

import numpy as np

from research import optimize_kalman_trend as o
from research.validate_kalman_trend import TICK_SIZE, load_daily_closes


def _nan_median(xs) -> float:
    """Median ignoring NaN; NaN if every entry is NaN (no all-NaN warning)."""
    xs = np.asarray(xs, float)
    return float(np.nanmedian(xs)) if np.any(np.isfinite(xs)) else float("nan")


def _pooled_sharpe(pnl: np.ndarray) -> float:
    """Annualized Sharpe of a pooled daily-P&L series, NaN when undefined (no
    variation, e.g. nothing traded) — same contract as optimize._sharpe so the
    two harnesses agree on the degenerate case instead of one saying 0.0 and the
    other -10.0."""
    sd = float(np.std(pnl))
    return float(np.mean(pnl) / sd * np.sqrt(o.TRADING_DAYS)) if sd > 0 else float("nan")


def _fold_oos(closes, a, b, c, *, kind, params, cost, session_ends=None,
              ohlc=None) -> o.SimResult:
    """OOS SimResult on the test slice [b:c]; the signal is warmed up from the
    train start a (params were fit on [a:b] only — causal, no look-ahead).
    Returns the full SimResult (daily P&L + n_trades) so the caller can both
    pool the P&L and require a real trade count for the verdict.

    `session_ends` (intraday runs only) is sliced to the test window so the OOS
    evaluation models the runner's daily flatten (#121)."""
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
                      cost_per_unit=cost,
                      session_ends=None if session_ends is None else session_ends[b:c],
                      **o._ohlc_kwargs(ohlc, b, c))


def walk_forward(symbol: str, closes: np.ndarray, *, train_len: int, test_len: int,
                 step: int, seeds: list[int], n_gen: int, cost: float,
                 session_ends=None, ohlc=None) -> dict:
    n = len(closes)
    starts = list(range(0, n - train_len - test_len + 1, step))
    if not starts:
        raise ValueError(f"{symbol}: {n} bars too short for train {train_len} + "
                         f"test {test_len}")
    # Per-SEED pooled Sharpe, then median over seeds (NOT an element-wise average
    # of seed P&L streams — averaging ~uncorrelated streams shrinks std ~1/√N and
    # inflates Sharpe ~√N, asymmetrically between the books). Mirrors the
    # per-seed-then-median basis validate_kalman_trend uses.
    seed_kal_sharpe, seed_ma_sharpe = [], []
    fold_wins = n_pairs = kal_trades_total = seeds_traded = 0
    kal_pnl_total = ma_pnl_total = 0.0
    for seed in seeds:
        kal_pnls, ma_pnls = [], []
        seed_kal_trades = 0
        for a in starts:
            b, c = a + train_len, a + train_len + test_len
            train = closes[a:b]
            train_ends = None if session_ends is None else session_ends[a:b]
            # The FIT gets the same fill model as the EVAL (o._ohlc_kwargs slices
            # all three arrays together). Fitting close-only while evaluating with
            # OHLC would be a fresh #121-class fit/deploy mismatch.
            train_ohlc = o._ohlc_kwargs(ohlc, a, b)
            # #125: intraday (a flatten is in force) -> the target cannot bind, so
            # do not fit it. The DAILY gates keep it (a bar IS a day; holds run
            # for days and the target genuinely binds).
            fit_tgt = session_ends is None
            kp = o.fit_kalman_reduced(train, tick_size=TICK_SIZE, cost_per_unit=cost,
                                      n_gen=n_gen, seed=seed, session_ends=train_ends,
                                      fit_target=fit_tgt, **train_ohlc)
            mp = o.fit_ma_crossover(train, tick_size=TICK_SIZE, cost_per_unit=cost,
                                    n_gen=n_gen, seed=seed, session_ends=train_ends,
                                    fit_target=fit_tgt, **train_ohlc)
            kr = _fold_oos(closes, a, b, c, kind="kalman", params=kp, cost=cost,
                           session_ends=session_ends, ohlc=ohlc)
            mr = _fold_oos(closes, a, b, c, kind="ma", params=mp, cost=cost,
                           session_ends=session_ends, ohlc=ohlc)
            kal_pnls.append(kr.daily_pnl)
            ma_pnls.append(mr.daily_pnl)
            ks, ms = float(kr.daily_pnl.sum()), float(mr.daily_pnl.sum())
            # A fold-win requires Kalman to have ACTUALLY TRADED and strictly
            # out-P&L'd MA. Without the n_trades guard a no-trade Kalman (sum 0)
            # "wins" any fold where MA merely lost (0 > negative) — that loophole
            # let a mostly-inert Kalman pass the gate.
            fold_wins += int(kr.n_trades > 0 and ks > ms)
            n_pairs += 1
            kal_trades_total += kr.n_trades
            seed_kal_trades += kr.n_trades
            kal_pnl_total += ks
            ma_pnl_total += ms
        seed_kal_sharpe.append(_pooled_sharpe(np.concatenate(kal_pnls)))
        seed_ma_sharpe.append(_pooled_sharpe(np.concatenate(ma_pnls)))
        seeds_traded += int(seed_kal_trades > 0)

    kal_sharpe = _nan_median(seed_kal_sharpe)
    ma_sharpe = _nan_median(seed_ma_sharpe)
    fold_win_rate = fold_wins / n_pairs
    # Shared GO/NO-GO policy, PLUS a guard that a MAJORITY of seeds actually
    # traded — else _nan_median would silently report the one trading seed's
    # Sharpe for a strategy inert on the rest.
    passed = (o.verdict_passed(kal_sharpe, ma_sharpe, kal_trades_total, fold_win_rate)
              and seeds_traded > len(seeds) / 2)
    return {
        "symbol": symbol, "n_bars": n, "n_folds": len(starts),
        "oos_days": len(starts) * test_len,
        "kal_pooled_sharpe": kal_sharpe, "ma_pooled_sharpe": ma_sharpe,
        # mean per-seed OOS P&L (matches the per-seed Sharpe basis, not a summed
        # total — so it's the typical single strategy's P&L, not N strategies').
        "kal_pnl_per_seed": round(kal_pnl_total / len(seeds), 2),
        "ma_pnl_per_seed": round(ma_pnl_total / len(seeds), 2),
        "kal_trades": kal_trades_total, "seeds_traded": seeds_traded,
        "fold_win_rate": fold_win_rate,
        "passed": bool(passed),
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

    # Timeframe (issue #63): default multi-symbol run is daily closes. A --csv
    # naming a 5-min series (e.g. NIFTY_5minute.csv) is already compliant, so
    # only warn when the source is daily. Infer from the FILENAME STEM (not the
    # whole path — a "minute_bars/" dir must not silence the warning for a daily
    # file) and require an intraday token while excluding daily/eod, so the guard
    # fails safe (over-warn) rather than silently passing a daily run as 5-min.
    from pathlib import Path
    from core.backtest_timeframe import warn_coarse_timeframe
    _stem = Path(args.csv).stem.lower() if args.csv else ""
    _is_5min = (("5min" in _stem or "1min" in _stem)
                and not ("daily" in _stem or "eod" in _stem))
    _tf = "5min" if _is_5min else "daily"
    warn_coarse_timeframe(_tf, backtest="backtest_kalman_trend",
                          reason="daily-close trend follower — only the index "
                          "sleeve has 5-min history; pass --csv "
                          "data_cache/NIFTY_5minute.csv for a 5-min run (issue #63)")

    if args.csv:
        from pathlib import Path
        from core.data_cache_io import read_table
        df = read_table(args.csv)
        cols = {c.lower(): c for c in df.columns}
        # Build the session mask so the fit models the runner's 15:25 flatten
        # (#121). Decide from the DATA, not the filename: >1 bar per calendar day
        # means intraday. A filename heuristic would silently un-fix #121 the
        # moment a file is named something else (e.g. the OHLC re-fetch #122
        # requires) — the timestamps needed to do this correctly are right here.
        # A daily table has exactly 1 bar/day -> None (holding across bars IS the
        # daily strategy).
        ts_col = next((cols[k] for k in ("datetime", "timestamp", "date") if k in cols), None)
        ends = ohlc = None
        if ts_col is not None:
            import pandas as _pd
            _ts = _pd.to_datetime(df[ts_col])
            _n_days = _ts.dt.date.nunique()
            if len(_ts) > _n_days:                       # intraday: many bars/day
                ends = o.session_ends_from_timestamps(_ts.tolist())
                print(f"intraday source detected ({len(_ts)} bars over {_n_days} "
                      f"days) — modelling the 15:25 session flatten (#121)")
                if {"open", "high", "low"} <= set(cols):
                    ohlc = o.OHLC.from_frame(df)
                    print("OHLC present — honest touch/gap fills (#122)")
                else:
                    print("WARNING: close-only tape — fills book at the stop LEVEL, "
                          "which flatters tight stops. Re-fetch for OHLC (#122).",
                          file=sys.stderr)
        if ends is None and _is_5min:
            # Name says intraday but the data disagrees (or carries no usable
            # timestamp): the fit would silently revert to multi-day holds.
            print("WARNING: --csv looks intraday by name but no per-day session "
                  "structure was found; the 15:25 flatten is NOT modelled (#121). "
                  "Check the timestamp column.", file=sys.stderr)
        jobs = [(Path(args.csv).stem, df[cols["close"]].to_numpy(float), ends, ohlc)]
    else:
        jobs = []
        for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            try:
                jobs.append((sym, load_daily_closes(sym)[1], None, None))  # daily: no flatten/OHLC
            except FileNotFoundError as e:
                print(f"\nDATA MISSING for {sym}:\n{e}\n", file=sys.stderr)
                return 2

    results, all_pass = [], True
    for sym, closes, ends, ohlc in jobs:
        r = walk_forward(sym, closes, train_len=args.train_len, test_len=args.test_len,
                         step=args.step, seeds=seeds, n_gen=args.n_gen, cost=args.cost,
                         session_ends=ends, ohlc=ohlc)
        results.append(r)
        all_pass &= r["passed"]

    print(f"\nKalman trend walk-forward (reduced 4-param fit; train {args.train_len}"
          f"/test {args.test_len}/step {args.step}; {args.seeds} seeds; POOLED OOS)\n")
    hdr = (f"{'symbol':<10}{'bars':>6}{'folds':>6}{'oosDays':>8}{'kalOOS_Sh':>10}"
           f"{'maOOS_Sh':>10}{'kalPnl/sd':>10}{'maPnl/sd':>10}{'foldWin':>9}{'  verdict'}")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['symbol']:<10}{r['n_bars']:>6}{r['n_folds']:>6}{r['oos_days']:>8}"
              f"{r['kal_pooled_sharpe']:>10.2f}{r['ma_pooled_sharpe']:>10.2f}"
              f"{r['kal_pnl_per_seed']:>10.0f}{r['ma_pnl_per_seed']:>10.0f}"
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
