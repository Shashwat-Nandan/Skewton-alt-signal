"""
Sector-demeaned short-term reversal on NSE single-stock futures — offline test
=============================================================================
FT §6.2 (``docs/research/strategy-finetuning-profitability-2026-08-30.md``),
sharpened by ``docs/research/drive-research-library-review-2026-09-26.md`` §2
(Da, Liu & Schaumburg: only the within-industry residual reverses).

PRE-REGISTERED SPEC — frozen 2026-09-27 before the first run. Changing any
line below after seeing a result makes it a new test, not a retune.

Data
  UDiFF F&O bhavcopy, 2024-07-08 → latest, from
  ``data_cache/research_bhavcopy_raw/`` (NOT ``bhavcopy_raw/``: the live pair
  screener reads that directory in full, so research history must never be
  written there). Populate with ``backfill()`` below. Broker-fallback days
  (``.broker-fallback``, or a leftover ``.kite-fallback`` = partial STF
  list) are excluded.

Returns
  Same-contract close-to-close: the return on day t uses the contract that
  was front-month at t−1, so an expiry roll never enters a return. Renames
  and history cutoffs via ``core.universe.apply_long``.

Universe (per day t)
  STF names with a return at t, a sector in ``market_data/sectors.csv``, and
  20-session median traded value (volume × close, through t) NOT in the
  bottom quintile of that day's names. No F&O ban-list or earnings
  blackout: neither dataset exists on this host — both omissions bias the
  result UP, so a NO-GO is robust to them and a GO is not.

Signal (at close t)
  x_i = −(r_i,t − mean r over i's sector at t). A sector with < 3 eligible
  names that day falls back to the market mean (count reported).

Portfolio
  PRIMARY: long top quintile of x (relative losers), short bottom quintile,
  equal weight, each side = 1 notional; held close t → close t+1.
  REPORTED, not used for the verdict: 6+6 extremes (the book FT says paper
  would actually run) and a 5-day hold (5 overlapping daily cohorts, 1/5
  each) — the 5-day hold is FT's ONLY permitted retry if 1-day dies on cost.

Cost (per unit of notional traded, i.e. per |Δweight|)
  ``core.costs.estimate_transaction_cost`` at 1 lot, average of BUY and SELL
  side, with STT at the rate in force on the date (``FUT_STT_SCHEDULE``),
  and 5 bp slippage per side in total (FT paper-pair convention; 2 bp of it
  is inside ``core.costs``). Corrected 2026-09-27: the first runs charged
  today's STT on all history and double-counted slippage (7 bp/side).

Verdict
  Holdout = final 40 % of sessions. KILL if holdout net annualised Sharpe
  ≤ 0 (FT §6.2). Full-sample t-stat reported alongside.

Corporate actions (fixed 2026-09-27, after the first verdict)
  The first run did not adjust for splits, bonuses or demergers: an ex-date
  appeared as a fake −50 % to −90 % move. ``corporate_action_adjusted``
  now rescales split/bonus days by the lot-size ratio and drops unexplained
  moves beyond ±35 % (demergers). This is a data fix, not a spec change;
  both verdicts are in the review doc §2.2.

Usage
  python -m research.backtest_sector_reversal --backfill   # fetch history (slow, once)
  python -m research.backtest_sector_reversal              # run the test
  python -m research.backtest_sector_reversal --mis        # cash-intraday variant

MIS variant — a separate test, asked 2026-09-27. Not a retune of the spec
above. An exploratory split of the frozen book (found by looking, on this
same sample) put the gross in the next session, about +6.8 bp/day open to
close, and the overnight gap at about −3.7. This run prices that session as
cash intraday (MIS). A positive result here is not out-of-sample
confirmation. The 2016–2023 cash bhavcopy that would be is not on this host,
and this command does not fetch it.

  Signal and quintile book: unchanged, formed at close t.
  Trade: long the losers and short the winners at the open of t+1, flat by
  that close. The contract priced is the one that was front at t. Futures
  open/close stand in for cash; the basis sits mostly in the overnight gap,
  which this trade does not hold. No 5-day book: MIS cannot carry.
  Cost: ``estimate_equity_cost`` product MIS, buy and sell, every day, on
  the whole position (nothing carries, so an unchanged book pays again).
  Side notional is ₹10 lakh, the unit used in the vehicle comparison. Each
  name is sized ``side × |weight|``, so a ~25-name quintile does not receive
  the ₹20 brokerage cap — that cap binds only above about ₹67k per order.
  Statutory cost (no slippage) is the verdict. Two sensitivities are
  reported beside it: 5 bp per order, and every order forced to ₹10 lakh
  (the capped 4 bp round trip; that book is tens of crores). Kill rule is
  the same holdout net Sharpe ≤ 0. Daily bars: the
  bhavcopy open and close are the only prices this host has for the universe.
"""

