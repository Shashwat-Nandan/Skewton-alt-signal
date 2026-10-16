"""
NIFTY 50 index 5-minute candles from Kotak, for intraday research backtests.

Kotak's ``historical_data`` serves 5-minute candles for the NIFTY 50 index
(scrip row ``NIFTY 50``) well over a year back (probed 2026-10-16: 30, 120
and 400 days back all returned full sessions). Expired futures contracts
have no intraday history, so the index is the research series; a backtest
on it costs fills as NIFTY futures and says so.

Writes one parquet per calendar month to
``data_cache/research_nifty_5min/nifty50_5min_YYYY-MM.parquet`` (columns
``ts`` tz-aware IST bar start, ``open``, ``high``, ``low``, ``close``).
Resumable: a month whose file already covers every NSE session in it is
not fetched again.

SESSION SAFETY: reuses the cached broker session through
``market_data.fetch_5min_stf.get_cached_client`` and never logs in fresh.
Read-only: no order call exists here.

RATE LIMIT: the pair runners share this account's Kotak quota and were
429'd in October. One request every ``--delay`` seconds (default 15), a
60 s back-off on 429, and the run refuses 09:00–15:45 IST on a weekday
unless ``--force``.

Fail loud: after the fetch, every NSE session (weekdays outside
``holidays.csv``) between the first and last candle must have at least
75 bars (09:15–15:25 starts). Missing or short sessions are listed and
the exit code is 1.

    python -m market_data.fetch_index_5min --start 2024-01-01
"""
from __future__ import annotations

import argparse
import logging
import sys
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from core.runner_common import load_holidays
from market_data.history_limits import chunk_days

logger = logging.getLogger("fetch_index_5min")

HERE = Path(__file__).resolve().parent.parent
OUT_DIR = HERE / "data_cache" / "research_nifty_5min"
HOLIDAYS = HERE / "market_data" / "holidays.csv"
IST = ZoneInfo("Asia/Kolkata")
INDEX_SYMBOL = "NIFTY 50"
INTERVAL = "5minute"
# 09:15 .. 15:25 bar starts. Kotak also prints a 15:30 bar; it is kept.
FULL_SESSION_BARS = 75
QUIET_START, QUIET_END = time(9, 0), time(15, 45)


def chunk_ranges(start: date, end: date, span: int) -> List[Tuple[date, date]]:
    """Inclusive [from, to] windows of at most ``span`` days covering start..end."""
    if end < start:
        return []
    out, cur = [], start
    while cur <= end:
        to = min(cur + timedelta(days=span - 1), end)
        out.append((cur, to))
        cur = to + timedelta(days=1)
    return out


