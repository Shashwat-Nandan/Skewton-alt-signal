"""
SQLite persistence for runs / proposals / P&L snapshots.

Why stdlib sqlite3 and not SQLAlchemy: this is a single-writer dashboard
with maybe a hundred rows per run. The schema is small and the queries
are trivial. ORM bloat would just hide what's happening.

WAL mode is enabled so the FastAPI tick loop can keep writing while the
HTTP layer reads (e.g. /runs/{id} polling at 2s).

Recovery model: a backend restart kills every in-memory strategy tick
loop. We have no way to safely resume those (positions, kite session,
greeks state are all gone). On startup mark_orphan_runs_stopped() flips
any RUNNING/STOPPING rows to STOPPED with an explanatory error so the
dashboard shows them honestly as terminated.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .settings import get_settings

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    mode TEXT NOT NULL,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    stopped_at TEXT,
    last_tick_at TEXT,
    tick_count INTEGER NOT NULL DEFAULT 0,
    n_signals INTEGER NOT NULL DEFAULT 0,
    n_trades INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    last_eod_report_json TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    tradingsymbol TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    lot_size INTEGER NOT NULL,
    price REAL NOT NULL,
    rationale TEXT,
    status TEXT,
    order_id TEXT,
    mode TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_proposals_run_id ON proposals (run_id, id);

CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    total_pnl REAL,
    report_json TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pnl_run_id ON pnl_snapshots (run_id, id);

-- ──────────────────────────────────────────────────────────
-- Market Profile data
-- ──────────────────────────────────────────────────────────
-- bars_universe: every symbol whose 30-min bars we want to track. Populated
-- by fetch_bars.py on first --backfill run; the dashboard reads this to
-- populate the symbol selector.
CREATE TABLE IF NOT EXISTS bars_universe (
    symbol TEXT PRIMARY KEY,
    instrument_token INTEGER NOT NULL,
    exchange TEXT NOT NULL,
    name TEXT,
    last_backfilled_at TEXT,
    last_update_at TEXT,
    earliest_bar_ts TEXT,
    latest_bar_ts TEXT
);

-- bars: OHLCV candles. The PK collapses duplicates so re-runs of fetch_bars
-- are idempotent. `interval_minutes` is stored explicitly so we can
-- co-mingle different period bars in the same table if we ever extend
-- beyond 30-min, without an "intervals" join.
CREATE TABLE IF NOT EXISTS bars (
    instrument_token INTEGER NOT NULL,
    interval_minutes INTEGER NOT NULL,
    ts TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (instrument_token, interval_minutes, ts)
);

CREATE INDEX IF NOT EXISTS idx_bars_token_ts
    ON bars (instrument_token, interval_minutes, ts);

-- ──────────────────────────────────────────────────────────
-- Equity-swing paper book (Phase 3)
-- ──────────────────────────────────────────────────────────
-- One row per *position* (open or closed). A "scan" is identified by
-- (scan_id, scan_kind) — scan_kind is "open" or "close". No FK to runs:
-- the equity strategy runs as a cron job, not a backend-managed run, so
-- it has its own audit trail rather than borrowing the runs/proposals
-- pair. Reads from /equity/positions hit only this table.
CREATE TABLE IF NOT EXISTS equity_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                 -- always 'LONG' in v1
    entry_dt TEXT NOT NULL,
    entry_px REAL NOT NULL,
    qty INTEGER NOT NULL,
    initial_sl REAL NOT NULL,
    current_sl REAL NOT NULL,
    target REAL NOT NULL,
    atr_at_entry REAL NOT NULL,
    rationale TEXT,
    last_mtm_dt TEXT,
    last_mtm_px REAL,
    high_watermark REAL,
    status TEXT NOT NULL,               -- OPEN | CLOSED
    exit_dt TEXT,
    exit_px REAL,
    exit_reason TEXT,                   -- SL_HIT | TARGET_HIT | TIME_STOP | TRAIL_STOP | MANUAL
    pnl REAL,
    opened_by_scan TEXT                 -- "open" | "close" (which scan kind opened it)
);

CREATE INDEX IF NOT EXISTS idx_eq_pos_status_dt
    ON equity_positions (status, entry_dt DESC);

CREATE INDEX IF NOT EXISTS idx_eq_pos_symbol
    ON equity_positions (symbol, entry_dt DESC);

-- One row per scan invocation. Captures n_signals, n_trades, mode for the
-- /equity/scans dashboard tile. Lightweight — we never read the rationale.
CREATE TABLE IF NOT EXISTS equity_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_dt TEXT NOT NULL,              -- ISO timestamp
    scan_kind TEXT NOT NULL,            -- 'open' | 'close'
    mode TEXT NOT NULL,                 -- 'signals' | 'paper'
    n_signals INTEGER NOT NULL DEFAULT 0,
    n_trades INTEGER NOT NULL DEFAULT 0,
    n_open_positions INTEGER NOT NULL DEFAULT 0,
    n_closed_today INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_eq_scans_dt
    ON equity_scans (scan_dt DESC);

-- Entry-signal queue: close-scan emits entry signals AFTER market hours,
-- so a real fill cannot happen at signal-day close. Instead each signal
-- lands here as PENDING and the NEXT close-scan fills it at that day's
-- open (the official open from bhavcopy). Status transitions:
--   PENDING       — written by close-scan after signal generated
--   FILLED        — next close-scan opened a position at next-day open
--   SKIPPED_GAP   — next-day open gapped > 1.5×ATR from signal close
--   SKIPPED_STALE — no usable next-day bar (panel missing or aged > 5d)
-- One PENDING row per symbol at a time; the runner dedupes before insert.
CREATE TABLE IF NOT EXISTS equity_pending_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_dt TEXT NOT NULL,              -- ISO date of close-scan that emitted it
    symbol TEXT NOT NULL,
    side TEXT NOT NULL DEFAULT 'LONG',
    signal_close REAL NOT NULL,           -- close price the signal was anchored on
    sl_distance REAL NOT NULL,            -- absolute ₹ distance: atr × stop_multiplier
    target_distance REAL NOT NULL,        -- absolute ₹ distance: atr × stop_multiplier × RR
    atr REAL NOT NULL,
    qty INTEGER NOT NULL,
    rationale TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_eq_pending_status
    ON equity_pending_entries (status, signal_dt DESC);

CREATE INDEX IF NOT EXISTS idx_eq_pending_symbol
    ON equity_pending_entries (symbol, signal_dt DESC);
"""