import argparse
import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
LIVE_RAW_DIR = REPO / "data_cache" / "bhavcopy_raw"
RESEARCH_RAW_DIR = REPO / "data_cache" / "research_bhavcopy_raw"
UDIFF_START = datetime(2024, 7, 8)

# Frozen spec constants (see docstring) — not CLI knobs on purpose.
LIQ_WINDOW = 20          # sessions for median traded value
LIQ_DROP_QUANTILE = 0.2  # drop the bottom quintile by that median
MIN_SECTOR_NAMES = 3     # below this, demean against the market instead
QUANTILE = 0.2           # primary book: top/bottom quintile
EXTREMES_N = 6           # reported 6+6 book
HOLD_5D = 5              # reported overlapping-cohort hold
# estimate_transaction_cost already carries 2 bp/side of futures slippage;
# this tops it up to the spec's 5 bp/side. (Was 5 bp ON TOP until
# 2026-09-27 — slippage double-counted at 7 bp/side.)
SLIPPAGE = 0.0003
MODEL_FUT_STT = 0.0005   # sell-side futures STT hard-coded in core.costs (current rate)
# Futures sell-side STT in force by date (Finance Act 2024: 0.0125% → 0.02%
# from 2024-10-01; Budget 2026: → 0.05% from 2026-04-01). core.costs uses
# today's rate, which is right for live gates and wrong for history.
FUT_STT_SCHEDULE = [(date(2024, 10, 1), 0.000125), (date(2026, 4, 1), 0.0002)]
HOLDOUT_FRAC = 0.4       # final 40 % of sessions
ANN = 252
CA_MAX_ABS_RET = 0.35    # unexplained same-contract move beyond this → no return
# Vehicle-comparison unit (review doc §2.2): ₹10 lakh per side, not per name.
MIS_SIDE_NOTIONAL = 1_000_000.0
# Same rupee figure as one order. The ₹20 brokerage cap binds here, which is
# the 4 bp round trip in the vehicle table. A quintile book at this order
# size is tens of crores and is reported, not used for the verdict.
MIS_NAME_NOTIONAL = 1_000_000.0
MIS_SLIPPAGE_BPS = 5.0   # per order, same 5 bp the futures spec uses per side

# Partial same-day boards. Both names: new writes are .broker-fallback,
# leftovers from the previous Kotak-via-Kite-named path are .kite-fallback.
_FALLBACK_SUFFIXES = (".broker-fallback", ".kite-fallback")


def _fallback_marker_names(raw_dir: Path) -> list:
    return sorted(
        p.name
        for suffix in _FALLBACK_SUFFIXES
        for p in raw_dir.glob(f"*{suffix}")
    )


