"""Buy-on-Gap paper-trading P&L for the dashboard.

Reads the per-day EOD sidecar JSONs that ``run_paper_buy_on_gap.py`` writes
(``data_cache/buy_on_gap_paper{,_<system>}_eod_<date>.json``) and exposes a
daily + cumulative P&L series plus the day's open intraday positions.

Read-only: a request only reads the on-disk sidecars (no trade re-execution),
so it is cheap and safe to poll — mirrors arbitrage_paper.py's contract.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..settings import REPO_ROOT
from ..trading_calendar import collect_trading_days

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/buy-on-gap-paper", tags=["buy-on-gap-paper"])

DATA_CACHE = REPO_ROOT / "data_cache"


def _eod_path(system: str, d: date) -> Path:
    # Match run_paper_buy_on_gap.write_eod_sidecar()'s naming.
    if system == "baseline":
        return DATA_CACHE / f"buy_on_gap_paper_eod_{d.isoformat()}.json"
    return DATA_CACHE / f"buy_on_gap_paper_{system}_eod_{d.isoformat()}.json"


def _cumulative_net(report: dict) -> float:
    """Running book P&L as of this EOD. realized_pnl already nets transaction
    costs and is CUMULATIVE across sessions; unrealized is the open MTM (≈0 for
    this intraday strategy once flat). Never sum this across days."""
    return float(report.get("realized_pnl", 0.0)) + float(report.get("unrealized_pnl", 0.0))


def _session_net(report: dict) -> float:
    """This session's P&L delta (realized/unrealized are cumulative, so the
    per-day figure comes from the strategy's session_*_delta fields). Falls
    back to 0.0 for legacy sidecars rather than fabricating a daily number."""
    return (float(report.get("session_realized_delta", 0.0))
            + float(report.get("session_unrealized_delta", 0.0)))


# ───────────────────────── response shape ─────────────────────────

class DailyRow(BaseModel):
    date: str
    has_data: bool
    day_pnl: Optional[float] = None          # session_realized_delta + session_unrealized_delta
    day_realized: Optional[float] = None     # session_realized_delta alone (net of costs)
    n_closed_trades: Optional[int] = None    # cumulative closed-trade count at EOD
    n_open_positions: Optional[int] = None
    win_rate: Optional[float] = None
    cumulative_net_pnl: Optional[float] = None


class OpenPosition(BaseModel):
    symbol: str
    entry_px: float
    qty: int
    stop_px: float
    gap_z: float
    last_mtm_px: float
    pnl: float


class Summary(BaseModel):
    system: str
    n_days_with_data: int
    latest_date: Optional[str]
    realized_pnl: float          # cumulative
    unrealized_pnl: float
    net_pnl: float
    transaction_costs: float
    n_closed_trades: int
    n_open_positions: int
    win_rate: float
    universe_size: Optional[int]


class BuyOnGapResponse(BaseModel):
    start_date: str
    end_date: str
    system: str
    summary: Summary
    daily: List[DailyRow]
    open_positions: List[OpenPosition]


# ───────────────────────── endpoint ─────────────────────────

@router.get("", response_model=BuyOnGapResponse)
def buy_on_gap_paper(
    days: int = Query(10, ge=1, le=60,
                      description="Trading days back from --end (default 10)"),
    end: Optional[str] = Query(None, description="End date YYYY-MM-DD (default today)"),
    system: str = Query("baseline", description="System tag (default baseline)"),
) -> BuyOnGapResponse:
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"Invalid `end` date: {end!r}. Expected YYYY-MM-DD.")
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
            n_open_positions=len(report.get("open_positions", []) or []),
            win_rate=report.get("win_rate"),
            cumulative_net_pnl=_cumulative_net(report),
        ))

    rep = latest_report or {}
    open_positions = [
        OpenPosition(
            symbol=p.get("symbol", ""),
            entry_px=float(p.get("entry_px", 0.0)),
            qty=int(p.get("qty", 0)),
            stop_px=float(p.get("stop_px", 0.0)),
            gap_z=float(p.get("gap_z", 0.0)),
            last_mtm_px=float(p.get("last_mtm_px", 0.0)),
            pnl=float(p.get("pnl", 0.0)),
        )
        for p in (rep.get("open_positions", []) or [])
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
        n_open_positions=len(open_positions),
        win_rate=float(rep.get("win_rate", 0.0)),
        universe_size=rep.get("universe_size"),
    )

    return BuyOnGapResponse(
        start_date=day_list[0].isoformat(),
        end_date=day_list[-1].isoformat(),
        system=system,
        summary=summary,
        daily=daily_rows,
        open_positions=open_positions,
    )
