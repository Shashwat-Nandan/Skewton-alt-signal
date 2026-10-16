"""ADX breakout expressed with ATM NIFTY options. Research only.

Same entries and exits as ``research.adx_breakout`` on 5-minute NIFTY
index bars, but each trade buys one lot of an at-the-money NIFTY option
(CE on a long breakout, PE on a short) instead of a future.

There is no intraday option history on the host, so option prices along
the 5-minute path are modelled:

* Black-76 on the forward. The forward is the index times the carry
  measured on the prior session (front future close over index close).
* The contract's IV is implied from its prior-session bhavcopy close and
  held fixed through the trade. IV moves at a breakout are not modelled.
* Time runs on the calendar to 15:30 IST on the expiry date.

``model_check`` scores that model against the bhavcopy before any
strategy result is shown. If the median error is above
``MODEL_MAX_MEDIAN_ERR`` of premium, the run stops: the model cannot
carry the test.

Contract: nearest expiry after the entry date (no 0-DTE), the strike
nearest the forward at the fill among the strikes that traded on the
prior session. Costs: ``core.costs`` option charges on premium, with
the options STT in force on the fill date, plus a half-spread in premium
points a side in place of core's percentage slippage.

Owner-approved gates (2026-10-16), published defaults, train only:
at least 100 trades, net > 0, daily Sharpe > 0, net > 0 at 1.5x costs,
and the three chronological windows not all negative. A pass is not a
paper promotion: the 2026 holdout was already seen for this signal, so a
pass only justifies recording real option quotes for a clean test.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy.stats import norm

from core.costs import estimate_transaction_cost
from core.data_cache_io import find_tables, read_table, table_columns
from research.adx_breakout import Params, Trade, _as_date, add_indicators, simulate
from research.optimize_adx_breakout import (
    five_min_span,
    load_nifty_front,
    ready_frame,
    split_train_holdout,
)

IST = "Asia/Kolkata"
RATE = 0.065                 # discounting only; the forward carries the basis
YEAR_SECONDS = 365.0 * 86400.0
EXPIRY_TIME = time(15, 30)
MIN_PREMIUM = 0.05           # NSE option tick
MODEL_MAX_MEDIAN_ERR = 0.10  # of premium, at the next session's close
HALF_SPREAD = 0.5            # premium points a side
MIN_TRADES = 100
COST_STRESS = 1.5
_CORE_OPT_SLIPPAGE = 0.0005  # core.costs' option slippage, replaced by HALF_SPREAD
_CORE_OPT_STT = 0.0015

# Options STT on the sell side (premium), by effective date. The
# 2026-04-01 step mirrors core.costs and still needs owner confirmation,
# like the futures one. (from, rate)
OPT_STT_HISTORY = (
    (date(1900, 1, 1), 0.000625),   # 0.0625%
    (date(2024, 10, 1), 0.001),     # 0.1%, Finance (No. 2) Act 2024
    (date(2026, 4, 1), 0.0015),     # 0.15%, core.costs rate
)


def opt_stt_rate(on: date) -> float:
    rate = OPT_STT_HISTORY[0][1]
    for start, r in OPT_STT_HISTORY:
        if on >= start:
            rate = r
    return rate


def option_cost(premium: float, lot: int, sell: bool, on: date,
                half_spread: float = HALF_SPREAD) -> float:
    """Rupees for one lot, one side."""
    if lot <= 0 or not premium > 0:
        raise ValueError(f"cannot cost an option fill at premium={premium} lot={lot}")
    rupees = estimate_transaction_cost(premium, 1, lot, "SELL" if sell else "BUY", "OPT")
    rupees -= premium * lot * _CORE_OPT_SLIPPAGE
    if sell:
        rupees += premium * lot * (opt_stt_rate(on) - _CORE_OPT_STT)
    return rupees + half_spread * lot


def black76(fwd, strike, t, sigma, cp, rate: float = RATE):
    """Black-76 price. ``cp`` is +1 for a call, -1 for a put. Vectorised."""
    fwd, strike, t, sigma = (np.asarray(x, dtype=float) for x in (fwd, strike, t, sigma))
    cp = np.asarray(cp, dtype=float)
    intrinsic = np.maximum(cp * (fwd - strike), 0.0)
    live = (t > 0) & (sigma > 0)
    st = np.where(live, sigma * np.sqrt(np.where(live, t, 1.0)), 1.0)
    d1 = (np.log(fwd / strike) + 0.5 * st ** 2) / st
    d2 = d1 - st
    value = np.exp(-rate * np.maximum(t, 0.0)) * cp * (
        fwd * norm.cdf(cp * d1) - strike * norm.cdf(cp * d2))
    return np.where(live, value, intrinsic)


def implied_vol(price, fwd, strike, t, cp, lo: float = 0.01, hi: float = 3.0,
                iters: int = 80):
    """Bisection IV. NaN where the price is outside the model's range."""
    price, fwd, strike, t = (np.asarray(x, dtype=float) for x in (price, fwd, strike, t))
    cp = np.asarray(cp, dtype=float)
    a = np.full(np.broadcast(price, fwd, strike, t, cp).shape, lo)
    b = np.full_like(a, hi)
    p_lo = black76(fwd, strike, t, a, cp)
    p_hi = black76(fwd, strike, t, b, cp)
    ok = (t > 0) & (price > p_lo) & (price < p_hi)
    for _ in range(iters):
        mid = 0.5 * (a + b)
        above = black76(fwd, strike, t, mid, cp) > price
        b = np.where(above, mid, b)
        a = np.where(above, a, mid)
    return np.where(ok, 0.5 * (a + b), np.nan)


