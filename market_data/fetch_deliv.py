"""
Fetch NSE delivery data — daily per-stock delivery percentage
=============================================================
Mirror of ``market_data/fetch_bhavcopy_eq.py`` for NSE's ``sec_bhavdata_full``
report, which carries the delivered-quantity columns the UDiFF bhavcopy lacks.
Writes one table per symbol under ``data_cache/equity_delivery/<SYMBOL>.parquet``
with schema ``date,traded_qty,deliv_qty,deliv_per`` consumed by
``strategies/_delivery.py``.

Source: NSE securities full bhavdata archive
  https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{DDMMYYYY}.csv

Quirks of this file (each pinned by a test in tests/test_fetch_deliv.py):
  - header and values are whitespace-padded (`` SERIES``, `` EQ``)
  - DELIV_QTY / DELIV_PER are ``" -"`` for non-EQ series → numeric parse must
    coerce, and rows are filtered to SERIES == EQ first
  - DATE1 is ``01-Jan-2024``-style; we stamp ``date`` from the requested
    trading day instead of parsing it (same convention as _parse_eq_day)

Idempotent: raw days are cached under ``data_cache/deliv_raw/`` and re-running
merges new dates into the per-symbol tables without duplicating rows.

Usage::

    python -m market_data.fetch_deliv --days 30
    python -m market_data.fetch_deliv --from-date 2022-01-01 --to-date 2026-07-22

Note: nsearchives.nseindia.com sits behind the same Akamai gate as the other
NSE endpoints and additionally wants the homepage-cookie bootstrap the FII/DII
fetcher uses. Runs cleanly from the VPS; may 503 from dev environments.
"""
from __future__ import annotations

import argparse
import io
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

import pandas as pd
import requests

from core.data_cache_io import read_table, table_exists, write_table
from market_data.fetch_bhavcopy_eq import load_holidays, trading_days

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_DELIV_DIR = CACHE_DIR / "deliv_raw"
DELIV_OUT_DIR = CACHE_DIR / "equity_delivery"
SEC_BHAVDATA_URL = (
    "https://nsearchives.nseindia.com/products/content/"
    "sec_bhavdata_full_{ddmmyyyy}.csv"
)
NSE_HOMEPAGE = "https://www.nseindia.com/"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
RATE_LIMIT_DELAY = 0.4

REQUIRED_COLS = {"SYMBOL", "SERIES", "TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"}


def bootstrap_session() -> requests.Session:
    """nsearchives wants the bot-detection cookies a homepage hit seeds."""
    s = requests.Session()
    try:
        s.get(NSE_HOMEPAGE, headers=REQUEST_HEADERS, timeout=15)
    except requests.RequestException as e:
        # The archive GET may still succeed on a warm edge; don't abort here.
        logger.warning("Homepage bootstrap failed (continuing): %s", e)
    return s


def _download_one(date: datetime, session: requests.Session) -> Optional[pd.DataFrame]:
    yyyymmdd = date.strftime("%Y%m%d")
    cache_file = RAW_DELIV_DIR / f"deliv_{yyyymmdd}.parquet"
    if table_exists(cache_file):
        return read_table(cache_file)

    url = SEC_BHAVDATA_URL.format(ddmmyyyy=date.strftime("%d%m%Y"))
    try:
        resp = session.get(url, headers=REQUEST_HEADERS, timeout=30)
    except requests.RequestException as e:
        logger.warning("Download failed for %s: %s", yyyymmdd, e)
        return None
    if resp.status_code == 404:
        logger.info("No sec_bhavdata for %s (404 — likely holiday/weekend)", yyyymmdd)
        return None
    if resp.status_code != 200:
        logger.warning("Unexpected status %d for %s", resp.status_code, yyyymmdd)
        return None
    try:
        # dtype=str end-to-end: DELIV_* hold " -" for non-EQ series, so numeric
        # inference would give mixed-object columns; parse happens in
        # _parse_deliv_day where the coercion is explicit and counted.
        day_df = pd.read_csv(
            io.BytesIO(resp.content), dtype=str, skipinitialspace=True
        )
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as e:
        logger.warning("Unparseable sec_bhavdata for %s: %s", yyyymmdd, e)
        return None
    day_df.columns = day_df.columns.str.strip()
    # Validate BEFORE caching (code-review 2026-07-22): an Akamai block page
    # arrives as HTTP 200 UTF-8 HTML and pd.read_csv parses it happily, so
    # without this gate it would be cached and table_exists would pin the
    # poisoned day forever. Mirrors fetch_bhavcopy_eq's zip-validates-before-
    # cache contract.
    missing = REQUIRED_COLS - set(day_df.columns)
    if missing:
        logger.warning(
            "Not a sec_bhavdata file for %s (missing %s — Akamai block page?); "
            "NOT caching", yyyymmdd, sorted(missing),
        )
        return None
    write_table(day_df, cache_file)
    return day_df


