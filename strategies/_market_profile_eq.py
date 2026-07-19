"""
Daily-bar Market Profile adapter for the equity-swing strategy.

The repo's existing ``core/market_profile.py`` operates on intraday TPO bars and
expects 30-min data from ``backend/bars.db``. For the swing horizon we
want a profile built from *daily* bars over a rolling N-day window — same
VAH/POC/VAL semantics, but the raw data is one OHLCV row per day per
symbol (which we already have for all 209 names via the F&O STF archive).

What this module produces, per (symbol, date):

  ``mp_vah`` -- value-area-high price  (top of the 70 % of cumulative volume
                centred on POC)
  ``mp_poc`` -- point-of-control price (mode of the volume×price distribution)
  ``mp_val`` -- value-area-low price

Construction
------------
For each (symbol, date) we build a price histogram from the last
``lookback`` daily bars. Each bar contributes its **traded volume**
distributed uniformly across the price bins covered by ``[low, high]``.
The bin size is ``tick_pct * recent_close`` (default 0.20 % of close).
We then walk outward from POC accumulating bin volume until we cross the
``value_area_pct`` threshold (default 70 %) — that pair of edges is
``mp_val`` / ``mp_vah``.

Why volume-weighted, not TPO? With one bar per day we can't meaningfully
slice by time-of-day. Volume × price is the cleanest "where did most
trade actually print" signal at the daily-bar resolution.

Strategy use
------------
  - Long-side **boost**: today's close > mp_vah (acceptance above value)
  - Long-side **veto**:  today's close < mp_val (price below recent
    value area - the trend gate is probably stale)
  - Pullback setups: prefer entries with close >= mp_poc (mean of value)

This is a thin, pure-pandas helper — no I/O, no Kite, no bars.db.
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd


def _value_area(prices: np.ndarray, volumes: np.ndarray, value_area_pct: float) -> Tuple[float, float, float]:
    """
    Inputs are 1-D numpy arrays of equal length, one entry per price bin.

    Returns ``(poc, val, vah)`` — POC is the price at peak volume, VAL/VAH
    bracket the smallest contiguous range around POC containing
    ``value_area_pct`` % of total volume. Walks outward from POC, at each
    step extending toward whichever neighbour has the higher residual
    volume (canonical Steidlmayer construction).
    """
    if volumes.sum() <= 0:
        return float("nan"), float("nan"), float("nan")
    target = volumes.sum() * (value_area_pct / 100.0)
    poc_i = int(np.argmax(volumes))
    cum = float(volumes[poc_i])
    lo = hi = poc_i
    n = len(volumes)
    while cum < target and (lo > 0 or hi < n - 1):
        below = volumes[lo - 1] if lo > 0 else -1.0
        above = volumes[hi + 1] if hi < n - 1 else -1.0
        if above >= below and hi < n - 1:
            hi += 1
            cum += float(volumes[hi])
        elif lo > 0:
            lo -= 1
            cum += float(volumes[lo])
        else:
            break
    return float(prices[poc_i]), float(prices[lo]), float(prices[hi])


def rolling_value_area(
    df: pd.DataFrame,
    lookback: int = 20,
    value_area_pct: float = 70.0,
    tick_pct: float = 0.20,
    min_periods: int | None = None,
) -> pd.DataFrame:
    """
    Compute rolling VAH/POC/VAL columns for one symbol's daily OHLCV.

    Parameters
    ----------
    df : DataFrame with columns date, open, high, low, close, volume.
         Must be sorted ascending by date and refer to a single symbol.
    lookback : window size in trading days (= bars).
    value_area_pct : portion of total volume that defines the value area.
    tick_pct : bin size as % of the most recent close in the window. Larger
               ticks = coarser bins = faster, less precise. 0.20 % is a
               sensible default for liquid Indian equities.
    min_periods : require at least this many bars to compute (default = lookback).

    Returns DataFrame indexed by ``date`` with columns mp_vah, mp_poc, mp_val.
    Warm-up rows are NaN — caller must check before using.
    """
    if min_periods is None:
        min_periods = lookback

    g = df.sort_values("date").reset_index(drop=True)
    out = pd.DataFrame(index=pd.DatetimeIndex(g["date"]),
                       columns=["mp_vah", "mp_poc", "mp_val"], dtype=float)

    highs = g["high"].to_numpy(dtype=float)
    lows = g["low"].to_numpy(dtype=float)
    closes = g["close"].to_numpy(dtype=float)
    vols = g["volume"].to_numpy(dtype=float)

    n = len(g)
    for end in range(min_periods - 1, n):
        start = max(0, end - lookback + 1)
        window_h = highs[start:end + 1]
        window_l = lows[start:end + 1]
        window_v = vols[start:end + 1]
        ref_close = closes[end]
        if ref_close <= 0 or window_v.sum() <= 0:
            continue
        tick = max(ref_close * tick_pct / 100.0, 0.05)
        global_lo = float(window_l.min())
        global_hi = float(window_h.max())
        if global_hi <= global_lo:
            continue
        # bin edges: floor lo / ceil hi to nearest tick
        lo_edge = np.floor(global_lo / tick) * tick
        hi_edge = np.ceil(global_hi / tick) * tick
        n_bins = max(int(round((hi_edge - lo_edge) / tick)) + 1, 1)
        if n_bins > 2000:
            # safety: extreme range → coarsen ticks rather than allocate forever
            tick = (hi_edge - lo_edge) / 2000.0
            n_bins = 2001
        edges = lo_edge + np.arange(n_bins + 1) * tick
        mids = 0.5 * (edges[:-1] + edges[1:])
        bin_vol = np.zeros(n_bins, dtype=float)

        # Distribute each bar's volume uniformly across the bins it covers.
        for h, l, v in zip(window_h, window_l, window_v):
            if v <= 0 or h < lo_edge or l > hi_edge:
                continue
            i_lo = max(0, int(np.floor((l - lo_edge) / tick)))
            i_hi = min(n_bins - 1, int(np.floor((h - lo_edge) / tick)))
            span = max(i_hi - i_lo + 1, 1)
            bin_vol[i_lo:i_hi + 1] += v / span

        poc, val, vah = _value_area(mids, bin_vol, value_area_pct)
        out.iloc[end] = [vah, poc, val]
    return out


def panel_value_area(
    panel: pd.DataFrame,
    lookback: int = 20,
    value_area_pct: float = 70.0,
    tick_pct: float = 0.20,
) -> pd.DataFrame:
    """Compute mp_vah/poc/val for every (symbol, date) in a multi-symbol panel.

    Returns a DataFrame indexed by (symbol, date) with three columns.
    """
    out_frames = []
    for sym, g in panel.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        va = rolling_value_area(g, lookback=lookback, value_area_pct=value_area_pct,
                                tick_pct=tick_pct)
        va = va.assign(symbol=sym).reset_index().rename(columns={"index": "date"})
        out_frames.append(va)
    if not out_frames:
        return pd.DataFrame(columns=["symbol", "date", "mp_vah", "mp_poc", "mp_val"])
    out = pd.concat(out_frames, ignore_index=True)
    return out.set_index(["symbol", "date"]).sort_index()
