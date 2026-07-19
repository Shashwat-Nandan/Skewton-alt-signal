"""
Open Interest confluence signal for the equity-swing strategy.

Reads the F&O bhavcopy archive (``data_cache/bhavcopy_raw/``) — already
present in the repo for the calendar/arbitrage strategies — and produces a
per-(date, symbol) classification of the price-vs-OI relationship over a
rolling N-day window.

Classifications (Varsity Module 5 / Module 9):

  ``LONG_BUILDUP``     price ↑  +  OI ↑     bullish, fresh long money
  ``SHORT_COVERING``   price ↑  +  OI ↓     bullish but unsustainable
  ``SHORT_BUILDUP``    price ↓  +  OI ↑     bearish, fresh short money
  ``LONG_UNWINDING``   price ↓  +  OI ↓     bearish but profit-taking
  ``NEUTRAL``          insufficient move OR data missing

Signal use in the strategy
--------------------------
For LONG entries:
  - LONG_BUILDUP    -> score boost (high-conviction bull confluence)
  - SHORT_COVERING  -> neutral (no boost, no veto — unsustainable)
  - SHORT_BUILDUP   -> veto (strong short money is fresh; trend gate is suspect)
  - LONG_UNWINDING  -> neutral
  - NEUTRAL         -> neutral

Stocks not in F&O have no STF row — the helper returns ``NEUTRAL`` and
the gate is default-allow (don't penalise non-F&O names that pass every
other gate).

Front-month rule (mirrors ``screen_pairs.load_front_month_panel``): for
each (TradDt, TckrSymb) pick the row whose XpryDt is the smallest value
≥ TradDt. On expiry day this is today's contract; the next day it rolls
to next month automatically.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, List

import pandas as pd

from core.data_cache_io import find_tables, read_table

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_FO_DIR = CACHE_DIR / "bhavcopy_raw"

CLASSIFICATIONS = (
    "LONG_BUILDUP", "SHORT_COVERING", "SHORT_BUILDUP", "LONG_UNWINDING", "NEUTRAL",
)


def load_total_oi(
    universe: Iterable[str],
    raw_dir: Path = RAW_FO_DIR,
) -> pd.DataFrame:
    """
    Long-format DataFrame ``date, symbol, close, oi`` aggregated across all
    live STF expiries per (date, symbol).

    ``close`` is the front-month STF close (what most STF volume trades on).
    ``oi`` is the **total OI summed across every live expiry** that day,
    which removes the calendar-roll noise that front-month-only OI suffers
    from: front-month OI drops to zero on expiry day and the next front
    month inherits the bulk of open positions, producing apparent
    "long buildups" of 200-500 % that are pure plumbing artifacts.

    OI unit: **contracts** (lessons.md: unit-bearing fields declare units).
    Returns an empty frame if the archive is empty.
    """
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    if not files:
        logger.warning("No F&O bhavcopy files in %s — OI gate will be neutral", raw_dir)
        return pd.DataFrame(columns=["date", "symbol", "close", "oi"])

    universe_set = {s.upper() for s in universe}

    # Short-circuit: scan the most recent file's STF universe. If our
    # universe has zero overlap (typical in tests with synthetic symbols),
    # skip the 125-file walk entirely. Cuts test-suite time from ~90s back
    # to ~5s without hiding real coverage gaps.
    sample = read_table(files[-1], usecols=["FinInstrmTp", "TckrSymb"],
                        dtype={"TckrSymb": str, "FinInstrmTp": str})
    sample_universe = set(sample[sample["FinInstrmTp"] == "STF"]["TckrSymb"].unique())
    if not (universe_set & sample_universe):
        return pd.DataFrame(columns=["date", "symbol", "close", "oi"])

    rows: List[pd.DataFrame] = []
    for f in files:
        df = read_table(
            f,
            usecols=["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "ClsPric", "OpnIntrst"],
            dtype={"TckrSymb": str, "FinInstrmTp": str},
        )
        df = df[(df["FinInstrmTp"] == "STF") & (df["TckrSymb"].isin(universe_set))]
        if df.empty:
            continue
        df["TradDt"] = pd.to_datetime(df["TradDt"]).dt.date
        df["XpryDt"] = pd.to_datetime(df["XpryDt"]).dt.date
        df = df[df["XpryDt"] >= df["TradDt"]]
        # Total OI across all live expiries
        oi_sum = df.groupby(["TradDt", "TckrSymb"])["OpnIntrst"].sum().reset_index()
        # Front-month close for the price reference
        idx = df.groupby(["TradDt", "TckrSymb"])["XpryDt"].idxmin()
        front = df.loc[idx, ["TradDt", "TckrSymb", "ClsPric"]]
        merged = front.merge(oi_sum, on=["TradDt", "TckrSymb"])
        rows.append(merged)

    if not rows:
        return pd.DataFrame(columns=["date", "symbol", "close", "oi"])

    out = pd.concat(rows, ignore_index=True).rename(columns={
        "TradDt": "date", "TckrSymb": "symbol",
        "ClsPric": "close", "OpnIntrst": "oi",
    })
    out["date"] = pd.to_datetime(out["date"])
    out = out.sort_values(["symbol", "date"]).reset_index(drop=True)
    return out


# Back-compat alias — earlier draft of this module exported this name.
load_front_month_oi = load_total_oi


def classify_oi(
    oi_df: pd.DataFrame,
    lookback: int = 5,
    min_price_pct: float = 1.0,
    min_oi_pct: float = 2.0,
) -> pd.DataFrame:
    """
    Add ``oi_signal`` column (one of ``CLASSIFICATIONS``) to a long-form OI
    frame. Per (symbol, date), compares close vs close N bars ago and OI
    vs OI N bars ago — both deltas must exceed their min-pct threshold to
    register a non-neutral signal.

    Threshold rationale: a 0.3 % drift in OI on 1 % price drift is noise,
    not a "buildup". Defaults (1 % price, 2 % OI) keep the classification
    crisp; the strategy can soften them via tunables if backtest shows the
    gate is too restrictive.
    """
    if oi_df.empty:
        return oi_df.assign(oi_signal=pd.Series(dtype=str))
    out = []
    for sym, g in oi_df.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True).copy()
        g["close_lag"] = g["close"].shift(lookback)
        g["oi_lag"] = g["oi"].shift(lookback)
        g["price_chg_pct"] = (g["close"] - g["close_lag"]) / g["close_lag"] * 100
        g["oi_chg_pct"] = (g["oi"] - g["oi_lag"]) / g["oi_lag"] * 100
        signals = []
        for _, r in g.iterrows():
            p, o = r["price_chg_pct"], r["oi_chg_pct"]
            if pd.isna(p) or pd.isna(o):
                signals.append("NEUTRAL")
                continue
            if abs(p) < min_price_pct or abs(o) < min_oi_pct:
                signals.append("NEUTRAL")
                continue
            if p > 0 and o > 0:
                signals.append("LONG_BUILDUP")
            elif p > 0 and o < 0:
                signals.append("SHORT_COVERING")
            elif p < 0 and o > 0:
                signals.append("SHORT_BUILDUP")
            else:
                signals.append("LONG_UNWINDING")
        g["oi_signal"] = signals
        out.append(g[["date", "symbol", "close", "oi", "oi_signal",
                      "price_chg_pct", "oi_chg_pct"]])
    return pd.concat(out, ignore_index=True)


def build_oi_panel(
    universe: Iterable[str],
    lookback: int = 5,
    min_price_pct: float = 1.0,
    min_oi_pct: float = 2.0,
    raw_dir: Path = RAW_FO_DIR,
) -> pd.DataFrame:
    """End-to-end: load total-expiry STF OI then classify. Empty-safe."""
    raw = load_total_oi(universe, raw_dir=raw_dir)
    if raw.empty:
        return pd.DataFrame(columns=["date", "symbol", "oi_signal"])
    return classify_oi(raw, lookback=lookback,
                       min_price_pct=min_price_pct, min_oi_pct=min_oi_pct)
