"""Live position tracker.

Reads the three paper-trading state JSONs that the runners rewrite every
few seconds during market hours, and returns a unified view of:
  - open positions per system (Taleb straddle, pair-baseline, pair-persistent)
  - today's closed trades per system
  - a per-system P&L summary (realized + unrealized + costs)

The endpoint is read-only — it does no order placement, no recomputation,
no Kite calls. Polling cost is one ~6 KB JSON read per system.

Live vs paper: each runner writes its own `mode` ("live"/"paper", from its
`--mode` flag) into the state file, and the badge renders that per-system
value. This is deliberately NOT keyed off the global `allow_live_mode`
setting — that flag is true whenever ANY system is live, so reading it here
would mislabel the still-paper systems (baseline, Taleb) as real-money.
A state file with no `mode` field falls back to "paper" (fail-safe: never
render a paper book as live).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/positions", tags=["positions"])

DATA_CACHE = REPO_ROOT / "data_cache"


# ─────────────────────────── response shape ───────────────────────────

class OpenPosition(BaseModel):
    # `group` lets the UI keep pair legs visually together; a Taleb structure's
    # legs share one group named for the structure, e.g. "NIFTY straddle" or
    # "NIFTY asymmetric strangle".
    group: str
    tradingsymbol: str
    side: str  # "LONG" or "SHORT"
    quantity: int  # absolute lots
    lot_size: int
    entry_price: float
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    entry_time: Optional[str] = None
    # Free-form context: z-score for pairs, strike/expiry for options
    note: Optional[str] = None


class ClosedTrade(BaseModel):
    group: str
    entry_time: Optional[str] = None
    exit_time: Optional[str] = None
    realized_pnl: float
    transaction_costs: Optional[float] = None
    note: Optional[str] = None


class SystemSummary(BaseModel):
    realized_pnl: float
    unrealized_pnl: float
    transaction_costs: float
    total_pnl: float
    n_open_positions: int
    n_closed_today: int


class SystemBlock(BaseModel):
    name: str
    label: str
    mode: str  # "paper" | "live"
    state_file: str
    updated_at: Optional[str] = None
    available: bool
    summary: SystemSummary
    open_positions: List[OpenPosition]
    closed_today: List[ClosedTrade]


class PositionsResponse(BaseModel):
    generated_at: str
    systems: List[SystemBlock]


# ─────────────────────────── helpers ───────────────────────────

def _empty_summary() -> SystemSummary:
    return SystemSummary(
        realized_pnl=0.0, unrealized_pnl=0.0, transaction_costs=0.0,
        total_pnl=0.0, n_open_positions=0, n_closed_today=0,
    )


def _load_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logger.warning("Failed to parse %s: %s", path, e)
        return None


def _is_today(iso: Optional[str], today: date) -> bool:
    """Return True if the ISO timestamp falls on `today` (local-naive parse).

    The runners write naive local-time ISO strings, so we parse naive and
    compare to the server's local date — that matches how the operator
    interprets "today" in the UI.
    """
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso).date() == today
    except ValueError:
        return False


def _state_mode(payload: Optional[dict]) -> str:
    """Per-system live/paper label authored by the runner into the state file
    (`write_state_file`, driven by `--mode`). Falls back to "paper" when the
    field is absent or unrecognised — a missing flag must never render a paper
    book as real-money."""
    mode = (payload or {}).get("mode")
    return mode if mode in ("live", "paper") else "paper"


# ─────────────────────────── per-system builders ───────────────────────────

# Taleb regime dispatch routes to one of several structures (not just a
# straddle). Map the runner's structure value(s) to a display name so the
# dashboard labels each trade by what was actually traded.
_STRUCTURE_LABELS = {
    "straddle": "straddle",
    "risk_reversal_long_put": "risk reversal",
    "calendar_short_front": "calendar",
    "backspread": "backspread",
    "asymmetric_strangle": "asymmetric strangle",
}


def _taleb_group(types) -> str:
    """Group label for a Taleb position/trade from its structure value(s).

    `types` is the runner's active_structure_types (a list) or the
    comma-joined `structure` string recorded on a closed trade. Falls back to
    "NIFTY options" when the structure is unknown (e.g. trades closed before
    the structure was recorded)."""
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    names = [_STRUCTURE_LABELS.get(t, t.replace("_", " ")) for t in (types or [])]
    names = list(dict.fromkeys(names))  # dedupe, preserve order
    return "NIFTY " + " + ".join(names) if names else "NIFTY options"


def _build_taleb_block(today: date) -> SystemBlock:
    path = DATA_CACHE / "taleb_paper_state.json"
    payload = _load_state(path)
    if not payload:
        return SystemBlock(
            name="taleb", label="Taleb hedger", mode=_state_mode(payload),
            state_file=path.name, available=False,
            summary=_empty_summary(), open_positions=[], closed_today=[],
        )

    state = payload.get("state", {}) or {}
    entry_time = state.get("entry_time")
    open_group = _taleb_group(state.get("active_structure_types"))

    open_positions: List[OpenPosition] = []
    for p in state.get("positions", []) or []:
        qty = int(p.get("quantity", 0))
        if qty == 0:
            continue
        entry_px = float(p.get("entry_price", 0.0))
        cur_px = p.get("current_price")
        lot = int(p.get("lot_size", 0))
        unrealized = None
        if cur_px is not None:
            unrealized = (float(cur_px) - entry_px) * qty * lot
        strike = p.get("strike")
        expiry = p.get("expiry")
        opt_type = p.get("option_type")
        note_bits = []
        if strike is not None and opt_type:
            note_bits.append(f"{int(strike)} {opt_type}")
        if expiry:
            note_bits.append(f"exp {expiry}")
        open_positions.append(OpenPosition(
            group=open_group,
            tradingsymbol=str(p.get("tradingsymbol", "")),
            side="LONG" if qty > 0 else "SHORT",
            quantity=abs(qty),
            lot_size=lot,
            entry_price=entry_px,
            current_price=float(cur_px) if cur_px is not None else None,
            unrealized_pnl=unrealized,
            entry_time=entry_time,
            note=" · ".join(note_bits) if note_bits else None,
        ))

    closed_today: List[ClosedTrade] = []
    for t in state.get("closed_trades", []) or []:
        if not _is_today(t.get("exit_time"), today):
            continue
        # The taleb runner deducts costs from realized_pnl as legs close
        # (realized_pnl -= cost), so the per-trade `gross_pnl` field is ALREADY
        # net of costs. Report it directly; `costs` is the positive cost total
        # carried alongside for display (matches the pair block convention and
        # the net state-level realized_pnl in the summary).
        net = float(t.get("gross_pnl", 0.0))
        costs = float(t.get("costs", 0.0))
        closed_today.append(ClosedTrade(
            group=_taleb_group(t.get("structure")),
            entry_time=t.get("entry_time"),
            exit_time=t.get("exit_time"),
            realized_pnl=net,
            transaction_costs=costs,
            note=f"{t.get('n_rehedges', 0)} rehedges · "
                 f"{t.get('holding_minutes', 0)} min held",
        ))

    realized = float(state.get("realized_pnl", 0.0))
    unrealized = float(state.get("unrealized_pnl", 0.0))
    costs = float(state.get("total_transaction_costs", 0.0))
    return SystemBlock(
        name="taleb", label="Taleb hedger", mode=_state_mode(payload),
        state_file=path.name, updated_at=payload.get("saved_at"), available=True,
        summary=SystemSummary(
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            transaction_costs=costs,
            total_pnl=realized + unrealized,
            n_open_positions=len(open_positions),
            n_closed_today=len(closed_today),
        ),
        open_positions=open_positions,
        closed_today=closed_today,
    )


def _build_pair_block(name: str, label: str, filename: str, today: date) -> SystemBlock:
    path = DATA_CACHE / filename
    payload = _load_state(path)
    if not payload:
        return SystemBlock(
            name=name, label=label, mode=_state_mode(payload),
            state_file=path.name, available=False,
            summary=_empty_summary(), open_positions=[], closed_today=[],
        )

    open_positions: List[OpenPosition] = []
    closed_today: List[ClosedTrade] = []
    realized_total = 0.0
    unrealized_total = 0.0
    costs_total = 0.0

    for pair in payload.get("pairs", []) or []:
        pair_field = pair.get("pair", [])
        pair_label = "/".join(pair_field) if isinstance(pair_field, (list, tuple)) else str(pair_field)
        st = pair.get("state", {}) or {}

        realized_total += float(st.get("realized_pnl", 0.0))
        unrealized_total += float(st.get("unrealized_pnl", 0.0))
        costs_total += float(st.get("total_transaction_costs", 0.0))

        if st.get("position") and st.get("position") != "FLAT":
            entry_time = st.get("entry_time")
            entry_z = st.get("entry_z")
            spread_note = (
                f"{st.get('position')} · entry z={entry_z:+.2f}"
                if isinstance(entry_z, (int, float))
                else str(st.get("position", ""))
            )
            for leg in st.get("legs", []) or []:
                qty = int(leg.get("quantity", 0))
                if qty == 0:
                    continue
                entry_px = float(leg.get("entry_price", 0.0))
                cur_px = leg.get("current_price")
                lot = int(leg.get("lot_size", 0))
                unrealized = None
                if cur_px is not None:
                    unrealized = (float(cur_px) - entry_px) * qty * lot
                open_positions.append(OpenPosition(
                    group=pair_label,
                    tradingsymbol=str(leg.get("tradingsymbol", leg.get("symbol", ""))),
                    side="LONG" if qty > 0 else "SHORT",
                    quantity=abs(qty),
                    lot_size=lot,
                    entry_price=entry_px,
                    current_price=float(cur_px) if cur_px is not None else None,
                    unrealized_pnl=unrealized,
                    entry_time=entry_time,
                    note=spread_note,
                ))

        for t in st.get("closed_trades", []) or []:
            if not _is_today(t.get("exit_time"), today):
                continue
            closed_today.append(ClosedTrade(
                group=pair_label,
                entry_time=t.get("entry_time"),
                exit_time=t.get("exit_time"),
                realized_pnl=float(t.get("realized_pnl", 0.0)),
                transaction_costs=float(t.get("transaction_costs", 0.0)),
                note=str(t.get("position", "")) or None,
            ))

    return SystemBlock(
        name=name, label=label, mode=_state_mode(payload),
        state_file=path.name, updated_at=payload.get("updated_at"), available=True,
        summary=SystemSummary(
            realized_pnl=realized_total,
            unrealized_pnl=unrealized_total,
            transaction_costs=costs_total,
            total_pnl=realized_total + unrealized_total,
            n_open_positions=len(open_positions),
            n_closed_today=len(closed_today),
        ),
        open_positions=open_positions,
        closed_today=closed_today,
    )


# ─────────────────────────── endpoint ───────────────────────────

@router.get("", response_model=PositionsResponse)
def list_positions() -> PositionsResponse:
    today = date.today()
    systems = [
        _build_taleb_block(today),
        _build_pair_block("pair_baseline", "Pair trading — baseline",
                          "pair_paper_state_baseline.json", today),
        _build_pair_block("pair_persistent", "Pair trading — persistent",
                          "pair_paper_state_persistent.json", today),
    ]
    return PositionsResponse(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        systems=systems,
    )