def years_to_expiry(at: datetime, expiry: date) -> float:
    end = datetime.combine(expiry, EXPIRY_TIME)
    return max((end - at).total_seconds(), 0.0) / YEAR_SECONDS


def load_nifty_options(raw_dir: Path) -> pd.DataFrame:
    """Daily NIFTY index-option rows: date, expiry, strike, cp, close, high, low, trades, lot."""
    cols = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
            "HghPric", "LwPric", "ClsPric", "TtlNbOfTxsExctd", "NewBrdLotQty"]
    frames = []
    for path in find_tables(raw_dir, "bhavcopy_fo_*"):
        missing = [c for c in cols if c not in table_columns(path)]
        if missing:
            raise RuntimeError(f"{path.name} lacks {missing}")
        df = read_table(path, usecols=cols, dtype={"TckrSymb": str, "FinInstrmTp": str})
        frames.append(df[(df["FinInstrmTp"] == "IDO") & (df["TckrSymb"] == "NIFTY")])
    if not frames:
        raise RuntimeError(f"no bhavcopy tables in {raw_dir}")
    raw = pd.concat(frames, ignore_index=True)
    out = pd.DataFrame({
        "date": pd.to_datetime(raw["TradDt"]).dt.date,
        "expiry": pd.to_datetime(raw["XpryDt"]).dt.date,
        "strike": raw["StrkPric"].astype(float),
        "cp": np.where(raw["OptnTp"] == "CE", 1, -1),
        "close": raw["ClsPric"].astype(float),
        "high": raw["HghPric"].astype(float),
        "low": raw["LwPric"].astype(float),
        "trades": raw["TtlNbOfTxsExctd"].astype(float),
        "lot": raw["NewBrdLotQty"].astype(int),
    })
    return out[out["expiry"] >= out["date"]].reset_index(drop=True)


def session_closes(continuous: pd.DataFrame) -> pd.Series:
    """Index close of each session: its last regular bar."""
    dates = continuous["date"].map(_as_date)
    return continuous.groupby(dates)["close"].last()


def carry_by_date(front: pd.DataFrame, index_close: pd.Series) -> pd.Series:
    """Annual carry implied by the front future on each session, clipped."""
    f = front.set_index("date")
    common = sorted(set(f.index) & set(index_close.index))
    out = {}
    for d in common:
        t = years_to_expiry(datetime.combine(d, EXPIRY_TIME), f.at[d, "expiry"])
        if t <= 1.0 / 365.0:
            continue          # expiry day: the basis is settling, not carrying
        out[d] = float(np.clip(math.log(f.at[d, "close"] / index_close[d]) / t, -0.05, 0.15))
    return pd.Series(out).sort_index()


def _forward(spot: float, carry: float, t: float) -> float:
    return spot * math.exp(carry * t)


def _prior_session(sessions: List[date], d: date) -> Optional[date]:
    i = np.searchsorted(np.array(sessions, dtype=object), d) - 1
    return sessions[i] if i >= 0 else None