def backfill(raw_dir: Path = RESEARCH_RAW_DIR, start: datetime = UDIFF_START,
             end: datetime = None) -> None:
    """Fetch UDiFF bhavcopies into ``raw_dir`` and copy in the live archive.

    Redirects ``fetch_bhavcopy.RAW_DIR`` for the duration so its cache writes
    land in ``raw_dir``; restored in ``finally``. Historical days never reach
    the Kotak same-day fallback (that branch is today-only).
    """
    import time

    import requests

    from market_data import fetch_bhavcopy as fb

    raw_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in sorted(LIVE_RAW_DIR.glob("bhavcopy_fo_*.parquet")):
        if any(f.with_suffix(suffix).exists() for suffix in _FALLBACK_SUFFIXES):
            logger.warning("skip %s: broker-fallback day (partial STF list)", f.name)
            continue
        dst = raw_dir / f.name
        if not dst.exists():
            shutil.copy2(f, dst)
            copied += 1
    logger.info("copied %d live-archive days into %s", copied, raw_dir)

    first_live = min((f.stem.removeprefix("bhavcopy_fo_")
                      for f in LIVE_RAW_DIR.glob("bhavcopy_fo_*.parquet")), default=None)
    end = end or (datetime.strptime(first_live, "%Y%m%d") if first_live else datetime.now())
    days = [d for d in fb.trading_days(start, end, fb.load_holidays())
            if d.strftime("%Y%m%d") != first_live]
    saved_raw_dir = fb.RAW_DIR
    fb.RAW_DIR = raw_dir
    got = 0
    try:
        session = requests.Session()
        for i, day in enumerate(days, 1):
            if fb._download_bhavcopy(day, session) is not None:
                got += 1
            if i % 50 == 0:
                logger.info("  %d/%d days tried, %d present", i, len(days), got)
            time.sleep(fb.RATE_LIMIT_DELAY)
    finally:
        fb.RAW_DIR = saved_raw_dir
    logger.info("backfill: %d/%d weekdays present (misses = holidays or NSE gaps)",
                got, len(days))


def same_contract_returns(stf: pd.DataFrame) -> pd.DataFrame:
    """Per (date, symbol): return in the contract that was front-month on the
    PREVIOUS session, plus that day's front-month close / lot / traded value.

    ``stf`` is ``load_stf_panel`` output. A symbol absent on the previous
    session gets no return for t (no spanning gaps). Traded value is
    ``volume × lot × close`` — ``TtlTradgVol`` counts contracts.
    """
    df = stf[stf["expiry"] >= stf["date"]]
    sessions = sorted(df["date"].unique())
    prev_of = dict(zip(sessions[1:], sessions[:-1]))
    # The contract held over close t must outlive t: on an expiry day the
    # expiring contract cannot be carried, so "front" = nearest expiry > t.
    # It is still priced on its own expiry day via `px` below.
    alive = df[df["expiry"] > df["date"]]
    front = (alive.loc[alive.groupby(["date", "symbol"])["expiry"].idxmin(),
                    ["date", "symbol", "expiry", "close", "lot_size", "volume"]]
             .rename(columns={"expiry": "front"}))
    front["value"] = front["volume"] * front["lot_size"] * front["close"]
    px = df.set_index(["date", "symbol", "expiry"])["close"]
    lot = df.set_index(["date", "symbol", "expiry"])["lot_size"]
    held = front[["date", "symbol", "front"]].copy()
    held["date"] = held["date"].map({v: k for k, v in prev_of.items()})
    held = held.dropna(subset=["date"])        # last session has no successor
    # held: (t, symbol, contract that was front at t−1)
    prev_date = held["date"].map(prev_of)
    p_now = px.reindex(list(zip(held["date"], held["symbol"], held["front"]))).to_numpy()
    k_prev = list(zip(prev_date, held["symbol"], held["front"]))
    k_now = list(zip(held["date"], held["symbol"], held["front"]))
    p_prev = px.reindex(k_prev).to_numpy()
    held["ret"] = corporate_action_adjusted(
        p_now, p_prev, lot.reindex(k_now).to_numpy(), lot.reindex(k_prev).to_numpy(),
        held["date"].to_numpy(), held["symbol"].to_numpy())
    out = front.drop(columns=["volume"]).merge(
        held[["date", "symbol", "ret"]], on=["date", "symbol"], how="left")
    return out.sort_values(["date", "symbol"]).reset_index(drop=True)


