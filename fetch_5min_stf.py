"""
5-minute single-stock-futures (STF) fetcher for the Kalman pairs backtest.
================================================================================
Pulls 5-minute CONTINUOUS front-month futures candles for the pair universe via
Kite's historical API and writes one CSV per symbol to data_cache/stf_5min/.
The Kalman pairs backtest's 5-min replay mode (backtest_kalman_pairs.py
--timeframe 5min) loads these.

WHY this exists (vs fetch_bars.py): fetch_bars.py pulls 30-min NSE *cash* bars
for the Market Profile feature. The pairs system trades single-stock *futures*,
so we need 5-min NFO front-month data, roll-stitched across monthly expiries —
which Kite gives us directly via `continuous=True`, avoiding manual stitching.

SESSION SAFETY (standing rule): this REUSES the cached Kite session and NEVER
fresh-logs-in. If the cached token is missing or rejected server-side it ABORTS
loudly — it does not fall back to the TOTP login flow (a fresh login invalidates
the account's current token and can break a live runner / evening job).

RUN ON THE HOST: the valid cached session lives where the runners run. Run this
after market close, reusing that session:
    python fetch_5min_stf.py --days 90
    python fetch_5min_stf.py --symbols RELIANCE,INFY --days 60

Kite caps intraday history at ~60-90 days, so the corpus is shallow; run it
periodically (or via a timer) to grow it forward.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

logger = logging.getLogger("fetch_5min_stf")

KITE_RATE_LIMIT_DELAY = 0.35      # ~3 req/s, matches fetch_bars.py
KITE_CHUNK_DAYS = 55              # under the 60-day per-request intraday cap
OUT_DIR = HERE / "data_cache" / "stf_5min"


def get_cached_kite(config_path: str):
    """Return a KiteConnect using ONLY the cached session. Aborts (SystemExit)
    if there is no valid cached token — never triggers the login flow."""
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")        # KITE_* into env for the SDK (no secrets printed)
    from kite_auth import KiteAuthManager

    auth = KiteAuthManager(config_path)
    if not auth._load_cached_token():
        logger.error("ABORT: no cached Kite token (%s). Refusing to fresh-login.",
                     KiteAuthManager.TOKEN_CACHE_FILE)
        raise SystemExit(2)
    auth.kite.set_access_token(auth._access_token)
    try:
        prof = auth.kite.profile()    # server-side validity check
    except Exception as e:
        logger.error("ABORT: cached token rejected by Kite (%s). Refusing to "
                     "fresh-login — run where the live session is cached.", e)
        raise SystemExit(3)
    logger.info("Reusing cached session: %s (%s)", prof["user_name"], prof["user_id"])
    return auth.kite


def front_month_fut_token(nfo_instruments, symbol: str) -> Optional[Tuple[int, str]]:
    """Resolve the nearest non-expired NFO front-month future for `symbol` from a
    pre-fetched NFO instrument dump. Returns (instrument_token, tradingsymbol) or
    None. Takes the dump (not a kite handle) so the caller fetches it ONCE for the
    whole universe instead of per symbol."""
    today = datetime.now().date()
    futs = []
    for i in nfo_instruments:
        if i.get("name") != symbol or i.get("instrument_type") != "FUT":
            continue
        exp = _as_date(i.get("expiry"))
        if exp and exp >= today:
            futs.append((exp, i))
    if not futs:
        logger.warning("%s: no live NFO future found", symbol)
        return None
    futs.sort(key=lambda t: t[0])
    front = futs[0][1]
    return int(front["instrument_token"]), front["tradingsymbol"]


def _as_date(v):
    if v is None:
        return None
    if hasattr(v, "year") and not hasattr(v, "hour"):   # date
        return v
    if hasattr(v, "date"):                              # datetime
        return v.date()
    try:
        return datetime.strptime(str(v), "%Y-%m-%d").date()
    except ValueError:
        return None


def fetch_5min_continuous(kite, token: int, days: int) -> Tuple[List[dict], int]:
    """5-minute CONTINUOUS futures candles over the last `days`, chunked under the
    Kite 60-day cap. continuous=True roll-stitches across monthly expiries.
    Returns (candles, n_failed_chunks): a failed chunk leaves a HOLE in the series,
    so the caller must surface a non-zero failure count rather than treat a partial
    fetch as complete (Rule 12)."""
    to_d = datetime.now()
    from_d = to_d - timedelta(days=days)
    out: List[dict] = []
    failed = 0
    cur = from_d
    while cur < to_d:
        chunk_end = min(cur + timedelta(days=KITE_CHUNK_DAYS), to_d)
        try:
            candles = kite.historical_data(
                token, cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"),
                "5minute", continuous=True,
            )
        except Exception as e:
            logger.warning("fetch failed token=%d %s→%s: %s",
                           token, cur.date(), chunk_end.date(), e)
            candles = []
            failed += 1
        out.extend(candles)
        time.sleep(KITE_RATE_LIMIT_DELAY)
        cur = chunk_end + timedelta(days=1)
    return out, failed


def write_csv(symbol: str, candles: List[dict], out_dir: Path) -> int:
    """Write date,open,high,low,close,volume sorted+de-duped on timestamp."""
    rows = {}
    for c in candles:
        ts = c.get("date")
        ts_iso = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        rows[ts_iso] = (ts_iso, c["open"], c["high"], c["low"], c["close"],
                        int(c.get("volume", 0) or 0))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for k in sorted(rows):
            w.writerow(rows[k])
    return len(rows)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated underlyings. Default: screen_pairs.NIFTY_50.")
    ap.add_argument("--days", type=int, default=90,
                    help="Lookback window (Kite caps intraday ~60-90d).")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--config", default="config.ini")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        from screen_pairs import NIFTY_50
        symbols = list(NIFTY_50)

    kite = get_cached_kite(args.config)
    out_dir = Path(args.out_dir)
    # Fetch the NFO instrument master ONCE (it's multi-MB) and resolve every
    # front-month token from it, rather than re-downloading it per symbol.
    nfo = kite.instruments("NFO")
    ok = skipped = partial = 0
    for sym in symbols:
        resolved = front_month_fut_token(nfo, sym)
        if not resolved:
            skipped += 1
            continue
        token, tsym = resolved
        candles, failed_chunks = fetch_5min_continuous(kite, token, args.days)
        if not candles:
            logger.warning("%s (%s): no candles returned", sym, tsym)
            skipped += 1
            continue
        n = write_csv(sym, candles, out_dir)
        if failed_chunks:
            # Fail loud: the CSV has a hole — do NOT report it as a clean write.
            logger.warning("%s (%s): wrote %d 5-min bars but %d chunk(s) FAILED — "
                           "CSV is INCOMPLETE; re-run to backfill the gap",
                           sym, tsym, n, failed_chunks)
            partial += 1
        else:
            logger.info("%s (%s): wrote %d 5-min bars", sym, tsym, n)
            ok += 1
    logger.info("Done: %d complete, %d PARTIAL (gaps), %d skipped → %s",
                ok, partial, skipped, out_dir)
    # Non-zero exit if anything is incomplete, so a wrapper notices the gaps.
    return 0 if (ok and not partial and not skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
