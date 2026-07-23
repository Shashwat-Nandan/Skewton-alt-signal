"""
Delivery-accumulation API (read-only).

  GET  /api/delivery/positions?status=open|closed
  GET  /api/delivery/scans?limit=N
  GET  /api/delivery/pending-entries?status=...
  GET  /api/delivery/signals?date=YYYY-MM-DD

Mirror of the /equity/* endpoints against the delivery_* tables written by
``runners/run_delivery_accum.py``. The strategy writes only via the cron
path; the dashboard never places orders (safety rule 1).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/delivery", tags=["delivery-accum"])

LOG_DIR = REPO_ROOT / "logs"

STRATEGY_NAME = "delivery_accum"


class DeliveryPositionRow(BaseModel):
    id: int
    symbol: str
    side: str
    entry_dt: str
    entry_px: float
    qty: int
    initial_sl: float
    current_sl: float
    target: float
    atr_at_entry: float
    rationale: Optional[str] = None
    last_mtm_dt: Optional[str] = None
    last_mtm_px: Optional[float] = None
    high_watermark: Optional[float] = None
    status: str
    exit_dt: Optional[str] = None
    exit_px: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: Optional[float] = None
    opened_by_scan: Optional[str] = None


class DeliveryPositionsResponse(BaseModel):
    positions: List[DeliveryPositionRow]


class DeliverySignal(BaseModel):
    timestamp: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    price: float
    rationale: Optional[str] = None


class DeliverySignalsResponse(BaseModel):
    date: str
    generated_at: Optional[str] = None
    signals: List[DeliverySignal]


class DeliveryScanRow(BaseModel):
    id: int
    scan_dt: str
    scan_kind: str
    mode: str
    n_signals: int
    n_trades: int
    n_open_positions: int
    n_closed_today: int
    notes: Optional[str] = None


class DeliveryScansResponse(BaseModel):
    scans: List[DeliveryScanRow]


class DeliveryPendingEntryRow(BaseModel):
    id: int
    signal_dt: str
    symbol: str
    side: str
    signal_close: float
    sl_distance: float
    target_distance: float
    atr: float
    qty: int
    rationale: Optional[str] = None
    status: str
    created_at: str
    resolved_at: Optional[str] = None
    resolution_note: Optional[str] = None


class DeliveryPendingEntriesResponse(BaseModel):
    pending: List[DeliveryPendingEntryRow]


_PENDING_STATUSES = {
    "PENDING", "FILLED", "SKIPPED_GAP", "SKIPPED_STALE", "SKIPPED_OPEN",
}


def _normalise_pending_status(raw: Optional[str]) -> str:
    if raw is None:
        return "PENDING"
    s = raw.strip().upper()
    if s in _PENDING_STATUSES:
        return s
    raise HTTPException(
        status_code=400,
        detail=(f"status must be one of {sorted(_PENDING_STATUSES)}, "
                f"got {raw!r}"),
    )


def _normalise_status(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = raw.strip().upper()
    if s in ("OPEN", "CLOSED"):
        return s
    raise HTTPException(
        status_code=400,
        detail=f"status must be 'open' or 'closed', got {raw!r}",
    )


@router.get("/positions", response_model=DeliveryPositionsResponse)
def list_positions(
    status: Optional[str] = Query(None, description="open | closed (default: both)"),
    limit: int = Query(500, ge=1, le=2000),
) -> DeliveryPositionsResponse:
    rows = db.list_delivery_positions(status=_normalise_status(status), limit=limit)
    out: List[DeliveryPositionRow] = []
    for r in rows:
        try:
            out.append(DeliveryPositionRow(
                **{k: r.get(k) for k in DeliveryPositionRow.model_fields}))
        except Exception as e:
            logger.warning("skip malformed delivery position row id=%s: %s", r.get("id"), e)
    return DeliveryPositionsResponse(positions=out)


_SIGNALS_TAIL_BYTES = 4 * 1024 * 1024   # scan at most the last 4 MB


def _tail_text(path, max_bytes: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()   # discard the partial first line
        return f.read().decode("utf-8", errors="replace")


@router.get("/signals", response_model=DeliverySignalsResponse)
def list_signals(
    date_: Optional[str] = Query(
        None, alias="date",
        description="ISO date (YYYY-MM-DD). Defaults to today.",
    ),
    limit: int = Query(200, ge=1, le=2000),
) -> DeliverySignalsResponse:
    iso = date_ or date.today().isoformat()
    try:
        date.fromisoformat(iso)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"date must be YYYY-MM-DD, got {iso!r}")

    path = LOG_DIR / f"signals-{iso}.jsonl"
    if not path.exists():
        return DeliverySignalsResponse(date=iso, generated_at=None, signals=[])

    generated_at = datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="milliseconds")

    signals: List[DeliverySignal] = []
    for ln, raw in enumerate(_tail_text(path, _SIGNALS_TAIL_BYTES).splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("signals-%s.jsonl tail:%d malformed (%s)", iso, ln, e)
            continue
        if rec.get("strategy") != STRATEGY_NAME:
            continue
        try:
            signals.append(DeliverySignal(
                timestamp=str(rec.get("timestamp", "")),
                tradingsymbol=str(rec["tradingsymbol"]),
                transaction_type=str(rec["transaction_type"]),
                quantity=int(rec.get("quantity", 0)),
                price=float(rec.get("price", 0.0)),
                rationale=rec.get("rationale"),
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("signals-%s.jsonl tail:%d skip row (%s)", iso, ln, e)
    return DeliverySignalsResponse(
        date=iso, generated_at=generated_at, signals=signals[-limit:],
    )


@router.get("/scans", response_model=DeliveryScansResponse)
def list_scans(
    limit: int = Query(50, ge=1, le=500),
) -> DeliveryScansResponse:
    rows = db.list_delivery_scans(limit=limit)
    out: List[DeliveryScanRow] = []
    for r in rows:
        try:
            out.append(DeliveryScanRow(
                **{k: r.get(k) for k in DeliveryScanRow.model_fields}))
        except Exception as e:
            logger.warning("skip malformed delivery scan row id=%s: %s", r.get("id"), e)
    return DeliveryScansResponse(scans=out)


@router.get("/pending-entries", response_model=DeliveryPendingEntriesResponse)
def list_pending_entries(
    status: Optional[str] = Query(
        None,
        description=("PENDING | FILLED | SKIPPED_GAP | SKIPPED_STALE | "
                     "SKIPPED_OPEN. Defaults to PENDING."),
    ),
    limit: int = Query(500, ge=1, le=2000),
) -> DeliveryPendingEntriesResponse:
    rows = db.list_delivery_pending_entries(
        status=_normalise_pending_status(status), limit=limit,
    )
    out: List[DeliveryPendingEntryRow] = []
    for r in rows:
        try:
            out.append(DeliveryPendingEntryRow(
                **{k: r.get(k) for k in DeliveryPendingEntryRow.model_fields}
            ))
        except Exception as e:
            logger.warning("skip malformed pending row id=%s: %s", r.get("id"), e)
    return DeliveryPendingEntriesResponse(pending=out)
