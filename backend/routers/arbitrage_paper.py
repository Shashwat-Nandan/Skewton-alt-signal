"""Arbitrage paper-trading P&L for the dashboard.

Reads the per-day EOD sidecar JSONs that `run_paper_arbitrage.py` writes
(``data_cache/arbitrage_paper{,_<system>}_eod_<date>.json``) and exposes a
daily + cumulative P&L series plus the current open calendar spreads.

This router DOES NOT re-execute trades or re-run analysis — it only reads the
on-disk JSON sidecars, so a fresh request is cheap and the endpoint is safe to
poll (mirrors pair_paper_compare.py's read-only contract).
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..settings import REPO_ROOT
from ..trading_calendar import collect_trading_days

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/arbitrage-paper", tags=["arbitrage-paper"])

DATA_CACHE = REPO_ROOT / "data_cache"


def _eod_path(system: str, d: date) -> Path:
    # Match run_paper_arbitrage.write_eod_sidecar()'s naming.
    if system == "baseline":
        return DATA_CACHE / f"arbitrage_paper_eod_{d.isoformat()}.json"
    return DATA_CACHE / f"arbitrage_paper_{system}_eod_{d.isoformat()}.json"


def _cumulative_net(report: dict) -> float:
    """Cumulative book P&L as of this EOD. realized_pnl already nets transaction
    costs (arbitrage._apply_fill subtracts each fill's cost from realized_pnl)
    and is CUMULATIVE across sessions; unrealized is the open MTM. Their sum is
    the running book total — it is NOT a per-day figure, so it must never be
    summed across days."""
    return float(report.get("realized_pnl", 0.0)) + float(report.get("unrealized_pnl", 0.0))


def _session_net(report: dict) -> float:
    """This session's P&L delta. Uses the strategy's session_*_delta fields
    (added because realized/unrealized are cumulative). Falls back to 0.0 for
    legacy sidecars written before the fields existed — better a flat day than
    a fabricated daily number from a cumulative value."""
    return (float(report.get("session_realized_delta", 0.0))
            + float(report.get("session_unrealized_delta", 0.0)))


# ───────────────────────── response shape ─────────────────────────

class DailyRow(BaseModel):
    date: str
    has_data: bool
    # This session's P&L delta (session_realized_delta + session_unrealized_delta).
    day_pnl: Optional[float] = None
    # This session's realized delta alone (net of the day's costs).
    day_realized: Optional[float] = None
    n_closed_trades: Optional[int] = None
    n_open_calendars: Optional[int] = None
    # Cumulative book P&L as of this EOD (realized_pnl + unrealized_pnl,
    # already cumulative — taken from the report, never re-summed).
    cumulative_net_pnl: Optional[float] = None


class OpenCalendar(BaseModel):
    symbol: str
    position: str
    entry_carry_diff: float
    legs: List[dict]


class Summary(BaseModel):
    system: str
    n_days_with_data: int
    latest_date: Optional[str]
    # Latest snapshot (most recent day with a sidecar) — the live book view.
    realized_pnl: float
    unrealized_pnl: float
    net_pnl: float
    transaction_costs: float
    n_closed_trades: int
    n_open_calendars: int
    universe_size: Optional[int]


class ArbitrageResponse(BaseModel):
    start_date: str
    end_date: str
    system: str
    summary: Summary
    daily: List[DailyRow]
    open_calendars: List[OpenCalendar]


# ───────────────────────── endpoint ─────────────────────────

@router.get("", response_model=ArbitrageResponse)
def arbitrage_paper(
    days: int = Query(10, ge=1, le=60,
                      description="Trading days back from --end (default 10)"),
    end: Optional[str] = Query(None, description="End date YYYY-MM-DD (default today)"),
    system: str = Query("baseline", description="System tag (default baseline)"),
) -> ArbitrageResponse:
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid `end` date: {end!r}. Expected YYYY-MM-DD.",
            )
    else:
        end_date = date.today()

    day_list = collect_trading_days(end_date, days)
    if not day_list:
        raise HTTPException(status_code=500, detail="Failed to enumerate trading days")

    daily_rows: List[DailyRow] = []
    latest_report: Optional[dict] = None
    latest_date: Optional[str] = None
    n_days_with_data = 0

    for d in day_list:
        path = _eod_path(system, d)
        if not path.exists():
            daily_rows.append(DailyRow(date=d.isoformat(), has_data=False))
            continue
        try:
            payload = json.loads(path.read_text())
        except Exception as e:
            logger.warning("Failed to read %s: %s", path, e)
            daily_rows.append(DailyRow(date=d.isoformat(), has_data=False))
            continue

        report = payload.get("report") or {}
        n_days_with_data += 1
        latest_report = report
        latest_date = d.isoformat()
        daily_rows.append(DailyRow(
            date=d.isoformat(),
            has_data=True,
            day_pnl=_session_net(report),
            day_realized=float(report.get("session_realized_delta", 0.0)),
            n_closed_trades=int(report.get("n_closed_trades", 0)),
            n_open_calendars=len(report.get("open_calendars", []) or []),
            # Cumulative comes straight from the report (already cumulative);
            # do NOT accumulate _session_net here or a restart's carried-over
            # book value would be counted twice.
            cumulative_net_pnl=_cumulative_net(report),
        ))

    rep = latest_report or {}
    open_calendars = [
        OpenCalendar(
            symbol=c.get("symbol", ""),
            position=c.get("position", ""),
            entry_carry_diff=float(c.get("entry_carry_diff", 0.0)),
            legs=c.get("legs", []) or [],
        )
        for c in (rep.get("open_calendars", []) or [])
    ]
    summary = Summary(
        system=system,
        n_days_with_data=n_days_with_data,
        latest_date=latest_date,
        realized_pnl=float(rep.get("realized_pnl", 0.0)),
        unrealized_pnl=float(rep.get("unrealized_pnl", 0.0)),
        net_pnl=_cumulative_net(rep) if rep else 0.0,
        transaction_costs=float(rep.get("transaction_costs", 0.0)),
        n_closed_trades=int(rep.get("n_closed_trades", 0)),
        n_open_calendars=len(open_calendars),
        universe_size=rep.get("universe_size"),
    )

    return ArbitrageResponse(
        start_date=day_list[0].isoformat(),
        end_date=day_list[-1].isoformat(),
        system=system,
        summary=summary,
        daily=daily_rows,
        open_calendars=open_calendars,
    )
