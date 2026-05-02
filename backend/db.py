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
    counter = "n_signals" if source == "signal" else "n_trades"
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
        conn.execute(f"UPDATE runs SET {counter} = {counter} + 1 WHERE id = ?", (run_id,))
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
