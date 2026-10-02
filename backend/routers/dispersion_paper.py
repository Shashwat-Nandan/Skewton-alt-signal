"""Dispersion paper books for the dashboard.

Reads what ``runners/run_paper_dispersion.py`` persists — one state JSON per
book (open book + every closed cycle) — and, for context before the paper
record is long enough to mean anything, the 2-year replay CSVs written by
``research.backtest_dispersion``.

Read-only: nothing here quotes, sizes or trades. The books are monthly, so
the unit is the expiry cycle, not the day. An open book's option legs are
carried at entry price; only the futures hedge is marked, so the open
book's running figure is hedge P&L minus costs, never an option MTM.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter
from pydantic import BaseModel

from ..settings import REPO_ROOT

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dispersion-paper", tags=["dispersion-paper"])

DATA_CACHE = REPO_ROOT / "data_cache"

# Match run_paper_dispersion.STATE_FILE / SHORT_VOL_STATE_FILE and the
# replay CSVs written with --sizing matched / raw. The replay row shown is
# each book's running config: Book A, hold to expiry, futures hedge.
BOOKS = (
    {
        "name": "dispersion_paper",
        "label": "Dispersion (matched)",
        "sizing": "matched",
        "state": "dispersion_paper_state.json",
        "replay": "dispersion_cycles_matched.csv",
    },
    {
        "name": "dispersion_short_vol_paper",
        "label": "Short vol (PR 9 sizing)",
        "sizing": "raw",
        "state": "dispersion_short_vol_paper_state.json",
        "replay": "dispersion_cycles_short_vol.csv",
    },
)


class Leg(BaseModel):
    symbol: str
    side: int
    strike: float
    lots: int
    lot_size: int
    premium: float          # ce + pe at entry, per unit
    iv: float
    hedge_lots: int


class OpenBook(BaseModel):
    expiry: str
    entry: str
    index_lots: int
    n_names: int
    covered_weight: float
    notional_ratio: Optional[float]
    weighting: str
    costs: float
    futures_pnl: float
    last_hedge_session: Optional[str]
    legs: List[Leg]


class Cycle(BaseModel):
    expiry: str
    entry: Optional[str] = None
    status: str
    index_lots: Optional[int] = None
    covered_weight: Optional[float] = None
    notional_ratio: Optional[float] = None
    premium_pnl: float
    futures_pnl: float
    costs: float
    net: float
    cumulative: float
    weighting: Optional[str] = None
    settle_basis: Optional[str] = None


class Summary(BaseModel):
    cycles: int
    total_net: float
    wins: int
    best: Optional[float]
    worst: Optional[float]
    costs: float


class Book(BaseModel):
    name: str
    label: str
    sizing: str
    has_state: bool
    state_error: Optional[str] = None
    # Weightings the paper record actually used (closed cycles + open book).
    # The runner's --equal-weight switches the matched book on the same state
    # file, so the label cannot assume one; more than one means mixed history.
    weightings: List[str]
    replay_weighting: Optional[str]
    open: Optional[OpenBook]
    paper: List[Cycle]
    paper_summary: Summary
    replay: List[Cycle]
    replay_summary: Summary
    replay_source: Optional[str]


class DispersionResponse(BaseModel):
    books: List[Book]


def _summary(cycles: List[Cycle]) -> Summary:
    nets = [c.net for c in cycles]
    return Summary(
        cycles=len(cycles),
        total_net=float(sum(nets)),
        wins=sum(1 for n in nets if n > 0),
        best=max(nets) if nets else None,
        worst=min(nets) if nets else None,
        costs=float(sum(c.costs for c in cycles)),
    )


def _with_cumulative(rows: List[dict]) -> List[Cycle]:
    out, running = [], 0.0
    for r in sorted(rows, key=lambda r: r["expiry"]):
        running += float(r["net"])
        out.append(Cycle(**{**r, "cumulative": running}))
    return out


def _open_book(raw: dict) -> OpenBook:
    futures = raw.get("futures") or {}
    legs = [
        Leg(
            symbol=leg["symbol"], side=int(leg["side"]), strike=float(leg["strike"]),
            lots=int(leg["lots"]), lot_size=int(leg["lot_size"]),
            premium=float(leg["ce"]) + float(leg["pe"]), iv=float(leg["iv"]),
            hedge_lots=int((futures.get(leg["symbol"]) or {}).get("lots", 0)),
        )
        for leg in raw.get("legs") or []
    ]
    return OpenBook(
        expiry=raw["expiry"], entry=raw["entry"], index_lots=int(raw["index_lots"]),
        n_names=sum(1 for leg in legs if leg.side > 0),
        covered_weight=float(raw["covered_weight"]),
        notional_ratio=raw.get("notional_ratio"),
        weighting=str(raw.get("weighting", "")),
        costs=float(raw.get("costs", 0.0)),
        futures_pnl=float(raw.get("futures_pnl", 0.0)),
        last_hedge_session=raw.get("last_hedge_session"),
        legs=legs,
    )


def _paper(path: Path):
    """(has_state, error, open_book, closed_cycles) from one state file."""
    if not path.exists():
        return False, None, None, []
    try:
        state = json.loads(path.read_text())
        book = _open_book(state["book"]) if state.get("book") else None
        closed = [
            {
                "expiry": r["expiry"], "entry": r.get("entry"), "status": r.get("status", "ok"),
                "index_lots": r.get("index_lots"), "covered_weight": r.get("covered_weight"),
                "notional_ratio": r.get("notional_ratio"),
                "premium_pnl": float(r.get("premium_pnl", 0.0)),
                "futures_pnl": float(r.get("futures_pnl", 0.0)),
                "costs": float(r.get("costs", 0.0)), "net": float(r["net"]),
                "weighting": r.get("weighting"), "settle_basis": r.get("settle_basis"),
            }
            for r in state.get("closed") or []
        ]
        return True, None, book, _with_cumulative(closed)
    except Exception as e:                                    # noqa: BLE001
        # Surface it on the page instead of showing an empty, healthy-looking book.
        logger.warning("Failed to read %s: %s", path, e)
        return True, f"{type(e).__name__}: {e}", None, []


def _replay(path: Path) -> List[Cycle]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("Failed to read %s: %s", path, e)
        return []
    if "weighting" not in df.columns:
        df["weighting"] = None
    df = df[(df["book"] == "A") & (df["exit_mode"] == "expiry")
            & (df["hedge"] == "future") & (df["status"] == "ok")]
    rows = [
        {
            "expiry": str(r.expiry), "entry": str(r.entry), "status": "replay",
            "index_lots": int(r.index_lots), "covered_weight": float(r.covered_weight),
            "premium_pnl": float(r.premium_pnl), "futures_pnl": float(r.futures_pnl),
            "costs": float(r.costs), "net": float(r.net),
            "weighting": r.weighting if isinstance(r.weighting, str) else None,
        }
        for r in df.itertuples()
    ]
    return _with_cumulative(rows)


@router.get("", response_model=DispersionResponse)
def dispersion_paper() -> DispersionResponse:
    books = []
    for spec in BOOKS:
        has_state, err, open_book, paper = _paper(DATA_CACHE / spec["state"])
        replay_path = DATA_CACHE / spec["replay"]
        replay = _replay(replay_path)
        used = {c.weighting for c in paper if c.weighting}
        if open_book is not None and open_book.weighting:
            used.add(open_book.weighting)
        replay_w = sorted({c.weighting for c in replay if c.weighting})
        books.append(Book(
            name=spec["name"], label=spec["label"], sizing=spec["sizing"],
            has_state=has_state, state_error=err,
            weightings=sorted(used),
            replay_weighting=", ".join(replay_w) if replay_w else None,
            open=open_book,
            paper=paper, paper_summary=_summary(paper),
            replay=replay, replay_summary=_summary(replay),
            replay_source=replay_path.name if replay else None,
        ))
    return DispersionResponse(books=books)