def sessions_between(start: date, end: date, holidays: set) -> List[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def session_report(frame: pd.DataFrame, holidays: set) -> Tuple[List[date], Dict[date, int]]:
    """(missing sessions, short sessions -> bar count) between the first
    and last candle. The span outside the data is not judged."""
    if frame.empty:
        return [], {}
    days = frame["ts"].dt.tz_convert(IST).dt.date
    counts = days.value_counts().to_dict()
    expected = sessions_between(min(counts), max(counts), holidays)
    missing = [d for d in expected if d not in counts]
    short = {d: int(counts[d]) for d in expected if 0 < counts.get(d, 0) < FULL_SESSION_BARS}
    return missing, short


def candles_to_frame(candles: Iterable[dict]) -> pd.DataFrame:
    rows = [
        {"ts": pd.Timestamp(c["date"]), "open": float(c["open"]), "high": float(c["high"]),
         "low": float(c["low"]), "close": float(c["close"])}
        for c in candles
    ]
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close"])
    df = pd.DataFrame(rows)
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize(IST)
    else:
        df["ts"] = df["ts"].dt.tz_convert(IST)
    bad = df[(df["high"] < df["low"]) | (df["open"] > df["high"]) | (df["open"] < df["low"])
             | (df["close"] > df["high"]) | (df["close"] < df["low"]) | (df["low"] <= 0)]
    if len(bad):
        raise ValueError(f"inconsistent OHLC candle at {bad.iloc[0]['ts']}")
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def month_path(out_dir: Path, year: int, month: int) -> Path:
    return out_dir / f"nifty50_5min_{year:04d}-{month:02d}.parquet"


def load_all(out_dir: Path = OUT_DIR) -> pd.DataFrame:
    files = sorted(out_dir.glob("nifty50_5min_*.parquet"))
    if not files:
        raise FileNotFoundError(f"no 5-minute NIFTY files in {out_dir}")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def write_months(frame: pd.DataFrame, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    local = frame["ts"].dt.tz_convert(IST)
    for (y, m), part in frame.groupby([local.dt.year, local.dt.month]):
        path = month_path(out_dir, int(y), int(m))
        if path.exists():
            part = pd.concat([pd.read_parquet(path), part], ignore_index=True)
        part = part.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        tmp = path.with_suffix(".tmp")
        part.to_parquet(tmp, index=False)
        tmp.replace(path)
        written.append(path)
    return written


def month_complete(out_dir: Path, year: int, month: int, holidays: set, today: date) -> bool:
    path = month_path(out_dir, year, month)
    if not path.exists():
        return False
    days = set(pd.read_parquet(path)["ts"].dt.tz_convert(IST).dt.date)
    first = date(year, month, 1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    want = sessions_between(first, min(last, today - timedelta(days=1)), holidays)
    return all(d in days for d in want)


def resolve_index_token(client) -> int:
    rows = [r for r in client.instruments("NSE")
            if str(r.get("tradingsymbol", "")).strip().upper() == INDEX_SYMBOL]
    if len(rows) != 1:
        raise RuntimeError(f"expected one {INDEX_SYMBOL!r} row in the NSE dump, found {len(rows)}")
    return int(rows[0]["instrument_token"])


def fetch_chunk(client, token: int, frm: date, to: date, delay: float) -> Optional[List[dict]]:
    """Candles for one window; None when Kotak has none for it. Retries 429s."""
    from core.broker.errors import BrokerNetworkError, BrokerOrderError

    for attempt in range(4):
        try:
            return client.historical_data(token, frm, to, INTERVAL)
        except BrokerOrderError as e:
            if "no candles" in str(e):
                return None
            raise
        except BrokerNetworkError as e:
            if "429" in str(e) and attempt < 3:
                logger.warning("429 on %s..%s; backing off 60 s", frm, to)
                _time.sleep(60)
                continue
            raise
        finally:
            _time.sleep(delay)
    raise RuntimeError(f"gave up on {frm}..{to} after repeated 429s")


def in_quiet_window(now: datetime) -> bool:
    return now.weekday() < 5 and QUIET_START <= now.time() <= QUIET_END


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, default=None,
                   help="last day (default: yesterday)")
    p.add_argument("--delay", type=float, default=15.0, help="seconds between requests")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    p.add_argument("--config", default=str(HERE / "config.ini"))
    p.add_argument("--force", action="store_true",
                   help="run 09:00-15:45 IST on a weekday (shares the runners' quota)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    now = datetime.now(IST)
    if in_quiet_window(now) and not args.force:
        logger.error("refusing: %s is inside 09:00-15:45 IST; the runners share this "
                     "Kotak quota. Re-run after 15:45 or pass --force.", now.strftime("%H:%M"))
        return 2
    today = now.date()
    end = args.end or today - timedelta(days=1)
    holidays = load_holidays(HOLIDAYS)

    from market_data.fetch_5min_stf import get_cached_client
    client = get_cached_client(args.config)
    token = resolve_index_token(client)
    logger.info("%s token %s, %s .. %s, %.0f s between requests",
                INDEX_SYMBOL, token, args.start, end, args.delay)

    empty, fetched = [], 0
    for frm, to in chunk_ranges(args.start, end, chunk_days(INTERVAL)):
        months = {(frm.year, frm.month), (to.year, to.month)}
        if all(month_complete(args.out_dir, y, m, holidays, today) for y, m in months):
            continue
        candles = fetch_chunk(client, token, frm, to, args.delay)
        if not candles:
            empty.append((frm, to))
            logger.info("%s..%s: no candles", frm, to)
            continue
        frame = candles_to_frame(candles)
        write_months(frame, args.out_dir)
        fetched += len(frame)
        logger.info("%s..%s: %d candles", frm, to, len(frame))

    try:
        data = load_all(args.out_dir)
    except FileNotFoundError:
        logger.error("no candles at all for %s..%s", args.start, end)
        return 1
    missing, short = session_report(data, holidays)
    first, last = data["ts"].iloc[0], data["ts"].iloc[-1]
    logger.info("stored %d candles, %s -> %s (%d new this run); windows with no data: %d",
                len(data), first, last, fetched, len(empty))
    if empty:
        logger.info("no-data windows (history starts after them, or a gap): %s",
                    ", ".join(f"{a}..{b}" for a, b in empty))
    if short:
        logger.warning("SHORT sessions (< %d bars): %s", FULL_SESSION_BARS,
                       ", ".join(f"{d} ({n})" for d, n in sorted(short.items())))
    if missing:
        logger.error("MISSING sessions inside the data span (%d): %s", len(missing),
                     ", ".join(str(d) for d in missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