def corporate_action_adjusted(p_now, p_prev, lot_now, lot_prev, dates, symbols):
    """Same-contract return with split/bonus/demerger ex-dates handled.

    NSE's bhavcopy carries no adjustment factor (``PrvsClsgPric`` is the raw
    previous close, checked 2026-09-27). Two rules instead:

    * Lot size of the held contract changed → splits and bonuses rescale the
      lot by the split ratio, so ``p_now · lot_now / lot_prev`` is on the old
      basis. Routine lot revisions change the lot with NO price change, where
      rescaling would invent a jump — so the rescaled return is used only
      when it is smaller in magnitude than the raw one.
    * Any remaining |return| > CA_MAX_ABS_RET (demergers: no lot change, no
      factor) → NaN, each one logged. The largest genuine one-day move seen
      in 2024-07 → 2026-09 was −29 %; the demergers were −40 % to −66 %.
    """
    raw = p_now / p_prev - 1.0
    scaled = p_now * lot_now / lot_prev / p_prev - 1.0
    use_scaled = (lot_now != lot_prev) & (np.abs(scaled) < np.abs(raw))
    ret = np.where(use_scaled, scaled, raw)
    wild = np.abs(ret) > CA_MAX_ABS_RET
    if wild.any():
        logger.warning("dropped %d unexplained |return| > %.0f%% (likely demergers): %s",
                       int(wild.sum()), 100 * CA_MAX_ABS_RET,
                       ", ".join(f"{s} {d} {r:+.0%}" for s, d, r in
                                 zip(symbols[wild], dates[wild], ret[wild])))
    logger.info("lot-rescaled %d split/bonus returns", int(use_scaled.sum()))
    return np.where(wild, np.nan, ret)


def add_signal(panel: pd.DataFrame, sectors: Dict[str, str]) -> pd.DataFrame:
    """Liquidity gate + sector-demeaned reversal score ``x`` (higher = buy).

    Adds ``eligible``, ``sector`` and ``fallback`` (True where the sector had
    < MIN_SECTOR_NAMES eligible names and the market mean was used).
    """
    df = panel.sort_values(["symbol", "date"]).copy()
    df["value_med"] = (df.groupby("symbol")["value"]
                       .transform(lambda v: v.rolling(LIQ_WINDOW, min_periods=LIQ_WINDOW).median()))
    df["sector"] = df["symbol"].map(sectors)
    base = df["ret"].notna() & df["value_med"].notna() & df["sector"].notna()
    # Percentile RANK, not `> quantile`: with tied values the quantile equals
    # every value and a strict `>` would exclude the whole day.
    pct = df[base].groupby("date")["value_med"].rank(pct=True)
    df["eligible"] = False
    df.loc[pct.index, "eligible"] = pct > LIQ_DROP_QUANTILE
    e = df[df["eligible"]]
    sec_n = e.groupby(["date", "sector"])["ret"].transform("size")
    sec_mean = e.groupby(["date", "sector"])["ret"].transform("mean")
    mkt_mean = e.groupby("date")["ret"].transform("mean")
    fallback = sec_n < MIN_SECTOR_NAMES
    df["fallback"] = False
    df.loc[e.index, "fallback"] = fallback
    df["x"] = np.nan
    df.loc[e.index, "x"] = -(e["ret"] - np.where(fallback, mkt_mean, sec_mean))
    return df.sort_values(["date", "symbol"]).reset_index(drop=True)


