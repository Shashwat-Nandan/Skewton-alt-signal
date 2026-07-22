"""
Delivery-percentage features for equity strategies.

Reads the per-symbol tables produced by ``market_data/fetch_deliv.py``
(``data_cache/equity_delivery/<SYMBOL>.parquet``) and builds the
own-history percentile features described in the Varsity delivery note:
a stock's delivery percentage is only meaningful against ITS OWN
distribution — "45% might be extraordinary for one stock and ordinary
for another".

Features (per date, symbol):
  ``deliv_pctile``      rolling own-history percentile of deliv_per
                        (window ends at the current row)
  ``deliv_val_pctile``  same rank on delivery VALUE (deliv_qty × close) —
                        catches accumulation that a %-only view misses when
                        volume also expands; NaN when no price panel given
  ``deliv_hits_5d``     count of the last 5 sessions at/above the hit
                        threshold — single-day spikes (BTST churn, block
                        deals) don't cluster; real accumulation does

Anti-lookahead contract:
  The day-D percentile ranks day-D's value within the trailing window
  ending at D. That is the signal definition, not lookahead: delivery
  data publishes ~19:00 IST on day D and every consumer acts no earlier
  than day D+1's open (next-day-open fill queue). Nothing here may ever
  rank against the FULL series — appending future rows must not change
  past feature values (pinned by test_delivery_features.py).

Default-neutral on missing data (lessons.md: gates default-allow on
absence): empty cache → empty frame; symbols without delivery history →
NaN features. Missing data can never CREATE a signal.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from core.data_cache_io import read_table

logger = logging.getLogger(__name__)

DELIV_CACHE_DIR = Path("./data_cache") / "equity_delivery"

PANEL_COLS = ["date", "symbol", "traded_qty", "deliv_qty", "deliv_per"]
FEATURE_COLS = ["date", "symbol", "deliv_per", "deliv_pctile", "deliv_val_pctile",
                "deliv_hits_5d"]


def load_delivery_panel(
    universe: Optional[Iterable[str]] = None,
    cache_dir: Path = DELIV_CACHE_DIR,
) -> pd.DataFrame:
    """Long frame of per-day delivery rows for `universe` (None = every cached
    symbol). Empty frame with canonical columns if the cache is absent."""
    if not cache_dir.exists():
        return pd.DataFrame(columns=PANEL_COLS)
    if universe is not None:
        wanted = {s.upper() for s in universe}
        paths = [cache_dir / f"{s}.parquet" for s in sorted(wanted)]
    else:
        paths = sorted(cache_dir.glob("*.parquet"))
    frames = []
    for path in paths:
        try:
            df = read_table(path, parse_dates=["date"])
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            logger.warning("delivery: bad cache table %s: %s", path, e)
            continue
        df["symbol"] = path.stem.upper()
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=PANEL_COLS)
    out = pd.concat(frames, ignore_index=True)
    return out[PANEL_COLS].sort_values(["symbol", "date"]).reset_index(drop=True)


def build_delivery_features(
    deliv_panel: Optional[pd.DataFrame] = None,
    price_panel: Optional[pd.DataFrame] = None,
    *,
    pctile_window: int = 252,
    pctile_min_periods: int = 126,
    hits_threshold: float = 0.92,
    hits_lookback: int = 5,
) -> pd.DataFrame:
    """
    Per-(date, symbol) delivery features. See module docstring for the
    anti-lookahead contract.

    `price_panel` is the canonical OHLCV panel from
    ``strategies._eq_data.load_equity_panel`` — only ``date, symbol, close``
    are used, for the delivery-value rank. When omitted, ``deliv_val_pctile``
    is NaN (feature degrades, never fabricates).

    Below `pctile_min_periods` of history the percentile is NaN — a rank
    over a short window reads as extreme far too easily, and a fabricated
    "neutral" would be the autoresearch pinned-50 bug all over again.
    """
    df = deliv_panel if deliv_panel is not None else load_delivery_panel()
    if df.empty:
        return pd.DataFrame(columns=FEATURE_COLS)
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True).copy()

    if price_panel is not None and not price_panel.empty:
        closes = price_panel[["date", "symbol", "close"]]
        df = df.merge(closes, on=["date", "symbol"], how="left")
    else:
        df["close"] = float("nan")
    df["deliv_value"] = df["deliv_qty"] * df["close"]

    grouped = df.groupby("symbol", sort=False)
    df["deliv_pctile"] = (
        grouped["deliv_per"]
        .rolling(pctile_window, min_periods=pctile_min_periods)
        .rank(pct=True)
        .reset_index(level=0, drop=True)
    )
    df["deliv_val_pctile"] = (
        grouped["deliv_value"]
        .rolling(pctile_window, min_periods=pctile_min_periods)
        .rank(pct=True)
        .reset_index(level=0, drop=True)
    )
    hit = (df["deliv_pctile"] >= hits_threshold).astype(float)
    df["deliv_hits_5d"] = (
        hit.groupby(df["symbol"], sort=False)
        .rolling(hits_lookback, min_periods=1)
        .sum()
        .reset_index(level=0, drop=True)
    )
    return df[FEATURE_COLS].reset_index(drop=True)
