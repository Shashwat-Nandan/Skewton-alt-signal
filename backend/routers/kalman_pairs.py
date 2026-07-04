"""Kalman pair-trading monitoring view for the dashboard.

Reads the per-day EOD sidecars that `run_paper_kalman_pairs.py` writes
(``data_cache/pair_paper_kalman_eod_<date>.json``) and exposes the LATEST
session's per-pair detail: which pairs the Kalman runner is monitoring, each
pair's tracked hedge ratio γ_t and intercept μ_t, its current z-score and entry
band, open position, the structure risk band, and the session P&L.

Read-only — it only reads the on-disk JSON sidecars (no re-execution), so it is
cheap and safe to poll. The sidecar is written once per session at 15:25 IST, so
this reflects the most recent completed (or in-progress, on the day's file)
session; before the runner's first session it returns an empty snapshot.
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

from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
# Must match the filename run_paper_kalman_pairs.write_eod_sidecar() writes:
# pair_paper_kalman_eod_<YYYY-MM-DD>.json. (No shared constant — same
# duplicate-the-convention pattern as the other EOD routers; keep in lockstep
# with the writer if either side is renamed.)
_EOD_RE = re.compile(r"^pair_paper_kalman_eod_(\d{4}-\d{2}-\d{2})\.json$")
router = APIRouter(prefix="/kalman-pairs", tags=["kalman-pairs"])

DATA_CACHE = REPO_ROOT / "data_cache"


def _pair_label(pair_field) -> str:
    if isinstance(pair_field, (list, tuple)) and len(pair_field) == 2:
        return f"{pair_field[0]}/{pair_field[1]}"
    return str(pair_field)


def _session_pnl(rep: dict) -> float:
    return (float(rep.get("session_realized_delta", 0.0))
            + float(rep.get("session_unrealized_delta", 0.0)))


def _opt_bool(v) -> Optional[bool]:
    """Regime flags are DISPLAY-ONLY. pydantic v2's Optional[bool] rejects a
    non-bool with a ValidationError (a ValueError subclass) that the endpoint's
    per-record skip-handler would catch — dropping the whole pair's row (γ,
    position, P&L and all) over a cosmetic field. Coerce anything non-bool to
    None so a malformed regime value degrades to '—', never a vanished pair."""
    return v if isinstance(v, bool) else None


def _opt_float(v) -> Optional[float]:
    """Same rationale as _opt_bool for the ADF p-value (excluding bool, which is
    an int subclass that would otherwise coerce to 0.0/1.0)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ───────────────────────── response shape ─────────────────────────

class KalmanPair(BaseModel):
    pair: str
    model: Optional[str] = None
    # γ_t / μ_t the filter currently tracks (the live predicted state).
    gamma: Optional[float] = None
    mu: Optional[float] = None
    position: str
    # Rolling z-score of the Kalman spread, and the z the position was opened at.
    current_z: Optional[float] = None
    entry_z: Optional[float] = None
    # ADF regime gate visibility (issue #67): the p-value of the raw-residual
    # window, whether the gate currently permits new entries, and whether the
    # window is stale (predates a data gap → gate fail-closed; issue #65).
    regime_adf_p: Optional[float] = None
    regime_gate_open: Optional[bool] = None
    regime_stale: Optional[bool] = None
    # Structure risk band (₹), present only while a position is open.
    stop_inr: Optional[float] = None
    target_inr: Optional[float] = None
    spread_std: Optional[float] = None
    day_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    n_closed_trades: int = 0
    spread_history_size: Optional[int] = None


class KalmanPairsResponse(BaseModel):
    latest_date: Optional[str]
    # Total Kalman EOD sidecars on disk (≤ end) — how many sessions recorded.
    n_sessions_recorded: int
    n_pairs: int
    # P&L of the LATEST session only (not cumulative across n_sessions_recorded).
    session_pnl: float
    pairs: List[KalmanPair]