def daily_weights(sig: pd.DataFrame, extremes: int = None) -> pd.DataFrame:
    """Wide weights (index=date, cols=symbol) set at each close.

    Long the top of ``x`` (relative losers), short the bottom; each side sums
    to 1. ``extremes=None`` → quintiles, else top/bottom ``extremes`` names.
    """
    rows = []
    for d, g in sig[sig["eligible"]].groupby("date"):
        g = g.sort_values("x")
        k = extremes if extremes else int(len(g) * QUANTILE)
        if k < 1 or len(g) < 2 * k:
            continue
        rows += [(d, s, -1.0 / k) for s in g["symbol"].iloc[:k]]
        rows += [(d, s, 1.0 / k) for s in g["symbol"].iloc[-k:]]
    w = pd.DataFrame(rows, columns=["date", "symbol", "w"])
    return w.pivot(index="date", columns="symbol", values="w").fillna(0.0)


def overlapping(w: pd.DataFrame, hold: int) -> pd.DataFrame:
    """Jegadeesh–Titman: average of the last ``hold`` daily cohorts."""
    return w.rolling(hold, min_periods=hold).mean().dropna(how="all")


def fut_stt_rate(d: date) -> float:
    """Futures sell-side STT in force on ``d``."""
    for until, rate in FUT_STT_SCHEDULE:
        if d < until:
            return rate
    return MODEL_FUT_STT


def side_cost_frac(panel: pd.DataFrame) -> pd.DataFrame:
    """Cost of trading one unit of notional in one direction, per (date, symbol).

    ``core.costs`` charges today's STT; the historical rate is swapped in
    here (STT is sell-side only, so it averages to half the rate per side).
    """
    from core.costs import estimate_transaction_cost

    def one(row):
        notional = row.close * row.lot_size
        buy = estimate_transaction_cost(row.close, 1, row.lot_size, "BUY", "FUT")
        sell = estimate_transaction_cost(row.close, 1, row.lot_size, "SELL", "FUT")
        stt_fix = (fut_stt_rate(row.date) - MODEL_FUT_STT) / 2.0
        return (buy + sell) / 2.0 / notional + stt_fix + SLIPPAGE

    c = panel[["date", "symbol"]].copy()
    c["c"] = [one(r) for r in panel[["date", "close", "lot_size"]].itertuples(index=False)]
    return c.pivot(index="date", columns="symbol", values="c")


def simulate(w: pd.DataFrame, rets: pd.DataFrame, cost: pd.DataFrame) -> pd.DataFrame:
    """Daily P&L of weights set at close t, earned over t → t+1.

    ``rets`` / ``cost`` are wide (date × symbol). A held name with no
    return at t+1 earns 0 and is counted in ``missing``. Cost is charged
    on |Δw| at the rebalance close and booked to the same period.
    """
    dates = rets.index
    nxt = dict(zip(dates[:-1], dates[1:]))
    w = w.reindex(columns=rets.columns, fill_value=0.0)
    prev = pd.Series(0.0, index=rets.columns)
    out = []
    for d in w.index:
        if d not in nxt:
            continue
        wt = w.loc[d]
        r = rets.loc[nxt[d]]
        held = wt != 0
        missing = int((held & r.isna()).sum())
        gross = float((wt * r.fillna(0.0)).sum())
        dw = (wt - prev).abs()
        tc = float((dw * cost.loc[d].reindex(dw.index)).fillna(0.0).sum())
        out.append((nxt[d], gross, tc, gross - tc, float(dw.sum()), missing))
        prev = wt
    return pd.DataFrame(out, columns=["date", "gross", "cost", "net", "turnover", "missing"]
                        ).set_index("date")


def stats(pnl: pd.Series) -> Dict[str, float]:
    n = len(pnl)
    sd = pnl.std(ddof=1)
    return {
        "days": n,
        "mean_bp": 1e4 * pnl.mean(),
        "sharpe": float(np.sqrt(ANN) * pnl.mean() / sd) if sd > 0 else float("nan"),
        "t": float(np.sqrt(n) * pnl.mean() / sd) if sd > 0 else float("nan"),
    }


