"""
Fetch NSE F&O Bhav Copy (EOD) Historical Data
=============================================
Downloads NSE's free daily F&O bhav copy (UDiFF format) for a date range,
filters to the chosen underlying (default NIFTY), and reshapes into the same
CSV schema the backtester already consumes.

Unlike fetch_historical_data.py (which pulls intraday candles through Kite
and is limited by the live instrument master), this source has full history
for all expired strikes. Tradeoff: EOD only — one mark per instrument per
day — so the output is suited to daily-rebalance regime research and RV/IV
calibration, not intraday gamma-scalp simulation.

Source:
  https://archives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip
  (UDiFF format, used by NSE from July 2024 onwards)

Usage:
  python fetch_bhavcopy.py --from-date 2026-01-01 --to-date 2026-04-18
  python fetch_bhavcopy.py --days 60 --underlying NIFTY
  python fetch_bhavcopy.py --from-date 2025-01-01 --to-date 2026-04-18 --nearest-expiry-only
"""

import argparse
import io
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(__file__))

from greeks_engine import implied_volatility_bisect, time_to_expiry

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_DIR = CACHE_DIR / "bhavcopy_raw"
UDIFF_URL = (
    "https://archives.nseindia.com/content/fo/"
    "BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
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
EOD_TIME = (15, 14)  # Just inside the hedger's close cutoff (15:30 − 15m buffer)
RATE_LIMIT_DELAY = 0.4  # polite pacing between archive hits


def load_holidays(path: str = "holidays.csv") -> set:
    holidays = set()
    if not Path(path).exists():
        return holidays
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            date_str = line.split(",", 1)[0].strip()
            try:
                holidays.add(datetime.strptime(date_str, "%Y-%m-%d").date())
            except ValueError:
                continue
    return holidays


def trading_days(from_date: datetime, to_date: datetime, holidays: set) -> List[datetime]:
    """Weekdays in [from_date, to_date] minus NSE holidays."""
    days = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5 and d.date() not in holidays:
            days.append(d)
        d += timedelta(days=1)
    return days


def _download_bhavcopy(date: datetime, session: requests.Session) -> Optional[bytes]:
    """
    Download the UDiFF F&O bhav copy zip for a single date.
    Caches the raw zip to RAW_DIR. Returns the CSV bytes inside the zip, or None
    if the archive is missing (holiday, weekend, or not yet published).
    """
    yyyymmdd = date.strftime("%Y%m%d")
    cache_file = RAW_DIR / f"bhavcopy_fo_{yyyymmdd}.csv"
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
                logger.warning("No CSV inside zip for %s", yyyymmdd)
                return None
            csv_bytes = zf.read(names[0])
    except zipfile.BadZipFile:
        logger.warning("Bad zip returned for %s", yyyymmdd)
        return None

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(csv_bytes)
    return csv_bytes


def _parse_udiff_day(
    csv_bytes: bytes,
    date: datetime,
    underlying: str,
    nearest_expiry_only: bool,
) -> pd.DataFrame:
    """
    Parse one day's UDiFF F&O bhav copy into rows with the backtest schema.
    Keeps index options for `underlying` plus one synthetic IDX row.
    """
    df = pd.read_csv(io.BytesIO(csv_bytes))

    # UDiFF column names — see NSE circular on bhav copy format change.
    required = {"TckrSymb", "FinInstrmTp", "XpryDt", "StrkPric", "OptnTp",
                "ClsPric", "UndrlygPric", "NewBrdLotQty"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"UDiFF file missing expected columns: {missing}")

    # Keep only index options for the chosen underlying. IDO = Index Options.
    df = df[(df["TckrSymb"] == underlying) & (df["FinInstrmTp"] == "IDO")].copy()
    if df.empty:
        return df

    df["expiry"] = pd.to_datetime(df["XpryDt"]).dt.strftime("%Y-%m-%d")
    df["strike"] = df["StrkPric"].astype(float)
    df["option_type"] = df["OptnTp"]
    df["last_price"] = df["ClsPric"].astype(float)
    df["underlying_price"] = df["UndrlygPric"].astype(float)
    df["lot_size"] = df["NewBrdLotQty"].astype(int)

    # Drop rows with unusable prices (no trade that day, or stale settle).
    df = df[df["last_price"] > 0].copy()
    if df.empty:
        return df

    if nearest_expiry_only:
        min_exp = df["expiry"].min()
        df = df[df["expiry"] == min_exp].copy()

    # Timestamp at NSE close on this date, IST.
    ts = datetime(date.year, date.month, date.day, EOD_TIME[0], EOD_TIME[1], tzinfo=IST)
    df["timestamp"] = ts

    # Canonical symbol: NIFTY{YYMMDD}{CE/PE}{strike} (unambiguous, unique per row).
    exp_short = pd.to_datetime(df["XpryDt"]).dt.strftime("%y%m%d")
    df["symbol"] = underlying + exp_short + df["option_type"] + df["strike"].astype(int).astype(str)

    # Bhav copy has no bid/ask — synthesize a ~0.3% spread around close.
    # The backtest's MockKite already overrides bid/ask with a 0.3% synthetic
    # spread around last_price, so these columns exist only to satisfy the
    # schema consumers expect.
    df["bid"] = df["last_price"] * 0.9985
    df["ask"] = df["last_price"] * 1.0015

    options = df[[
        "timestamp", "symbol", "underlying_price", "strike", "option_type",
        "expiry", "last_price", "bid", "ask", "lot_size",
    ]].copy()

    # Synthetic IDX row — use nearest expiry of the retained option set so the
    # backtester's "current chain" view matches the existing fetch_historical_data
    # output format. UndrlygPric is identical on every option row for the day.
    spot = float(df["underlying_price"].iloc[0])
    lot = int(df["lot_size"].iloc[0])
    idx_expiry = df["expiry"].min()
    idx_row = pd.DataFrame([{
        "timestamp": ts,
        "symbol": underlying,
        "underlying_price": spot,
        "strike": 0.0,
        "option_type": "IDX",
        "expiry": idx_expiry,
        "last_price": spot,
        "bid": spot * 0.9999,
        "ask": spot * 1.0001,
        "lot_size": lot,
    }])

    return pd.concat([idx_row, options], ignore_index=True)


def _compute_iv(df: pd.DataFrame, risk_free_rate: float = 0.065) -> pd.DataFrame:
    """Add an `iv` column, solved via bisection per row. IDX rows get 0."""
    ivs = []
    for _, row in df.iterrows():
        otype = row["option_type"]
        if otype not in ("CE", "PE"):
            ivs.append(0.0)
            continue
        try:
            # time_to_expiry expects a naive datetime; strip tz.
            ref = pd.Timestamp(row["timestamp"]).tz_convert(IST).tz_localize(None).to_pydatetime()
            T = time_to_expiry(row["expiry"], reference_time=ref)
            if T <= 0:
                ivs.append(0.0)
                continue
            iv = implied_volatility_bisect(
                row["last_price"], row["underlying_price"], row["strike"],
                T, risk_free_rate, otype,
            )
            ivs.append(round(iv, 4) if iv and iv > 0 else 0.0)
        except Exception:
            ivs.append(0.0)
    df = df.copy()
    df["iv"] = ivs
    return df


def fetch_bhavcopy_range(
    underlying: str,
    from_date: datetime,
    to_date: datetime,
    nearest_expiry_only: bool = False,
) -> pd.DataFrame:
    holidays = load_holidays()
    days = trading_days(from_date, to_date, holidays)
    logger.info("Fetching %d trading days (%s → %s)", len(days),
                from_date.date(), to_date.date())

    session = requests.Session()
    frames = []
    for i, day in enumerate(days, 1):
        logger.info("  [%d/%d] %s", i, len(days), day.strftime("%Y-%m-%d"))
        csv_bytes = _download_bhavcopy(day, session)
        if csv_bytes is None:
            continue
        day_df = _parse_udiff_day(csv_bytes, day, underlying, nearest_expiry_only)
        if not day_df.empty:
            frames.append(day_df)
        time.sleep(RATE_LIMIT_DELAY)

    if not frames:
        raise RuntimeError("No bhav copy data retrieved for the requested range")

    combined = pd.concat(frames, ignore_index=True)
    logger.info("Combined %d rows across %d days; computing IV...",
                len(combined), combined["timestamp"].dt.date.nunique())
    combined = _compute_iv(combined)
    combined = combined.sort_values(
        ["timestamp", "option_type", "strike"]
    ).reset_index(drop=True)
    return combined


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    p = argparse.ArgumentParser(description="Fetch NSE F&O bhav copy (EOD, free)")
    p.add_argument("--underlying", default="NIFTY")
    p.add_argument("--from-date", type=str, default=None, help="YYYY-MM-DD")
    p.add_argument("--to-date", type=str, default=None, help="YYYY-MM-DD (default: today)")
    p.add_argument("--days", type=int, default=30, help="Lookback if --from-date omitted")
    p.add_argument("--nearest-expiry-only", action="store_true",
                   help="Keep only the nearest expiry per day (mirrors fetch_historical_data)")
    p.add_argument("--output", type=str, default=None)
    args = p.parse_args()

    to_date = (datetime.strptime(args.to_date, "%Y-%m-%d") if args.to_date
               else datetime.now())
    from_date = (datetime.strptime(args.from_date, "%Y-%m-%d") if args.from_date
                 else to_date - timedelta(days=args.days))

    data = fetch_bhavcopy_range(
        args.underlying, from_date, to_date,
        nearest_expiry_only=args.nearest_expiry_only,
    )

    if args.output:
        output_path = args.output
    else:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tag = "_nearest" if args.nearest_expiry_only else ""
        output_path = str(
            CACHE_DIR
            / f"{args.underlying}_{from_date.strftime('%Y%m%d')}_{to_date.strftime('%Y%m%d')}_eod{tag}.csv"
        )

    data.to_csv(output_path, index=False)
    print(f"\nSaved {len(data):,} rows to {output_path}")
    print(f"  Days:     {data['timestamp'].dt.date.nunique()}")
    print(f"  Symbols:  {data['symbol'].nunique()}")
    print(f"  Expiries: {data['expiry'].nunique()}")
    print(f"  Range:    {data['timestamp'].min()} — {data['timestamp'].max()}")
    print(f"\nTo run backtest:")
    print(f"  python backtest.py --data {output_path} --underlying {args.underlying}")


if __name__ == "__main__":
    main()
