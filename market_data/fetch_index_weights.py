#!/usr/bin/env python3
"""
Fetch Nifty 50 free-float weights from NSE and write ``nifty50_weights.csv``.

Source: NSE's market-watch API,
``/api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol=NIFTY%2050``.
Each constituent row carries ``ffmc`` (free-float market cap, rupees). The
weight is ``ffmc / Σ ffmc``. The older ``/api/equity-stockIndices`` returns
404 from this host as of 2026-10-02.

Bloch §7.6.5.1 sizes the stock straddles by market cap (ν_i ∝ N_i S_i), so
the dispersion book reads this file instead of equal weights. The CSV is a
snapshot: weights drift with price between refreshes. ``asof`` is NSE's
timestamp for the snapshot.

The write refuses a payload whose symbols differ from the pinned
constituent list, or with a missing or non-positive ``ffmc``: a silent
partial file would mis-size every leg.

    python -m market_data.fetch_index_weights
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence

import pandas as pd
import requests

from market_data.fetch_board_meetings import HEADERS, _bootstrap_session

logger = logging.getLogger(__name__)

NIFTY50_WEIGHTS_PATH = Path(__file__).resolve().parent / "nifty50_weights.csv"
NSE_INDEX_URL = (
    "https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi"
    "?functionName=getIndicesData&symbol=NIFTY%2050"
)
_REFERER = "https://www.nseindia.com/market-data/live-equity-market"


def weights_from_payload(payload: dict, universe: Sequence[str]) -> pd.DataFrame:
    """``symbol, weight, ffmc, asof`` from one getIndicesData payload.

    Raises when the constituents differ from ``universe`` or any ``ffmc``
    is missing or non-positive.
    """
    body = payload.get("data") or {}
    rows = [r for r in body.get("data") or [] if r.get("priority") == 0]
    ffmc: Dict[str, float] = {}
    for r in rows:
        sym = str(r.get("symbol") or "").strip()
        val = r.get("ffmc")
        if not sym or val is None or float(val) <= 0:
            raise ValueError(f"constituent {sym!r} has no positive ffmc")
        ffmc[sym] = float(val)
    missing = sorted(set(universe) - set(ffmc))
    extra = sorted(set(ffmc) - set(universe))
    if missing or extra:
        raise ValueError(f"constituents differ from the pinned list: missing={missing} extra={extra}")
    total = sum(ffmc.values())
    asof = str(body.get("timestamp") or "")
    out = pd.DataFrame(
        [{"symbol": s, "weight": v / total, "ffmc": v, "asof": asof} for s, v in ffmc.items()]
    )
    return out.sort_values("weight", ascending=False).reset_index(drop=True)


def fetch_payload(session: Optional[requests.Session] = None) -> dict:
    s = session or _bootstrap_session()
    headers = dict(HEADERS)
    headers["Referer"] = _REFERER
    resp = s.get(NSE_INDEX_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main(argv: Optional[Sequence[str]] = None) -> int:
    from research.backtest_dispersion import NIFTY50_2026_10_01

    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--out", type=Path, default=NIFTY50_WEIGHTS_PATH)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    frame = weights_from_payload(fetch_payload(), NIFTY50_2026_10_01)
    frame.to_csv(args.out, index=False)
    logger.info(
        "wrote %d weights as of %s to %s (top: %s %.2f%%)",
        len(frame), frame["asof"].iloc[0], args.out,
        frame["symbol"].iloc[0], 100 * frame["weight"].iloc[0],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
