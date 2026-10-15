"""Kill-switch and halt flags currently set in data_cache/, for a global banner.

2026-10-13: an automated halt froze every runner's entries for a day and a
half and nobody noticed — no page showed it. This lists every HALT_* file
(shared operator flags and per-runner latches alike) so the dashboard can
show them on every page. Read-only: it never sets or clears a flag.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from ..settings import REPO_ROOT

router = APIRouter(prefix="/halt-flags", tags=["halt-flags"])

DATA_CACHE = REPO_ROOT / "data_cache"

# What each flag means to an operator. Unknown HALT_* names are still listed.
_MEANING = {
    "HALT_ALL": "All runners frozen: no entries and no exits (operator kill switch).",
    "HALT_NEW_ENTRIES": "No new entries on any runner (shared operator flag).",
    "HALT_DAILY_LOSS": "Live pair runner hit its daily-loss breaker: no new entries.",
}


class HaltFlag(BaseModel):
    name: str
    since: str
    meaning: str
    self_clearing: bool


class HaltFlagsResponse(BaseModel):
    flags: List[HaltFlag]


def _meaning(name: str) -> str:
    if name in _MEANING:
        return _MEANING[name]
    if name.endswith("_broker_unreadable"):
        return ("Runner could not read broker positions for several checks: no new "
                "entries until it can (clears itself).")
    if name.startswith("HALT_NEW_ENTRIES_"):
        return (f"No new entries on {name[len('HALT_NEW_ENTRIES_'):]} only (automated "
                "latch: verify broker vs state, then remove the file).")
    if name.startswith("HALT_DAILY_LOSS_"):
        return f"Daily-loss breaker for {name[len('HALT_DAILY_LOSS_'):]}: no new entries."
    return "Halt flag set."


@router.get("", response_model=HaltFlagsResponse)
def halt_flags() -> HaltFlagsResponse:
    flags = []
    for path in sorted(DATA_CACHE.glob("HALT_*")):
        if not path.is_file():
            continue
        flags.append(HaltFlag(
            name=path.name,
            since=datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            meaning=_meaning(path.name),
            self_clearing=path.name.endswith("_broker_unreadable"),
        ))
    return HaltFlagsResponse(flags=flags)
