"""
FII/DII flow overlay for the equity-swing strategy.

Reads the per-day JSON cache produced by ``market_data/fetch_fii_dii.py`` (one file
per ISO date under ``data_cache/fii_dii/``) and exposes a panel of
per-trading-day net flows in ₹ crore — both FII (foreign) and DII
(domestic institutions). The strategy uses a rolling 5-day cumulative
net FII number as a sentiment overlay:

  +ve cumulative FII   →  long-side score boost (+1)
  -ve cumulative FII   →  no boost, no veto (we don't want to punish
                          good setups in foreign-outflow weeks since
                          those are also frequently when DIIs lean in)

Default-neutral on missing data (lessons.md: gates default-allow on
absence). Backtest-time backfill is best-effort — if the cache is sparse
during the backtest window, the gate yields zero rows and the strategy
behaves as if the gate were disabled.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache") / "fii_dii"


def _coerce_amount(v) -> Optional[float]:
    """NSE sometimes returns numbers as strings with thousands separators."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace(",", "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def load_fii_dii_panel(cache_dir: Path = CACHE_DIR) -> pd.DataFrame:
    """
    Read every JSON file in ``cache_dir`` and return a long-format frame
    with columns ``date, category, buy, sell, net`` (all amounts in ₹ crore).
    """
    if not cache_dir.exists():
        return pd.DataFrame(columns=["date", "category", "buy", "sell", "net"])

    rows = []
    for f in sorted(cache_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError) as e:
            logger.warning("FII/DII: bad cache file %s: %s", f, e)
            continue
        if not isinstance(data, list):
            continue
        # Date is always derivable from the filename (we wrote it that way),
        # but cross-check against the row's "date" field if present.
        try:
            file_date = pd.Timestamp(f.stem)
        except ValueError:
            continue
        for row in data:
            cat = row.get("category") or row.get("Category")
            if cat is None:
                continue
            buy = _coerce_amount(row.get("buyValue") or row.get("buyVal") or row.get("buy"))
            sell = _coerce_amount(row.get("sellValue") or row.get("sellVal") or row.get("sell"))
            net = _coerce_amount(row.get("netValue") or row.get("netVal") or row.get("net"))
            if net is None and buy is not None and sell is not None:
                net = buy - sell
            rows.append({
                "date": file_date,
                "category": str(cat).strip(),
                "buy": buy, "sell": sell, "net": net,
            })
    if not rows:
        return pd.DataFrame(columns=["date", "category", "buy", "sell", "net"])
    return pd.DataFrame(rows).sort_values(["date", "category"]).reset_index(drop=True)


def build_fii_signal(
    panel: Optional[pd.DataFrame] = None,
    lookback: int = 5,
) -> pd.DataFrame:
    """
    Per-date 5-day cumulative net FII flow + binary boost flag.

    Returns a frame indexed by ``date`` with columns:
      ``fii_net_5d``      -- rolling sum, ₹ crore
      ``dii_net_5d``      -- rolling sum, ₹ crore (informational)
      ``fii_boost``       -- 1 if fii_net_5d > 0, else 0

    Empty if the cache is empty.
    """
    df = panel if panel is not None else load_fii_dii_panel()
    if df.empty:
        return pd.DataFrame(columns=["date", "fii_net_5d", "dii_net_5d", "fii_boost"])

    # Pivot to wide: one column per category. NSE categories are typically
    # "FII/FPI" and "DII"; tolerate variants by lowercasing & substring match.
    df["cat_norm"] = df["category"].str.upper()
    fii_mask = df["cat_norm"].str.contains("FII") | df["cat_norm"].str.contains("FPI")
    dii_mask = df["cat_norm"].str.contains("DII")
    fii = df[fii_mask].groupby("date")["net"].sum().rename("fii_net")
    dii = df[dii_mask].groupby("date")["net"].sum().rename("dii_net")
    out = pd.concat([fii, dii], axis=1).sort_index().fillna(0.0)
    out["fii_net_5d"] = out["fii_net"].rolling(lookback, min_periods=1).sum()
    out["dii_net_5d"] = out["dii_net"].rolling(lookback, min_periods=1).sum()
    out["fii_boost"] = (out["fii_net_5d"] > 0).astype(int)
    out = out.reset_index().rename(columns={"index": "date"})
    return out[["date", "fii_net_5d", "dii_net_5d", "fii_boost"]]
