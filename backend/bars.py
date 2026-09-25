"""
SQLite storage layer for the Market Profile feature.

The schema lives in `backend.db.SCHEMA`. This module is the read/write
surface used by both the FastAPI router (`/api/market-profile/...`) and the
ingestion script (`market_data/fetch_bars.py`).

Why stdlib sqlite3 here too: same as runs/proposals — single-writer
dashboard, simple queries, no ORM.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import db

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# Universe
# ──────────────────────────────────────────────────────────

def upsert_universe(
    symbol: str, instrument_token: int, exchange: str, name: Optional[str] = None,
) -> None:
    """Idempotent insert/update — keeps `name` and exchange fresh on re-runs."""
    conn = db.get_conn()
    conn.execute(
        """
        INSERT INTO bars_universe (symbol, instrument_token, exchange, name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            instrument_token = excluded.instrument_token,
            exchange         = excluded.exchange,
            name             = COALESCE(excluded.name, bars_universe.name)
        """,
        (symbol, int(instrument_token), exchange, name),
    )


def list_universe() -> List[Dict[str, Any]]:
    conn = db.get_conn()
    rows = conn.execute(
        """
        SELECT symbol, instrument_token, exchange, name,
               last_backfilled_at, last_update_at,
               earliest_bar_ts, latest_bar_ts
          FROM bars_universe
         ORDER BY symbol ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_universe_row(symbol: str) -> Optional[Dict[str, Any]]:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT * FROM bars_universe WHERE symbol = ?", (symbol,)
    ).fetchone()
    return dict(row) if row else None


def mark_backfilled(symbol: str) -> None:
    conn = db.get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        """
        UPDATE bars_universe
           SET last_backfilled_at = ?,
               last_update_at     = ?
         WHERE symbol = ?
        """,
        (now, now, symbol),
    )
    _refresh_bar_range(symbol)


def mark_updated(symbol: str) -> None:
    conn = db.get_conn()
    conn.execute(
        "UPDATE bars_universe SET last_update_at = ? WHERE symbol = ?",
        (datetime.now().isoformat(), symbol),
    )
    _refresh_bar_range(symbol)


def _refresh_bar_range(symbol: str) -> None:
    """
    Recompute earliest_bar_ts / latest_bar_ts for the symbol from its bars
    rows. Cheap (covered by the (instrument_token, interval_minutes, ts) PK)
    and avoids drift between universe row metadata and the actual data.
    """
    conn = db.get_conn()
    row = conn.execute(
        "SELECT instrument_token FROM bars_universe WHERE symbol = ?", (symbol,)
    ).fetchone()
    if not row:
        return
    rng = conn.execute(
        "SELECT MIN(ts) AS mn, MAX(ts) AS mx FROM bars WHERE instrument_token = ?",
        (row["instrument_token"],),
    ).fetchone()
    conn.execute(
        """
        UPDATE bars_universe
           SET earliest_bar_ts = ?,
               latest_bar_ts   = ?
         WHERE symbol = ?
        """,
        (rng["mn"], rng["mx"], symbol),
    )


# ──────────────────────────────────────────────────────────
# Bars
# ──────────────────────────────────────────────────────────

def insert_bars(
    instrument_token: int,
    interval_minutes: int,
    rows: Iterable[Tuple[str, float, float, float, float, int]],
) -> int:
    """
    Bulk-insert OHLCV rows for a single instrument+interval. Each input row
    is (ts_iso, open, high, low, close, volume). Idempotent via the
    composite primary key — `INSERT OR IGNORE` skips existing bars on
    re-fetch so backfill→update→re-backfill is safe.

    Returns the number of NEW rows inserted.
    """
    rows = list(rows)
    if not rows:
        return 0
    conn = db.get_conn()
    cur = conn.executemany(
        """
        INSERT OR IGNORE INTO bars
            (instrument_token, interval_minutes, ts, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (int(instrument_token), int(interval_minutes), ts,
             float(o), float(h), float(l), float(c), int(v))
            for (ts, o, h, l, c, v) in rows
        ],
    )
    return cur.rowcount


def get_bars(
    instrument_token: int, interval_minutes: int,
    from_ts: Optional[str] = None, to_ts: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch bars for one instrument+interval, optionally bounded. Timestamps
    are inclusive on both ends. Sorted ascending.
    """
    conn = db.get_conn()
    sql = (
        "SELECT ts, open, high, low, close, volume "
        "FROM bars "
        "WHERE instrument_token = ? AND interval_minutes = ?"
    )
    params: List[Any] = [int(instrument_token), int(interval_minutes)]
    if from_ts is not None:
        sql += " AND ts >= ?"
        params.append(from_ts)
    if to_ts is not None:
        sql += " AND ts <= ?"
        params.append(to_ts)
    sql += " ORDER BY ts ASC"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def latest_bar_ts(instrument_token: int, interval_minutes: int) -> Optional[str]:
    """ISO timestamp of the latest stored bar, for incremental --update."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT MAX(ts) AS mx FROM bars "
        "WHERE instrument_token = ? AND interval_minutes = ?",
        (int(instrument_token), int(interval_minutes)),
    ).fetchone()
    return row["mx"] if row and row["mx"] else None


def count_bars(instrument_token: int, interval_minutes: int) -> int:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM bars "
        "WHERE instrument_token = ? AND interval_minutes = ?",
        (int(instrument_token), int(interval_minutes)),
    ).fetchone()
    return int(row["n"]) if row else 0


def copy_bars(src_token: int, dst_token: int, interval_minutes: int) -> int:
    """Copy one interval's OHLCV from `src_token` onto `dst_token`.

    A broker switch changes the instrument id, not the cash prices. The
    copy keeps that history readable under the new id. INSERT OR IGNORE
    so a retry does not duplicate rows. Returns how many rows were newly
    stored under `dst_token`.
    """
    src_token = int(src_token)
    dst_token = int(dst_token)
    interval_minutes = int(interval_minutes)
    if src_token == dst_token:
        return 0
    conn = db.get_conn()
    before = count_bars(dst_token, interval_minutes)
    conn.execute(
        """
        INSERT OR IGNORE INTO bars
            (instrument_token, interval_minutes, ts, open, high, low, close, volume)
        SELECT ?, interval_minutes, ts, open, high, low, close, volume
          FROM bars
         WHERE instrument_token = ? AND interval_minutes = ?
        """,
        (dst_token, src_token, interval_minutes),
    )
    return count_bars(dst_token, interval_minutes) - before
