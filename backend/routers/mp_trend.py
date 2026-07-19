"""Market-Profile trend_up paper-book P&L for the dashboard.

Reads the ``mp_trend_positions`` / ``mp_trend_runs`` tables that
``runners/run_paper_mp.py`` writes into dashboard.db and exposes a cumulative net-P&L
series, the day's open (overnight) positions, and a summary.

Read-only and paper-only: this book never touches an order path. The tables are
created by the runner (not in the core SCHEMA), so every query first checks the
table exists and returns an empty book otherwise — the page renders cleanly
before the runner has ever run.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mp-trend", tags=["mp-trend"])


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ───────────────────────── response shape ─────────────────────────

class DailyRun(BaseModel):
    date: str
    n_trend_up: int
    n_opened: int
    n_closed: int
    day_net: float
    cum_net: float
    halted: bool
    reason: Optional[str] = None


class OpenPosition(BaseModel):
    symbol: str
    entry_date: str
    entry_px: float
    qty: int
    notional: float


class Summary(BaseModel):
    net_pnl: float            # cumulative realized net (closed trades)
    gross_pnl: float
    costs: float
    n_closed_trades: int
    n_open_positions: int
    win_rate: float
    latest_date: Optional[str]
    halted: bool
    halt_reason: Optional[str] = None


class MpTrendResponse(BaseModel):
    summary: Summary
    daily: List[DailyRun]
    open_positions: List[OpenPosition]


_EMPTY = MpTrendResponse(
    summary=Summary(net_pnl=0.0, gross_pnl=0.0, costs=0.0, n_closed_trades=0,
                    n_open_positions=0, win_rate=0.0, latest_date=None,
                    halted=False, halt_reason=None),
    daily=[], open_positions=[],
)


# ───────────────────────── endpoint ─────────────────────────

@router.get("", response_model=MpTrendResponse)
def mp_trend(
    days: int = Query(60, ge=1, le=400,
                      description="Most-recent run days to return (default 60)"),
) -> MpTrendResponse:
    conn = db.get_conn()
    # Both tables are created together by the runner; guard both so a partial /
    # legacy DB (one table dropped or renamed) returns an empty book instead of
    # a 500 from an unguarded query on the missing table.
    if not (_table_exists(conn, "mp_trend_runs")
            and _table_exists(conn, "mp_trend_positions")):
        return _EMPTY

    # Daily runs (most recent `days`, returned oldest→newest for charting).
    rows = conn.execute(
        "SELECT run_date, n_trend_up, n_opened, n_closed, day_net, cum_net, "
        "halted, reason FROM mp_trend_runs ORDER BY run_date DESC LIMIT ?",
        (days,),
    ).fetchall()
    daily = [
        DailyRun(
            date=r["run_date"], n_trend_up=int(r["n_trend_up"] or 0),
            n_opened=int(r["n_opened"] or 0), n_closed=int(r["n_closed"] or 0),
            day_net=float(r["day_net"] or 0.0), cum_net=float(r["cum_net"] or 0.0),
            halted=bool(r["halted"]), reason=r["reason"] or None,
        )
        for r in reversed(rows)
    ]

    # Position aggregates (over the whole book, not just the window).
    agg = conn.execute(
        "SELECT COALESCE(SUM(net),0) net, COALESCE(SUM(gross),0) gross, "
        "COALESCE(SUM(cost),0) cost, COUNT(*) n, "
        "COALESCE(SUM(CASE WHEN net>0 THEN 1 ELSE 0 END),0) wins "
        "FROM mp_trend_positions WHERE status='CLOSED'"
    ).fetchone()
    n_closed = int(agg["n"] or 0)

    open_rows = conn.execute(
        "SELECT symbol, entry_date, entry_px, qty FROM mp_trend_positions "
        "WHERE status='OPEN' ORDER BY entry_date DESC, symbol"
    ).fetchall()
    open_positions = [
        OpenPosition(
            symbol=r["symbol"], entry_date=r["entry_date"],
            entry_px=float(r["entry_px"]), qty=int(r["qty"]),
            notional=float(r["entry_px"]) * int(r["qty"]),
        )
        for r in open_rows
    ]

    latest = daily[-1] if daily else None
    summary = Summary(
        net_pnl=float(agg["net"] or 0.0),
        gross_pnl=float(agg["gross"] or 0.0),
        costs=float(agg["cost"] or 0.0),
        n_closed_trades=n_closed,
        n_open_positions=len(open_positions),
        win_rate=(int(agg["wins"] or 0) / n_closed) if n_closed else 0.0,
        latest_date=latest.date if latest else None,
        halted=latest.halted if latest else False,
        halt_reason=latest.reason if (latest and latest.halted) else None,
    )
    return MpTrendResponse(summary=summary, daily=daily, open_positions=open_positions)