def _parse_deliv_day(df: pd.DataFrame, date: datetime, universe: Set[str]) -> pd.DataFrame:
    """Filter a sec_bhavdata day frame to EQ-series universe rows; return canonical columns."""
    df = df.copy()
    df.columns = df.columns.str.strip()
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"sec_bhavdata file missing columns: {missing}")
    for col in ("SYMBOL", "SERIES"):
        df[col] = df[col].astype(str).str.strip()
    df = df[(df["SERIES"] == "EQ") & (df["SYMBOL"].isin(universe))].copy()
    if df.empty:
        return df

    for col in ("TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"):
        df[col] = pd.to_numeric(df[col].astype(str).str.strip(), errors="coerce")
    n_before = len(df)
    df = df.dropna(subset=["TTL_TRD_QNTY", "DELIV_QTY", "DELIV_PER"])
    n_unparseable = n_before - len(df)
    if n_unparseable:
        logger.warning(
            "%s: dropped %d EQ rows with unparseable delivery fields",
            date.strftime("%Y-%m-%d"), n_unparseable,
        )
    sane = (
        (df["DELIV_QTY"] <= df["TTL_TRD_QNTY"])
        & (df["DELIV_PER"] >= 0.0)
        & (df["DELIV_PER"] <= 100.0)
    )
    if (~sane).any():
        logger.warning(
            "%s: dropped %d EQ rows failing sanity checks (deliv>traded or pct outside 0-100): %s",
            date.strftime("%Y-%m-%d"), int((~sane).sum()),
            df.loc[~sane, "SYMBOL"].tolist(),
        )
        df = df[sane]
    if df.empty:
        return df

    out = df.rename(columns={
        "SYMBOL": "symbol",
        "TTL_TRD_QNTY": "traded_qty",
        "DELIV_QTY": "deliv_qty",
        "DELIV_PER": "deliv_per",
    })[["symbol", "traded_qty", "deliv_qty", "deliv_per"]].copy()
    out["date"] = pd.Timestamp(date.date())
    return out[["date", "symbol", "traded_qty", "deliv_qty", "deliv_per"]]


def fetch_deliv_range(
    universe: List[str],
    from_date: datetime,
    to_date: datetime,
) -> pd.DataFrame:
    holidays = load_holidays()
    days = trading_days(from_date, to_date, holidays)
    logger.info("Fetching sec_bhavdata for %d trading days (%s → %s)",
                len(days), from_date.date(), to_date.date())
    session = bootstrap_session()
    universe_set = {s.upper() for s in universe}
    frames: List[pd.DataFrame] = []
    for i, day in enumerate(days, 1):
        logger.info("  [%d/%d] %s", i, len(days), day.strftime("%Y-%m-%d"))
        was_cached = table_exists(RAW_DELIV_DIR / f"deliv_{day.strftime('%Y%m%d')}.parquet")
        raw_day = _download_one(day, session)
        if not was_cached:
            # Sleep after EVERY network attempt, success or failure — the
            # sleep used to sit on the success path only, so once Akamai
            # started rejecting, the loop hammered NSE back-to-back and
            # deepened the block (code-review 2026-07-22). Cache hits made
            # no request and skip the delay.
            time.sleep(RATE_LIMIT_DELAY)
        if raw_day is None:
            continue
        try:
            day_df = _parse_deliv_day(raw_day, day, universe_set)
        except ValueError as e:
            logger.warning("Parse error on %s: %s", day.strftime("%Y-%m-%d"), e)
            continue
        if not day_df.empty:
            frames.append(day_df)
    if not frames:
        raise RuntimeError("No delivery data retrieved for the requested range")
    combined = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"])
    return combined.reset_index(drop=True)


def write_per_symbol(df: pd.DataFrame, out_dir: Path = DELIV_OUT_DIR) -> int:
    """Merge `df` into per-symbol cache tables, dedupe by date. Returns symbol count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for sym, group in df.groupby("symbol"):
        path = out_dir / f"{sym}.parquet"
        try:
            existing = read_table(path, parse_dates=["date"])
            merged = pd.concat([existing, group], ignore_index=True)
        except FileNotFoundError:
            merged = group
        merged = (merged.drop_duplicates(subset=["date"], keep="last")
                        .sort_values("date")
                        .reset_index(drop=True))
        write_table(
            merged[["date", "traded_qty", "deliv_qty", "deliv_per"]], path
        )
        n += 1
    return n


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    p = argparse.ArgumentParser(description="Fetch NSE sec_bhavdata → per-symbol delivery %")
    p.add_argument("--from-date", type=str, default=None)
    p.add_argument("--to-date", type=str, default=None)
    p.add_argument("--days", type=int, default=30, help="lookback if --from-date omitted")
    p.add_argument("--universe", type=str, default="data_cache/nifty200.csv")
    args = p.parse_args()

    to_date = (datetime.strptime(args.to_date, "%Y-%m-%d") if args.to_date else datetime.now())
    from_date = (datetime.strptime(args.from_date, "%Y-%m-%d") if args.from_date
                 else to_date - timedelta(days=args.days))

    universe_df = pd.read_csv(args.universe)
    universe = sorted({str(s).strip().upper() for s in universe_df["symbol"]})

    df = fetch_deliv_range(universe, from_date, to_date)
    n_syms = write_per_symbol(df)
    logger.info("Wrote %d symbol tables → %s", n_syms, DELIV_OUT_DIR)
    print(f"Days: {df['date'].dt.date.nunique()}  Rows: {len(df):,}  Symbols: {df['symbol'].nunique()}")


if __name__ == "__main__":
    main()
