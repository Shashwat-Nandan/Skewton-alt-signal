"""
Equity-swing API.

  GET  /api/equity/positions?status=open|closed
       Returns rows from the ``equity_positions`` table written by
       ``run_equity_swing.py``. Default returns both, with OPEN first.

  GET  /api/equity/signals?date=YYYY-MM-DD
       Tails ``logs/signals-<date>.jsonl`` and returns the subset emitted
       by ``varsity_equity_swing``. Today's file is the default. Survives
       missing-file (returns an empty list with ``generated_at=null``).

  GET  /api/equity/scans?limit=N
       Recent scan invocations (n_signals, n_trades, mode). Used by the
       page header to show last-scan timing & counts.

  GET  /api/equity/fii-dii?days=N
       Per-day FII/DII net flow + rolling 5-day sums, as read by the
       strategy overlay. 503s when the cache directory is empty (no
       fetches have run yet) so the frontend can render a "no data"
       state distinct from "no flows".

  GET  /api/equity/pending-entries?status=PENDING|FILLED|SKIPPED_GAP|SKIPPED_STALE|SKIPPED_OPEN
       (EQ-FU-1) Reads the ``equity_pending_entries`` queue. After
       close-scan a signal lives here until tomorrow's 18:30 fill;
       /positions is empty until the fill lands so without this the
       interim state is invisible to the operator.

All five are read-only — the strategy itself writes via the cron path.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/equity", tags=["equity-swing"])

LOG_DIR = REPO_ROOT / "logs"
FII_CACHE_DIR = REPO_ROOT / "data_cache" / "fii_dii"


class EquityPositionRow(BaseModel):
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


class EquityPositionsResponse(BaseModel):
    positions: List[EquityPositionRow]


class EquitySignal(BaseModel):
    timestamp: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    price: float
    rationale: Optional[str] = None


class EquitySignalsResponse(BaseModel):
    date: str
    generated_at: Optional[str] = None
    signals: List[EquitySignal]


class EquityScanRow(BaseModel):
    id: int
    scan_dt: str
    scan_kind: str
    mode: str
    n_signals: int
    n_trades: int
    n_open_positions: int
    n_closed_today: int
    notes: Optional[str] = None


class EquityScansResponse(BaseModel):
    scans: List[EquityScanRow]


class FiiDiiRow(BaseModel):
    date: str
    fii_net: Optional[float] = None
    dii_net: Optional[float] = None
    fii_net_5d: Optional[float] = None
    dii_net_5d: Optional[float] = None
    fii_boost: Optional[int] = None


class FiiDiiResponse(BaseModel):
    generated_at: Optional[str] = None
    rows: List[FiiDiiRow]


class EquityPendingEntryRow(BaseModel):
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


class EquityPendingEntriesResponse(BaseModel):
    pending: List[EquityPendingEntryRow]


_PENDING_STATUSES = {
    "PENDING", "FILLED", "SKIPPED_GAP", "SKIPPED_STALE", "SKIPPED_OPEN",
}


def _normalise_pending_status(raw: Optional[str]) -> str:
    # EQ-FU-1: default PENDING — that's the operator's "what's queued for
    # tomorrow's open" view. Resolved statuses are still queryable for
    # post-mortem ("why did MARICO not fill on 2026-05-26?").
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


@router.get("/positions", response_model=EquityPositionsResponse)
def list_positions(
    status: Optional[str] = Query(None, description="open | closed (default: both)"),
    limit: int = Query(500, ge=1, le=2000),
) -> EquityPositionsResponse:
    rows = db.list_equity_positions(status=_normalise_status(status), limit=limit)
    out: List[EquityPositionRow] = []
    for r in rows:
        try:
            out.append(EquityPositionRow(**{k: r.get(k) for k in EquityPositionRow.model_fields}))
        except Exception as e:
            logger.warning("skip malformed equity position row id=%s: %s", r.get("id"), e)
    return EquityPositionsResponse(positions=out)


@router.get("/signals", response_model=EquitySignalsResponse)
def list_signals(
    date_: Optional[str] = Query(
        None, alias="date",
        description="ISO date (YYYY-MM-DD). Defaults to today.",
    ),
) -> EquitySignalsResponse:
    iso = date_ or date.today().isoformat()
    try:
        date.fromisoformat(iso)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"date must be YYYY-MM-DD, got {iso!r}")

    path = LOG_DIR / f"signals-{iso}.jsonl"
    if not path.exists():
        return EquitySignalsResponse(date=iso, generated_at=None, signals=[])

    generated_at = datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    ).isoformat(timespec="milliseconds")

    signals: List[EquitySignal] = []
    with path.open() as f:
        for ln, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("signals-%s.jsonl:%d malformed (%s)", iso, ln, e)
                continue
            if rec.get("strategy") != "varsity_equity_swing":
                continue
            try:
                signals.append(EquitySignal(
                    timestamp=str(rec.get("timestamp", "")),
                    tradingsymbol=str(rec["tradingsymbol"]),
                    transaction_type=str(rec["transaction_type"]),
                    quantity=int(rec.get("quantity", 0)),
                    price=float(rec.get("price", 0.0)),
                    rationale=rec.get("rationale"),
                ))
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("signals-%s.jsonl:%d skip row (%s)", iso, ln, e)
    return EquitySignalsResponse(date=iso, generated_at=generated_at, signals=signals)


@router.get("/scans", response_model=EquityScansResponse)
def list_scans(
    limit: int = Query(50, ge=1, le=500),
) -> EquityScansResponse:
    rows = db.list_equity_scans(limit=limit)
    out: List[EquityScanRow] = []
    for r in rows:
        try:
            out.append(EquityScanRow(**{k: r.get(k) for k in EquityScanRow.model_fields}))
        except Exception as e:
            logger.warning("skip malformed equity scan row id=%s: %s", r.get("id"), e)
    return EquityScansResponse(scans=out)


@router.get("/pending-entries", response_model=EquityPendingEntriesResponse)
def list_pending_entries(
    status: Optional[str] = Query(
        None,
        description=(
            "PENDING | FILLED | SKIPPED_GAP | SKIPPED_STALE | SKIPPED_OPEN. "
            "Defaults to PENDING."
        ),
    ),
    limit: int = Query(500, ge=1, le=2000),
) -> EquityPendingEntriesResponse:
    """EQ-FU-1: surface today's close-scan signals queued for tomorrow's
    18:30 fill. Without this, /equity/positions is empty for the
    overnight window and the dashboard has no signal of pending work."""
    rows = db.list_equity_pending_entries(
        status=_normalise_pending_status(status), limit=limit,
    )
    out: List[EquityPendingEntryRow] = []
    for r in rows:
        try:
            out.append(EquityPendingEntryRow(
                **{k: r.get(k) for k in EquityPendingEntryRow.model_fields}
            ))
        except Exception as e:
            logger.warning("skip malformed pending row id=%s: %s",
                           r.get("id"), e)
    return EquityPendingEntriesResponse(pending=out)


@router.get("/fii-dii", response_model=FiiDiiResponse)
def get_fii_dii(
    days: int = Query(60, ge=5, le=720),
) -> FiiDiiResponse:
    """
    503s when the cache dir doesn't exist or is empty (fetchers haven't
    run on this host yet). That's distinguishable from an empty body
    (cache exists, every day in window was a flat zero).
    """
    if not FII_CACHE_DIR.exists() or not any(FII_CACHE_DIR.glob("*.json")):
        raise HTTPException(
            status_code=503,
            detail=(
                "FII/DII cache empty. Run `python fetch_fii_dii.py` on the host. "
                "Cache dir: ./data_cache/fii_dii/"
            ),
        )

    # Lazy import — keeps backend startup light when pandas isn't ready
    from strategies._fii_dii import build_fii_signal, load_fii_dii_panel

    panel = load_fii_dii_panel(FII_CACHE_DIR)
    if panel.empty:
        return FiiDiiResponse(generated_at=None, rows=[])

    # Pivot to per-date so the response carries both legs and the rolling sums.
    panel = panel.copy()
    panel["cat_norm"] = panel["category"].str.upper()
    is_fii = panel["cat_norm"].str.contains("FII") | panel["cat_norm"].str.contains("FPI")
    is_dii = panel["cat_norm"].str.contains("DII")
    fii_per_day = panel[is_fii].groupby("date")["net"].sum().rename("fii_net")
    dii_per_day = panel[is_dii].groupby("date")["net"].sum().rename("dii_net")

    signal = build_fii_signal(panel)
    signal = signal.set_index("date") if not signal.empty else signal

    import pandas as pd
    merged = pd.concat([fii_per_day, dii_per_day, signal], axis=1).sort_index()
    merged = merged.tail(days)

    # `generated_at` reflects the freshest cache file we used.
    latest = max((p.stat().st_mtime for p in FII_CACHE_DIR.glob("*.json")), default=None)
    generated_at = (
        datetime.fromtimestamp(latest, tz=timezone.utc).isoformat(timespec="milliseconds")
        if latest else None
    )

    def _opt_float(v) -> Optional[float]:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f != f:  # NaN
            return None
        return f

    def _opt_int(v) -> Optional[int]:
        f = _opt_float(v)
        return int(f) if f is not None else None

    rows: List[FiiDiiRow] = []
    for d, row in merged.iterrows():
        try:
            iso = d.date().isoformat()
        except AttributeError:
            iso = str(d)
        rows.append(FiiDiiRow(
            date=iso,
            fii_net=_opt_float(row.get("fii_net")),
            dii_net=_opt_float(row.get("dii_net")),
            fii_net_5d=_opt_float(row.get("fii_net_5d")),
            dii_net_5d=_opt_float(row.get("dii_net_5d")),
            fii_boost=_opt_int(row.get("fii_boost")),
        ))
    return FiiDiiResponse(generated_at=generated_at, rows=rows)
