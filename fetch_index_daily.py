#!/usr/bin/env python3
"""
Fetch daily index closes → data_cache/<SYMBOL>_daily.parquet (date,close).
==========================================================================
Feeds the Kalman trend-following correctness gate (`validate_kalman_trend.py`)
and backtest, which read `data_cache/<SYMBOL>_daily.{parquet,csv}`. Indices (NIFTY,
BANKNIFTY, …) are spot series — there is no F&O bhavcopy underlying for BANKNIFTY
cached, so pull them from Kite `historical_data` at the 'day' interval.

Run on the HOST (a valid Kite session is required). It reuses the cached session
via `KiteAuthManager`; do NOT trigger a fresh login while a live runner is active
(token invalidation) — run it in a quiet window or reuse the active session.

Usage (on host):
    python fetch_index_daily.py --symbol BANKNIFTY --days 400
    python fetch_index_daily.py --symbol NIFTY --from-date 2025-05-01 --to-date 2026-05-31
    python fetch_index_daily.py --symbol FINNIFTY --nse-symbol "NIFTY FIN SERVICE"
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from data_cache_io import write_table
from kite_auth import KiteAuthManager

CACHE = Path("data_cache")

# NSE `tradingsymbol` for each index (kite.instruments("NSE") lists the spot
# index under these names, not the F&O alias).
NSE_INDEX_NAME = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
    "NIFTYNXT50": "NIFTY NEXT 50",
}

# Kite caps `historical_data('day')` at ~2000 days per request; chunk to be safe.
_CHUNK_DAYS = 1800


def resolve_index_token(kite, symbol: str, nse_symbol: str | None) -> int:
    """Find the NSE instrument token for an index. Tries the explicit
    --nse-symbol, then the known alias, then the symbol itself / '<symbol> 50'.
    Fails loud listing nearby index names so a typo is obvious."""
    candidates = []
    if nse_symbol:
        candidates.append(nse_symbol)
    if symbol in NSE_INDEX_NAME:
        candidates.append(NSE_INDEX_NAME[symbol])
    candidates += [symbol, f"{symbol} 50"]

    instruments = kite.instruments("NSE")
    by_name = {inst["tradingsymbol"]: inst for inst in instruments}
    for name in candidates:
        if name in by_name:
            return int(by_name[name]["instrument_token"])

    indices = sorted(s for s in by_name
                     if "NIFTY" in s.upper() or s.upper().startswith("NIFTY"))
    raise ValueError(
        f"could not resolve an NSE index token for {symbol!r} "
        f"(tried {candidates}). Pass the exact name with --nse-symbol. "
        f"Some NSE index names: {indices[:25]}")


# Kite caps history per request by interval; chunk under the cap.
_INTERVAL_CHUNK_DAYS = {
    "day": 1800, "minute": 55, "3minute": 90, "5minute": 90,
    "10minute": 90, "15minute": 180, "30minute": 180, "60minute": 360,
}


def fetch_closes(kite, token: int, from_date: date, to_date: date,
                 interval: str = "day") -> pd.DataFrame:
    """Pull candles at `interval` in chunks; return a (ts, close) frame
    sorted/deduped. The timestamp column is `date` for daily (one row/day) and
    `datetime` for intraday (so the backtest's `close`-column reader is happy
    either way)."""
    chunk_days = _INTERVAL_CHUNK_DAYS.get(interval, 90)
    is_daily = interval == "day"
    rows = []
    cur = from_date
    while cur <= to_date:
        chunk_end = min(cur + timedelta(days=chunk_days), to_date)
        candles = kite.historical_data(token, cur, chunk_end, interval)
        for c in candles:
            ts = c["date"]
            if is_daily:
                ts = ts.date() if hasattr(ts, "date") else ts
            rows.append((ts, float(c["close"])))
        cur = chunk_end + timedelta(days=1)
    if not rows:
        raise ValueError("Kite returned no candles for the requested range")
    col = "date" if is_daily else "datetime"
    df = (pd.DataFrame(rows, columns=[col, "close"])
          .drop_duplicates(col).sort_values(col).reset_index(drop=True))
    return df


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BANKNIFTY", help="index symbol (output file stem)")
    ap.add_argument("--nse-symbol", default=None,
                    help="exact NSE tradingsymbol if not in the known alias map")
    ap.add_argument("--days", type=int, default=400,
                    help="lookback in calendar days (ignored if --from-date given)")
    ap.add_argument("--from-date", default=None, help="YYYY-MM-DD")
    ap.add_argument("--to-date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--interval", default="day",
                    choices=list(_INTERVAL_CHUNK_DAYS),
                    help="candle interval (day or intraday, e.g. 5minute)")
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--output", default=None, help="override output path")
    args = ap.parse_args()

    # Load KITE_* credentials from .env in-process, as the other fetch scripts do
    # (fetch_historical_data.py / fetch_bars.py) — KiteAuthManager reads them from
    # the environment.
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")

    to_d = datetime.strptime(args.to_date, "%Y-%m-%d").date() if args.to_date else date.today()
    from_d = (datetime.strptime(args.from_date, "%Y-%m-%d").date()
              if args.from_date else to_d - timedelta(days=args.days))
    if from_d >= to_d:
        print(f"from-date {from_d} must be before to-date {to_d}", file=sys.stderr)
        return 2

    kite = KiteAuthManager(args.config).get_kite()
    token = resolve_index_token(kite, args.symbol, args.nse_symbol)
    df = fetch_closes(kite, token, from_d, to_d, args.interval)

    stem = args.symbol if args.interval == "day" else f"{args.symbol}_{args.interval}"
    suffix = "daily" if args.interval == "day" else args.interval
    out = (Path(args.output) if args.output
           else CACHE / (f"{args.symbol}_daily.parquet" if args.interval == "day"
                         else f"{stem}.parquet"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out = write_table(df, out)
    tcol = df.columns[0]
    print(f"wrote {len(df)} {suffix} bars for {args.symbol} "
          f"({df[tcol].iloc[0]} → {df[tcol].iloc[-1]}, "
          f"range {df['close'].min():.1f}–{df['close'].max():.1f}) to {out}")
    if len(df) < 60:
        print(f"WARNING: only {len(df)} bars — increase --days.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
