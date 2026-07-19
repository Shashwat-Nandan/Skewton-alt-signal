"""
RV/IV Regime Analyzer (bhavcopy EOD)
====================================
Takes a bhavcopy-built EOD CSV (one tick per instrument per trading day) and
produces a per-day regime table: spot, ATM IV, rolling realized vol, RV/IV
ratio. Reports ratio distribution, gate-admission rates for a grid of
thresholds, and flags notable regime transitions.

Usage:
  python -m research.analyze_rv_iv_regime --data data_cache/NIFTY_20251019_20260420_eod_nearest.csv
  python -m research.analyze_rv_iv_regime --data <csv> --rv-windows 3,5,10,20
"""

import argparse
import math
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

from core.data_cache_io import read_table


def compute_daily_atm_iv(df: pd.DataFrame, min_tte_days: int = 2) -> pd.DataFrame:
    """
    For each trading day pick the nearest-expiry-with-TTE>=min_tte, then the
    strike closest to spot, and average CE+PE IV there.
    """
    opt = df[df["option_type"].isin(("CE", "PE"))].copy()
    opt["ts_date"] = pd.to_datetime(opt["timestamp"]).dt.date
    opt["expiry_date"] = pd.to_datetime(opt["expiry"]).dt.date
    opt["tte_days"] = (opt["expiry_date"] - opt["ts_date"]).apply(lambda d: d.days)
    opt = opt[opt["tte_days"] >= min_tte_days]
    opt = opt[opt["iv"] > 0]

    rows = []
    for day, day_df in opt.groupby("ts_date"):
        spot = float(day_df["underlying_price"].iloc[0])
        # nearest expiry with TTE>=min_tte
        near_exp = day_df["expiry_date"].min()
        near_df = day_df[day_df["expiry_date"] == near_exp]
        # ATM strike = closest to spot
        atm_strike = near_df["strike"].iloc[(near_df["strike"] - spot).abs().argsort()[:1]].iloc[0]
        atm_df = near_df[near_df["strike"] == atm_strike]
        ce = atm_df[atm_df["option_type"] == "CE"]["iv"]
        pe = atm_df[atm_df["option_type"] == "PE"]["iv"]
        if ce.empty and pe.empty:
            continue
        iv = float(pd.concat([ce, pe]).mean())
        rows.append({
            "date": pd.Timestamp(day),
            "spot": spot,
            "atm_strike": float(atm_strike),
            "atm_iv": iv,
            "tte_days": int((near_exp - day).days),
        })
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def add_rolling_rv(reg: pd.DataFrame, windows: List[int]) -> pd.DataFrame:
    """Add annualized rolling realized vol columns rv_{w} for each window (trading days)."""
    reg = reg.copy()
    reg["log_ret"] = np.log(reg["spot"] / reg["spot"].shift(1))
    for w in windows:
        # Annualize with 252 trading days; sample std over w-day log returns.
        reg[f"rv_{w}"] = reg["log_ret"].rolling(w).std() * math.sqrt(252)
        reg[f"ratio_{w}"] = reg[f"rv_{w}"] / reg["atm_iv"]
    return reg


def print_header(msg: str) -> None:
    print("\n" + "=" * 70)
    print(msg)
    print("=" * 70)


def describe_ratio(reg: pd.DataFrame, w: int) -> None:
    col = f"ratio_{w}"
    s = reg[col].dropna()
    if s.empty:
        print(f"  rv_{w}: no data")
        return
    print(f"  rv_{w} ({len(s)} days):")
    print(f"    mean     {s.mean():.3f}")
    print(f"    median   {s.median():.3f}")
    print(f"    std      {s.std():.3f}")
    print(f"    q10/q90  {s.quantile(0.1):.3f} / {s.quantile(0.9):.3f}")
    print(f"    min/max  {s.min():.3f} / {s.max():.3f}")


def admission_table(reg: pd.DataFrame, windows: List[int], thresholds: List[float]) -> None:
    print_header("Gate admission rates (% of days with ratio >= threshold)")
    header = f"{'thresh':>7}" + "".join(f"{'rv_' + str(w):>10}" for w in windows)
    print(header)
    print("-" * len(header))
    for t in thresholds:
        row = f"{t:>7.2f}"
        for w in windows:
            col = f"ratio_{w}"
            s = reg[col].dropna()
            pct = (s >= t).mean() * 100 if len(s) else 0.0
            row += f"{pct:>9.1f}%"
        print(row)