def model_check(options: pd.DataFrame, continuous: pd.DataFrame, carry: pd.Series,
                dates: set) -> pd.DataFrame:
    """Next-session error of the model on near-ATM weeklies.

    For each session D in ``dates`` with a prior session P: the nearest
    expiry after P, the CE and PE nearest P's forward, IV from P's close.
    The model prices them at D's close and at D's intraday extremes
    (CE high from the index high, PE high from the index low) and is
    compared with D's bhavcopy close and high. Expiry days are excluded
    from the close comparison (the bhavcopy close is not intrinsic).
    """
    closes = session_closes(continuous)
    sessions = sorted(closes.index)
    by_day = options.groupby("date")
    cdates = continuous["date"].map(_as_date)
    rows = []
    for d in sorted(dates):
        p = _prior_session(sessions, d)
        if p is None or p not in carry.index or p not in by_day.groups or d not in by_day.groups:
            continue
        prev, today = by_day.get_group(p), by_day.get_group(d)
        expiry = prev.loc[prev["expiry"] > p, "expiry"].min()
        if pd.isna(expiry):
            continue
        t_p = years_to_expiry(datetime.combine(p, EXPIRY_TIME), expiry)
        f_p = _forward(closes[p], carry[p], t_p)
        chain = prev[(prev["expiry"] == expiry) & (prev["trades"] > 0) & (prev["close"] > 0)]
        if chain.empty:
            continue
        strike = chain["strike"].iloc[(chain["strike"] - f_p).abs().argmin()]
        bars = continuous.loc[cdates == d]
        for cp in (1, -1):
            c_prev = chain[(chain["strike"] == strike) & (chain["cp"] == cp)]
            c_now = today[(today["expiry"] == expiry) & (today["strike"] == strike)
                          & (today["cp"] == cp)]
            if c_prev.empty or c_now.empty or not c_now["trades"].iloc[0] > 0:
                continue
            iv = float(implied_vol(c_prev["close"].iloc[0], f_p, strike, t_p, cp))
            if not np.isfinite(iv):
                continue
            carry_d = carry.get(d, carry[p])
            t_d = years_to_expiry(datetime.combine(d, EXPIRY_TIME), expiry)
            model_close = float(black76(_forward(closes[d], carry_d, t_d), strike, t_d, iv, cp))
            # Intraday high: each bar's favourable extreme at that bar's close time.
            ext = bars["high"].to_numpy(float) if cp == 1 else bars["low"].to_numpy(float)
            ts = [datetime.combine(d, time(int(h) // 100, int(h) % 100)) for h in bars["hhmm"]]
            t_bar = np.array([years_to_expiry(x, expiry) for x in ts])
            fwd_bar = ext * np.exp(carry_d * t_bar)
            model_high = float(np.max(black76(fwd_bar, strike, t_bar, iv, cp)))
            actual_close, actual_high = float(c_now["close"].iloc[0]), float(c_now["high"].iloc[0])
            rows.append({
                "date": d, "cp": cp, "strike": strike, "expiry": expiry,
                "dte": (expiry - d).days, "iv": iv,
                "model_close": model_close, "actual_close": actual_close,
                "model_high": model_high, "actual_high": actual_high,
                "close_err": (model_close - actual_close) / actual_close if d != expiry else np.nan,
                "high_err": (model_high - actual_high) / actual_high,
            })
    return pd.DataFrame(rows)


def check_summary(check: pd.DataFrame) -> dict:
    close = check["close_err"].dropna()
    high = check["high_err"].dropna()
    by_dte = (check.dropna(subset=["close_err"]).assign(abs_err=lambda x: x["close_err"].abs())
              .groupby(np.minimum(check["dte"], 7))["abs_err"].median())
    return {
        "pairs": int(len(check)),
        "close_median_abs_err": float(close.abs().median()),
        "close_p90_abs_err": float(close.abs().quantile(0.9)),
        "close_median_signed_err": float(close.median()),
        "high_median_abs_err": float(high.abs().median()),
        "high_median_signed_err": float(high.median()),
        "close_median_abs_err_by_dte": {int(k): float(v) for k, v in by_dte.items()},
    }


@dataclass
class OptionTrade:
    side: str
    entry_date: date
    exit_date: date
    expiry: date
    strike: float
    cp: int
    iv: float
    entry_prem: float
    exit_prem: float
    lot: int
    cost_rupees: float
    reason: str

    @property
    def gross_rupees(self) -> float:
        return (self.exit_prem - self.entry_prem) * self.lot

    @property
    def net_rupees(self) -> float:
        return self.gross_rupees - self.cost_rupees


def _bar_time(row) -> datetime:
    h = int(row["hhmm"])
    return datetime.combine(_as_date(row["date"]), time(h // 100, h % 100))


def express(trade: Trade, sample: pd.DataFrame, options_by_day, closes: pd.Series,
            carry: pd.Series, half_spread: float = HALF_SPREAD) -> Optional[OptionTrade]:
    """The option trade for one index trade, or None when no contract can be priced."""
    sessions = sorted(closes.index)
    p = _prior_session(sessions, trade.entry_date)
    if p is None or p not in carry.index or p not in options_by_day.groups:
        return None
    prev = options_by_day.get_group(p)
    live = prev[(prev["expiry"] > trade.entry_date) & (prev["trades"] > 0) & (prev["close"] > 0)]
    if live.empty:
        return None
    expiry = live["expiry"].min()
    cp = 1 if trade.side == "LONG" else -1
    chain = live[(live["expiry"] == expiry) & (live["cp"] == cp)]
    entry_at = _bar_time(sample.iloc[trade.entry_i])
    exit_at = _bar_time(sample.iloc[trade.exit_i])
    t_in = years_to_expiry(entry_at, expiry)
    f_in = _forward(trade.entry_px, carry[p], t_in)
    row = chain.iloc[(chain["strike"] - f_in).abs().argmin()]
    t_p = years_to_expiry(datetime.combine(p, EXPIRY_TIME), expiry)
    iv = float(implied_vol(row["close"], _forward(closes[p], carry[p], t_p), row["strike"], t_p, cp))
    if not np.isfinite(iv):
        return None
    t_out = years_to_expiry(exit_at, expiry)
    f_out = _forward(trade.exit_px, carry[p], t_out)
    entry_prem = max(float(black76(f_in, row["strike"], t_in, iv, cp)), MIN_PREMIUM)
    exit_prem = max(float(black76(f_out, row["strike"], t_out, iv, cp)), MIN_PREMIUM)
    lot = int(row["lot"])
    cost = (option_cost(entry_prem, lot, False, trade.entry_date, half_spread)
            + option_cost(exit_prem, lot, True, trade.exit_date, half_spread))
    return OptionTrade(trade.side, trade.entry_date, trade.exit_date, expiry, float(row["strike"]),
                       cp, iv, entry_prem, exit_prem, lot, cost, trade.exit_reason)


def run(continuous: pd.DataFrame, params: Params, want: set, options: pd.DataFrame,
        carry: pd.Series, half_spread: float = HALF_SPREAD) -> dict:
    built = add_indicators(continuous, params)
    sample = built.loc[built["date"].map(_as_date).isin(want)].reset_index(drop=True)
    index_trades = simulate(sample, params).trades
    closes = session_closes(continuous)
    by_day = options.groupby("date")
    priced = [express(t, sample, by_day, closes, carry, half_spread) for t in index_trades]
    trades = [t for t in priced if t is not None]
    return summarize(trades, sorted(want), len(index_trades) - len(trades))


def summarize(trades: List[OptionTrade], sessions: List[date], unpriced: int) -> dict:
    daily = pd.Series(0.0, index=pd.Index(sessions))
    for t in trades:
        daily.loc[t.exit_date] += t.net_rupees
    std = float(daily.std(ddof=1)) if len(daily) > 1 else 0.0
    sharpe = float(daily.mean() / std * math.sqrt(252)) if std > 0 else 0.0
    equity = daily.cumsum()
    gross = sum(t.gross_rupees for t in trades)
    costs = sum(t.cost_rupees for t in trades)
    windows = [float(w.sum()) for w in np.array_split(daily, 3)]
    n = len(trades)
    return {
        "trades": n,
        "unpriced": unpriced,
        "gross_rupees": gross,
        "cost_rupees": costs,
        "net_rupees": gross - costs,
        "net_at_cost_stress": gross - COST_STRESS * costs,
        "win_rate": sum(t.net_rupees > 0 for t in trades) / n if n else 0.0,
        "sharpe": sharpe,
        "max_dd_rupees": float((equity.cummax() - equity).max()),
        "windows": windows,
        "median_entry_premium": float(np.median([t.entry_prem for t in trades])) if n else 0.0,
        "median_iv": float(np.median([t.iv for t in trades])) if n else 0.0,
        "median_premium_move": float(np.median([t.exit_prem - t.entry_prem for t in trades])) if n else 0.0,
        "lots": sorted({t.lot for t in trades}),
    }


def verdict(s: dict) -> str:
    if (s["trades"] < MIN_TRADES or s["net_rupees"] <= 0 or s["sharpe"] <= 0
            or s["net_at_cost_stress"] <= 0 or all(w < 0 for w in s["windows"])):
        return "KILL"
    return "PASSES_TRAIN"


def prose(s: dict) -> str:
    return (f"{s['trades']} trades ({s['unpriced']} unpriced), net ₹{s['net_rupees']:,.0f} "
            f"(gross ₹{s['gross_rupees']:,.0f}, costs ₹{s['cost_rupees']:,.0f}), "
            f"at {COST_STRESS}x costs ₹{s['net_at_cost_stress']:,.0f}, win {s['win_rate']:.0%}, "
            f"Sharpe {s['sharpe']:.2f}, max DD ₹{s['max_dd_rupees']:,.0f}, windows "
            + ", ".join(f"₹{w:,.0f}" for w in s["windows"])
            + f". Median entry premium {s['median_entry_premium']:.1f}, IV "
            f"{s['median_iv']:.1%}, premium move {s['median_premium_move']:+.1f}. Lots {s['lots']}.")


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (np.floating, np.integer)):
        return v.item()
    if isinstance(v, date):
        return v.isoformat()
    return v


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw-dir", default="data_cache/research_bhavcopy_raw")
    ap.add_argument("--five-min-dir", default="data_cache/research_nifty_5min")
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 20))
    ap.add_argument("--accept-short", type=date.fromisoformat, action="append",
                    default=[date(2025, 1, 31)])
    ap.add_argument("--options-cache", default=None,
                    help="parquet of load_nifty_options output, to skip the archive scan")
    ap.add_argument("--out", default="/tmp/options_breakout.json")
    args = ap.parse_args(argv)

    from market_data.fetch_index_5min import load_all
    front = load_nifty_front(Path(args.raw_dir))
    continuous, _ = five_min_span(load_all(Path(args.five_min_dir)), front, args.end,
                                  args.accept_short)
    train, _, cut = split_train_holdout(ready_frame(add_indicators(continuous, Params())))
    train_dates = set(train["date"].map(_as_date))
    print(f"train {len(train_dates)} sessions {min(train_dates)} -> {max(train_dates)}; "
          f"holdout from {cut} is not scored (already seen for this signal)", flush=True)

    cache = Path(args.options_cache) if args.options_cache else None
    if cache and cache.exists():
        options = pd.read_parquet(cache)
        for col in ("date", "expiry"):
            options[col] = pd.to_datetime(options[col]).dt.date
    else:
        options = load_nifty_options(Path(args.raw_dir))
        if cache:
            options.to_parquet(cache)
    carry = carry_by_date(front, session_closes(continuous))
    print(f"option rows {len(options)}; carry median {carry.median():.2%}", flush=True)

    check = check_summary(model_check(options, continuous, carry, train_dates))
    print("model check (train sessions):", json.dumps(_jsonable(check)), flush=True)
    payload: Dict[str, object] = {"model_check": check, "train_start": min(train_dates),
                                  "train_end": max(train_dates)}
    if check["close_median_abs_err"] > MODEL_MAX_MEDIAN_ERR:
        print(f"STOP: median close error {check['close_median_abs_err']:.1%} > "
              f"{MODEL_MAX_MEDIAN_ERR:.0%}; the model cannot carry the test", flush=True)
        Path(args.out).write_text(json.dumps(_jsonable(payload), indent=2))
        return 1

    published = run(continuous, Params(), train_dates, options, carry)
    v = verdict(published)
    print("published defaults, train:", prose(published), flush=True)
    print(f"verdict (pre-registered): {v}", flush=True)
    payload.update({"published": published, "verdict": v, "described": {}})

    print("described only (not gated):", flush=True)
    for half_spread in (0.0, 1.0):
        s = run(continuous, Params(), train_dates, options, carry, half_spread)
        payload["described"][f"published_half_spread_{half_spread}"] = s
        print(f"  published, half-spread {half_spread}: {prose(s)}", flush=True)
    for trail, bte in ((40.0, 10), (80.0, 10), (1e6, 10), (150.0, 20), (1e6, 20)):
        s = run(continuous, replace(Params(), trail_amount=trail, bars_to_exit=bte),
                train_dates, options, carry)
        name = f"trail {'off' if trail > 1e5 else int(trail)} / {bte} bars"
        payload["described"][name] = s
        print(f"  {name}: {prose(s)}", flush=True)
    Path(args.out).write_text(json.dumps(_jsonable(payload), indent=2))
    print(f"wrote {args.out}", flush=True)
    if v != "KILL":
        print("A train pass is not a paper promotion: record real option quotes first.",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
