"""Kalman-trend loop monitoring view for the dashboard.

Surfaces the loop-engineering pilot (PR #60) for the operator:
  * daily positions + Kalman-vs-MA performance, read from the per-session EOD
    sidecars `run_paper_kalman_trend.py` writes
    (``data_cache/kalman_trend_eod_<date>.json``), and
  * the loop-specific bits the other strategy tabs don't have — the independent
    checker verdict, the kill-switch (risk monitor) status, and the compounding
    lessons feed — read from the loop's memory file ``state/kalman_trend/STATE.md``
    via loop_engine.memory (the same parser the loop writes with).

Read-only: it only reads on-disk artifacts (no re-execution), so it is cheap and
safe to poll. The EOD sidecar is written once per session at 15:25 IST; before
the runner's first session this returns an empty-but-healthy snapshot.

NOTE the strategy is NO-GO vs MA (a forward-parity testbed, not an edge claim);
this tab makes the loop's honest verdict visible, it does not assert alpha.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from loop_engine import memory
from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
# Must match run_paper_kalman_trend.write_eod()'s filename (no shared constant —
# same duplicate-the-convention pattern as the other EOD routers; keep in lockstep
# with the writer if either side is renamed).
_EOD_RE = re.compile(r"^kalman_trend_eod_(\d{4}-\d{2}-\d{2})\.json$")
router = APIRouter(prefix="/kalman-trend", tags=["kalman-trend"])

DATA_CACHE = REPO_ROOT / "data_cache"
STATE_ROOT = REPO_ROOT / "state"          # loop_engine memory root (state/<strategy>/)
STRATEGY = "kalman_trend"
MAX_LESSONS = 8                            # newest-first slice surfaced to the UI


# ───────────────────────── response shape ─────────────────────────

class TrendBook(BaseModel):
    """One book (Kalman OR MA) of the A/B for a single instrument."""
    signal_kind: str
    realized_rupees: float = 0.0
    n_trades: int = 0
    win_rate: Optional[float] = None
    open_pos: int = 0                      # -1 short / 0 flat / +1 long at session end
    n_bars: int = 0


class TrendInstrument(BaseModel):
    symbol: str
    kalman: TrendBook
    ma: TrendBook
    # Kalman − MA realized ₹ for THIS instrument (the A/B's whole point).
    edge_rupees: float = 0.0


class LoopStatus(BaseModel):
    """The loop's last-run header, read from STATE.md (values are strings as
    stored — the loop renders a human-readable markdown header)."""
    timestamp: Optional[str] = None
    status: Optional[str] = None
    checker: Optional[str] = None          # 'pass' | 'REJECT: …' | 'skipped:…' | 'deferred…'
    risk: Optional[str] = None             # 'ok' | 'HALT_NEW_ENTRIES'
    kalman_minus_ma_rupees: Optional[str] = None


class KalmanTrendResponse(BaseModel):
    latest_date: Optional[str]
    # Total EOD sidecars on disk (≤ end) — how many sessions recorded.
    n_sessions_recorded: int
    total_kalman_rupees: float
    total_ma_rupees: float
    # Kalman − MA realized ₹ for the LATEST session (not cumulative).
    kalman_minus_ma_rupees: float
    instruments: List[TrendInstrument]
    # Loop-engineering memory (None when STATE.md has no run yet).
    loop: Optional[LoopStatus]
    lessons: List[str]                     # newest-first, capped at MAX_LESSONS


def _book(blob: Optional[dict], default_kind: str) -> TrendBook:
    blob = blob or {}
    return TrendBook(
        signal_kind=str(blob.get("signal_kind", default_kind)),
        realized_rupees=float(blob.get("realized_rupees", 0.0)),
        n_trades=int(blob.get("n_trades", 0)),
        win_rate=blob.get("win_rate"),
        open_pos=int(blob.get("open_pos", 0)),
        n_bars=int(blob.get("n_bars", 0)),
    )


def _instrument(rep: dict) -> TrendInstrument:
    kal, ma = _book(rep.get("kalman"), "kalman"), _book(rep.get("ma"), "ma")
    return TrendInstrument(
        symbol=str(rep.get("symbol", "?")),
        kalman=kal, ma=ma,
        edge_rupees=round(kal.realized_rupees - ma.realized_rupees, 2),
    )


def _loop_status(last_run: dict) -> Optional[LoopStatus]:
    if not last_run:
        return None
    return LoopStatus(
        timestamp=last_run.get("timestamp"),
        status=last_run.get("status"),
        checker=last_run.get("checker"),
        risk=last_run.get("risk"),
        kalman_minus_ma_rupees=last_run.get("kalman_minus_ma_rupees"),
    )


# ───────────────────────── endpoint ─────────────────────────

@router.get("", response_model=KalmanTrendResponse)
def kalman_trend(
    end: Optional[str] = Query(None, description="As-of date YYYY-MM-DD (default today)"),
) -> KalmanTrendResponse:
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"Invalid `end` date: {end!r}. Expected YYYY-MM-DD.")
    else:
        end_date = date.today()

    # Newest sidecar on/▸before end, by globbing filenames (robust to long runner
    # outages — the last known session still shows). Read newest-first.
    dated: list[tuple[str, Path]] = []
    if DATA_CACHE.exists():
        for p in DATA_CACHE.glob("kalman_trend_eod_*.json"):
            m = _EOD_RE.match(p.name)
            if m and m.group(1) <= end_date.isoformat():
                dated.append((m.group(1), p))
    dated.sort(key=lambda t: t[0], reverse=True)   # ISO dates sort chronologically

    report: Optional[dict] = None
    latest_date: Optional[str] = None
    for d_str, path in dated:
        try:
            report = json.loads(path.read_text())
            latest_date = d_str
            break
        except Exception as e:
            logger.warning("Failed to read %s: %s", path, e)

    instruments: List[TrendInstrument] = []
    if report:
        for rep in report.get("instruments", []):
            try:
                instruments.append(_instrument(rep))
            except (AttributeError, TypeError, ValueError, KeyError) as e:
                # Skip ONE malformed record without 500ing; a broader except would
                # let a renamed field silently empty the whole table.
                logger.warning("Skipping malformed kalman-trend instrument in %s: %s",
                               latest_date, e)
    instruments.sort(key=lambda i: i.symbol)

    # Loop memory (STATE.md) — read through the same parser the loop writes with.
    state = memory.read_state(STRATEGY, root=STATE_ROOT)

    return KalmanTrendResponse(
        latest_date=latest_date,
        n_sessions_recorded=len(dated),
        total_kalman_rupees=float(report.get("total_kalman_rupees", 0.0)) if report else 0.0,
        total_ma_rupees=float(report.get("total_ma_rupees", 0.0)) if report else 0.0,
        kalman_minus_ma_rupees=float(report.get("kalman_minus_ma_rupees", 0.0)) if report else 0.0,
        instruments=instruments,
        loop=_loop_status(state.last_run),
        lessons=state.lessons[:MAX_LESSONS],
    )
