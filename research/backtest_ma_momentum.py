"""
Index-futures MA-momentum research harness (§6.3)
=================================================
`docs/research/strategy-finetuning-profitability-2026-08-30.md` §6.3.

The Kalman-trend A/B's MA *control* already beat Kalman in-sample, OOS, and
forward (+₹40,310 vs −₹32,059). This harness replays that control as a
standalone book with the **same fitted SMA lengths the paper runner used**,
frozen. It does not call CMA-ES. Jointly refitting the windows is how
Kalman-trend got an in-sample Sharpe of 5 and an OOS of luck.

Execution matches the paper runner: 5-min OHLC, 15:25 flatten, 2.5 pts/side,
no target (#125), 1 lot. Signal is `optimize_kalman_trend.ma_direction`
(Algorithm 5); fills are `simulate` with honest touch/gap (#122).

Tape on disk is index **spot** (`data_cache/{SYM}_5minute.parquet`), through
2026-07-14. The paper traded the front-month *future* and refit on 2026-07-15
from a 40-calendar-day warmup. Dates before that warmup are the OOS prior;
the warmup window is in-sample for these params. Do not transplant 5-min
window lengths onto daily bars.

Kill (any one fires → do not add a paper daemon, do not then CMA-ES):
  1. OOS-prior Sharpe ≤ 0
  2. OOS-prior net < 2 × round-trip cost × trade count
  3. OOS-prior trades = 0

Usage:
    python -m research.backtest_ma_momentum
    python -m research.backtest_ma_momentum --symbols NIFTY
"""
from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from core.backtest_timeframe import warn_coarse_timeframe
from core.data_cache_io import read_table, table_exists
from research import optimize_kalman_trend as opt
from strategies.ma_momentum import (
    COST_PER_UNIT_POINTS,
    FIT_END,
    FIT_START,
    FROZEN_PARAMS,
    LOT_SIZE,
    TICK_SIZE,
)

logger = logging.getLogger(__name__)

CACHE = Path("./data_cache")


def load_5min(symbol: str) -> pd.DataFrame:
    """OHLC 5-min index bars. Fails loud if the file or columns are missing."""
    path = CACHE / f"{symbol}_5minute.parquet"
    if not table_exists(path):
        raise FileNotFoundError(
            f"no 5-min tape for {symbol}: expected {path}. §6.3 is an "
            "intraday flatten strategy; do not silently fall back to daily."
        )
    df = read_table(path)
    cols = {c.lower(): c for c in df.columns}
    need = {"open", "high", "low", "close"}
    missing = need - set(cols)
    if missing:
        raise ValueError(
            f"{path} lacks {sorted(missing)} — close-only 5-min would book "
            "stops at the LEVEL (issue #122). Re-fetch OHLC."
        )
    ts_col = next((cols[k] for k in ("datetime", "timestamp", "date") if k in cols), None)
    if ts_col is None:
        raise ValueError(f"{path} has no datetime/timestamp/date column")
    out = pd.DataFrame({
        "datetime": pd.to_datetime(df[ts_col]),
        "open": df[cols["open"]].astype(float),
        "high": df[cols["high"]].astype(float),
        "low": df[cols["low"]].astype(float),
        "close": df[cols["close"]].astype(float),
    }).dropna()
    if getattr(out["datetime"].dt, "tz", None) is not None:
        out["datetime"] = out["datetime"].dt.tz_convert("Asia/Kolkata")
    out["session"] = out["datetime"].dt.date
    out = out.sort_values("datetime").reset_index(drop=True)
    n_days = out["session"].nunique()
    if len(out) <= n_days:
        raise ValueError(
            f"{path} looks daily ({len(out)} bars / {n_days} days) — the "
            "15:25 flatten would be a no-op. §6.3 needs true 5-min bars."
        )
    return out


def _daily_rupees(bar_pnl_pts: np.ndarray, sessions: pd.Series, lot: int) -> pd.Series:
    """Sum 5-min mark-to-market points to calendar-day ₹.

    ``simulate``'s Sharpe treats each *bar* as a day (√252). That is fine
    for Kalman-vs-MA on the same tape; it is not a rupee Sharpe. Gate on
    this series instead.
    """
    s = pd.Series(bar_pnl_pts * lot, index=pd.Index(sessions, name="session"))
    return s.groupby(level=0).sum()


def _max_dd(daily: pd.Series) -> float:
    if daily.empty:
        return 0.0
    eq = daily.cumsum()
    return float((eq - eq.cummax()).min())


def _sharpe_daily(daily: pd.Series) -> float:
    if len(daily) < 2:
        return float("nan")
    sd = float(daily.std(ddof=0))
    if not (sd > 0):
        return float("nan")
    return float(daily.mean() / sd * np.sqrt(opt.TRADING_DAYS))


