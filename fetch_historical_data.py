"""
Fetch Historical NIFTY Option Chain Data from Zerodha Kite
==========================================================
Downloads spot + option chain candles via kite.historical_data(),
reconstructs the option chain at each timestamp, computes IV,
and outputs a backtest-compatible DataFrame/CSV.

Usage:
  python fetch_historical_data.py --days 30
  python fetch_historical_data.py --from-date 2026-03-01 --to-date 2026-03-30
  python fetch_historical_data.py --days 30 --interval 15minute --strikes 10

Requirements:
  - Valid Kite session (set KITE_API_KEY, KITE_API_SECRET, etc.)
  - kiteconnect package installed
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))

from data_cache_io import write_table
from greeks_engine import implied_volatility_bisect, time_to_expiry
from kite_auth import KiteAuthManager

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RATE_LIMIT_DELAY = 0.35  # seconds between API calls (3 req/s limit)


def fetch_instrument_master(kite, underlying: str = "NIFTY") -> pd.DataFrame:
    """
    Fetch and cache the NFO instrument master.
    Returns DataFrame with columns: tradingsymbol, instrument_token, name,
    strike, expiry, instrument_type, lot_size.
    """
    cache_file = CACHE_DIR / f"instruments_{underlying}_{datetime.now().strftime('%Y%m%d')}.csv"
    if cache_file.exists():
        logger.info("Loading cached instrument master from %s", cache_file)
        df = pd.read_csv(cache_file)
        df["expiry"] = pd.to_datetime(df["expiry"])
        df["strike"] = df["strike"].astype(float)
        return df

    logger.info("Fetching NFO instrument master...")
    instruments = kite.instruments("NFO")
    df = pd.DataFrame(instruments)

    # Filter to underlying
    df = df[df["name"] == underlying].copy()
    df["expiry"] = pd.to_datetime(df["expiry"])
    df["strike"] = df["strike"].astype(float)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_file, index=False)
    logger.info("Cached %d instruments to %s", len(df), cache_file)

    return df


def get_spot_token(kite, underlying: str = "NIFTY") -> int:
    """Get the instrument token for the underlying index.

    NSE lists index spot under display names, not the F&O underlying key
    (BANKNIFTY → "NIFTY BANK"). Reuses the canonical NSE_INDEX_NAME map from
    fetch_index_daily (the 5-index superset) rather than a local subset, and
    tries candidates in PRIORITY order — mapped display name first, then the
    raw key / "<key> 50" as fallbacks. Priority matters: the raw F&O key is
    never a real NSE spot symbol, so matching it ahead of the display name
    could return an unrelated equity/ETF that happens to share the name."""
    from fetch_index_daily import NSE_INDEX_NAME

    candidates = []
    if underlying in NSE_INDEX_NAME:
        candidates.append(NSE_INDEX_NAME[underlying])
    candidates += [underlying, f"{underlying} 50"]

    by_name = {inst["tradingsymbol"]: inst for inst in kite.instruments("NSE")}
    for name in candidates:
        if name in by_name:
            return by_name[name]["instrument_token"]
    raise ValueError(f"Could not find instrument token for {underlying} "
                     f"(tried {candidates})")


def select_strikes(
    spot: float,
    instruments_df: pd.DataFrame,
    expiry: pd.Timestamp,
    n_strikes: int = 10,
) -> pd.DataFrame:
    """
    Select ATM ± n_strikes for both CE and PE at the given expiry.
    Returns filtered instrument DataFrame.
    """
    chain = instruments_df[
        (instruments_df["expiry"] == expiry)
        & (instruments_df["instrument_type"].isin(["CE", "PE"]))
    ].copy()

    if chain.empty:
        return chain

    # Find ATM strike
    unique_strikes = sorted(chain["strike"].unique())
    atm_strike = min(unique_strikes, key=lambda k: abs(k - spot))
    atm_idx = unique_strikes.index(atm_strike)

    # Select range
    lo = max(0, atm_idx - n_strikes)
    hi = min(len(unique_strikes), atm_idx + n_strikes + 1)
    selected_strikes = unique_strikes[lo:hi]

    return chain[chain["strike"].isin(selected_strikes)]


def fetch_historical_candles(
    kite,
    instrument_token: int,
    from_date: datetime,
    to_date: datetime,
    interval: str = "30minute",
) -> pd.DataFrame:
    """
    Fetch historical OHLCV candles for a single instrument.
    Handles Kite's 60-day limit per request by chunking.
    """
    all_candles = []
    chunk_days = 55  # Stay under 60-day limit

    current_from = from_date
    while current_from < to_date:
        current_to = min(current_from + timedelta(days=chunk_days), to_date)

        try:
            candles = kite.historical_data(
                instrument_token,
                current_from.strftime("%Y-%m-%d"),
                current_to.strftime("%Y-%m-%d"),
                interval,
            )
            all_candles.extend(candles)
        except Exception as e:
            logger.warning(
                "Failed to fetch token %d (%s to %s): %s",
                instrument_token, current_from.date(), current_to.date(), e,
            )

        time.sleep(RATE_LIMIT_DELAY)
        current_from = current_to + timedelta(days=1)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles)
    df.rename(columns={"date": "timestamp"}, inplace=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def _build_expiry_windows(
    spot_df: pd.DataFrame,
    instruments_df: pd.DataFrame,
    from_date: datetime,
    to_date: datetime,
    fixed_expiry: Optional[str] = None,
) -> List[Dict]:
    """
    Partition the date range into windows, each mapped to the nearest weekly expiry.

    Returns list of dicts:
      {"expiry": pd.Timestamp, "from_date": datetime, "to_date": datetime,
       "spot_min": float, "spot_max": float}
    """
    all_expiries = sorted(instruments_df[
        (instruments_df["expiry"] >= pd.Timestamp(from_date))
        & (instruments_df["instrument_type"].isin(["CE", "PE"]))
    ]["expiry"].unique())

    if not len(all_expiries):
        raise ValueError("No expiries found covering the date range")

    if fixed_expiry:
        # Single-expiry mode (user override)
        return [{
            "expiry": pd.Timestamp(fixed_expiry),
            "from_date": from_date,
            "to_date": to_date,
            "spot_min": spot_df["spot_close"].min(),
            "spot_max": spot_df["spot_close"].max(),
        }]

    # For each trading date, assign the nearest expiry that is >= that date
    spot_df = spot_df.copy()
    spot_df["date"] = pd.to_datetime(spot_df["timestamp"]).dt.date

    daily_spots = spot_df.groupby("date")["spot_close"].agg(["min", "max"]).reset_index()
    daily_spots.columns = ["date", "spot_min", "spot_max"]

    windows = []
    current_window = None

    for _, day_row in daily_spots.iterrows():
        day = pd.Timestamp(day_row["date"])
        # Nearest expiry >= this date
        valid = [e for e in all_expiries if e >= day]
        if not valid:
            # Past last expiry — use the last available one
            nearest_expiry = all_expiries[-1]
        else:
            nearest_expiry = valid[0]

        if current_window is None or current_window["expiry"] != nearest_expiry:
            # Start a new window
            if current_window is not None:
                windows.append(current_window)
            current_window = {
                "expiry": nearest_expiry,
                "from_date": day_row["date"],
                "to_date": day_row["date"],
                "spot_min": day_row["spot_min"],
                "spot_max": day_row["spot_max"],
            }
        else:
            current_window["to_date"] = day_row["date"]
            current_window["spot_min"] = min(current_window["spot_min"], day_row["spot_min"])
            current_window["spot_max"] = max(current_window["spot_max"], day_row["spot_max"])

    if current_window is not None:
        windows.append(current_window)

    return windows


def fetch_option_chain_data(
    kite,
    underlying: str = "NIFTY",
    from_date: datetime = None,
    to_date: datetime = None,
    interval: str = "30minute",
    n_strikes: int = 10,
    expiry: Optional[str] = None,
    instruments_cache: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch complete option chain historical data with rolling expiry and
    per-window strike selection.

    Steps:
      1. Fetch instrument master and spot candles
      2. Partition date range into weekly expiry windows
      3. For each window: select ATM ± n_strikes using that window's spot range
      4. Fetch candles for each window's instruments
      5. Assemble into backtest-compatible DataFrame

    Returns DataFrame with columns:
      timestamp, symbol, underlying_price, strike, option_type, expiry,
      last_price, bid, ask, lot_size, iv
    """
    if from_date is None:
        from_date = datetime.now() - timedelta(days=30)
    if to_date is None:
        to_date = datetime.now()

    # ── Step 1: Instrument master ──
    if instruments_cache:
        logger.info("Using cached instrument master: %s", instruments_cache)
        instruments_df = pd.read_csv(instruments_cache)
        instruments_df["expiry"] = pd.to_datetime(instruments_df["expiry"])
        instruments_df["strike"] = instruments_df["strike"].astype(float)
    else:
        instruments_df = fetch_instrument_master(kite, underlying)

    # ── Step 2: Fetch spot data ──
    spot_token = get_spot_token(kite, underlying)
    logger.info("Fetching spot data (token=%d)...", spot_token)
    spot_df = fetch_historical_candles(kite, spot_token, from_date, to_date, interval)

    if spot_df.empty:
        raise ValueError("No spot data returned — check date range and session")

    spot_df = spot_df.rename(columns={"close": "spot_close"})
    logger.info("Got %d spot candles (%s to %s)",
                len(spot_df), spot_df["timestamp"].min(), spot_df["timestamp"].max())

    # ── Step 3: Build expiry windows ──
    windows = _build_expiry_windows(spot_df, instruments_df, from_date, to_date, expiry)
    logger.info("Built %d expiry windows:", len(windows))
    for w in windows:
        logger.info("  %s — %s: expiry %s, spot [%.0f, %.0f]",
                    w["from_date"], w["to_date"], pd.Timestamp(w["expiry"]).date(),
                    w["spot_min"], w["spot_max"])

    # ── Step 4: Fetch option candles per window ──
    option_data = []
    fetched_tokens = set()  # Avoid duplicate fetches for overlapping instruments
    lot_size = None

    for w_idx, window in enumerate(windows):
        w_expiry = pd.Timestamp(window["expiry"])
        w_from = datetime.combine(pd.Timestamp(window["from_date"]).date(), datetime.min.time())
        w_to = datetime.combine(pd.Timestamp(window["to_date"]).date(), datetime.max.time())
        expiry_str = w_expiry.strftime("%Y-%m-%d")

        # Select strikes covering the full spot range in this window
        # Use midpoint of spot range as ATM reference, with wider n_strikes
        spot_mid = (window["spot_min"] + window["spot_max"]) / 2
        spot_range_pct = (window["spot_max"] - window["spot_min"]) / spot_mid * 100
        # Add extra strikes to cover spot drift within the window
        extra_strikes = max(int(spot_range_pct / 1.0), 2)  # ~1 strike per 1% move
        effective_n_strikes = n_strikes + extra_strikes

        selected = select_strikes(spot_mid, instruments_df, w_expiry, effective_n_strikes)
        if selected.empty:
            logger.warning("No instruments for window %s (expiry %s) — skipping",
                          window["from_date"], w_expiry.date())
            continue

        if lot_size is None:
            lot_size = int(selected.iloc[0]["lot_size"])

        logger.info("  Window %d: %d instruments (±%d strikes around %.0f, expiry %s)",
                    w_idx + 1, len(selected), effective_n_strikes, spot_mid, w_expiry.date())

        for idx, (_, inst) in enumerate(selected.iterrows()):
            token = int(inst["instrument_token"])
            sym = inst["tradingsymbol"]
            strike = float(inst["strike"])
            otype = inst["instrument_type"]

            # Skip if already fetched (instrument spans multiple windows)
            fetch_key = (token, w_from.date(), w_to.date())
            if fetch_key in fetched_tokens:
                continue
            fetched_tokens.add(fetch_key)

            logger.info("    [%d/%d] Fetching %s (token=%d, %s—%s)...",
                        idx + 1, len(selected), sym, token, w_from.date(), w_to.date())

            candles = fetch_historical_candles(kite, token, w_from, w_to, interval)
            if candles.empty:
                logger.warning("    No data for %s — skipping", sym)
                continue

            for _, row in candles.iterrows():
                option_data.append({
                    "timestamp": row["timestamp"],
                    "symbol": sym,
                    "strike": strike,
                    "option_type": otype,
                    "expiry": expiry_str,
                    "last_price": row["close"],
                    "high": row["high"],
                    "low": row["low"],
                    "open": row["open"],
                    "volume": row.get("volume", 0),
                })

    logger.info("Fetched candles for %d instruments across %d windows",
                len(fetched_tokens), len(windows))

    if not option_data:
        raise ValueError("No option candle data returned")

    options_df = pd.DataFrame(option_data)

    # ── Step 6: Merge spot prices and compute IV ──
    options_df["timestamp"] = pd.to_datetime(options_df["timestamp"])

    # Merge on exact timestamp match (same interval candles)
    options_df = options_df.merge(
        spot_df[["timestamp", "spot_close"]],
        on="timestamp",
        how="left",
    )

    # Forward-fill any missing spot prices (e.g., option traded but spot candle missing)
    options_df["spot_close"] = options_df["spot_close"].ffill()
    options_df = options_df.dropna(subset=["spot_close"])

    # Estimate bid/ask from high/low (conservative proxy)
    # If high == low (no range), use 0.2% spread
    options_df["bid"] = options_df.apply(
        lambda r: r["low"] if r["high"] > r["low"]
        else r["last_price"] * 0.999, axis=1
    )
    options_df["ask"] = options_df.apply(
        lambda r: r["high"] if r["high"] > r["low"]
        else r["last_price"] * 1.001, axis=1
    )

    # Compute IV using bisection (each row carries its own expiry)
    logger.info("Computing implied volatilities...")
    ivs = []
    for _, row in options_df.iterrows():
        try:
            T = time_to_expiry(row["expiry"], reference_time=row["timestamp"].to_pydatetime())
            if T <= 0:
                ivs.append(0.0)
                continue
            iv = implied_volatility_bisect(
                row["last_price"], row["spot_close"], row["strike"],
                T, 0.065, row["option_type"],
            )
            ivs.append(round(iv, 4) if iv and iv > 0 else 0.0)
        except Exception:
            ivs.append(0.0)
    options_df["iv"] = ivs

    if lot_size is None:
        lot_size = 25  # Fallback

    # ── Step 7: Assemble spot rows ──
    # Each spot row gets the expiry of the nearest weekly chain at that timestamp
    spot_rows = []
    for _, row in spot_df.iterrows():
        ts = row["timestamp"]
        ts_date = pd.Timestamp(ts).date() if hasattr(pd.Timestamp(ts), 'date') else ts.date()
        # Find the expiry window this timestamp belongs to
        spot_expiry = pd.Timestamp(windows[-1]["expiry"]).strftime("%Y-%m-%d")  # fallback
        for w in windows:
            if w["from_date"] <= ts_date <= w["to_date"]:
                spot_expiry = pd.Timestamp(w["expiry"]).strftime("%Y-%m-%d")
                break
        spot_rows.append({
            "timestamp": ts,
            "symbol": underlying,
            "underlying_price": row["spot_close"],
            "strike": 0,
            "option_type": "IDX",
            "expiry": spot_expiry,
            "last_price": row["spot_close"],
            "bid": row["spot_close"] * 0.999,
            "ask": row["spot_close"] * 1.001,
            "lot_size": lot_size,
            "iv": 0,
        })

    spot_result = pd.DataFrame(spot_rows)

    # ── Step 8: Assemble final DataFrame ──
    result = pd.DataFrame({
        "timestamp": options_df["timestamp"],
        "symbol": options_df["symbol"],
        "underlying_price": options_df["spot_close"],
        "strike": options_df["strike"],
        "option_type": options_df["option_type"],
        "expiry": options_df["expiry"],
        "last_price": options_df["last_price"],
        "bid": options_df["bid"],
        "ask": options_df["ask"],
        "lot_size": lot_size,
        "iv": options_df["iv"],
    })

    result = pd.concat([spot_result, result], ignore_index=True)
    result = result.sort_values(["timestamp", "option_type", "strike"]).reset_index(drop=True)

    # Drop rows with zero/negative prices
    result = result[result["last_price"] > 0]

    logger.info("Final dataset: %d rows, %d timestamps, %d symbols, %d expiries",
                len(result),
                result["timestamp"].nunique(),
                result["symbol"].nunique(),
                result["expiry"].nunique())

    return result


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Fetch historical NIFTY option chain data")
    parser.add_argument("--days", type=int, default=30,
                        help="Number of days of data to fetch (default: 30)")
    parser.add_argument("--from-date", type=str, default=None,
                        help="Start date (YYYY-MM-DD). Overrides --days.")
    parser.add_argument("--to-date", type=str, default=None,
                        help="End date (YYYY-MM-DD). Default: today.")
    parser.add_argument("--interval", type=str, default="30minute",
                        choices=["minute", "3minute", "5minute", "15minute", "30minute", "60minute"],
                        help="Candle interval (default: 30minute)")
    parser.add_argument("--underlying", type=str, default="NIFTY",
                        help="Underlying to fetch (default: NIFTY)")
    parser.add_argument("--strikes", type=int, default=10,
                        help="Number of strikes above and below ATM (default: 10)")
    parser.add_argument("--expiry", type=str, default=None,
                        help="Specific expiry date (YYYY-MM-DD). Default: auto-select.")
    parser.add_argument("--output", type=str, default=None,
                        help="Output CSV path. Default: data_cache/{underlying}_{dates}.csv")
    parser.add_argument("--config", type=str, default="config.ini",
                        help="Config file path (default: config.ini)")
    parser.add_argument("--instruments-cache", type=str, default=None,
                        help="Path to a previously-saved instrument master CSV. "
                             "Use this when the live master no longer contains the "
                             "required (expired) contracts.")
    args = parser.parse_args()

    # Parse dates
    if args.to_date:
        to_date = datetime.strptime(args.to_date, "%Y-%m-%d")
    else:
        to_date = datetime.now()

    if args.from_date:
        from_date = datetime.strptime(args.from_date, "%Y-%m-%d")
    else:
        from_date = to_date - timedelta(days=args.days)

    logger.info("Date range: %s to %s", from_date.date(), to_date.date())

    # Authenticate
    auth = KiteAuthManager(args.config)
    kite = auth.get_kite()
    profile = kite.profile()
    logger.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])

    # Fetch data
    data = fetch_option_chain_data(
        kite,
        underlying=args.underlying,
        from_date=from_date,
        to_date=to_date,
        interval=args.interval,
        n_strikes=args.strikes,
        expiry=args.expiry,
        instruments_cache=args.instruments_cache,
    )

    # Save
    if args.output:
        output_path = args.output
    else:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(
            CACHE_DIR
            / f"{args.underlying}_{from_date.strftime('%Y%m%d')}_{to_date.strftime('%Y%m%d')}.parquet"
        )

    output_path = str(write_table(data, output_path))
    logger.info("Saved %d rows to %s", len(data), output_path)

    # Summary
    print("\nData fetched successfully:")
    print(f"  Rows:       {len(data):,}")
    print(f"  Timestamps: {data['timestamp'].nunique():,}")
    print(f"  Symbols:    {data['symbol'].nunique()}")
    print(f"  Date range: {data['timestamp'].min()} — {data['timestamp'].max()}")
    print(f"  Output:     {output_path}")
    print("\nTo run backtest:")
    print(f"  python backtest.py --data {output_path} --underlying {args.underlying}")
    print("\nTo run autoresearch:")
    print(f"  python run_autoresearch.py --data {output_path}")


if __name__ == "__main__":
    main()