def mis_roundtrip_frac(notional: float, slippage_bps: float) -> float:
    """Buy-plus-sell MIS cost as a fraction of ``notional``.

    ``estimate_equity_cost`` caps brokerage at ₹20 per order. That cap binds
    near ₹67k, so the fraction depends on the order size and must be priced
    per name, not once for the whole side.
    """
    from core.costs import estimate_equity_cost

    if notional <= 0:
        return 0.0
    buy = estimate_equity_cost(notional, 1, "BUY", "MIS", slippage_bps=slippage_bps)
    sell = estimate_equity_cost(notional, 1, "SELL", "MIS", slippage_bps=slippage_bps)
    return (buy + sell) / notional


def load_stf_opens_closes(raw_dir: Path) -> pd.DataFrame:
    """STF open and close keyed by date, canonical symbol, expiry."""
    from core.data_cache_io import find_tables, read_table
    from core.universe import canonical

    frames = []
    for f in find_tables(raw_dir, "bhavcopy_fo_*"):
        df = read_table(
            f,
            usecols=["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "OpnPric", "ClsPric"],
            dtype={"TckrSymb": str},
        )
        df = df[df["FinInstrmTp"] == "STF"]
        if df.empty:
            continue
        frames.append(df)
    if not frames:
        raise RuntimeError(f"No STF opens in {raw_dir}")
    out = pd.concat(frames, ignore_index=True)
    out = out.rename(columns={
        "TradDt": "date", "TckrSymb": "symbol", "XpryDt": "expiry",
        "OpnPric": "open", "ClsPric": "close",
    })
    out["date"] = pd.to_datetime(out["date"]).dt.date
    out["expiry"] = pd.to_datetime(out["expiry"]).dt.date
    out["symbol"] = out["symbol"].map(canonical)
    out["open"] = pd.to_numeric(out["open"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.drop_duplicates(["date", "symbol", "expiry"])


def next_session_open_to_close(panel: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Open-to-close return earned the session after each panel row.

    The contract is ``panel['front']`` — front-month at the signal close.
    Open and close are that next session, so an overnight split or demerger
    is outside the return. A missing or non-positive open is NaN: no fill.
    """
    sessions = sorted(panel["date"].unique())
    nxt = dict(zip(sessions[:-1], sessions[1:]))
    px = prices.set_index(["date", "symbol", "expiry"])
    rows = panel.loc[panel["date"].map(nxt).notna(), ["date", "symbol", "front"]].copy()
    rows["earn"] = rows["date"].map(nxt)
    keys = list(zip(rows["earn"], rows["symbol"], rows["front"]))
    op = px["open"].reindex(keys).to_numpy(dtype=float)
    cl = px["close"].reindex(keys).to_numpy(dtype=float)
    good = np.isfinite(op) & np.isfinite(cl) & (op > 0)
    ret = np.full(len(op), np.nan)
    ret[good] = cl[good] / op[good] - 1.0
    return pd.DataFrame({
        "date": rows["earn"].to_numpy(), "symbol": rows["symbol"].to_numpy(), "ret": ret,
    })


def simulate_mis(w: pd.DataFrame, rets: pd.DataFrame, side_notional: float,
                 slippage_bps: float, order_notional: float = None) -> pd.DataFrame:
    """P&L of a book entered at the next open and flat by that close.

    Cost is a full MIS round trip on every name actually priced. A missing
    open is not a fill: it earns nothing and pays nothing, and is counted
    in ``missing``. An unchanged book still pays, because yesterday's
    position was closed.

    ``order_notional`` fixes every order at one size (the vehicle table's
    ₹10 lakh name, where the ₹20 brokerage cap binds). Left unset, each
    order is ``side_notional × |weight|``.
    """
    dates = list(rets.index)
    nxt = dict(zip(dates[:-1], dates[1:]))
    w = w.reindex(columns=rets.columns, fill_value=0.0)
    out = []
    for d in w.index:
        if d not in nxt:
            continue
        wt = w.loc[d]
        r = rets.loc[nxt[d]].reindex(wt.index)
        held = wt != 0
        tradable = held & r.notna()
        gross = float((wt[tradable] * r[tradable]).sum())
        cost = 0.0
        for sym in wt.index[tradable]:
            weight = float(wt[sym])
            notion = order_notional if order_notional is not None else side_notional * abs(weight)
            cost += abs(weight) * mis_roundtrip_frac(notion, slippage_bps)
        out.append((nxt[d], gross, cost, gross - cost, int((held & r.isna()).sum()),
                    int(tradable.sum())))
    return pd.DataFrame(
        out, columns=["date", "gross", "cost", "net", "missing", "names"],
    ).set_index("date")


def run_mis(raw_dir: Path = RESEARCH_RAW_DIR) -> Dict[str, Dict[str, pd.DataFrame]]:
    """Quintile and 6+6 books, statutory cost and the 5 bp/order case."""
    from core.backtest_timeframe import warn_coarse_timeframe

    from market_data.fetch_sectors import load_sector_map
    from research.backtest_arbitrage import load_stf_panel

    warn_coarse_timeframe(
        "daily",
        backtest="research.backtest_sector_reversal --mis",
        reason="the bhavcopy open and close are the only cash-session prices "
               "on this host; there is no 5-minute tape for this universe",
    )
    fallback_days = _fallback_marker_names(raw_dir)
    if fallback_days:
        raise RuntimeError(f"broker-fallback days in {raw_dir}: {fallback_days}")
    panel = same_contract_returns(load_stf_panel(raw_dir=raw_dir))
    sig = add_signal(panel, load_sector_map())
    session = next_session_open_to_close(panel, load_stf_opens_closes(raw_dir))
    rets = session.pivot(index="date", columns="symbol", values="ret")
    books = {
        "quintile MIS (PRIMARY)": daily_weights(sig),
        "extremes6 MIS": daily_weights(sig, EXTREMES_N),
    }
    out = {}
    for name, w in books.items():
        out[name] = {
            "statutory": simulate_mis(w, rets, MIS_SIDE_NOTIONAL, 0.0),
            "slip5": simulate_mis(w, rets, MIS_SIDE_NOTIONAL, MIS_SLIPPAGE_BPS),
            "per_name": simulate_mis(
                w, rets, MIS_SIDE_NOTIONAL, 0.0, order_notional=MIS_NAME_NOTIONAL),
        }
    return out


def report_mis(results: Dict[str, Dict[str, pd.DataFrame]]) -> str:
    lines = [
        f"MIS open-to-close, side notional ₹{MIS_SIDE_NOTIONAL:,.0f}. "
        "Gross does not depend on the cost column. This sample is where the "
        "open-to-close split was found.",
    ]
    primary = None
    for name, cols in results.items():
        base = cols["statutory"]
        slip = cols["slip5"]
        per_name = cols["per_name"]
        split = base.index[int(len(base) * (1 - HOLDOUT_FRAC))]
        if primary is None:
            primary = (base, split)
        lines.append(
            f"\n{name}   (holdout from {split}; names/day {base['names'].mean():.0f}; "
            f"missing-open cells {int(base['missing'].sum())})"
        )
        for label, part, part_s, part_n in (
            ("full", base, slip, per_name),
            ("holdout", base[base.index >= split], slip[slip.index >= split],
             per_name[per_name.index >= split]),
        ):
            g, n = stats(part["gross"]), stats(part["net"])
            ns, nn = stats(part_s["net"]), stats(part_n["net"])
            lines.append(
                f"  {label:<8} days {n['days']:>3}  gross {g['mean_bp']:6.1f} bp/d "
                f"t {g['t']:5.2f} | statutory cost {1e4 * part['cost'].mean():5.1f} "
                f"net {n['mean_bp']:6.1f} SR {n['sharpe']:6.2f} | "
                f"+{MIS_SLIPPAGE_BPS:.0f}bp/order net {ns['mean_bp']:6.1f} SR {ns['sharpe']:6.2f} | "
                f"₹10L/name net {nn['mean_bp']:6.1f} SR {nn['sharpe']:6.2f}"
            )
    base, split = primary
    hold_sr = stats(base.loc[base.index >= split, "net"])["sharpe"]
    verdict = "KILL (holdout net Sharpe <= 0)" if not hold_sr > 0 else "SURVIVES the FT kill rule"
    lines.append(f"\nVERDICT (primary, statutory MIS): {verdict} — holdout net SR {hold_sr:.2f}")
    return "\n".join(lines)


def run(raw_dir: Path = RESEARCH_RAW_DIR) -> Dict[str, pd.DataFrame]:
    from market_data.fetch_sectors import load_sector_map
    from research.backtest_arbitrage import load_stf_panel

    fallback_days = _fallback_marker_names(raw_dir)
    if fallback_days:
        raise RuntimeError(f"broker-fallback days in {raw_dir}: {fallback_days}")
    panel = same_contract_returns(load_stf_panel(raw_dir=raw_dir))
    sectors = load_sector_map()
    unmapped = sorted(set(panel["symbol"]) - set(sectors))
    if unmapped:
        logger.warning("%d symbols have no sector and are excluded: %s",
                       len(unmapped), unmapped)
    sig = add_signal(panel, sectors)
    e = sig[sig["eligible"]]
    logger.info("sessions %d (%s → %s); eligible names/day median %d; "
                "sector-fallback share %.1f%%",
                sig["date"].nunique(), sig["date"].min(), sig["date"].max(),
                int(e.groupby("date").size().median()), 100 * e["fallback"].mean())
    rets = sig.pivot(index="date", columns="symbol", values="ret")
    cost = side_cost_frac(sig).reindex(index=rets.index, columns=rets.columns)
    wq = daily_weights(sig)
    books = {
        "quintile_1d (PRIMARY)": wq,
        "extremes6_1d": daily_weights(sig, EXTREMES_N),
        "quintile_5d": overlapping(wq, HOLD_5D),
    }
    return {name: simulate(w, rets, cost) for name, w in books.items()}


def report(results: Dict[str, pd.DataFrame]) -> str:
    lines = []
    for name, pnl in results.items():
        split = pnl.index[int(len(pnl) * (1 - HOLDOUT_FRAC))]
        rows = [("full", pnl), ("in-sample", pnl[pnl.index < split]),
                ("holdout", pnl[pnl.index >= split])]
        lines.append(f"\n{name}   (holdout from {split}; turnover/day "
                     f"{pnl['turnover'].mean():.2f}; cost/day "
                     f"{1e4 * pnl['cost'].mean():.1f} bp; missing-return cells "
                     f"{int(pnl['missing'].sum())})")
        for label, part in rows:
            g, n = stats(part["gross"]), stats(part["net"])
            lines.append(f"  {label:<9} days {n['days']:>3}  gross {g['mean_bp']:6.1f} bp/d "
                         f"SR {g['sharpe']:5.2f} t {g['t']:5.2f} | net {n['mean_bp']:6.1f} bp/d "
                         f"SR {n['sharpe']:5.2f} t {n['t']:5.2f}")
    primary = next(iter(results.values()))
    split = primary.index[int(len(primary) * (1 - HOLDOUT_FRAC))]
    hold_sr = stats(primary.loc[primary.index >= split, "net"])["sharpe"]
    verdict = "KILL (holdout net Sharpe <= 0)" if not hold_sr > 0 else "SURVIVES the FT kill rule"
    lines.append(f"\nVERDICT (primary, FT §6.2): {verdict} — holdout net SR {hold_sr:.2f}")
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--backfill", action="store_true", help="fetch history, then exit")
    p.add_argument("--mis", action="store_true",
                   help="price the open-to-close book as cash intraday (MIS)")
    args = p.parse_args()
    if args.backfill:
        backfill()
        return 0
    print(report_mis(run_mis()) if args.mis else report(run()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
