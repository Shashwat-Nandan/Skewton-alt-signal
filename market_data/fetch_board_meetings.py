#!/usr/bin/env python3
"""
Fetch NSE corporate board-meeting intimations (the earnings calendar).

Source: ``https://www.nseindia.com/api/corporate-board-meetings``

Companies must intimate the exchange before a board meeting that considers
financial results, so this endpoint is a *forward-looking* earnings calendar as
well as a historical record. Unlike ``fetch_fii_dii``'s endpoint it accepts an
explicit ``from_date``/``to_date`` range and serves full history, so a backfill
is just a loop over months.

Each row carries ``bm_timestamp`` — when the intimation was filed — which is
what makes an event study anti-look-ahead: a strategy may only act on a meeting
date that was already public at its decision time. ``load_results_calendar``
keeps that column for exactly this reason.

Cadence: once daily is plenty (intimations arrive throughout the day, and the
median lead time is ~11 days). Same shape as ``fetch-bars.timer``.

Typical operator usage::

    python -m market_data.fetch_board_meetings                    # next 90 days
    python -m market_data.fetch_board_meetings --days-ahead 45
    python -m market_data.fetch_board_meetings --from-date 2025-01-01 --to-date 2026-08-31

NSE archives are gated by Akamai. This works from the VPS (where
``market_data/fetch_bhavcopy.py`` already works); short-lived dev environments
may 403 on the homepage warm and still succeed on the API call.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache") / "board_meetings"
NSE_HOMEPAGE = "https://www.nseindia.com/"
NSE_BM_URL = "https://www.nseindia.com/api/corporate-board-meetings"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
}

# A results meeting, as opposed to fund-raising / buyback / dividend-only /
# board-reshuffle intimations, which share the endpoint. Matched against
# purpose + description, lowercased.
_RESULTS_RE = re.compile(r"financial result|quarterly result|audited result")

# Two intimations for the same symbol within this many days are the same
# quarter's meeting (companies revise dates); the latest intimation wins.
_QUARTER_CLUSTER_DAYS = 45


def _bootstrap_session() -> requests.Session:
    """NSE's API requires a homepage hit first to seed the bot-detection cookies."""
    s = requests.Session()
    try:
        s.get(NSE_HOMEPAGE, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        # Non-fatal: the API call sometimes succeeds on the seeded UA alone.
        logger.warning("NSE homepage warm failed (%s) — trying the API anyway", e)
    return s


def fetch_range(from_date: date, to_date: date,
                session: Optional[requests.Session] = None) -> Optional[list]:
    """Pull board meetings for [from_date, to_date]. Returns a list of dicts or None."""
    s = session or _bootstrap_session()
    url = (f"{NSE_BM_URL}?index=equities"
           f"&from_date={from_date.strftime('%d-%m-%Y')}"
           f"&to_date={to_date.strftime('%d-%m-%Y')}")
    try:
        resp = s.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        logger.warning("board-meetings fetch failed for %s..%s: %s", from_date, to_date, e)
        return None
    if resp.status_code != 200:
        logger.warning("board-meetings unexpected status %d for %s..%s",
                       resp.status_code, from_date, to_date)
        return None
    try:
        data = resp.json()
    except ValueError as e:
        logger.warning("board-meetings non-JSON response: %s", e)
        return None
    if not isinstance(data, list):
        logger.warning("board-meetings unexpected payload type: %r", type(data))
        return None
    return data


def _month_starts(from_date: date, to_date: date) -> List[date]:
    out, d = [], from_date.replace(day=1)
    while d <= to_date:
        out.append(d)
        d = (d.replace(day=28) + timedelta(days=5)).replace(day=1)
    return out


def _row_key(r: dict) -> tuple:
    """Identity of one intimation, for merge-dedupe."""
    return (r.get("bm_symbol"), r.get("bm_date"), r.get("bm_timestamp"),
            r.get("bm_purpose"))


def write_cached(rows: list, month: date) -> Path:
    """
    MERGE ``rows`` into ``data_cache/board_meetings/YYYY-MM.json``.

    Merge, not overwrite. ``sync`` clips its fetch to the requested range, so
    the daily runner call — ``sync(today, today + 45d)`` — asks for only the
    tail of the current month. Writing that verbatim would delete every earlier
    meeting in the file and progressively shred the historical record the
    research depends on (code review 2026-08-29, finding 3).

    Later rows win on a repeated key, so a re-fetch refreshes a revised
    intimation in place.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{month.strftime('%Y-%m')}.json"
    merged: dict = {}
    if out.exists():
        try:
            for r in json.loads(out.read_text()):
                merged[_row_key(r)] = r
        except (ValueError, OSError) as e:
            logger.warning("existing cache %s unreadable (%s) — rewriting", out.name, e)
            merged = {}
    before = len(merged)
    for r in rows:
        merged[_row_key(r)] = r
    out.write_text(json.dumps(list(merged.values()), indent=1, default=str))
    logger.debug("%s: %d existing + %d fetched -> %d rows",
                 out.name, before, len(rows), len(merged))
    return out


def sync(from_date: date, to_date: date) -> int:
    """Fetch and cache every month spanned by the range. Returns rows written."""
    session = _bootstrap_session()
    total = 0
    for m in _month_starts(from_date, to_date):
        end = (m.replace(day=28) + timedelta(days=5)).replace(day=1) - timedelta(days=1)
        rows = fetch_range(max(m, from_date), min(end, to_date), session)
        if rows is None:
            logger.warning("month %s: fetch failed, leaving any existing cache intact",
                           m.strftime("%Y-%m"))
            continue
        write_cached(rows, m)
        total += len(rows)
        logger.info("month %s: %d meetings", m.strftime("%Y-%m"), len(rows))
    return total


def load_results_calendar(cache_dir: Optional[Path] = None) -> pd.DataFrame:
    """
    Read every cached month and return the deduplicated results calendar.

    Columns: ``symbol``, ``event_date`` (Timestamp, the board-meeting date),
    ``announced_at`` (Timestamp, the intimation time). One row per symbol per
    quarter — meetings clustered within ``_QUARTER_CLUSTER_DAYS`` collapse to
    the LATEST intimation, which is the operative (possibly revised) date.

    Returns an empty frame with the right columns when the cache is missing, so
    a caller's gate degrades to "no earnings known" rather than raising.
    """
    cols = ["symbol", "event_date", "announced_at"]

    def _empty() -> pd.DataFrame:
        # Typed, not bare: callers do `cal.event_date.dt.normalize()`, and an
        # object-dtype column raises on `.dt` — turning "no earnings known"
        # into a crash (review finding 2).
        return pd.DataFrame({"symbol": pd.Series(dtype="object"),
                             "event_date": pd.Series(dtype="datetime64[ns]"),
                             "announced_at": pd.Series(dtype="datetime64[ns]")})

    d = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    if not d.exists():
        return _empty()
    rows: list = []
    for f in sorted(d.glob("*.json")):
        try:
            rows.extend(json.loads(f.read_text()))
        except (ValueError, OSError) as e:
            logger.warning("skipping unreadable board-meeting cache %s: %s", f, e)
    if not rows:
        return _empty()

    df = pd.DataFrame(rows)
    for c in ("bm_symbol", "bm_date", "bm_purpose", "bm_desc", "bm_timestamp"):
        if c not in df.columns:
            df[c] = None
    text = (df.bm_purpose.fillna("") + " " + df.bm_desc.fillna("")).str.lower()
    df = df[text.str.contains(_RESULTS_RE, regex=True, na=False)].copy()
    if df.empty:
        return _empty()

    df["event_date"] = pd.to_datetime(df.bm_date, format="%d-%b-%Y", errors="coerce")
    df["announced_at"] = pd.to_datetime(df.bm_timestamp, format="%d-%b-%Y %H:%M:%S",
                                        errors="coerce")
    df = df.dropna(subset=["event_date"]).rename(columns={"bm_symbol": "symbol"})
    df = df.sort_values(["symbol", "event_date", "announced_at"])

    # Anti-look-ahead depends entirely on announced_at. If NSE changes the
    # timestamp format every value coerces to NaT, the strategy's "was this
    # public yet?" guard silently passes everything, and we reproduce exactly
    # the contamination the research doc calls out (§7). Fail loud instead.
    nat = float(df.announced_at.isna().mean())
    if nat > 0.20:
        logger.error(
            "%.0f%% of board-meeting intimation timestamps failed to parse — the "
            "anti-look-ahead gate would be a no-op. Check NSE's bm_timestamp "
            "format against '%%d-%%b-%%Y %%H:%%M:%%S'.", 100 * nat,
        )

    gap = df.groupby("symbol").event_date.diff().dt.days
    df["cluster"] = (gap.isna() | (gap > _QUARTER_CLUSTER_DAYS)).groupby(df.symbol).cumsum()
    # drop_duplicates keeps a WHOLE row. groupby(...).last() would take the last
    # NON-NULL value per column independently, so a revised meeting whose
    # timestamp failed to parse could contribute event_date while announced_at
    # was spliced in from an older intimation — a stale-but-valid timestamp that
    # waves the event through the publicity check (review finding 4).
    # Prefer the most recently ANNOUNCED intimation; an unparseable timestamp
    # sorts last so the operative (revised) meeting date still wins, and it
    # carries its own NaT rather than an older row's timestamp. The consumer
    # then fails CLOSED on the unknown announcement time.
    out = (df.sort_values(["symbol", "cluster", "announced_at", "event_date"],
                          na_position="last")
             .drop_duplicates(subset=["symbol", "cluster"], keep="last")[cols])
    return out.sort_values(["event_date", "symbol"]).reset_index(drop=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fetch the NSE board-meeting (earnings) calendar")
    p.add_argument("--from-date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--to-date", help="YYYY-MM-DD (default: today + --days-ahead)")
    p.add_argument("--days-ahead", type=int, default=90,
                   help="forward window when --to-date is absent (default 90)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    frm = (datetime.strptime(args.from_date, "%Y-%m-%d").date()
           if args.from_date else date.today())
    to = (datetime.strptime(args.to_date, "%Y-%m-%d").date()
          if args.to_date else frm + timedelta(days=args.days_ahead))
    if to < frm:
        logger.error("--to-date %s is before --from-date %s", to, frm)
        return 2

    n = sync(frm, to)
    cal = load_results_calendar()
    logger.info("cached %d raw meetings; results calendar now holds %d symbol-quarters",
                n, len(cal))
    if n == 0:
        logger.error("no rows fetched for %s..%s — NSE may be blocking this host", frm, to)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