def _build_pair(rep: dict) -> KalmanPair:
    band = rep.get("risk_band") or {}
    position = rep.get("position", "FLAT")
    open_pos = position != "FLAT"
    # γ falls back to the static hedge_ratio only when gamma_filter is truly
    # absent/null (dict.get's default doesn't trigger on a present None).
    gamma = rep.get("gamma_filter")
    if gamma is None:
        gamma = rep.get("hedge_ratio")
    return KalmanPair(
        pair=_pair_label(rep.get("pair")),
        model=rep.get("model"),
        gamma=gamma,
        mu=rep.get("mu_filter"),
        position=position,
        current_z=rep.get("current_z"),
        entry_z=rep.get("entry_z"),
        regime_adf_p=_opt_float(rep.get("regime_adf_p")),
        regime_gate_open=_opt_bool(rep.get("regime_gate_open")),
        regime_stale=_opt_bool(rep.get("regime_stale")),
        # Risk band is meaningful only for an OPEN position; the strategy's
        # _last_risk_band can linger after a close, so gate on position here too.
        stop_inr=band.get("stop_inr") if open_pos else None,
        target_inr=band.get("target_inr") if open_pos else None,
        spread_std=band.get("spread_std") if open_pos else None,
        day_pnl=_session_pnl(rep),
        realized_pnl=float(rep.get("realized_pnl", 0.0)),
        unrealized_pnl=float(rep.get("unrealized_pnl", 0.0)),
        n_closed_trades=int(rep.get("n_closed_trades", 0)),
        spread_history_size=rep.get("spread_history_size"),
    )


# ───────────────────────── endpoint ─────────────────────────

@router.get("", response_model=KalmanPairsResponse)
def kalman_pairs(
    end: Optional[str] = Query(None, description="As-of date YYYY-MM-DD (default today)"),
) -> KalmanPairsResponse:
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=f"Invalid `end` date: {end!r}. Expected YYYY-MM-DD.")
    else:
        end_date = date.today()

    # Find the newest sidecar on/▸before end by globbing filenames (not a fixed
    # trading-day window) — robust to long runner outages: the last known
    # session still shows. Read newest-first, skipping unreadable files.
    dated: list[tuple[str, Path]] = []
    if DATA_CACHE.exists():
        for p in DATA_CACHE.glob("pair_paper_kalman_eod_*.json"):
            m = _EOD_RE.match(p.name)
            if m and m.group(1) <= end_date.isoformat():
                dated.append((m.group(1), p))
    # Sort by the date string only — never fall through to comparing Path
    # objects on a date tie (ISO dates sort lexicographically = chronologically).
    dated.sort(key=lambda t: t[0], reverse=True)  # newest date first

    latest_report: Optional[dict] = None
    latest_date: Optional[str] = None
    for d_str, path in dated:
        try:
            latest_report = json.loads(path.read_text())
            latest_date = d_str
            break
        except Exception as e:
            logger.warning("Failed to read %s: %s", path, e)

    pairs: List[KalmanPair] = []
    if latest_report:
        for rep in latest_report.get("pairs", []):
            try:
                pairs.append(_build_pair(rep))
            except (AttributeError, TypeError, ValueError, KeyError) as e:
                # Skip ONE malformed record (e.g. a non-dict entry) without
                # 500ing — but only data-shape errors. A broader `except` would
                # let a systematic bug (renamed field, logic error) silently
                # drop every pair and render an empty-but-healthy table.
                logger.warning("Skipping malformed kalman pair record in %s: %s",
                               latest_date, e)
    # Sort: open positions first, then by |current_z| desc (closest-to-signal on top).
    pairs.sort(key=lambda p: (p.position == "FLAT", -abs(p.current_z or 0.0)))

    return KalmanPairsResponse(
        latest_date=latest_date,
        n_sessions_recorded=len(dated),
        n_pairs=len(pairs),
        session_pnl=sum(p.day_pnl for p in pairs),
        pairs=pairs,
    )
