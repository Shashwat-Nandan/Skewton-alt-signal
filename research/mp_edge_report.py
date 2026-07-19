#!/usr/bin/env python3
"""Measure whether Market-Profile buckets predict forward returns — the Phase-2
go/no-go gate.

Reads the ``mp_features`` rows written by ``scripts/log_mp_features.py``, joins each
(instrument, day) to its forward outcome, and prints hit-rate + mean forward
return bucketed by ``open_type`` / ``day_shape`` / ``balance_state``. It also
prints a **directional-edge** table: each bucket carries an implied direction
(``*_up`` = long, ``*_down`` = short, ``higher`` = long, ``lower`` = short), and
we report the mean return *in that direction* — i.e. would trading the bucket
its implied way have paid, gross and net of a supplied per-trade cost.

Two horizons:
  - ``same_day`` = (close - open) / open. The open type is knowable early in the
    session, so same-day follow-through is a legitimate, tradeable outcome.
  - ``next_day`` = (next_close - close) / close. Does today's finished profile
    predict tomorrow.

Nothing here trades. This is the evidence a standalone MP strategy (Phase 3)
must clear before it is built (Rule 12: the live pair runner is the book's only
current earner — we do not add an unmeasured bleeder).
"""
from __future__ import annotations

import argparse

import pandas as pd

# Implied trade direction per bucket value (+1 long / -1 short / 0 stand-aside).
_OPEN_DIR = {
    "open_drive_up": 1, "open_test_drive_up": 1, "open_rejection_reverse_up": 1,
    "open_drive_down": -1, "open_test_drive_down": -1,
    "open_rejection_reverse_down": -1, "open_auction": 0,
}
_SHAPE_DIR = {"trend_up": 1, "trend_down": -1, "neutral": 0, "normal": 0,
              "p_shape": 0, "b_shape": 0}
_BALANCE_DIR = {"higher": 1, "lower": -1, "overlapping_higher": 1,
                "overlapping_lower": -1, "inside": 0, "outside": 0, "unknown": 0}


def load_features(db_path: str, source: str) -> pd.DataFrame:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT instrument, day, open_type, day_shape, balance_state, "
            "open, close FROM mp_features WHERE source = ? ORDER BY instrument, day",
            conn, params=(source,),
        )
    finally:
        conn.close()
    return df


def add_forward_returns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["same_day"] = (df["close"] - df["open"]) / df["open"]
    # next_day close-to-close, per instrument (no leakage across symbols).
    df["next_close"] = df.groupby("instrument")["close"].shift(-1)
    df["next_day"] = (df["next_close"] - df["close"]) / df["close"]
    return df


def _bucket_table(df: pd.DataFrame, col: str, ret: str, min_count: int) -> pd.DataFrame:
    g = df.dropna(subset=[ret]).groupby(col)[ret]
    out = pd.DataFrame({
        "n": g.size(),
        "hit_rate_%": (g.apply(lambda s: (s > 0).mean()) * 100),
        "mean_bps": g.mean() * 1e4,
        "median_bps": g.median() * 1e4,
    })
    out = out[out["n"] >= min_count].sort_values("mean_bps", ascending=False)
    return out.round(1)


