"""Kalman-trend loop monitoring view for the dashboard.

Surfaces the loop-engineering pilot (PR #60) for the operator:
  * daily positions + Kalman-vs-MA performance, read from the per-session EOD
    sidecars `runners/run_paper_kalman_trend.py` writes
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

class SessionTrade(BaseModel):
    """One closed fill from the latest session (from the EOD sidecar's
    per-book `session_trades`). Prices are index points; pnl is net of costs."""
    side: int                              # +1 long / -1 short
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl_points: float = 0.0
    pnl_rupees: float = 0.0
    reason: str = ""                       # "target" | "stop" | "force_close"


class TrendBook(BaseModel):
    """One book (Kalman OR MA) of the A/B for a single instrument."""
    signal_kind: str
    realized_rupees: float = 0.0
    n_trades: int = 0
    # CURRENT position from the runner's live state file (-1 short / 0 flat / +1
    # long). NOT from the EOD sidecar — that force-closes at 15:25 so its open_pos
    # is always 0; the live state shows real intraday exposure during the session.
    open_pos: int = 0
    # THIS session's fills + net ₹ from the sidecar (the realized_rupees above is
    # cumulative across the run). Absent on sidecars written before this shipped →
    # empty list / 0.0, so old sessions cleanly show aggregate-only.
    session_realized_rupees: float = 0.0
    session_trades: List[SessionTrade] = []


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
    risk: Optional[str] = None             # 'ok' | 'HALT_NEW_ENTRIES[_<strategy>]' | None (unknown)


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


def _live_positions(data_cache: Path) -> dict:
    """Current per-book positions from the runner's LIVE state file. The EOD
    sidecar force-closes at 15:25 (open_pos always 0 there), so real intraday
    exposure comes from kalman_trend_runner_state.json instead. Returns
    {symbol: {"kalman": pos, "ma": pos}}; empty on any read problem."""
    path = data_cache / "kalman_trend_runner_state.json"
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read kalman_trend_runner_state.json: %s", e)
        return {}
    out: dict = {}
    for inst in blob.get("instruments") or []:
        if not isinstance(inst, dict) or inst.get("symbol") is None:
            continue
        sides: dict = {}
        for side in ("kalman", "ma"):
            book = inst.get(side)
            if isinstance(book, dict) and book.get("pos") is not None:
                try:
                    sides[side] = int(book["pos"])
                except (TypeError, ValueError):
                    pass
        out[str(inst["symbol"])] = sides
    return out


def _session_trades(blob: dict) -> List[SessionTrade]:
    """Parse the sidecar's per-book `session_trades`, skipping any malformed row
    rather than dropping the whole book (old sidecars lack the key → [])."""
    out: List[SessionTrade] = []
    for t in blob.get("session_trades") or []:
        if not isinstance(t, dict) or t.get("side") is None:
            continue
        try:
            out.append(SessionTrade(
                side=int(t["side"]),
                entry_price=float(t.get("entry_price") or 0.0),
                exit_price=float(t.get("exit_price") or 0.0),
                pnl_points=float(t.get("pnl_points") or 0.0),
                pnl_rupees=float(t.get("pnl_rupees") or 0.0),
                reason=str(t.get("reason", "")),
            ))
        except (TypeError, ValueError):
            continue
    return out


def _book(blob: Optional[dict], default_kind: str, *, open_pos: int = 0) -> TrendBook:
    blob = blob or {}
    return TrendBook(
        signal_kind=str(blob.get("signal_kind", default_kind)),
        # `… or 0.0` so an explicit JSON null coerces to 0 (shows the row at 0)
        # rather than crashing float(None) and dropping the whole instrument.
        realized_rupees=float(blob.get("realized_rupees") or 0.0),
        n_trades=int(blob.get("n_trades") or 0),
        open_pos=open_pos,
        session_realized_rupees=float(blob.get("session_realized_rupees") or 0.0),
        session_trades=_session_trades(blob),
    )


def _instrument(rep: dict, live: dict) -> TrendInstrument:
    sym = str(rep.get("symbol", "?"))
    pos = live.get(sym, {})
    kal = _book(rep.get("kalman"), "kalman", open_pos=pos.get("kalman", 0))
    ma = _book(rep.get("ma"), "ma", open_pos=pos.get("ma", 0))
    return TrendInstrument(
        symbol=sym, kalman=kal, ma=ma,
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

    report = report or {}                          # guard None / fall-through
    raw_instruments = report.get("instruments")
    if not isinstance(raw_instruments, list):      # present-but-null / wrong type
        raw_instruments = []

    live = _live_positions(DATA_CACHE)             # real intraday positions
    instruments: List[TrendInstrument] = []
    for rep in raw_instruments:
        try:
            instruments.append(_instrument(rep, live))
        except (AttributeError, TypeError, ValueError, KeyError) as e:
            # Skip ONE malformed record without 500ing; a broader except would
            # let a renamed field silently empty the whole table.
            logger.warning("Skipping malformed kalman-trend instrument in %s: %s",
                           latest_date, e)
    instruments.sort(key=lambda i: i.symbol)

    # Recompute totals from the SURVIVING rows so the headline always sums to the
    # table — a dropped malformed row drops from both (the writer's precomputed
    # totals would otherwise still include it). Equal to the sidecar totals when
    # every row parses.
    total_k = round(sum(i.kalman.realized_rupees for i in instruments), 2)
    total_m = round(sum(i.ma.realized_rupees for i in instruments), 2)

    # Loop memory (STATE.md) — read through the same parser the loop writes with.
    # Returned independent of the EOD report so the loop's verdict/lessons show
    # even before the first session sidecar exists.
    state = memory.read_state(STRATEGY, root=STATE_ROOT)

    return KalmanTrendResponse(
        latest_date=latest_date,
        n_sessions_recorded=len(dated),
        total_kalman_rupees=total_k,
        total_ma_rupees=total_m,
        kalman_minus_ma_rupees=round(total_k - total_m, 2),
        instruments=instruments,
        loop=_loop_status(state.last_run),
        lessons=state.lessons[:MAX_LESSONS],
    )
