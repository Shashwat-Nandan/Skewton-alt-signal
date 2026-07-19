"""
Variance-swap-style P&L test for the RV/IV gate.

A daily delta-hedged ATM straddle P&L is approximately

    dP  =  0.5 * dollar_gamma * (r_t^2  -  sigma_imp^2 * dt)

where dt = 1/252 and sigma_imp is today's ATM IV. Summed over days, this is
the classic "gamma rental" P&L that a variance-swap long earns. It sidesteps
the broken intraday scalp mechanics (which require intraday data we do not
have from bhav copy) and measures only the signal quality of the RV/IV gate.

For each day t (with return realized over [t, t+1]) we compute:
    v_t  =  r_{t+1}^2  -  sigma_t^2 / 252   (P&L per unit dollar-gamma)

Then test several "strategies" built on the gate:
  * always-long        : take every day's  +v_t
  * always-short       : take every day's  -v_t
  * gated-long(T, w)   : +v_t when ratio_w[t] >= T, else flat
  * gated-short(T, w)  : -v_t when ratio_w[t] <  T, else flat
  * two-sided(T, w)    : +v_t when ratio_w >= T, -v_t when ratio_w < T

Values are reported in units of (return)^2 per unit dollar-gamma. To convert
to dollars: multiply by 0.5 * dollar_gamma of the straddle you'd actually
trade. For an ATM straddle, dollar_gamma = 0.7979 * S / (sigma * sqrt(T)).
Usage:
  python -m core.variance_pnl_gate --regime data_cache/regime_<stem>.csv
"""

import argparse
import math
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd


def sharpe(x: pd.Series) -> float:
    """Naive annualized Sharpe for a daily-P&L series."""
    x = x.dropna()
    if len(x) < 2 or x.std(ddof=0) == 0:
        return 0.0
    return float(x.mean() / x.std(ddof=0) * math.sqrt(252))


def summarize(name: str, pnl: pd.Series, out_rows: list) -> None:
    active = pnl[pnl != 0]
    total = float(pnl.sum())
    n = int((pnl != 0).sum())
    mean = float(active.mean()) if n else 0.0
    hit = float((active > 0).mean() * 100) if n else 0.0
    sh = sharpe(pnl)
    out_rows.append({
        "strategy": name,
        "n_days": n,
        "total_v": total,
        "mean_v": mean,
        "hit_rate_pct": hit,
        "sharpe": sh,
    })


def run_strategies(reg: pd.DataFrame, windows: List[int], thresholds: List[float]) -> pd.DataFrame:
    reg = reg.sort_values("date").reset_index(drop=True).copy()
    reg["ret_next"] = np.log(reg["spot"].shift(-1) / reg["spot"])
    # variance P&L contribution per day (per unit dollar gamma)
    reg["v_t"] = reg["ret_next"] ** 2 - (reg["atm_iv"] ** 2) / 252.0

    rows: list = []
    summarize("always_long", reg["v_t"].fillna(0), rows)
    summarize("always_short", (-reg["v_t"]).fillna(0), rows)

    for w in windows:
        col = f"ratio_{w}"
        if col not in reg.columns:
            continue
        for t in thresholds:
            mask_hi = reg[col] >= t
            mask_lo = reg[col] < t
            long_only = reg["v_t"].where(mask_hi, 0).fillna(0)
            short_only = (-reg["v_t"]).where(mask_lo, 0).fillna(0)
            two_sided = reg["v_t"].where(mask_hi, -reg["v_t"]).fillna(0)
            # zero-out two_sided where ratio is NaN (no signal yet)
            two_sided = two_sided.where(reg[col].notna(), 0)
            summarize(f"gated_long rv_{w}>={t}", long_only, rows)
            summarize(f"gated_short rv_{w}<{t}", short_only, rows)
            summarize(f"two_sided rv_{w}@{t}", two_sided, rows)

    return pd.DataFrame(rows)


def main():
    p = argparse.ArgumentParser(description="Variance-swap-style P&L test for RV/IV gate")
    p.add_argument("--regime", required=True, help="Per-day regime CSV from research/analyze_rv_iv_regime.py")
    p.add_argument("--windows", default="5,10,20", help="RV windows (must match regime CSV)")
    p.add_argument("--thresholds", default="0.8,1.0,1.2,1.4,1.6", help="Gate thresholds")
    p.add_argument("--notional", type=float, default=0.0,
                   help="If >0, scale v_t by 0.5 * notional to show approximate dollar P&L per rupee of ATM spot-squared gamma exposure. Purely illustrative.")
    args = p.parse_args()

    windows = [int(x) for x in args.windows.split(",")]
    thresholds = [float(x) for x in args.thresholds.split(",")]

    reg = pd.read_csv(args.regime, parse_dates=["date"])
    needed = {"date", "spot", "atm_iv"} | {f"ratio_{w}" for w in windows}
    missing = needed - set(reg.columns)
    if missing:
        raise SystemExit(f"regime CSV missing columns: {missing}")

    print(f"Loaded {len(reg)} days from {args.regime}")
    print(f"  Range: {reg['date'].min().date()} → {reg['date'].max().date()}")

    result = run_strategies(reg, windows, thresholds)

    # Print formatted
    scale = 0.5 * args.notional if args.notional > 0 else 1.0
    unit = "INR" if args.notional > 0 else "v units"
    print(f"\nP&L unit: {unit} (scale={scale})")
    print()
    hdr = f"{'strategy':<32} {'days':>5} {'total':>14} {'mean':>14} {'hit%':>7} {'sharpe':>8}"
    print(hdr)
    print("-" * len(hdr))
    for _, r in result.iterrows():
        print(
            f"{r['strategy']:<32} {int(r['n_days']):>5} "
            f"{r['total_v']*scale:>14,.4f} {r['mean_v']*scale:>14,.6f} "
            f"{r['hit_rate_pct']:>6.1f}% {r['sharpe']:>8.2f}"
        )

    # Highlight best & worst
    print("\nTop 5 strategies by total P&L:")
    print(result.sort_values("total_v", ascending=False).head(5).to_string(index=False))
    print("\nBottom 5:")
    print(result.sort_values("total_v").head(5).to_string(index=False))

    out = Path(args.regime).with_name("variance_pnl_" + Path(args.regime).name)
    result.to_csv(out, index=False)
    print(f"\nSaved strategy table to {out}")


if __name__ == "__main__":
    main()
