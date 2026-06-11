"""Run lifecycle endpoints."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from strategies import STRATEGIES, VALID_MODES

from .. import db, kite_oauth
from ..run_manager import get_run_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/runs", tags=["runs"])


class CreateRunRequest(BaseModel):
    strategy: str
    # NOTE: live is accepted by the schema but unconditionally 403'd in
    # create_run — the dashboard never trades live (audit 2026-06-10, 1.3).
    mode: Literal["signals", "paper", "live"]
    params: Dict[str, Any] = Field(default_factory=dict)


class RunSummary(BaseModel):
    id: str
    strategy_name: str
    mode: str
    params: Dict[str, Any]
    status: str
    created_at: str
    stopped_at: Optional[str] = None
    last_tick_at: Optional[str] = None
    tick_count: int = 0
    error: Optional[str] = None
    n_signals: int = 0
    n_trades: int = 0
    last_eod_report: Optional[Dict[str, Any]] = None


class RunDetail(RunSummary):
    signals: List[Dict[str, Any]] = Field(default_factory=list)
    trades: List[Dict[str, Any]] = Field(default_factory=list)
    pnl_history: List[Dict[str, Any]] = Field(default_factory=list)


@router.post("", response_model=RunSummary, status_code=201)
async def create_run(req: CreateRunRequest):
    if req.strategy not in STRATEGIES:
        raise HTTPException(status_code=400, detail=f"Unknown strategy {req.strategy!r}")
    if req.mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {VALID_MODES}")
    if req.mode == "live":
        # Audit 2026-06-10 task 1.3 (H-2): unconditional, no flag check.
        # ALLOW_LIVE_MODE in the host .env exists to arm the HEADLESS
        # runners' quad-lock; it must not also arm RunManager — a second
        # execution engine with none of the runner-side risk controls
        # (daily-loss halt, margin precheck, broker reconciliation,
        # partial-fill reversal). Going live = deploy/VPS_DEPLOYMENT.md §7,
        # never the dashboard.
        raise HTTPException(
            status_code=403,
            detail="Live mode is not available from the dashboard. "
                   "Use the headless runner (deploy/VPS_DEPLOYMENT.md §7).",
        )

    kite = kite_oauth.get_authenticated_kite()
    if kite is None:
        raise HTTPException(status_code=401, detail="Not authenticated with Kite — log in first")

    manager = get_run_manager()
    try:
        run = manager.create_run(req.strategy, req.mode, req.params, kite=kite)
    except Exception:
        # Don't echo the underlying exception — kiteconnect errors can
        # carry URL/API-key fragments that don't belong in client bodies.
        # The full traceback is in the journal via logger.exception.
        logger.exception("create_run failed for strategy=%s mode=%s", req.strategy, req.mode)
        raise HTTPException(status_code=500, detail="Failed to create run")
    return RunSummary(**run.to_dict())


@router.get("", response_model=List[RunSummary])
def list_runs():
    return [RunSummary(**r) for r in get_run_manager().list_runs()]


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: str):
    run_dict = get_run_manager().get_run_dict(run_id)
    if run_dict is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunDetail(
        **run_dict,
        signals=db.get_proposals(run_id, source="signal"),
        trades=db.get_proposals(run_id, source="trade"),
        pnl_history=db.get_pnl_history(run_id),
    )


@router.post("/{run_id}/stop", response_model=RunSummary)
async def stop_run(run_id: str):
    manager = get_run_manager()
    ok = await manager.stop_run(run_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Run not found")
    run_dict = manager.get_run_dict(run_id)
    if run_dict is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunSummary(**run_dict)
