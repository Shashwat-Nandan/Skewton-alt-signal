"""Strategy listing + per-strategy parameter schema."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from strategies import STRATEGIES

router = APIRouter(prefix="/strategies", tags=["strategies"])


# Hand-curated parameter schema per strategy. Auto-generation from the class
# is brittle (different __init__ shapes); a small dict is more accurate and
# the dashboard treats it as a form spec.
PARAM_SCHEMAS: Dict[str, List[Dict[str, Any]]] = {
    "taleb_karpathy": [
        # Strategy-specific knobs; risk rails are enforced by config.ini and
        # are not exposed here on purpose.
        {"name": "rehedge_delta_threshold", "type": "float", "default": 0.15,
         "description": "Net delta (lots) above which the futures hedge is rebalanced"},
        {"name": "gamma_scalp_band_pct", "type": "float", "default": 1.5,
         "description": "Spot move (% of strike) inside the gamma-scalp band"},
        {"name": "vega_limit", "type": "float", "default": 4000,
         "description": "Per-lot vega cap (₹/vol-pt) above which positions exit"},
        {"name": "min_rv_iv_ratio", "type": "float", "default": 1.0,
         "description": "Realized/Implied vol ratio required to enter"},
    ],
    "pair_trading": [
        {"name": "symbol_a", "type": "str", "default": None,
         "description": "Long-leg symbol. Leave blank to auto-pick from screener."},
        {"name": "symbol_b", "type": "str", "default": None,
         "description": "Short-leg symbol. Leave blank to auto-pick from screener."},
        {"name": "hedge_ratio", "type": "float", "default": None,
         "description": "OLS beta. Leave blank to auto-pick from screener."},
        {"name": "entry_z", "type": "float", "default": 2.0,
         "description": "|z| threshold to open a position"},
        {"name": "exit_z", "type": "float", "default": 0.5,
         "description": "|z| threshold for mean-revert exit"},
        {"name": "stop_z", "type": "float", "default": 4.0,
         "description": "|z| threshold for stop-loss exit"},
        {"name": "max_leg_notional", "type": "float", "default": None,
         "description": "Cap per-leg notional in ₹. Leave blank for no cap. "
                        "High-β pairs deploy huge amounts otherwise."},
    ],
    "arbitrage": [
        {"name": "risk_free_rate", "type": "float", "default": 0.07,
         "description": "Annualized risk-free rate used in cost-of-carry F* = S·exp((r-q)·T)"},
        {"name": "dividend_yield_default", "type": "float", "default": 0.0,
         "description": "Annualized dividend yield assumed for every name. "
                        "Bump up for high-yield stocks to avoid fake cash-rich signals."},
        {"name": "basis_entry_annual", "type": "float", "default": 0.015,
         "description": "|annualized cash-futures basis| above which to emit a basis signal "
                        "(signals-only — cash leg is never lifted to a live order)"},
        {"name": "calendar_entry_annual", "type": "float", "default": 0.020,
         "description": "|implied carry − fair carry| (annualized) to open a calendar spread"},
        {"name": "calendar_exit_annual", "type": "float", "default": 0.005,
         "description": "|implied carry − fair carry| at which an open calendar mean-reverts out"},
        {"name": "calendar_max_holding_days", "type": "int", "default": 15,
         "description": "Days after which a calendar spread is force-exited"},
        {"name": "calendar_max_leg_basis", "type": "float", "default": 0.10,
         "description": "Cleanliness gate: skip calendars where EITHER leg's "
                        "annualized cash-futures basis exceeds this. "
                        "Filters out dividend-pinned names."},
        {"name": "lots_per_leg", "type": "int", "default": 1,
         "description": "Lots traded per calendar leg"},
        {"name": "max_open_calendars", "type": "int", "default": 5,
         "description": "Cap on simultaneous open calendar trades"},
        {"name": "max_leg_notional", "type": "float", "default": None,
         "description": "Per-leg notional cap (₹). Skips entries where 1 lot busts the cap."},
        {"name": "disable_calendar", "type": "bool", "default": False,
         "description": "Pure basis-monitoring mode: emits dislocation signals "
                        "but never proposes calendar entries. Recommended for retail."},
    ],
}


class StrategyInfo(BaseModel):
    name: str
    description: str
    params: List[Dict[str, Any]]


@router.get("", response_model=List[StrategyInfo])
def list_strategies():
    out: List[StrategyInfo] = []
    for name, cls in STRATEGIES.items():
        doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        out.append(StrategyInfo(
            name=name,
            description=doc,
            params=PARAM_SCHEMAS.get(name, []),
        ))
    return out


@router.get("/{name}/params", response_model=List[Dict[str, Any]])
def get_params(name: str):
    if name not in STRATEGIES:
        raise HTTPException(status_code=404, detail=f"Unknown strategy {name!r}")
    return PARAM_SCHEMAS.get(name, [])