def regime_transitions(reg: pd.DataFrame, w: int, threshold: float = 1.0) -> None:
    """Show when the ratio crosses a threshold (regime flips)."""
    col = f"ratio_{w}"
    s = reg[[col, "date", "spot", "atm_iv", f"rv_{w}"]].dropna().copy()
    if s.empty:
        return
    s["regime"] = (s[col] >= threshold).astype(int)
    s["flip"] = s["regime"].diff().fillna(0).abs().astype(int)
    flips = s[s["flip"] == 1]
    print_header(f"Regime transitions (ratio_{w} crosses {threshold})")
    print(f"  {len(flips)} crossings across {len(s)} days")
    if not flips.empty:
        cols = ["date", "spot", "atm_iv", f"rv_{w}", col, "regime"]
        print(flips[cols].to_string(index=False,
                                    formatters={"spot": "{:,.1f}".format,
                                                "atm_iv": "{:.3f}".format,
                                                f"rv_{w}": "{:.3f}".format,
                                                col: "{:.3f}".format}))


def monthly_breakdown(reg: pd.DataFrame, w: int) -> None:
    col = f"ratio_{w}"
    s = reg[["date", col, "atm_iv", f"rv_{w}"]].dropna().copy()
    if s.empty:
        return
    s["month"] = s["date"].dt.to_period("M")
    print_header(f"Monthly summary (rv_{w})")
    grp = s.groupby("month").agg(
        days=("date", "count"),
        mean_ratio=(col, "mean"),
        median_ratio=(col, "median"),
        mean_rv=(f"rv_{w}", "mean"),
        mean_iv=("atm_iv", "mean"),
        pct_above_1=(col, lambda x: (x >= 1.0).mean() * 100),
    )
    print(grp.to_string(
        formatters={
            "mean_ratio": "{:.3f}".format,
            "median_ratio": "{:.3f}".format,
            "mean_rv": "{:.3f}".format,
            "mean_iv": "{:.3f}".format,
            "pct_above_1": "{:.1f}%".format,
        }
    ))


def main():
    p = argparse.ArgumentParser(description="RV/IV regime analyzer for bhavcopy EOD corpus")
    p.add_argument("--data", type=str, required=True, help="Bhavcopy EOD CSV")
    p.add_argument("--rv-windows", type=str, default="5,10,20",
                   help="Comma-separated RV windows in trading days (default 5,10,20)")
    p.add_argument("--thresholds", type=str, default="0.6,0.8,1.0,1.2,1.4,1.6",
                   help="Comma-separated admission thresholds")
    p.add_argument("--output", type=str, default=None, help="Per-day CSV output (optional)")
    args = p.parse_args()

    windows = [int(x) for x in args.rv_windows.split(",")]
    thresholds = [float(x) for x in args.thresholds.split(",")]

    df = read_table(args.data, parse_dates=["timestamp"])
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = df["timestamp"].dt.tz_localize(None)

    print(f"Loaded {len(df):,} rows from {args.data}")
    reg = compute_daily_atm_iv(df)
    print(f"  Days:      {len(reg)}")
    print(f"  Range:     {reg['date'].min().date()} → {reg['date'].max().date()}")
    print(f"  Spot:      {reg['spot'].min():,.0f} → {reg['spot'].max():,.0f}")
    print(f"  ATM IV:    mean {reg['atm_iv'].mean():.3f}, "
          f"q10/q90 {reg['atm_iv'].quantile(0.1):.3f}/{reg['atm_iv'].quantile(0.9):.3f}")

    reg = add_rolling_rv(reg, windows)

    print_header("RV/IV ratio distribution")
    for w in windows:
        describe_ratio(reg, w)

    admission_table(reg, windows, thresholds)

    monthly_breakdown(reg, windows[0] if windows else 5)

    # Use the shortest RV window for regime transitions (responsive)
    regime_transitions(reg, windows[0], threshold=1.0)

    if args.output:
        out = Path(args.output)
    else:
        stem = Path(args.data).stem
        out = Path("data_cache") / f"regime_{stem}.csv"
    cols = ["date", "spot", "atm_strike", "tte_days", "atm_iv", "log_ret"]
    cols += [f"rv_{w}" for w in windows] + [f"ratio_{w}" for w in windows]
    reg[cols].to_csv(out, index=False)
    print(f"\nPer-day regime table saved to {out}")


if __name__ == "__main__":
    main()
