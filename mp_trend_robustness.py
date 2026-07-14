#!/usr/bin/env python3
"""Stress-test one Market-Profile bucket's next-day edge before it can justify a
strategy (Phase-2.5 of the plan).

The edge report flagged `day_shape = trend_up` as the only next-day survivor. It
is broad and beats drift, but tail-driven and single-regime. Before writing any
strategy code we ask the questions that kill fragile edges:

  1. COST BREAKEVEN — at what round-trip cost does the edge hit zero? An
     overnight equity hold is delivery (CNC): STT alone is ~0.1% each side
     (~20 bps round trip) before brokerage/exchange/GST/stamp — so ~25-30 bps
     is realistic, not the 15 bps placeholder.
  2. TAIL SENSITIVITY — does the mean edge survive trimming the extreme moves?
     If dropping the top/bottom 1% collapses it, a few prints carry it.
  3. GAP DECOMPOSITION — how much of the next-day close-to-close is the
     overnight GAP (close→next_open, uncapturable without holding overnight and
     wearing gap risk) vs the next intraday session (next_open→next_close)?
  4. CONCENTRATION — does it survive dropping its best few names?
  5. SIGNIFICANCE — is the win rate distinguishable from a coin, and what is the
     mean's t-stat, on this sample?

Reads `mp_features` (written by log_mp_features.py). Trades nothing.
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import numpy as np
import pandas as pd


def load(db_path: str, source: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT instrument, day, day_shape, open_type, balance_state, "
            "open, close FROM mp_features WHERE source = ? ORDER BY instrument, day",
            conn, params=(source,),
        )
    finally:
        conn.close()
    df["next_open"] = df.groupby("instrument")["open"].shift(-1)
    df["next_close"] = df.groupby("instrument")["close"].shift(-1)
    df["nd"] = (df["next_close"] - df["close"]) / df["close"] * 1e4        # close→next_close
    df["gap"] = (df["next_open"] - df["close"]) / df["close"] * 1e4        # overnight gap
    df["intraday"] = (df["next_close"] - df["next_open"]) / df["next_open"] * 1e4
    return df


def cost_breakeven(x: pd.Series) -> None:
    mean = x.mean()
    print("  cost breakeven:")
    for c in (0, 10, 15, 20, 25, 30, 35):
        print(f"    @ {c:>2} bps → net mean {mean - c:6.1f} bps")
    print(f"    breakeven round-trip cost = {mean:.1f} bps "
          f"(edge is zero once cost reaches the gross mean)")


def tail_sensitivity(x: pd.Series) -> None:
    print("  tail sensitivity (gross mean bps):")
    print(f"    full                    : {x.mean():6.1f}  (median {x.median():6.1f}, n={len(x)})")
    for p in (0.01, 0.05):
        lo, hi = x.quantile(p), x.quantile(1 - p)
        trimmed = x[(x >= lo) & (x <= hi)]
        print(f"    trim {int(p*100):>2}%/{int(p*100)}% tails      : {trimmed.mean():6.1f}  "
              f"(n={len(trimmed)})")
        wins = np.clip(x, lo, hi)
        print(f"    winsorize {int(p*100):>2}%/{int(p*100)}%       : {wins.mean():6.1f}")


def gap_decomposition(sub: pd.DataFrame) -> None:
    g = sub["gap"].dropna()
    it = sub["intraday"].dropna()
    nd = sub["nd"].dropna()
    print("  gap decomposition (next-day close-to-close):")
    print(f"    overnight gap (close→next_open)   : {g.mean():6.1f} bps "
          f"({100*g.mean()/nd.mean():4.0f}% of total)" if nd.mean() else "")
    print(f"    next intraday (next_open→next_close): {it.mean():6.1f} bps "
          f"({100*it.mean()/nd.mean():4.0f}% of total)" if nd.mean() else "")
    print(f"    total (close→next_close)          : {nd.mean():6.1f} bps")
    print("    → if most sits in the gap, the trade REQUIRES an overnight hold\n"
          "      (gap risk) and can't be captured by a next-open entry.")


def concentration(sub: pd.DataFrame, drop_n: int) -> None:
    per = sub.groupby("instrument")["nd"].mean().sort_values()
    best = per.tail(drop_n).index.tolist()
    kept = sub[~sub["instrument"].isin(best)]
    print("  concentration:")
    print(f"    all names            : {sub['nd'].mean():6.1f} bps (n={len(sub)})")
    print(f"    drop best {drop_n:>2} names   : {kept['nd'].mean():6.1f} bps "
          f"(n={len(kept)})  [dropped {', '.join(best)}]")


def significance(x: pd.Series) -> None:
    n = len(x)
    mean, sd = x.mean(), x.std(ddof=1)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else float("nan")
    win = (x > 0).mean()
    # Normal-approx binomial z for win-rate vs 0.5
    z = (win - 0.5) / math.sqrt(0.25 / n)
    print("  significance:")
    print(f"    mean {mean:.1f} bps, sd {sd:.0f} bps, t-stat {t:.2f} (n={n})")
    print(f"    win rate {100*win:.1f}%  → z vs 50% = {z:.2f}")
    print("    (|t| and |z| ≳ 2 ≈ 5% significance; treat < 2 as noise on this sample)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data_cache/dashboard.db")
    ap.add_argument("--source", default="intraday_30m")
    ap.add_argument("--bucket-col", default="day_shape")
    ap.add_argument("--bucket-val", default="trend_up")
    ap.add_argument("--drop-n", type=int, default=5)
    args = ap.parse_args()

    df = load(args.db, args.source)
    sub = df[df[args.bucket_col] == args.bucket_val].dropna(subset=["nd"]).copy()
    if sub.empty:
        print(f"No rows for {args.bucket_col}={args.bucket_val!r}.")
        return

    base = df["nd"].dropna().mean()
    print(f"\nRobustness — {args.bucket_col}={args.bucket_val}  (source={args.source})")
    print(f"  {len(sub)} bucket-days, {sub['instrument'].nunique()} names, "
          f"days {sub['day'].min()}..{sub['day'].max()}")
    print(f"  always-long drift baseline = {base:.1f} bps  "
          f"→ bucket gross mean = {sub['nd'].mean():.1f} bps\n")

    cost_breakeven(sub["nd"]);      print()
    tail_sensitivity(sub["nd"]);    print()
    gap_decomposition(sub);         print()
    concentration(sub, args.drop_n); print()
    significance(sub["nd"])

    print("\n" + "=" * 68)
    print("NOTE: this sample is ~108 days of ONE macro regime (2026 H1). A")
    print("down-regime test needs a Kite backfill of more history (operator")
    print("step) — no amount of slicing the current window substitutes for it.")
    print("=" * 68)


if __name__ == "__main__":
    main()
