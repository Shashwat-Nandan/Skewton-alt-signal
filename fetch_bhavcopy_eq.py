"""
Fetch NSE Cash-Market (EQ) Bhav Copy — daily equity OHLCV
=========================================================
Mirror of ``fetch_bhavcopy.py`` (F&O) for the cash market segment. Writes
one CSV per symbol under ``data_cache/equity_ohlcv/<SYMBOL>.csv`` with the
canonical schema ``date,open,high,low,close,volume`` consumed by
``strategies/_eq_data.py``.

Source: NSE UDiFF cash-market archive
  https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip

Idempotent: re-running merges new dates into the existing per-symbol CSVs
without duplicating rows.

Usage::

    python fetch_bhavcopy_eq.py --days 800
    python fetch_bhavcopy_eq.py --from-date 2023-01-01 --to-date 2026-05-09
    python fetch_bhavcopy_eq.py --universe data_cache/nifty200.csv --days 30

Note: NSE archives are gated by Akamai. This runs cleanly from the user's
VPS (where ``fetch_bhavcopy.py`` already works) but may 503 from
short-lived dev environments.
"""
from __future__ import annotations

import argparse
import io
import logging
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Set

import pandas as pd
import requests

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_EQ_DIR = CACHE_DIR / "bhavcopy_eq_raw"
EQ_OUT_DIR = CACHE_DIR / "equity_ohlcv"
UDIFF_URL = (
    "https://archives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}
IST = timezone(timedelta(hours=5, minutes=30))
RATE_LIMIT_DELAY = 0.4


def load_holidays(path: str = "holidays.csv") -> Set:
    holidays: Set = set()
    if not Path(path).exists():
        return holidays
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                holidays.add(datetime.strptime(line.split(",", 1)[0].strip(), "%Y-%m-%d").date())
            except ValueError:
                continue
    return holidays


def trading_days(from_date: datetime, to_date: datetime, holidays: Set) -> List[datetime]:
    out = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5 and d.date() not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def _download_one(date: datetime, session: requests.Session) -> Optional[bytes]:
    yyyymmdd = date.strftime("%Y%m%d")
    cache_file = RAW_EQ_DIR / f"bhavcopy_eq_{yyyymmdd}.csv"
    if cache_file.exists():
        return cache_file.read_bytes()

    url = UDIFF_URL.format(yyyymmdd=yyyymmdd)
    try:
        resp = session.get(url, headers=REQUEST_HEADERS, timeout=30)
    except requests.RequestException as e:
        logger.warning("Download failed for %s: %s", yyyymmdd, e)
        return None
    if resp.status_code == 404:
        logger.info("No bhav copy for %s (404 — likely holiday/weekend)", yyyymmdd)
        return None
    if resp.status_code != 200:
        logger.warning("Unexpected status %d for %s", resp.status_code, yyyymmdd)
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                return None
            csv_bytes = zf.read(names[0])
    except zipfile.BadZipFile:
        return None
    RAW_EQ_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(csv_bytes)
    return csv_bytes


def _parse_eq_day(csv_bytes: bytes, date: datetime, universe: Set[str]) -> pd.DataFrame:
    """Filter UDiFF CM file to EQ-series rows for the universe; return canonical columns."""
    df = pd.read_csv(io.BytesIO(csv_bytes))
    required = {"TckrSymb", "SctySrs", "OpnPric", "HghPric", "LwPric", "ClsPric", "TtlTradgVol"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"UDiFF CM file missing columns: {missing}")
    df = df[(df["SctySrs"] == "EQ") & (df["TckrSymb"].isin(universe))].copy()
    if df.empty:
        return df
    out = df.rename(columns={
        "TckrSymb": "symbol",
        "OpnPric": "open", "HghPric": "high", "LwPric": "low", "ClsPric": "close",
        "TtlTradgVol": "volume",
    })[["symbol", "open", "high", "low", "close", "volume"]].copy()
    out["date"] = pd.Timestamp(date.date())
    return out[["date", "symbol", "open", "high", "low", "close", "volume"]]


def fetch_eq_range(
    universe: List[str],
    from_date: datetime,
    to_date: datetime,
) -> pd.DataFrame:
    holidays = load_holidays()
    days = trading_days(from_date, to_date, holidays)
    logger.info("Fetching EQ bhavcopy for %d trading days (%s → %s)",
                len(days), from_date.date(), to_date.date())
    session = requests.Session()
    universe_set = {s.upper() for s in universe}
    frames: List[pd.DataFrame] = []
    for i, day in enumerate(days, 1):
        logger.info("  [%d/%d] %s", i, len(days), day.strftime("%Y-%m-%d"))
        csv_bytes = _download_one(day, session)
        if csv_bytes is None:
            continue
        try:
            day_df = _parse_eq_day(csv_bytes, day, universe_set)
        except ValueError as e:
            logger.warning("Parse error on %s: %s", day.strftime("%Y-%m-%d"), e)
            continue
        if not day_df.empty:
            frames.append(day_df)
        time.sleep(RATE_LIMIT_DELAY)
    if not frames:
        raise RuntimeError("No EQ bhavcopy data retrieved for the requested range")
    combined = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"])
    return combined.reset_index(drop=True)


def write_per_symbol(df: pd.DataFrame, out_dir: Path = EQ_OUT_DIR) -> int:
    """Merge `df` into per-symbol cache CSVs, dedupe by date. Returns symbol count."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for sym, group in df.groupby("symbol"):
        path = out_dir / f"{sym}.csv"
        if path.exists():
            existing = pd.read_csv(path, parse_dates=["date"])
            merged = pd.concat([existing, group], ignore_index=True)
        else:
            merged = group
        merged = (merged.drop_duplicates(subset=["date"], keep="last")
                          .sort_values("date")
                          .reset_index(drop=True))
        merged[["date", "open", "high", "low", "close", "volume"]].to_csv(
            path, index=False
        )
        n += 1
    return n


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    p = argparse.ArgumentParser(description="Fetch NSE EQ bhav copy → per-symbol OHLCV")
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

    df = fetch_eq_range(universe, from_date, to_date)
    n_syms = write_per_symbol(df)
    logger.info("Wrote %d symbol CSVs → %s", n_syms, EQ_OUT_DIR)
    print(f"Days: {df['date'].dt.date.nunique()}  Rows: {len(df):,}  Symbols: {df['symbol'].nunique()}")


if __name__ == "__main__":
    main()
