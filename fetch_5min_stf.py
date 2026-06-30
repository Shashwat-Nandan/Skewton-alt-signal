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


def front_month_fut_token(kite, symbol: str) -> Optional[Tuple[int, str]]:
    """Resolve the nearest non-expired NFO front-month future for `symbol`.
    Returns (instrument_token, tradingsymbol) or None."""
    today = datetime.now().date()
    futs = [
        i for i in kite.instruments("NFO")
        if i.get("name") == symbol and i.get("instrument_type") == "FUT"
    ]
    futs = [f for f in futs if _as_date(f.get("expiry")) and _as_date(f["expiry"]) >= today]
    if not futs:
        logger.warning("%s: no live NFO future found", symbol)
        return None
    futs.sort(key=lambda f: _as_date(f["expiry"]))
    front = futs[0]
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


def fetch_5min_continuous(kite, token: int, days: int) -> List[dict]:
    """5-minute CONTINUOUS futures candles over the last `days`, chunked under the
    Kite 60-day cap. continuous=True roll-stitches across monthly expiries."""
    to_d = datetime.now()
    from_d = to_d - timedelta(days=days)
    out: List[dict] = []
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
        out.extend(candles)
        time.sleep(KITE_RATE_LIMIT_DELAY)
        cur = chunk_end + timedelta(days=1)
    return out


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
    ok = skipped = 0
    for sym in symbols:
        resolved = front_month_fut_token(kite, sym)
        if not resolved:
            skipped += 1
            continue
        token, tsym = resolved
        candles = fetch_5min_continuous(kite, token, args.days)
        if not candles:
            logger.warning("%s (%s): no candles returned", sym, tsym)
            skipped += 1
            continue
        n = write_csv(sym, candles, out_dir)
        logger.info("%s (%s): wrote %d 5-min bars", sym, tsym, n)
        ok += 1
    logger.info("Done: %d symbols written, %d skipped → %s", ok, skipped, out_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