def score_slice(daily: pd.Series, n_trades: int, lot: int,
                cost_pts: float = COST_PER_UNIT_POINTS) -> dict:
    net = float(daily.sum()) if len(daily) else 0.0
    rt = 2.0 * cost_pts * lot          # round-trip ₹ at 1 lot
    costs = float(n_trades) * rt
    return {
        "n_days": int(len(daily)),
        "n_trades": int(n_trades),
        "net": net,
        "gross": net + costs,
        "costs": costs,
        "avg_cost": rt,
        "hurdle": 2.0 * rt * n_trades,
        "sharpe": _sharpe_daily(daily),
        "max_dd": _max_dd(daily),
    }


def replay(df: pd.DataFrame, params: dict, lot: int) -> dict:
    """Run the frozen MA book on ``df``. Returns bar-level + daily ₹."""
    closes = df["close"].to_numpy(float)
    direction = opt.ma_direction(
        closes, short=params["short"], long=params["long"],
        offset=params["offset"],
    )
    ends = opt.session_ends_from_timestamps(df["datetime"].tolist())
    ohlc = opt.OHLC.from_frame(df)
    res = opt.simulate(
        closes, direction,
        stop_ticks=params["stop_ticks"],
        target_ticks=params["target_ticks"],
        tick_size=TICK_SIZE,
        cost_per_unit=COST_PER_UNIT_POINTS,
        session_ends=ends,
        **ohlc.as_kwargs(),
    )
    daily = _daily_rupees(res.daily_pnl, df["session"], lot)
    out = score_slice(daily, res.n_trades, lot)
    out["daily"] = daily
    out["realized_points"] = float(res.realized_pnl)
    return out


def split_prior_fit(df: pd.DataFrame, fit_start: date = FIT_START,
                    fit_end: date = FIT_END):
    """(prior, fit, post) — OOS before the warmup, in-sample inside it, and
    OOS again after the refit.

    The fit window must be CLOSED at `fit_end`. Left unbounded it swallows any
    session the tape gains later: the repo refreshes 5-min bars, and every
    genuinely post-refit session would land in the slice printed as
    "FIT WINDOW (IS, not a gate)" and be excluded from `prior`, the only slice
    kill_reasons() reads. That silently relabels real OOS evidence as
    in-sample. `post` is reported loudly and separately; it is deliberately
    NOT folded into the gate, because §6.3 pre-registered the gate as the
    prior slice alone.
    """
    prior = df[df["session"] < fit_start]
    fit = df[(df["session"] >= fit_start) & (df["session"] < fit_end)]
    post = df[df["session"] >= fit_end]
    return prior, fit, post


def kill_reasons(prior: dict) -> List[str]:
    """§6.3 / E2 gates on the OOS-prior slice. Empty list = all clear."""
    reasons: List[str] = []
    if prior["n_trades"] < 1:
        reasons.append("OOS-prior trades = 0")
        return reasons
    sh = prior["sharpe"]
    if not np.isfinite(sh) or sh <= 0:
        reasons.append(f"OOS-prior Sharpe {sh:.3f} ≤ 0")
    if prior["net"] < prior["hurdle"]:
        reasons.append(
            f"OOS-prior net ₹{prior['net']:,.0f} < 2×RT×n ₹{prior['hurdle']:,.0f}"
        )
    return reasons


def _print_slice(label: str, s: dict) -> None:
    sh = f"{s['sharpe']:.3f}" if np.isfinite(s["sharpe"]) else "nan"
    print(f"{label}")
    print(f"  days={s['n_days']}  trades={s['n_trades']}  Sharpe={sh}")
    print(f"  net=₹{s['net']:,.0f}  gross=₹{s['gross']:,.0f}  "
          f"costs=₹{s['costs']:,.0f}  maxDD=₹{s['max_dd']:,.0f}")
    if s["n_trades"]:
        print(f"  avg RT cost=₹{s['avg_cost']:,.0f}  "
              f"2×RT×n=₹{s['hurdle']:,.0f}")


def _combine(parts: List[dict]) -> dict:
    """Sum daily ₹ on the union of dates (1-lot NIFTY + 1-lot BANKNIFTY)."""
    daily = pd.Series(dtype=float)
    n_trades = 0
    costs = 0.0
    for p in parts:
        daily = daily.add(p["daily"], fill_value=0.0)
        n_trades += p["n_trades"]
        costs += p["costs"]
    daily = daily.sort_index()
    net = float(daily.sum()) if len(daily) else 0.0
    # Combined hurdle is the sum of the per-leg hurdles already in `costs`×2/n
    # — use 2× the actual costs charged.
    return {
        "n_days": int(len(daily)),
        "n_trades": int(n_trades),
        "net": net,
        "gross": net + costs,
        "costs": costs,
        "avg_cost": (costs / n_trades) if n_trades else 0.0,
        "hurdle": 2.0 * costs if n_trades else 0.0,
        "sharpe": _sharpe_daily(daily),
        "max_dd": _max_dd(daily),
        "daily": daily,
    }


