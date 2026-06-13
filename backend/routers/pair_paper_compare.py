"""Head-to-head comparison of paper-trading systems.

Reads the per-day EOD sidecar JSONs that `run_paper_pairs.py` writes
(``data_cache/pair_paper{,_<system>}_eod_<date>.json``) and exposes
per-day and aggregate P&L by system. The CLI `compare_paper_systems.py`
covers the same data — this router is the dashboard surface for the
parallel-system paper-trading experiment introduced 2026-05-17
(see tasks/todo.md).

This router DOES NOT re-execute trades or re-run any analysis — it only
reads the on-disk JSON sidecars. So a fresh request is cheap and the
endpoint is safe to poll.
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
router = APIRouter(prefix="/pair-paper-compare", tags=["pair-paper-compare"])

DATA_CACHE = REPO_ROOT / "data_cache"

# Match the filename convention in run_paper_pairs.write_eod_sidecar().
def _eod_path(system: str, d: date) -> Path:
    if system == "baseline":
        return DATA_CACHE / f"pair_paper_eod_{d.isoformat()}.json"
    return DATA_CACHE / f"pair_paper_{system}_eod_{d.isoformat()}.json"


def _pair_label(pair_field) -> str:
    if isinstance(pair_field, (list, tuple)) and len(pair_field) == 2:
        return f"{pair_field[0]}/{pair_field[1]}"
    return str(pair_field)


def _total_pnl(report: dict) -> float:
    """realized_pnl is net of costs (pair_trading._apply_fill subtracts them);
    unrealized should be 0 at EOD post-flatten but is kept for the edge case
    where a flatten failed on a quote outage."""
    return float(report.get("realized_pnl", 0.0)) + float(report.get("unrealized_pnl", 0.0))




# ───────────────────────── response shape ─────────────────────────

class DailyRow(BaseModel):
    date: str
    # system -> {"net_pnl": ..., "n_pairs": ..., "n_trades": ..., "costs": ...}
    systems: Dict[str, Optional[dict]]


class AggregateRow(BaseModel):
    system: str
    net_pnl: float
    n_days_with_data: int
    avg_per_day: float
    n_unique_pairs: int
    n_closed_trades: int
    transaction_costs: float


class PerPairRow(BaseModel):
    pair: str
    # system -> sum of net P&L over the window, or null if not traded by that system
    by_system: Dict[str, Optional[float]]
    # 'BOTH' or 'only baseline' / 'only persistent' / etc.
    traded_by: str


class ComparisonResponse(BaseModel):
    start_date: str
    end_date: str
    systems: List[str]
    daily: List[DailyRow]
    aggregate: List[AggregateRow]
    per_pair: List[PerPairRow]


# ───────────────────────── endpoint ─────────────────────────

@router.get("", response_model=ComparisonResponse)
def compare_systems(
    days: int = Query(5, ge=1, le=30, description="Trading days back from --end (default 5)"),
    end: Optional[str] = Query(None, description="End date YYYY-MM-DD (default today)"),
    systems: str = Query("baseline,persistent", description="Comma-separated system names"),
) -> ComparisonResponse:
    if end:
        try:
            end_date = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid `end` date: {end!r}. Expected YYYY-MM-DD.")
    else:
        end_date = date.today()
    sys_list = [s.strip() for s in systems.split(",") if s.strip()]
    if len(sys_list) < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 systems to compare")

    day_list = collect_trading_days(end_date, days)
    if not day_list:
        raise HTTPException(status_code=500, detail="Failed to enumerate trading days")

    daily_rows: List[DailyRow] = []
    # system → aggregate accumulator
    agg: Dict[str, dict] = {s: {"net_pnl": 0.0, "n_days_with_data": 0,
                                 "n_unique_pairs": set(), "n_closed_trades": 0,
                                 "transaction_costs": 0.0} for s in sys_list}
    # pair label → {system: sum_pnl}
    pair_pnl: Dict[str, Dict[str, float]] = {}

    for d in day_list:
        row_systems: Dict[str, Optional[dict]] = {}
        for sys_name in sys_list:
            path = _eod_path(sys_name, d)
            if not path.exists():
                row_systems[sys_name] = None
                continue
            try:
                payload = json.loads(path.read_text())
            except Exception as e:
                logger.warning("Failed to read %s: %s", path, e)
                row_systems[sys_name] = None
                continue

            pairs = payload.get("pairs", [])
            day_total = sum(_total_pnl(r) for r in pairs)
            day_costs = sum(float(r.get("transaction_costs", 0.0)) for r in pairs)
            day_trades = sum(int(r.get("n_closed_trades", 0)) for r in pairs)
            row_systems[sys_name] = {
                "net_pnl": day_total,
                "n_pairs": len(pairs),
                "n_trades": day_trades,
                "costs": day_costs,
            }
            agg[sys_name]["net_pnl"] += day_total
            agg[sys_name]["n_days_with_data"] += 1
            agg[sys_name]["n_closed_trades"] += day_trades
            agg[sys_name]["transaction_costs"] += day_costs
            for r in pairs:
                lbl = _pair_label(r.get("pair"))
                agg[sys_name]["n_unique_pairs"].add(lbl)
                pair_pnl.setdefault(lbl, {}).setdefault(sys_name, 0.0)
                pair_pnl[lbl][sys_name] += _total_pnl(r)

        daily_rows.append(DailyRow(date=d.isoformat(), systems=row_systems))

    aggregate_rows = [
        AggregateRow(
            system=sys_name,
            net_pnl=a["net_pnl"],
            n_days_with_data=a["n_days_with_data"],
            avg_per_day=(a["net_pnl"] / a["n_days_with_data"]
                          if a["n_days_with_data"] else 0.0),
            n_unique_pairs=len(a["n_unique_pairs"]),
            n_closed_trades=a["n_closed_trades"],
            transaction_costs=a["transaction_costs"],
        )
        for sys_name, a in agg.items()
    ]

    per_pair_rows: List[PerPairRow] = []
    for lbl in sorted(pair_pnl.keys(),
                       key=lambda k: -max(pair_pnl[k].values(), default=0.0)):
        present = [s for s in sys_list if s in pair_pnl[lbl]]
        if len(present) == len(sys_list):
            tag = "BOTH"
        else:
            tag = f"only {','.join(present)}"
        per_pair_rows.append(PerPairRow(
            pair=lbl,
            by_system={s: pair_pnl[lbl].get(s) for s in sys_list},
            traded_by=tag,
        ))

    return ComparisonResponse(
        start_date=day_list[0].isoformat(),
        end_date=day_list[-1].isoformat(),
        systems=sys_list,
        daily=daily_rows,
        aggregate=aggregate_rows,
        per_pair=per_pair_rows,
    )
