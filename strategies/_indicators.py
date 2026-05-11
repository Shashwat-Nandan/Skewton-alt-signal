"""
Pandas indicator helpers used by the equity swing strategy and its backtest.

Pure functions, no Kite/network/IO. Each takes a DataFrame or Series and
returns a Series indexed the same as the input. Operate on ascending-date
order; callers must sort first.

Conventions
-----------
- ``high``, ``low``, ``close`` arguments are pandas Series, ascending date.
- All functions return floats; warm-up rows are ``NaN`` rather than 0 — the
  bare-except-numeric-fallback lesson (`tasks/lessons.md`) applies. Callers
  must check for NaN before reading the value.
- Window sizes are in **bars** (= trading days when fed daily data).
"""
from __future__ import annotations

import pandas as pd


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average over `window` bars."""
    return close.rolling(window=window, min_periods=window).mean()


def ema(close: pd.Series, window: int) -> pd.Series:
    """Exponential moving average. ``adjust=False`` matches Wilder/standard
    charting platforms (Zerodha Kite, TradingView default)."""
    return close.ewm(span=window, adjust=False, min_periods=window).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """True range: max(H-L, |H-prev_close|, |L-prev_close|)."""
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low).abs(),
         (high - prev_close).abs(),
         (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's ATR — exponential of true range with alpha=1/window."""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def adx(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> pd.Series:
    """
    Average Directional Index (Wilder) — strength-of-trend, 0-100, no
    direction. Threshold: ADX > 20 = trending, > 25 = strong trend
    (Varsity Module 2). Warm-up = 2*window bars.
    """
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)).astype(float) * up.clip(lower=0)
    minus_dm = ((down > up) & (down > 0)).astype(float) * down.clip(lower=0)
    tr = true_range(high, low, close)

    atr_w = tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean() / atr_w

    # dx is 0/0 = NaN whenever the two DI lines collapse to zero (flat market).
    # Wilder's recipe just propagates the NaN — leave it; the consumer must
    # treat ADX-NaN as "insufficient signal", not as 0.
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


def donchian_high(high: pd.Series, window: int) -> pd.Series:
    """N-bar Donchian channel high (rolling max of high, excluding today)."""
    return high.shift(1).rolling(window=window, min_periods=window).max()


def donchian_low(low: pd.Series, window: int) -> pd.Series:
    """N-bar Donchian channel low (rolling min of low, excluding today)."""
    return low.shift(1).rolling(window=window, min_periods=window).min()


def chandelier_stop_long(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_window: int = 14,
    multiplier: float = 3.0,
    lookback: int = 22,
) -> pd.Series:
    """
    Long-side Chandelier exit: highest high over `lookback` − k·ATR.
    A trailing stop that rises with price and never falls. Used as our
    profit-runner trail once a long position has unrealised gain ≥ 1×SL.
    """
    a = atr(high, low, close, atr_window)
    hh = high.rolling(window=lookback, min_periods=lookback).max()
    raw = hh - multiplier * a
    # Enforce monotonic non-decreasing so the stop only ratchets up.
    return raw.cummax()
