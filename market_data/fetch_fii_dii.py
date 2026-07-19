"""
Fetch NSE FII/DII daily aggregate flows.

Source: ``https://www.nseindia.com/api/fiidiiTradeReact``

The endpoint returns a JSON list with one row per (category, date), where
``category`` is "FII/FPI" or "DII" and amounts are in ₹ crore. Cadence is
once per trading day, posted shortly after the close. We persist each
day's response under ``data_cache/fii_dii/YYYY-MM-DD.json`` (idempotent —
re-running the same date is a no-op).

Strategy use: see ``strategies/_fii_dii.py``, which reads this cache and
exposes a 5-day cumulative net cash signal as a long-side score boost
(positive flow = tilt toward longs). When the cache is empty/missing the
gate is default-neutral.

Caveats
-------
The endpoint serves the *latest* day on every call, not historical. To
backfill, NSDL's FII archive is the source of truth (different schema,
not implemented in v1). Operators should run this daily — same shape as
``fetch-bars.timer``.

Typical operator usage::

    python -m market_data.fetch_fii_dii             # fetch latest
    python -m market_data.fetch_fii_dii --date 2026-05-07   # write under that date

NSE archives are gated by Akamai. The fetcher works from the user's VPS
(where ``market_data/fetch_bhavcopy.py`` already works); short-lived dev environments
may 503.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache") / "fii_dii"
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_HOMEPAGE = "https://www.nseindia.com/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/reports/fii-dii",
}


def _bootstrap_session() -> requests.Session:
    """NSE's API requires a homepage hit first to seed the bot-detection cookies."""
    s = requests.Session()
    s.get(NSE_HOMEPAGE, headers=HEADERS, timeout=15)
    return s


def fetch_latest() -> Optional[list]:
    """Pull the latest FII/DII row(s) from NSE. Returns a list of dicts or None."""
    try:
        s = _bootstrap_session()
        resp = s.get(NSE_FII_DII_URL, headers=HEADERS, timeout=20)
    except requests.RequestException as e:
        logger.warning("FII/DII fetch failed: %s", e)
        return None
    if resp.status_code != 200:
        logger.warning("FII/DII unexpected status %d", resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError as e:
        logger.warning("FII/DII non-JSON response: %s", e)
        return None
    if not isinstance(data, list) or not data:
        logger.warning("FII/DII empty/unexpected payload: %r", data)
        return None
    return data


def write_cached(data: list, target_date: Optional[str] = None) -> Path:
    """
    Persist ``data`` to ``data_cache/fii_dii/<date>.json``.

    If ``target_date`` is None, derive the date from the first row's
    ``date`` field (NSE format like "07-May-2026"). Falls back to today
    if parsing fails.
    """
    if target_date is None:
        target_date = _infer_date(data) or datetime.now().date().isoformat()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{target_date}.json"
    out.write_text(json.dumps(data, indent=2, default=str))
    return out


def _infer_date(data: list) -> Optional[str]:
    """Convert NSE's '07-May-2026' to ISO '2026-05-07'."""
    if not data:
        return None
    raw = data[0].get("date") or data[0].get("Date")
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s %(message)s")
    p = argparse.ArgumentParser(description="Fetch NSE FII/DII daily aggregate flow")
    p.add_argument("--date", default=None,
                   help="Override target ISO date (default: derive from response)")
    args = p.parse_args()

    data = fetch_latest()
    if data is None:
        logger.error("No data retrieved. NSE archives may be gated; rerun on the VPS.")
        return 1
    out = write_cached(data, target_date=args.date)
    logger.info("Wrote %d rows → %s", len(data), out)
    for row in data:
        cat = row.get("category") or row.get("Category")
        net = row.get("netValue") or row.get("net")
        logger.info("  %-10s  net = %s ₹cr", cat, net)
    return 0


if __name__ == "__main__":
    sys.exit(main())
