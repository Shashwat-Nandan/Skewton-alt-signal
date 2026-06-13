"""
Pair Screener — Find Cointegrated Stock-Futures Pairs from NSE F&O Bhavcopy
============================================================================

Reads cached F&O bhavcopy CSVs (data_cache/bhavcopy_raw/), extracts the
front-month single-stock-futures (STF) close-price series for the NIFTY 50
universe, then runs Engle-Granger cointegration on every pair. Ranks
surviving pairs by p-value + half-life of mean reversion + spread vol so
the top of the list is both statistically robust *and* tradeable.

Output:
  data_cache/pair_candidates.csv — one row per qualifying pair with
  hedge ratio, p-value, half-life (days), spread vol (% of mean), and a
  composite rank score (lower = better).

Usage:
  python screen_pairs.py
  python screen_pairs.py --top 20 --min-coverage 0.85
  python screen_pairs.py --universe my_symbols.txt
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
from statsmodels.tsa.stattools import coint

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_DIR = CACHE_DIR / "bhavcopy_raw"
OUTPUT_PATH = CACHE_DIR / "pair_candidates.csv"

# NIFTY 50 constituents (snapshot — symbols missing from the bhavcopy on a
# given day are silently skipped, so this list can drift without breaking
# the screener).
NIFTY_50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC",
    "LT", "KOTAKBANK", "SBIN", "BHARTIARTL", "BAJFINANCE", "ASIANPAINT",
    "AXISBANK", "MARUTI", "M&M", "SUNPHARMA", "NESTLEIND", "ULTRACEMCO",
    "TITAN", "HCLTECH", "WIPRO", "ADANIENT", "ADANIPORTS", "NTPC",
    "POWERGRID", "ONGC", "COALINDIA", "JSWSTEEL", "TATASTEEL", "HINDALCO",
    "BAJAJFINSV", "BAJAJ-AUTO", "BRITANNIA", "DRREDDY", "CIPLA", "EICHERMOT",
    "GRASIM", "HEROMOTOCO", "BPCL", "INDUSINDBK", "DIVISLAB", "APOLLOHOSP",
    "TECHM", "SHRIRAMFIN", "TATACONSUM", "LTIM", "HDFCLIFE", "SBILIFE",
    "TATAMOTORS",
]


def load_front_month_panel(
    universe: List[str],
    raw_dir: Path = RAW_DIR,
    min_coverage: float = 0.80,
) -> pd.DataFrame:
    """
    Build a wide DataFrame (rows=trading dates, cols=symbols) of front-month
    single-stock-futures close prices.

    Front-month rule: for each (date, symbol), keep the row whose XpryDt is
    the smallest value strictly >= the trading date. On expiry day this is
    today's contract; the next day it rolls to next month automatically.

    Drops symbols whose coverage (non-NaN trading days / total trading days)
    is below `min_coverage`.
    """
    files = sorted(raw_dir.glob("bhavcopy_fo_*.csv"))
    if not files:
        raise RuntimeError(f"No bhavcopy CSVs found in {raw_dir}")
    logger.info("Reading %d bhavcopy files from %s", len(files), raw_dir)

    universe_set = set(universe)
    rows = []
    for f in files:
        df = pd.read_csv(
            f,
            usecols=["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "ClsPric"],
            dtype={"TckrSymb": str, "FinInstrmTp": str},
        )
        df = df[(df["FinInstrmTp"] == "STF") & (df["TckrSymb"].isin(universe_set))]
        if df.empty:
            continue
        df["TradDt"] = pd.to_datetime(df["TradDt"]).dt.date
        df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
        df = df[df["XpryDt"] >= df["TradDt"]]  # drop already-expired rows
        # Front month per (date, symbol): min XpryDt
        idx = df.groupby(["TradDt", "TckrSymb"])["XpryDt"].idxmin()
        df = df.loc[idx, ["TradDt", "TckrSymb", "ClsPric"]]
        rows.append(df)

    if not rows:
        raise RuntimeError("No STF rows for the requested universe")

    long = pd.concat(rows, ignore_index=True)
    panel = long.pivot(index="TradDt", columns="TckrSymb", values="ClsPric").sort_index()
    panel.index = pd.to_datetime(panel.index)

    n_days = len(panel)
    coverage = panel.notna().sum() / n_days
    keep = coverage[coverage >= min_coverage].index.tolist()
    dropped = sorted(set(panel.columns) - set(keep))
    if dropped:
        logger.info("Dropped %d symbols below %.0f%% coverage: %s",
                    len(dropped), min_coverage * 100, ", ".join(dropped))

    # Recent-tail coverage filter: a symbol can pass the full-window 80%
    # threshold while having dropped off the tape in the last few months
    # (e.g. LTIM had no STF rows from 2026-02-27 onward but still passed
    # the full-window filter on its prior ~450 days of data). With
    # dropna(how="any") below, a single such symbol truncates the panel
    # back to the day it disappeared. Apply the same coverage threshold
    # to the trailing TAIL_DAYS sessions to drop symbols that have fallen
    # off the tape, regardless of their long-window history.
    TAIL_DAYS = 30
    if n_days >= TAIL_DAYS and keep:
        tail = panel[keep].iloc[-TAIL_DAYS:]
        tail_coverage = tail.notna().sum() / TAIL_DAYS
        tail_keep = tail_coverage[tail_coverage >= min_coverage].index.tolist()
        tail_dropped = sorted(set(keep) - set(tail_keep))
        if tail_dropped:
            logger.info("Dropped %d symbols below %.0f%% coverage in last %d sessions: %s",
                        len(tail_dropped), min_coverage * 100, TAIL_DAYS,
                        ", ".join(tail_dropped))
        keep = tail_keep

    panel = panel[keep].dropna(how="any")
    logger.info("Final panel: %d trading days × %d symbols", len(panel), panel.shape[1])

    # Rule 12: surface a stale panel loudly. The screener silently used
    # data ending 2026-02-26 on 2026-05-17 because a per-symbol NaN
    # cliff truncated dropna(how="any") — the only signal downstream was
    # the last_data_date column. A trailing gap of >5 trading days from
    # the file system's latest bhavcopy almost certainly indicates a
    # data-pipeline problem worth investigating before trading.
    if len(panel) > 0:
        latest_bhav_date = max(
            pd.to_datetime(f.name.removeprefix("bhavcopy_fo_").removesuffix(".csv"),
                           format="%Y%m%d")
            for f in files
        )
        gap_days = (latest_bhav_date - panel.index[-1]).days
        if gap_days > 5:
            logger.warning(
                "Panel ends %s but latest bhavcopy is %s (gap=%dd) — "
                "one or more symbols may have fallen off the tape after "
                "the panel's last clean date. Investigate before trading.",
                panel.index[-1].date(), latest_bhav_date.date(), gap_days,
            )

    return panel


def _fit(y: np.ndarray, x: np.ndarray):
    """OLS y on x with intercept. Returns the fitted statsmodels result."""
    return OLS(y, add_constant(x)).fit()


def _hedge_ratio(y: np.ndarray, x: np.ndarray) -> float:
    """OLS slope of y on x with intercept (hedge ratio for the long leg)."""
    return float(_fit(y, x).params[1])


def _error_ratio(fit) -> float:
    """Varsity Ch. 10 Error Ratio = SE(intercept) / SE(regression).
    Lower ER → smaller intercept relative to residual scale → the y/x assignment
    where the regression is doing more of the explanatory work and the
    intercept is doing less. Used to pick which side is X vs Y when both
    directions are statistically plausible."""
    se_intercept = float(fit.bse[0])      # intercept is at index 0 (add_constant prepends)
    se_regression = float(np.sqrt(fit.scale))
    if se_regression == 0:
        return float("inf")
    return se_intercept / se_regression


def _half_life(spread: np.ndarray) -> float:
    """
    Half-life of mean reversion via AR(1) on the spread:
      ΔS_t = α + φ * S_{t-1} + ε   →   half-life = -ln(2) / ln(1 + φ)
    Returns +inf if the spread is non-mean-reverting (φ >= 0).
    """
    s = spread - spread.mean()
    s_lag = s[:-1]
    delta_s = s[1:] - s[:-1]
    if len(s_lag) < 2:
        return float("inf")
    phi = float(OLS(delta_s, add_constant(s_lag)).fit().params[1])
    if phi >= 0:
        return float("inf")
    return float(-np.log(2) / np.log(1 + phi))


def screen_pairs(
    panel: pd.DataFrame,
    p_threshold: float = 0.05,
    min_correlation: float = 0.5,
    min_hedge_ratio: float = 0.1,
    max_hedge_ratio: float = 10.0,
) -> pd.DataFrame:
    """
    Run pairwise Engle-Granger cointegration and return one row per
    qualifying pair with diagnostics.

    Pre-filter: skip pairs whose Pearson correlation is below
    `min_correlation` — uncorrelated series rarely cointegrate, and the
    coint() call dominates runtime.

    Tradeable-β filter: skip pairs whose |β| is outside
    [min_hedge_ratio, max_hedge_ratio]. Defaults match
    `strategies.pair_trading.HEDGE_RATIO_MIN`/`HEDGE_RATIO_MAX` so the
    output CSV is always consumable by that strategy without further
    filtering on the consumer side.
    """
    symbols = panel.columns.tolist()
    n_pairs = len(symbols) * (len(symbols) - 1) // 2
    logger.info("Screening %d pairs across %d symbols (%d trading days)",
                n_pairs, len(symbols), len(panel))

    corr = panel.corr().abs()

    skipped_beta = 0
    results = []
    for raw_a, raw_b in combinations(symbols, 2):
        if corr.loc[raw_a, raw_b] < min_correlation:
            continue
        # Varsity Ch. 10: regress both directions and keep the one with the
        # lower Error Ratio = SE(intercept)/SE(regression). Without this step,
        # iterating combinations() locks in symbol-alphabetical order — which
        # is statistically arbitrary — and we lose the cleaner residual the
        # other direction would have produced.
        series_a = panel[raw_a].values
        series_b = panel[raw_b].values
        fit_ab = _fit(series_a, series_b)   # y=raw_a on x=raw_b
        fit_ba = _fit(series_b, series_a)   # y=raw_b on x=raw_a
        er_ab = _error_ratio(fit_ab)
        er_ba = _error_ratio(fit_ba)
        if er_ab <= er_ba:
            a, b, fit, error_ratio = raw_a, raw_b, fit_ab, er_ab
        else:
            a, b, fit, error_ratio = raw_b, raw_a, fit_ba, er_ba

        ya = panel[a].values
        yb = panel[b].values
        try:
            _, p_value, _ = coint(ya, yb)
        except Exception as e:
            logger.debug("coint failed for %s/%s: %s", a, b, e)
            continue
        if p_value > p_threshold:
            continue

        beta = float(fit.params[1])
        if not min_hedge_ratio <= abs(beta) <= max_hedge_ratio:
            skipped_beta += 1
            logger.debug(
                "Skipped %s/%s: |β|=%.4f outside [%.2f, %.2f]",
                a, b, abs(beta), min_hedge_ratio, max_hedge_ratio,
            )
            continue
        spread = ya - beta * yb
        spread_mean = float(np.mean(spread))
        spread_std = float(np.std(spread))
        # Normalize spread vol by average leg price (a tradeable-move proxy).
        # Coefficient of variation (std/mean) blows up when the OLS hedge
        # produces a spread that oscillates around zero — common for pairs
        # with similar absolute price levels.
        avg_leg_price = (float(np.mean(ya)) + abs(beta) * float(np.mean(yb))) / 2.0
        if avg_leg_price <= 0:
            continue
        spread_vol_pct = spread_std / avg_leg_price * 100
        half_life = _half_life(spread)

        latest_spread = float(spread[-1])
        latest_z_score = (
            (latest_spread - spread_mean) / spread_std if spread_std > 0 else None
        )

        results.append({
            "symbol_a": a,
            "symbol_b": b,
            "correlation": float(corr.loc[a, b]),
            "hedge_ratio": beta,
            "intercept": float(fit.params[0]),
            "error_ratio": error_ratio,
            "coint_pvalue": float(p_value),
            "half_life_days": half_life,
            "spread_vol_pct": spread_vol_pct,
            "spread_mean": spread_mean,
            "spread_std": spread_std,
            "latest_spread": latest_spread,
            "latest_z_score": latest_z_score,
            "last_close_a": float(ya[-1]),
            "last_close_b": float(yb[-1]),
            "last_data_date": panel.index[-1].strftime("%Y-%m-%d"),
            "n_obs": len(panel),
        })

    if skipped_beta:
        logger.info("Skipped %d pair(s) with |β| outside [%.2f, %.2f]",
                    skipped_beta, min_hedge_ratio, max_hedge_ratio)

    if not results:
        logger.warning("No pairs passed p-value %.3f and correlation %.2f filters",
                       p_threshold, min_correlation)
        return pd.DataFrame()

    df = pd.DataFrame(results)

    # Composite rank: low p-value + low half-life + high spread vol all good.
    # Use percentile ranks so the score is scale-free.
    p_rank = df["coint_pvalue"].rank(pct=True)               # lower better
    hl_rank = df["half_life_days"].rank(pct=True)            # lower better
    vol_rank = (-df["spread_vol_pct"]).rank(pct=True)        # higher vol → lower rank
    df["rank_score"] = (p_rank + hl_rank + vol_rank) / 3.0

    df = df.sort_values("rank_score").reset_index(drop=True)
    return df


def screen_pairs_persistent(
    panel: pd.DataFrame,
    *,
    min_persistence: int,
    window_days: int = 130,
    step_days: int = 45,
    p_threshold: float = 0.05,
    min_correlation: float = 0.5,
    min_hedge_ratio: float = 0.1,
    max_hedge_ratio: float = 10.0,
) -> pd.DataFrame:
    """Rolling-window cointegration screen. A pair is admitted only if it
    passes `p<p_threshold` in ≥`min_persistence` of the rolling windows.

    Per-pair stats (β, p, half-life, latest_z, etc.) are taken from the
    MOST RECENT window where the pair passed, so the runner trades against
    the current cointegration parameters, not a stale window's.

    Backed by 2026-05-17 verification: across three OOS test windows
    (W4/W5/W7 spanning Feb 2025 → Feb 2026), pairs admitted under
    min_persistence=2 produced 22/23 profitable pair-windows and ₹1.73M
    aggregate net. Same pairs under the single-window screener collapsed
    to -₹450k on the worst slice. See tasks/todo.md (2026-05-17 entry).
    """
    end = len(panel)
    windows: List[Tuple[int, int]] = []
    while end - window_days >= 0:
        windows.append((end - window_days, end))
        end -= step_days
    windows.reverse()

    if len(windows) < min_persistence:
        logger.error(
            "Only %d rolling windows fit (window=%dd, step=%dd, panel=%dd) — "
            "need ≥%d for min_persistence=%d. Either lower min_persistence, "
            "shrink window_days/step_days, or fetch more bhavcopy history.",
            len(windows), window_days, step_days, len(panel),
            min_persistence, min_persistence,
        )
        return pd.DataFrame()

    logger.info(
        "Persistence screen: %d rolling windows × %dd, step %dd, min_persistence=%d",
        len(windows), window_days, step_days, min_persistence,
    )

    # pair_key -> {window_idx: row_dict}
    pair_results: Dict[Tuple[str, str], Dict[int, dict]] = defaultdict(dict)
    for i, (s, e) in enumerate(windows):
        sub = panel.iloc[s:e]
        screened = screen_pairs(
            sub,
            p_threshold=p_threshold,
            min_correlation=min_correlation,
            min_hedge_ratio=min_hedge_ratio,
            max_hedge_ratio=max_hedge_ratio,
        )
        for _, r in screened.iterrows():
            pair_results[(r["symbol_a"], r["symbol_b"])][i] = r.to_dict()
        logger.info(
            "  W%d: %s → %s — %d pairs passed",
            i, sub.index[0].date(), sub.index[-1].date(), len(screened),
        )

    # The most recent window IS the "current" cointegration test. A pair that
    # passed in W0/W3 but not W_last is not currently cointegrating — admitting
    # it would have the runner trade on stale hedge ratios. So the admission
    # rule is: pass in the latest window AND in ≥(min_persistence-1) prior
    # windows. Mirrors the OOS verification of 2026-05-17.
    last_w = len(windows) - 1
    rows = []
    for (a, b), window_rows in pair_results.items():
        if last_w not in window_rows:
            continue
        if len(window_rows) < min_persistence:
            continue
        row = dict(window_rows[last_w])
        row["persistence_count"] = len(window_rows)
        row["persistence_windows"] = ",".join(str(w) for w in sorted(window_rows.keys()))
        rows.append(row)

    if not rows:
        logger.warning(
            "No pairs passed in ≥%d windows out of %d. "
            "The universe may have no durable cointegration on this lookback — "
            "consider lowering min_persistence or running an alternative strategy.",
            min_persistence, len(windows),
        )
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    # Same composite rank_score as single-window screen (uses LATEST window's
    # stats). Persistence is a separate admission filter, not a rank component —
    # the runner's classify_pair_candidates does its own composite rerank, and
    # we want the downstream code to see a familiar shape.
    p_rank = df["coint_pvalue"].rank(pct=True)
    hl_rank = df["half_life_days"].rank(pct=True)
    vol_rank = (-df["spread_vol_pct"]).rank(pct=True)
    df["rank_score"] = (p_rank + hl_rank + vol_rank) / 3.0
    df = df.sort_values("rank_score").reset_index(drop=True)

    logger.info(
        "Persistence screen admitted %d pair(s) (≥%d / %d windows)",
        len(df), min_persistence, len(windows),
    )
    return df


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")

    p = argparse.ArgumentParser(description="Screen NIFTY 50 stock-futures pairs")
    p.add_argument("--top", type=int, default=25,
                   help="Print this many top candidates (default: 25)")
    p.add_argument("--min-coverage", type=float, default=0.80,
                   help="Drop symbols with fewer than this fraction of trading days (default: 0.80)")
    p.add_argument("--p-threshold", type=float, default=0.05,
                   help="Cointegration p-value cutoff (default: 0.05)")
    p.add_argument("--min-correlation", type=float, default=0.5,
                   help="Pre-filter: skip pairs below this |Pearson| (default: 0.5)")
    p.add_argument("--min-hedge-ratio", type=float, default=0.1,
                   help="Skip pairs with |β| below this (default: 0.1, matches strategy floor)")
    p.add_argument("--max-hedge-ratio", type=float, default=10.0,
                   help="Skip pairs with |β| above this (default: 10.0, matches strategy ceiling)")
    p.add_argument("--universe", type=str, default=None,
                   help="Path to a newline-separated symbol list (default: NIFTY 50)")
    p.add_argument("--output", type=str, default=str(OUTPUT_PATH),
                   help=f"Output CSV path (default: {OUTPUT_PATH})")
    p.add_argument("--window-days", type=int, default=130,
                   help="Baseline (single-window) mode: take the trailing N "
                        "trading days of the panel for the cointegration fit "
                        "(default: 130, matches --persistence-window-days). "
                        "Pass 0 to disable and fit over the full panel. "
                        "Ignored in persistence mode.")
    p.add_argument("--persistence-min", type=int, default=None,
                   help="If set, switch to persistence mode: only emit pairs "
                        "that pass p<p_threshold in ≥M rolling windows. The "
                        "single-window screen is bypassed. Reproducibly admits "
                        "durably cointegrating pairs; see tasks/todo.md "
                        "(2026-05-17) for OOS validation results.")
    p.add_argument("--persistence-window-days", type=int, default=130,
                   help="Persistence-mode rolling window length (default: 130)")
    p.add_argument("--persistence-step-days", type=int, default=45,
                   help="Persistence-mode step between windows (default: 45)")
    args = p.parse_args()

    if args.universe:
        universe = [s.strip() for s in Path(args.universe).read_text().splitlines() if s.strip()]
    else:
        universe = NIFTY_50

    panel = load_front_month_panel(universe, min_coverage=args.min_coverage)
    if panel.shape[1] < 2:
        logger.error("Need at least 2 symbols with sufficient coverage; got %d",
                     panel.shape[1])
        return 1

    if args.persistence_min is not None:
        df = screen_pairs_persistent(
            panel,
            min_persistence=args.persistence_min,
            window_days=args.persistence_window_days,
            step_days=args.persistence_step_days,
            p_threshold=args.p_threshold,
            min_correlation=args.min_correlation,
            min_hedge_ratio=args.min_hedge_ratio,
            max_hedge_ratio=args.max_hedge_ratio,
        )
    else:
        # Trailing-window slice for the baseline fit. Without this, the
        # screener fits one cointegration over every bhavcopy file ever
        # cached (~503 days at time of writing), which absorbs years of
        # drift into the regression and inflates half-lives well past
        # the runtime quality floor (HL ≤ 5d). 130 matches
        # --persistence-window-days so baseline and persistence are
        # using comparable lookbacks.
        baseline_panel = panel
        if args.window_days > 0 and len(panel) > args.window_days:
            baseline_panel = panel.iloc[-args.window_days:]
            logger.info("Baseline window: trailing %d of %d trading days (%s → %s)",
                        args.window_days, len(panel),
                        baseline_panel.index[0].date(),
                        baseline_panel.index[-1].date())
        df = screen_pairs(
            baseline_panel,
            p_threshold=args.p_threshold,
            min_correlation=args.min_correlation,
            min_hedge_ratio=args.min_hedge_ratio,
            max_hedge_ratio=args.max_hedge_ratio,
        )

    if df.empty:
        return 0

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False)
    logger.info("Wrote %d candidates to %s", len(df), args.output)

    has_persistence = "persistence_count" in df.columns
    print()
    print(f"Top {min(args.top, len(df))} pair candidates "
          f"(of {len(df)} total passing filters)")
    print("=" * 110)
    pers_header = "  pers" if has_persistence else ""
    print(f"{'#':<3} {'Symbol A (Y)':<14} {'Symbol B (X)':<14} {'corr':>6} "
          f"{'β':>8} {'ER':>6} {'p-val':>8} {'half-life':>11} {'vol%':>7} {'score':>7}{pers_header}")
    print("-" * (110 + len(pers_header)))
    for i, row in df.head(args.top).iterrows():
        hl = f"{row['half_life_days']:.1f}d" if np.isfinite(row['half_life_days']) else "  inf "
        pers_cell = f"  {int(row['persistence_count']):>3d}" if has_persistence else ""
        print(f"{i+1:<3} {row['symbol_a']:<14} {row['symbol_b']:<14} "
              f"{row['correlation']:>6.3f} {row['hedge_ratio']:>8.3f} "
              f"{row['error_ratio']:>6.3f} "
              f"{row['coint_pvalue']:>8.4f} {hl:>11} "
              f"{row['spread_vol_pct']:>6.2f}% {row['rank_score']:>7.3f}{pers_cell}")
    print("=" * (110 + len(pers_header)))
    print(f"Saved to: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
