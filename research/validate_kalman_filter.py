#!/usr/bin/env python3
"""
Phase-0 correctness gate for the Kalman pair filter
===================================================
Reproduces the headline result of Palomar Ch.15 §15.6.4 on data with KNOWN
ground truth, and FAILS LOUD (non-zero exit) if the filter is not behaving —
the gate that blocks Phases 1–4 (see tasks/kalman-pair-system-plan.md, success
criterion #1).

What it checks, on a synthetic cointegrated pair (Eq. 15.1) whose true hedge
ratio we control:
  1. γ tracking — the Kalman hedge ratio stays in a tight band around truth
     and (with a mid-series regime change) follows it (Fig. 15.21).
  2. Spread stationarity — the Kalman spread has materially lower variance
     than the static-OLS-β spread (Fig. 15.22).
  3. Cumulative return — a thresholded (s0=1) strategy on the Kalman spread
     beats the same strategy on the static-β spread, ignoring costs
     (Fig. 15.23: the book gets static 0.6 vs Kalman 2.0 / momentum 3.2).

This is deliberately a *filter-quality* gate, not the full costed multi-pair
backtest (that is Phase 2, research/backtest_kalman_pairs.py). Determinism (fixed seed,
no network) keeps it a reliable gate. Pass `--csv FILE` with columns
y1,y2 (prices or log-prices) to run the same diagnostics on a real pair.

Usage:
  python -m research.validate_kalman_filter
  python -m research.validate_kalman_filter --csv data_cache/ewa_ewc.csv
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from strategies.kalman_filter import KalmanPairFilter

TRAIN = 252            # 1y training window for the §15.6.3 heuristic
ZWIN = 126             # ~6-month rolling z-score lookback (book §15.6.4)
S0 = 1.0               # entry threshold (book §15.6.4)


def synthetic_pair(n=2000, seed=42):
    """Cointegrated pair per Eq. (15.1): y2 a random walk, y1 = μ + γ_t·y2 + ε,
    with γ stepping 0.60→0.75 at the midpoint so we also test adaptation."""
    rng = np.random.default_rng(seed)
    y2 = 50.0 + np.cumsum(rng.normal(0, 0.5, n))
    gamma = np.where(np.arange(n) < n // 2, 0.60, 0.75)
    y1 = 4.0 + gamma * y2 + rng.normal(0, 0.6, n)
    return y1, y2, gamma


def rolling_z(spread, win=ZWIN):
    """Causal rolling z-score of a spread series (mean/std over trailing win)."""
    s = pd.Series(spread)
    mean = s.rolling(win, min_periods=win).mean()
    std = s.rolling(win, min_periods=win).std(ddof=0)
    return ((s - mean) / std).to_numpy()


def thresholded_pnl(spread, z, s0=S0):
    """Mean-reversion thresholded strategy (book §15.5/§15.6.4): hold -sign(z)
    units of the spread while |z|>s0, flat once it reverts past 0. Daily P&L is
    yesterday's position times today's spread change. Costs ignored (Fig 15.23).
    Returns the cumulative P&L series."""
    pos = np.zeros_like(spread)
    cur = 0.0
    for t in range(len(spread)):
        zt = z[t]
        if np.isnan(zt):
            cur = 0.0
        elif cur == 0.0:
            if zt > s0:
                cur = -1.0
            elif zt < -s0:
                cur = 1.0
        else:  # in a position — exit when it reverts through the mean
            if (cur < 0 and zt <= 0) or (cur > 0 and zt >= 0):
                cur = 0.0
        pos[t] = cur
    dspread = np.diff(spread, prepend=spread[0])
    pnl = np.concatenate([[0.0], pos[:-1] * dspread[1:]])
    return np.cumsum(pnl)


def kalman_spread(y1, y2, model):
    f = KalmanPairFilter.from_training(y1[:TRAIN], y2[:TRAIN], model=model)
    spread, gamma = np.full(len(y1), np.nan), np.full(len(y1), np.nan)
    for t in range(TRAIN, len(y1)):
        step = f.update(float(y1[t]), float(y2[t]))
        spread[t] = step.spread
        gamma[t] = step.gamma_pred
    return spread, gamma


def static_beta_spread(y1, y2):
    """Incumbent baseline: hedge ratio fixed at the training-window OLS β
    (mirrors our current static pair_trading.py)."""
    X = np.column_stack([np.ones(TRAIN), y2[:TRAIN]])
    beta, *_ = np.linalg.lstsq(X, y1[:TRAIN], rcond=None)
    mu, g = float(beta[0]), float(beta[1])
    spread = np.full(len(y1), np.nan)
    # Normalize by gross leverage 1+|g| (matches KalmanPairFilter) so the
    # static-vs-Kalman spread-variance comparison is on the same basis — signed
    # 1+g would skew the comparison for inversely-cointegrated (g<0) pairs.
    spread[TRAIN:] = (y1[TRAIN:] - g * y2[TRAIN:] - mu) / (1.0 + abs(g))
    return spread, g


def _final(cum):
    return float(cum[~np.isnan(cum)][-1]) if np.any(~np.isnan(cum)) else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="CSV with columns y1,y2 for a real pair")
    args = ap.parse_args()

    if args.csv:
        df = pd.read_csv(args.csv)
        y1, y2 = df["y1"].to_numpy(float), df["y2"].to_numpy(float)
        gamma_true = None
        print(f"Loaded {len(y1)} rows from {args.csv}")
    else:
        y1, y2, gamma_true = synthetic_pair()
        print(f"Synthetic cointegrated pair: {len(y1)} samples, "
              f"true γ steps 0.60→0.75 at midpoint")

    static_sp, static_g = static_beta_spread(y1, y2)
    basic_sp, basic_g = kalman_spread(y1, y2, "basic")
    mom_sp, mom_g = kalman_spread(y1, y2, "momentum")

    # Trim each spread to the post-training valid region before scoring — the
    # warmup is all-NaN and a single 0·NaN term would poison the cumsum.
    def _cum(sp):
        v = sp[TRAIN:]
        return thresholded_pnl(v, rolling_z(v))

    cum = {
        "static-β": _cum(static_sp),
        "Kalman-basic": _cum(basic_sp),
        "Kalman-momentum": _cum(mom_sp),
    }
    valid = slice(TRAIN, None)
    print("\n  method            final γ    γ band        spread var    cum P&L")
    print("  " + "-" * 66)
    for name, g, sp in [("static-β", static_g, static_sp),
                        ("Kalman-basic", basic_g, basic_sp),
                        ("Kalman-momentum", mom_g, mom_sp)]:
        gband = (f"[{np.nanmin(np.atleast_1d(g)):.2f},{np.nanmax(np.atleast_1d(g)):.2f}]"
                 if np.ndim(g) else f"{g:.3f} (fixed)")
        gfin = np.atleast_1d(g)[-1] if np.ndim(g) else g
        print(f"  {name:16s}  {gfin:7.3f}   {gband:12s}  "
              f"{np.nanvar(sp[valid]):10.4f}   {_final(cum[name]):8.3f}")

    # ── Gate assertions (synthetic only — real CSV is for inspection) ──
    if gamma_true is None:
        print("\n[CSV mode] diagnostics printed; no pass/fail gate applied.")
        return 0

    failures = []
    # 1. γ tracking: Kalman γ band brackets both regimes (0.60 and 0.75) and
    #    stays tight; static β cannot (it is frozen at the early regime).
    for name, g in [("Kalman-basic", basic_g), ("Kalman-momentum", mom_g)]:
        gv = g[~np.isnan(g)]
        if not (0.50 < gv.min() and gv.max() < 0.85):
            failures.append(f"{name} γ band [{gv.min():.2f},{gv.max():.2f}] "
                            f"escaped [0.50,0.85]")
        if gv[-100:].mean() < 0.68:  # late regime truth is 0.75
            failures.append(f"{name} did not adapt to the γ step "
                            f"(late mean {gv[-100:].mean():.2f} < 0.68)")
    # 2. Spread stationarity: Kalman variance well below static-β.
    for name, sp in [("Kalman-basic", basic_sp), ("Kalman-momentum", mom_sp)]:
        if np.nanvar(sp[valid]) >= np.nanvar(static_sp[valid]):
            failures.append(f"{name} spread variance not below static-β")
    # 3. Cumulative return: both Kalman methods beat static-β (Fig 15.23).
    for name in ("Kalman-basic", "Kalman-momentum"):
        if _final(cum[name]) <= _final(cum["static-β"]):
            failures.append(f"{name} cum P&L {_final(cum[name]):.3f} did not "
                            f"beat static-β {_final(cum['static-β']):.3f}")

    print()
    if failures:
        print("GATE FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("GATE PASSED ✓  (stable adaptive γ, more-stationary spread, "
          "Kalman beats static-β)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
