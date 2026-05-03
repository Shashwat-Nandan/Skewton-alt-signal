"""
Market Profile API.

  GET  /market-profile/symbols
       Returns the universe ingested so far, with bar-range metadata so
       the frontend selector can show coverage at a glance.

  GET  /market-profile/{symbol}?days=N&period_minutes=30&mode=composite|daily
       Returns the computed Market Profile (POC, VAH/VAL, IB, bins).
       In `composite` mode (default) all bars in the window become a
       single profile. In `daily` mode the response includes one profile
       per trading day.

The compute lives in `market_profile.py`; this router is just glue
between the SQLite store and that pure function.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import bars as bars_db
from market_profile import (
    Bar,
    composite_to_dict,
    compute_composite,
    compute_day_profile,
    day_profile_to_dict,
    split_by_day,
)

router = APIRouter(prefix="/market-profile", tags=["market-profile"])
logger = logging.getLogger(__name__)


class UniverseRow(BaseModel):
    symbol: str
    instrument_token: int
    exchange: str
    name: Optional[str] = None
    last_backfilled_at: Optional[str] = None
    last_update_at: Optional[str] = None
    earliest_bar_ts: Optional[str] = None
    latest_bar_ts: Optional[str] = None


@router.get("/symbols", response_model=List[UniverseRow])
def list_symbols():
    return [UniverseRow(**row) for row in bars_db.list_universe()]


@router.get("/{symbol}")
def get_profile(
    symbol: str,
    days: int = Query(252, ge=1, le=720, description="Lookback window in calendar days."),
    period_minutes: int = Query(30, ge=1, le=240),
    tick_size: Optional[float] = Query(None, gt=0,
        description="Bin size. Omit for auto-pick (~50 bins across day range)."),
    value_area_pct: float = Query(0.70, gt=0.05, lt=0.99),
    ib_periods: int = Query(2, ge=1, le=10,
        description="Initial Balance period count (CBOT default = 2 × 30-min)."),
    mode: str = Query("composite", pattern="^(composite|daily)$"),
):
    """
    Compute a market profile for `symbol` over the last `days` calendar
    days. Default `mode=composite` returns one rolled-up profile; `daily`
    returns one profile per trading day plus a composite alongside.
    """
    sym = symbol.upper().strip()
    universe = bars_db.get_universe_row(sym)
    if not universe:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{sym!r} not in bars_universe. "
                f"Run `python fetch_bars.py --add-symbols {sym}` then "
                f"`python fetch_bars.py --backfill --symbols {sym}`."
            ),
        )

    token = int(universe["instrument_token"])
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)
    rows = bars_db.get_bars(
        token, period_minutes,
        from_ts=from_dt.isoformat(),
        to_ts=to_dt.isoformat(),
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No {period_minutes}m bars stored for {sym} in the last {days} days. "
                f"Run `python fetch_bars.py --backfill --symbols {sym}`."
            ),
        )

    bar_list: List[Bar] = []
    for r in rows:
        ts_raw = r["ts"]
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            # Strip timezone suffixes (Kite returns +05:30 sometimes)
            ts = datetime.fromisoformat(ts_raw.split("+")[0])
        bar_list.append(Bar(
            ts=ts,
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            volume=int(r["volume"] or 0),
        ))

    composite = compute_composite(
        bar_list, tick_size=tick_size, value_area_pct=value_area_pct,
    )

    response: Dict[str, Any] = {
        "symbol": sym,
        "name": universe.get("name"),
        "instrument_token": token,
        "exchange": universe["exchange"],
        "period_minutes": period_minutes,
        "lookback_days": days,
        "value_area_pct": value_area_pct,
        "first_bar_ts": rows[0]["ts"],
        "last_bar_ts": rows[-1]["ts"],
        "n_bars": len(bar_list),
        "composite": composite_to_dict(composite) if composite else None,
    }

    if mode == "daily":
        per_day = []
        for day_bars in split_by_day(bar_list):
            p = compute_day_profile(
                day_bars, tick_size=tick_size,
                value_area_pct=value_area_pct, ib_periods=ib_periods,
            )
            if p is not None:
                per_day.append(day_profile_to_dict(p))
        response["daily"] = per_day

    return response