def run_symbol(symbol: str, df: Optional[pd.DataFrame] = None) -> dict:
    if symbol not in FROZEN_PARAMS:
        raise KeyError(
            f"no frozen MA params for {symbol}; known: {sorted(FROZEN_PARAMS)}"
        )
    params = FROZEN_PARAMS[symbol]
    lot = LOT_SIZE[symbol]
    if df is None:
        df = load_5min(symbol)
    prior_df, fit_df, post_df = split_prior_fit(df)
    full = replay(df, params, lot)

    def _slice(sub):
        out = (replay(sub, params, lot) if len(sub)
               else score_slice(pd.Series(dtype=float), 0, lot))
        # Re-attach daily series for the combined book (score_slice omits it).
        out.setdefault("daily", pd.Series(dtype=float))
        return out

    prior, fit, post = _slice(prior_df), _slice(fit_df), _slice(post_df)
    if len(post_df):
        logger.warning(
            "%s: tape now extends to %s, past the %s refit — %d post-refit "
            "sessions are OOS, not in-sample. Reported as their own slice; "
            "the pre-registered gate is still the prior slice alone.",
            symbol, df["session"].iloc[-1], FIT_END.isoformat(),
            int(post_df["session"].nunique()))
    return {
        "symbol": symbol, "params": params, "lot": lot,
        "n_bars": int(len(df)), "n_days": int(df["session"].nunique()),
        "start": df["session"].iloc[0], "end": df["session"].iloc[-1],
        "fit_start": FIT_START, "fit_end": FIT_END,
        "full": full, "prior": prior, "fit": fit, "post": post,
        "n_post_days": int(post_df["session"].nunique()) if len(post_df) else 0,
        "kills": kill_reasons(prior),
    }


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--symbols", default="NIFTY,BANKNIFTY",
                   help="Comma-separated. Windows are frozen; this does not retune.")
    args = p.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    # 5-min is the standard; this is a no-op banner so a reader sees the
    # timeframe was considered rather than assumed.
    warn_coarse_timeframe(
        "5min", backtest="backtest_ma_momentum",
        reason="§6.3 MA control is the paper runner's 5-min flatten book",
        logger=logger,
    )

    results = []
    for sym in symbols:
        try:
            r = run_symbol(sym)
        except FileNotFoundError as e:
            logger.error("%s", e)
            return 2
        results.append(r)

    print()
    print("§6.3 MA-momentum  (frozen paper-control params, 1 lot, 2.5 pts/side, "
          "15:25 flatten)")
    print(f"  OOS prior = session < {FIT_START.isoformat()}  "
          f"(40 calendar days before the 2026-07-15 refit)")
    print(f"  Fit window = {FIT_START.isoformat()} ≤ session < "
          f"{FIT_END.isoformat()}  (in-sample for these SMA lengths; not a gate)")
    print(f"  Post-refit = session ≥ {FIT_END.isoformat()}  "
          f"(OOS, reported but NOT in the pre-registered gate)")
    print("  Tape is index SPOT 5-min; paper traded the future.")
    print("=" * 95)
    for r in results:
        par = r["params"]
        print(f"\n{r['symbol']}  short={par['short']} long={par['long']}  "
              f"offset={par['offset']:.3f}  stop={par['stop_ticks']:.3f} ticks  "
              f"target=None  lot={r['lot']}")
        print(f"  panel {r['n_bars']} bars / {r['n_days']} days  "
              f"{r['start']} → {r['end']}")
        _print_slice("  OOS PRIOR (gate)", r["prior"])
        _print_slice("  FIT WINDOW (IS, not a gate)", r["fit"])
        if r["n_post_days"]:
            _print_slice("  POST-REFIT OOS (not in the pre-registered gate)",
                         r["post"])
        _print_slice("  FULL (spot 5-min, not a gate)", r["full"])
        kills = r["kills"]
        print("  KILLS: " + ("none — CLEARS" if not kills else "; ".join(kills)))

    if len(results) > 1:
        print("\nCOMBINED 1-lot book")
        keys = [("OOS PRIOR (gate)", "prior"), ("FIT WINDOW (IS)", "fit")]
        if any(r["n_post_days"] for r in results):
            keys.append(("POST-REFIT OOS", "post"))
        keys.append(("FULL", "full"))
        for label, key in keys:
            comb = _combine([r[key] for r in results])
            _print_slice(f"  {label}", comb)
        comb_kills = kill_reasons(_combine([r["prior"] for r in results]))
        print("  KILLS: " + ("none — CLEARS" if not comb_kills
                             else "; ".join(comb_kills)))

    any_kill = any(r["kills"] for r in results)
    print()
    if any_kill:
        print("NO-GO. Do not add a paper daemon. Do not then CMA-ES the windows.")
        return 1
    print("CLEARS the OOS-prior gates. Promotion is still paper-only, 1-lot, "
          "and needs a CODEOWNERS review before any timer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
