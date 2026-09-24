"""
30-minute bar ingestion for the Market Profile feature.

Uses the configured broker (Kotak Neo by default).

Modes
-----
  --backfill          Pull as much history as Kite will return per token.
                      Idempotent — re-running is safe (PK collapses dupes).
  --update            Incremental: from last stored bar to today.
  --add-symbols A,B,C Resolve and store these symbols in bars_universe
                      without fetching bars yet (handy for staging).

Sources
-------
  The configured broker's historical_data per symbol (Kotak Neo by
  default). Keep the daily update running; the corpus grows forward.

Universe
--------
By default we ingest every NIFTY-50 spot. Pass --universe-csv FILE or
--symbols A,B,C to override.

Schema and storage helpers live in `backend/bars.py`.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Tuple


from backend import bars as bars_db
from backend import db as backend_db

logger = logging.getLogger(__name__)

KITE_RATE_LIMIT_DELAY = 0.35     # 3 req/s leaves margin
# 30-minute candles: under both Kite's ~60-day cap and Kotak's 90-day cap.
KITE_CHUNK_DAYS = 55

# Default universe = NIFTY 50. We deliberately don't pull this from a
# Kite call — `screen_pairs.NIFTY_50` is the canonical list used elsewhere
# in the repo. Keeping a single source of truth.
try:
    from core.screen_pairs import NIFTY_50
    DEFAULT_UNIVERSE = list(NIFTY_50)
except Exception:
    DEFAULT_UNIVERSE = []


# ──────────────────────────────────────────────────────────
# Symbol resolution
# ──────────────────────────────────────────────────────────

def resolve_nse_symbols(kite, symbols: Iterable[str]) -> List[Tuple[str, int, str]]:
    """
    Look up `instrument_token` for each NSE cash-equity symbol. Returns a
    list of (symbol, instrument_token, name) tuples. Missing symbols are
    logged and skipped — the caller decides whether that's fatal.
    """
    wanted = {s.strip().upper() for s in symbols if s and s.strip()}
    if not wanted:
        return []

    logger.info("Loading NSE instrument master to resolve %d symbols…", len(wanted))
    instruments = kite.instruments("NSE")
    out: List[Tuple[str, int, str]] = []
    seen: set[str] = set()
    for inst in instruments:
        ts = inst.get("tradingsymbol", "")
        if ts in wanted and ts not in seen:
            out.append((ts, int(inst["instrument_token"]), inst.get("name") or ts))
            seen.add(ts)

    missing = wanted - seen
    if missing:
        logger.warning("Could not resolve %d symbols on NSE: %s",
                       len(missing), sorted(missing))
    return out


# ──────────────────────────────────────────────────────────
# Kite fetch wrapper
# ──────────────────────────────────────────────────────────

def fetch_30min_bars(
    kite, instrument_token: int,
    from_date: datetime, to_date: datetime,
) -> List[Tuple[str, float, float, float, float, int]]:
    """
    Pull 30-min OHLCV between [from_date, to_date], chunking under the
    broker's per-request limit. Returns a list of tuples ready for
    `bars_db.insert_bars`.
    """
    rows: List[Tuple[str, float, float, float, float, int]] = []
    cur = from_date
    while cur < to_date:
        chunk_end = min(cur + timedelta(days=KITE_CHUNK_DAYS), to_date)
        try:
            candles = kite.historical_data(
                instrument_token,
                cur.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                "30minute",
            )
        except Exception as e:
            logger.warning("historical fetch failed for token %d %s→%s: %s",
                           instrument_token, cur.date(), chunk_end.date(), e)
            candles = []

        for c in candles:
            ts_obj = c.get("date")
            ts_iso = ts_obj.isoformat() if hasattr(ts_obj, "isoformat") else str(ts_obj)
            rows.append((
                ts_iso,
                float(c["open"]), float(c["high"]),
                float(c["low"]), float(c["close"]),
                int(c.get("volume", 0) or 0),
            ))
        time.sleep(KITE_RATE_LIMIT_DELAY)
        cur = chunk_end + timedelta(days=1)
    return rows


# ──────────────────────────────────────────────────────────
# Top-level operations
# ──────────────────────────────────────────────────────────

def cmd_add_symbols(kite, symbols: List[str]) -> None:
    resolved = resolve_nse_symbols(kite, symbols)
    for sym, token, name in resolved:
        bars_db.upsert_universe(sym, token, "NSE", name=name)
    logger.info("Added/updated %d symbols in bars_universe", len(resolved))


def cmd_backfill(kite, symbols: List[str], days: int) -> None:
    """Backfill `days` of history per symbol."""
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)
    resolved = resolve_nse_symbols(kite, symbols)

    for sym, token, name in resolved:
        bars_db.upsert_universe(sym, token, "NSE", name=name)
        logger.info("Backfilling %s (token=%d) %s → %s …",
                    sym, token, from_date.date(), to_date.date())
        rows = fetch_30min_bars(kite, token, from_date, to_date)
        new_n = bars_db.insert_bars(token, 30, rows)
        bars_db.mark_backfilled(sym)
        total_n = bars_db.count_bars(token, 30)
        logger.info("  → fetched %d, inserted %d new, %d total bars stored",
                    len(rows), new_n, total_n)


def cmd_update(kite) -> None:
    """
    Incremental: for every symbol in bars_universe, fetch from the last
    stored bar (+1 minute, to avoid re-pulling it) up to now.

    The stored instrument_token belongs to whichever broker wrote it.
    Re-resolve from the current master so a Kite token is not sent to Kotak.
    """
    universe = bars_db.list_universe()
    if not universe:
        logger.warning("bars_universe is empty — run --backfill first.")
        return

    resolved = {
        sym: (token, name)
        for sym, token, name in resolve_nse_symbols(
            kite, [row["symbol"] for row in universe],
        )
    }
    now = datetime.now()
    for row in universe:
        sym = row["symbol"]
        stored = int(row["instrument_token"])
        if sym not in resolved:
            logger.error(
                "%s is in bars_universe but not in the current broker's NSE master. Skipping.",
                sym,
            )
            continue
        token, name = resolved[sym]
        if token != stored:
            logger.warning(
                "%s instrument_token changed %s -> %s under the current broker. "
                "New bars are stored under %s. Bars under %s are left in place.",
                sym, stored, token, token, stored,
            )
            bars_db.upsert_universe(sym, token, "NSE", name)
        latest = bars_db.latest_bar_ts(token, 30)

        if latest:
            # Kite's `historical_data` returns tz-aware ISO timestamps
            # (e.g. "2026-04-30T15:15:00+05:30"). Strip the offset so the
            # comparison against naive `datetime.now()` works without
            # introducing a tz dependency to the rest of the script.
            stripped = latest.split("+")[0].split("-05:30")[0]
            try:
                from_dt = datetime.fromisoformat(stripped) + timedelta(minutes=1)
            except ValueError:
                from_dt = now - timedelta(days=KITE_CHUNK_DAYS)
        else:
            from_dt = now - timedelta(days=KITE_CHUNK_DAYS)

        if from_dt >= now:
            logger.info("%s already up to date (latest=%s)", sym, latest)
            continue

        logger.info("Updating %s (token=%d) %s → %s …",
                    sym, token, from_dt, now)
        rows = fetch_30min_bars(kite, token, from_dt, now)
        new_n = bars_db.insert_bars(token, 30, rows)
        bars_db.mark_updated(sym)
        logger.info("  → fetched %d, inserted %d new", len(rows), new_n)


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    p = argparse.ArgumentParser(description=__doc__)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--backfill", action="store_true",
                    help="Backfill --days of history for the configured universe.")
    grp.add_argument("--update", action="store_true",
                    help="Incremental fetch from each symbol's latest stored bar.")
    grp.add_argument("--add-symbols", type=str, default=None,
                    help="Resolve and stage symbols in bars_universe without fetching.")

    p.add_argument("--days", type=int, default=180,
                   help="Backfill window in days (default: 180). Note: Kite "
                        "intraday history is typically capped to ~60-90 days.")
    p.add_argument("--symbols", type=str, default=None,
                   help="Comma-separated symbols. Default: NIFTY 50.")
    p.add_argument("--universe-csv", type=str, default=None,
                   help="Path to a newline-separated symbol file (alternative to --symbols).")
    p.add_argument("--config", type=str, default="config.ini",
                   help="config.ini. [broker] name selects the session (kotak by default).")
    args = p.parse_args()

    # Resolve universe
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe_csv:
        symbols = [s.strip().upper() for s in
                   Path(args.universe_csv).read_text().splitlines() if s.strip()]
    else:
        symbols = list(DEFAULT_UNIVERSE)
    if args.backfill or args.add_symbols:
        if not symbols:
            logger.error("No symbols to operate on. Pass --symbols or --universe-csv.")
            return 1

    # Auth + DB
    from core.broker import get_trading_client
    kite = get_trading_client(args.config)
    profile = kite.profile()
    logger.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])

    backend_db.init_schema()

    if args.add_symbols:
        cmd_add_symbols(kite, [s.strip() for s in args.add_symbols.split(",") if s.strip()])
        return 0
    if args.backfill:
        cmd_backfill(kite, symbols, args.days)
        return 0
    if args.update:
        cmd_update(kite)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