_conn: Optional[sqlite3.Connection] = None
_path_override: Optional[Path] = None


def _resolve_path() -> Path:
    return _path_override if _path_override is not None else get_settings().db_path


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _resolve_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(
            str(path),
            check_same_thread=False,   # we share across asyncio tasks
            isolation_level=None,      # autocommit; we manage transactions manually
        )
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
    return _conn


def init_schema() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    logger.info("DB schema applied at %s", _resolve_path())


def reset_for_tests(path: Optional[Path] = None) -> None:
    """Close the singleton and (optionally) bind a tmp path. Used by pytest fixtures."""
    global _conn, _path_override
    if _conn is not None:
        _conn.close()
        _conn = None
    _path_override = path


# ──────────────────────────────────────────────────────────
# Runs
# ──────────────────────────────────────────────────────────

def insert_run(run) -> None:
    """First write — Run was just created in memory."""
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO runs (id, strategy_name, mode, params_json, status, created_at,
                          tick_count, n_signals, n_trades)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
        """,
        (
            run.id, run.strategy_name, run.mode, json.dumps(run.params),
            run.status, run.created_at.isoformat(),
        ),
    )


def update_run_status(
    run_id: str, status: str,
    error: Optional[str] = None,
    stopped_at: Optional[datetime] = None,
) -> None:
    conn = get_conn()
    conn.execute(
        """
        UPDATE runs
           SET status     = ?,
               error      = COALESCE(?, error),
               stopped_at = COALESCE(?, stopped_at)
         WHERE id = ?
        """,
        (
            status, error,
            stopped_at.isoformat() if stopped_at else None,
            run_id,
        ),
    )


def update_run_tick(
    run_id: str, tick_count: int, last_tick_at: datetime,
    last_eod_report: Optional[Dict[str, Any]],
) -> None:
    conn = get_conn()
    conn.execute(
        """
        UPDATE runs
           SET tick_count           = ?,
               last_tick_at         = ?,
               last_eod_report_json = ?
         WHERE id = ?
        """,
        (
            tick_count, last_tick_at.isoformat(),
            json.dumps(last_eod_report) if last_eod_report else None,
            run_id,
        ),
    )


def list_runs() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
    return [_row_to_run_dict(r) for r in rows]


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run_dict(row) if row else None


def mark_orphan_runs_stopped() -> int:
    """Called on startup. Returns the number of rows updated."""
    conn = get_conn()
    cur = conn.execute(
        """
        UPDATE runs
           SET status     = 'STOPPED',
               error      = COALESCE(error, 'Backend restarted; live state lost'),
               stopped_at = COALESCE(stopped_at, ?)
         WHERE status IN ('RUNNING', 'STOPPING')
        """,
        (datetime.now().isoformat(),),
    )
    return cur.rowcount


# ──────────────────────────────────────────────────────────
# Proposals (signals + trades, distinguished by `source`)
# ──────────────────────────────────────────────────────────

def append_proposal(
    run_id: str, kind: str, source: str, prop, result: Dict[str, Any],
) -> None:
    conn = get_conn()
    conn.execute("BEGIN")
    try:
        conn.execute(
            """
            INSERT INTO proposals
                (run_id, timestamp, kind, source, tradingsymbol, transaction_type,
                 quantity, lot_size, price, rationale, status, order_id, mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, datetime.now().isoformat(), kind, source,
                prop.tradingsymbol, prop.transaction_type, prop.quantity, prop.lot_size,
                prop.price, prop.rationale,
                result.get("status"), result.get("order_id"), result.get("mode"),
            ),
        )
        # Counter column chosen by an explicit branch — never f-string a SQL
        # identifier, even when the input is currently bounded by a Pydantic
        # Literal upstream.
        if source == "signal":
            conn.execute("UPDATE runs SET n_signals = n_signals + 1 WHERE id = ?", (run_id,))
        else:
            conn.execute("UPDATE runs SET n_trades = n_trades + 1 WHERE id = ?", (run_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_proposals(run_id: str, source: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
    conn = get_conn()
    if source:
        rows = conn.execute(
            "SELECT * FROM proposals WHERE run_id = ? AND source = ? ORDER BY id ASC LIMIT ?",
            (run_id, source, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM proposals WHERE run_id = ? ORDER BY id ASC LIMIT ?",
            (run_id, limit),
        ).fetchall()
    return [_row_to_proposal_dict(r) for r in rows]


# ──────────────────────────────────────────────────────────
# PnL snapshots
# ──────────────────────────────────────────────────────────

def append_pnl(run_id: str, report: Optional[Dict[str, Any]]) -> None:
    realized = unrealized = total = None
    if report:
        r = report.get("realized_pnl")
        u = report.get("unrealized_pnl")
        if isinstance(r, (int, float)):
            realized = float(r)
        if isinstance(u, (int, float)):
            unrealized = float(u)
        if realized is not None and unrealized is not None:
            total = realized + unrealized

    conn = get_conn()
    conn.execute(
        """
        INSERT INTO pnl_snapshots (run_id, timestamp, realized_pnl, unrealized_pnl,
                                   total_pnl, report_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, datetime.now().isoformat(), realized, unrealized, total,
            json.dumps(report) if report else None,
        ),
    )


def get_pnl_history(run_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM pnl_snapshots WHERE run_id = ? ORDER BY id ASC LIMIT ?",
        (run_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        report = json.loads(r["report_json"]) if r["report_json"] else None
        out.append({"timestamp": r["timestamp"], "report": report})
    return out


# ──────────────────────────────────────────────────────────
# Row → dict helpers (camelCase fields the API exposes)
# ──────────────────────────────────────────────────────────

def _row_to_run_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json"))
    eod = d.pop("last_eod_report_json")
    d["last_eod_report"] = json.loads(eod) if eod else None
    return d


def _row_to_proposal_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "timestamp": row["timestamp"],
        "kind": row["kind"],
        "source": row["source"],
        "tradingsymbol": row["tradingsymbol"],
        "transaction_type": row["transaction_type"],
        "quantity": row["quantity"],
        "lot_size": row["lot_size"],
        "price": row["price"],
        "rationale": row["rationale"],
        "status": row["status"],
        "order_id": row["order_id"],
        "mode": row["mode"],
    }


# ──────────────────────────────────────────────────────────
# Equity-swing paper book + scans (Phase 3)
# ──────────────────────────────────────────────────────────

def insert_equity_position(pos_dict: Dict[str, Any], opened_by_scan: str) -> int:
    """Insert a fresh OPEN position. Returns the row id."""
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO equity_positions
            (symbol, side, entry_dt, entry_px, qty, initial_sl, current_sl, target,
             atr_at_entry, rationale, last_mtm_dt, last_mtm_px, high_watermark,
             status, opened_by_scan)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """,
        (
            pos_dict["symbol"], pos_dict.get("side", "LONG"),
            pos_dict["entry_dt"], pos_dict["entry_px"], pos_dict["qty"],
            pos_dict["initial_sl"], pos_dict.get("current_sl", pos_dict["initial_sl"]),
            pos_dict["target"], pos_dict.get("atr_at_entry", 0.0),
            pos_dict.get("rationale"),
            pos_dict.get("last_mtm_dt"), pos_dict.get("last_mtm_px"),
            pos_dict.get("high_watermark", pos_dict["entry_px"]),
            opened_by_scan,
        ),
    )
    return int(cur.lastrowid)


def update_equity_position_mtm(
    position_id: int,
    last_mtm_dt: str,
    last_mtm_px: float,
    current_sl: float,
    high_watermark: float,
) -> None:
    """Mark-to-market update on an open position (no exit)."""
    conn = get_conn()
    conn.execute(
        """
        UPDATE equity_positions
           SET last_mtm_dt    = ?,
               last_mtm_px    = ?,
               current_sl     = ?,
               high_watermark = ?
         WHERE id = ?
        """,
        (last_mtm_dt, last_mtm_px, current_sl, high_watermark, position_id),
    )


def close_equity_position(
    position_id: int,
    exit_dt: str,
    exit_px: float,
    exit_reason: str,
    pnl: float,
) -> None:
    conn = get_conn()
    conn.execute(
        """
        UPDATE equity_positions
           SET status      = 'CLOSED',
               exit_dt     = ?,
               exit_px     = ?,
               exit_reason = ?,
               pnl         = ?
         WHERE id = ?
        """,
        (exit_dt, exit_px, exit_reason, pnl, position_id),
    )


def list_equity_positions(status: Optional[str] = None,
                          limit: int = 500) -> List[Dict[str, Any]]:
    conn = get_conn()
    if status is None:
        rows = conn.execute(
            """
            SELECT * FROM equity_positions
             ORDER BY (status='OPEN') DESC, entry_dt DESC, id DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM equity_positions WHERE status = ? "
            " ORDER BY entry_dt DESC, id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def find_open_equity_position(symbol: str) -> Optional[Dict[str, Any]]:
    """Resolve an open paper position by symbol (v1 has at most one per symbol)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM equity_positions WHERE symbol = ? AND status = 'OPEN' "
        " ORDER BY id DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def insert_equity_scan(
    scan_dt: str, scan_kind: str, mode: str,
    n_signals: int, n_trades: int,
    n_open_positions: int, n_closed_today: int,
    notes: Optional[str] = None,
) -> int:
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO equity_scans
            (scan_dt, scan_kind, mode, n_signals, n_trades,
             n_open_positions, n_closed_today, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (scan_dt, scan_kind, mode, n_signals, n_trades,
         n_open_positions, n_closed_today, notes),
    )
    return int(cur.lastrowid)


def insert_equity_pending_entry(
    signal_dt: str,
    symbol: str,
    side: str,
    signal_close: float,
    sl_distance: float,
    target_distance: float,
    atr: float,
    qty: int,
    rationale: Optional[str],
) -> int:
    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO equity_pending_entries
            (signal_dt, symbol, side, signal_close, sl_distance, target_distance,
             atr, qty, rationale, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
        """,
        (signal_dt, symbol, side, signal_close, sl_distance, target_distance,
         atr, qty, rationale, datetime.now().isoformat()),
    )
    return int(cur.lastrowid)


def list_equity_pending_entries(status: str = "PENDING",
                                 limit: int = 500) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM equity_pending_entries WHERE status = ? "
        " ORDER BY signal_dt ASC, id ASC LIMIT ?",
        (status, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def has_pending_entry_for_symbol(symbol: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM equity_pending_entries "
        " WHERE symbol = ? AND status = 'PENDING' LIMIT 1",
        (symbol,),
    ).fetchone()
    return row is not None


def update_equity_pending_entry_status(
    pending_id: int,
    status: str,
    note: Optional[str] = None,
) -> None:
    conn = get_conn()
    conn.execute(
        """
        UPDATE equity_pending_entries
           SET status         = ?,
               resolved_at    = ?,
               resolution_note = ?
         WHERE id = ?
        """,
        (status, datetime.now().isoformat(), note, pending_id),
    )


def list_equity_scans(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM equity_scans ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