def _directional_table(df: pd.DataFrame, col: str, dir_map: dict, ret: str,
                       min_count: int, cost_bps: float,
                       drift_bps: float = 0.0) -> pd.DataFrame:
    d = df.dropna(subset=[ret]).copy()
    d["dir"] = d[col].map(dir_map).fillna(0)
    d = d[d["dir"] != 0]
    d["signed_bps"] = d["dir"] * d[ret] * 1e4
    # Direction is constant within a bucket value, so the drift a naive
    # dir-holder earns for free is dir*drift. `vs_drift_bps` strips it out:
    # this is the honest "is there Market-Profile skill beyond beta" number.
    dir_by_group = d.groupby(col)["dir"].first()
    g = d.groupby(col)["signed_bps"]
    gross = g.mean()
    out = pd.DataFrame({
        "n": g.size(),
        "win_rate_%": g.apply(lambda s: (s > 0).mean()) * 100,
        "gross_bps": gross,
        "net_bps": gross - cost_bps,
        "vs_drift_bps": gross - dir_by_group * drift_bps,
    })
    out = out[out["n"] >= min_count].sort_values("vs_drift_bps", ascending=False)
    return out.round(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data_cache/dashboard.db")
    ap.add_argument("--source", default="intraday_30m",
                    help="mp_features.source to analyze (default: intraday_30m)")
    ap.add_argument("--min-count", type=int, default=30,
                    help="suppress buckets with fewer rows (default: 30)")
    ap.add_argument("--cost-bps", type=float, default=0.0,
                    help="round-trip cost in bps subtracted from directional "
                         "gross (default: 0 = gross). Indian equity intraday "
                         "round-trip is ~10-20 bps — pass it to see net edge.")
    args = ap.parse_args()

    df = load_features(args.db, args.source)
    if df.empty:
        print(f"No mp_features rows for source={args.source!r} in {args.db}.")
        print("Run scripts/log_mp_features.py first.")
        return
    df = add_forward_returns(df)

    n_instr = df["instrument"].nunique()
    # Drift baseline = mean next-day return across ALL instrument-days. Being
    # long earns this for free; a long-biased bucket must BEAT it to have skill.
    drift_bps = df["next_day"].dropna().mean() * 1e4
    print(f"\nMarket-Profile edge report — source={args.source}")
    print(f"  {len(df)} instrument-days, {n_instr} instruments, "
          f"days {df['day'].min()}..{df['day'].max()}")
    print(f"  min bucket count = {args.min_count}, cost = {args.cost_bps} bps")
    print(f"  next-day DRIFT baseline (always-long) = {drift_bps:.1f} bps  "
          f"→ 'vs_drift_bps' below strips this out")

    # ── PREDICTIVE horizon: next_day is the honest test (today's finished
    # profile → tomorrow's return; no overlap with the label).
    print(f"\n{'#' * 68}\n# PREDICTIVE — next_day  (the honest go/no-go horizon)\n{'#' * 68}")
    for col in ("open_type", "day_shape", "balance_state"):
        print(f"\n-- raw return by {col} --")
        print(_bucket_table(df, col, "next_day", args.min_count).to_string())
    print("\n-- DIRECTIONAL edge (trade each bucket its implied way) [next_day] --")
    for col, dmap in (("open_type", _OPEN_DIR), ("day_shape", _SHAPE_DIR),
                      ("balance_state", _BALANCE_DIR)):
        t = _directional_table(df, col, dmap, "next_day", args.min_count,
                               args.cost_bps, drift_bps)
        if not t.empty:
            print(f"\n  [{col}]")
            print(t.to_string())

    # ── DESCRIPTIVE horizon: same_day is CONTAMINATED — the classifiers consume
    # `close`, and same_day = (close-open)/open, so *_up buckets are ~100%
    # positive BY CONSTRUCTION. Detect and flag it so no reader mistakes the
    # tautology for an edge (Rule 12).
    same = _directional_table(df, "open_type", _OPEN_DIR, "same_day",
                              args.min_count, args.cost_bps)
    leaked = (not same.empty) and (same["win_rate_%"].max() >= 99.0)
    print(f"\n{'#' * 68}\n# DESCRIPTIVE — same_day  (CONTAMINATED — not an edge)\n{'#' * 68}")
    if leaked:
        print("  LEAKAGE: open_type win_rate hits ~100% because the classifier\n"
              "  reads `close` and same_day = (close-open)/open. These numbers\n"
              "  only confirm the labels are internally consistent; they say\n"
              "  NOTHING about predictive edge. A tradeable same-day signal would\n"
              "  need the open type fixed from the FIRST K periods and the return\n"
              "  measured from an IB-close entry — a separate build, justified\n"
              "  only if next_day shows something worth chasing.")
    for col in ("open_type", "day_shape", "balance_state"):
        print(f"\n-- raw return by {col} [same_day, descriptive] --")
        print(_bucket_table(df, col, "same_day", args.min_count).to_string())

    print("\n" + "=" * 68)
    print("READ: judge Phase-3 ONLY on PREDICTIVE (next_day). A bucket needs")
    print("net_bps > 0 (survives cost) AND vs_drift_bps > 0 (beats just holding")
    print("the market direction — else it is beta, not Market-Profile skill).")
    print("Ignore same_day: it is contaminated by label construction (Rule 12).")
    print("=" * 68)


if __name__ == "__main__":
    main()
