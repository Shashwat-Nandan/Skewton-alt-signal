#!/usr/bin/env python3
"""
Strategy decay state machine + ledger (efficiency review E1, evolved).
======================================================================
Persistent per-strategy health states over the monthly net-realized series
the scoreboard already extracts. Replaces the stateless `kill_verdict`
(2026-07-19): the old rule recomputed "PARK CANDIDATE" from scratch each
run, so one lucky month silently cleared two months of decay and nothing
recorded when we first knew. This keeps the SAME trigger — two consecutive
losing complete months — and adds memory, hysteresis, and an audit trail.
Adapted from HKUDS/Vibe-Trading `strategy_store/decay.py`
(consecutive-signal transitions), reshaped to our monthly cadence.

REPLAY, not accumulation (2026-07-19 code review). The health state is a
PURE FUNCTION of the whole monthly series, recomputed from scratch every
run; the ledger stores the resulting trajectory as an audit trail, not as
an accumulator. The first design consumed each month once and advanced a
`last_evaluated_month` cursor, which meant a transient data-source outage
(dashboard.db unreadable for one run) permanently scored that month
NO_DATA: a real −₹80,000 month could never count again, and two losing
months either side of the gap never reached PARK RECOMMENDED. Replay makes
backfilled data re-score automatically, makes re-runs idempotent by
construction, and lets a corrected month be reported as a REVISED
transition instead of vanishing.

Health states (advisory — nothing here disables a timer):
  ACTIVE           --losing month-->            MONITORING
  MONITORING       --2nd consecutive losing-->  PARK_RECOMMENDED
  MONITORING       --healthy month-->           ACTIVE
  PARK_RECOMMENDED --2 consecutive healthy-->   ACTIVE   (hysteresis: one
                   good month must not reset a decayed strategy)
  any              --CRITICAL month-->          PARK_RECOMMENDED (fast path;
                   requires an opt-in per-strategy monthly-loss cap, unset
                   by default so merge changes no semantics)

PARKED is an OVERLAY on top of the health state, never a state the replay
can enter, and it comes in two flavours that behave differently on purpose:
  * SENTINEL park — DERIVED from a runner's kill file on every run. It
    appears when the file appears and clears when the file is removed, so
    deleting the sentinel revives the row exactly as the pre-machine
    verdict did. `--unpark` cannot override it (the file is the authority);
    the caller is told to remove the file instead.
  * OPERATOR park — PERSISTED in the ledger, sticky until `--unpark`.
Underneath either overlay the health replay keeps running, so an un-parked
strategy shows its true current health immediately rather than a stale
"fresh start".

Signal semantics match the old rule: HEALTHY iff net realized >= 0 (flat
never counts against), WARNING iff < 0, CRITICAL iff < 0 and beyond the
configured cap. NO_DATA (month absent) and PARTIAL (month the caller flags
as a known-incomplete observation window, e.g. Taleb's first month, whose
backup series starts mid-life) are NEUTRAL: recorded, never advancing or
resetting a streak — the standing "a missing month never counts against a
strategy" rule.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

LEDGER_VERSION = 2

# Signals (per complete month)
HEALTHY = "HEALTHY"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
NO_DATA = "NO_DATA"
PARTIAL = "PARTIAL"
NEUTRAL_SIGNALS = frozenset({NO_DATA, PARTIAL})

# Health states
ACTIVE = "ACTIVE"
MONITORING = "MONITORING"
PARK_RECOMMENDED = "PARK_RECOMMENDED"

# Park overlay sources
SENTINEL = "sentinel"
OPERATOR = "operator"

# Consecutive-month thresholds. Module constants, not config: they ARE the
# standing book-wide kill rule (2 losing months) and its 2026-07-19 recovery
# decision (2 healthy months) — changing them is a policy change that should
# look like one in a diff, not a config drift.
WARN_MONTHS_TO_PARK = 2
HEALTHY_MONTHS_TO_RECOVER = 2

HISTORY_CAP = 36  # months of audit trail kept per strategy


# ── Month arithmetic (one implementation; callers use these) ──────────────

def month_ord(month: str) -> int:
    """'YYYY-MM' -> sortable integer ordinal."""
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def ord_month(ordinal: int) -> str:
    """Inverse of month_ord."""
    return f"{ordinal // 12:04d}-{ordinal % 12 + 1:02d}"


def month_range(first: str, last: str) -> List[str]:
    """Inclusive 'YYYY-MM' range ([] when first > last)."""
    return [ord_month(o) for o in range(month_ord(first), month_ord(last) + 1)]


def shift_month(month: str, delta: int) -> str:
    return ord_month(month_ord(month) + delta)


# ── Pure replay ───────────────────────────────────────────────────────────

def classify_month(pnl: Optional[float],
                   critical_cap: Optional[float] = None) -> str:
    """Signal for one complete month of net realized P&L.

    `critical_cap` is a POSITIVE rupee amount (monthly loss beyond which a
    single month is disqualifying); None = fast path disabled (default).
    """
    if pnl is None:
        return NO_DATA
    if pnl >= 0:
        return HEALTHY
    if critical_cap is not None and pnl <= -abs(critical_cap):
        return CRITICAL
    return WARNING


def replay(monthly: Dict[str, float], months: Sequence[str],
           critical_cap: Optional[float] = None,
           partial_months: Iterable[str] = ()) -> dict:
    """Recompute the full health trajectory from the monthly series.

    Pure: same inputs → same output, no I/O, no ledger. Returns
    {state, warn_streak, healthy_streak, since, history}. `history` entries
    carry `why` on the months where the state changed, so the caller can
    render transitions without re-deriving them.
    """
    partial = set(partial_months)
    state, warn, healthy, since = ACTIVE, 0, 0, None
    history: List[dict] = []

    for month in sorted(months):
        pnl = monthly.get(month)
        signal = PARTIAL if month in partial else classify_month(pnl, critical_cap)
        why = None

        if signal in NEUTRAL_SIGNALS:
            pass                                  # neutral: streaks untouched
        elif signal == CRITICAL:
            warn += 1
            healthy = 0
            if state != PARK_RECOMMENDED:
                state, since = PARK_RECOMMENDED, month
                why = (f"critical month {pnl:+,.0f} beyond cap "
                       f"-{abs(critical_cap):,.0f}")
        elif signal == WARNING:
            warn += 1
            healthy = 0
            if state == ACTIVE:
                state, since = MONITORING, month
                why = f"losing month {pnl:+,.0f}"
            elif state == MONITORING and warn >= WARN_MONTHS_TO_PARK:
                state, since = PARK_RECOMMENDED, month
                why = f"{warn} consecutive losing months"
        else:                                     # HEALTHY
            healthy += 1
            warn = 0
            if state == MONITORING:
                state, since = ACTIVE, month
                why = f"healthy month {pnl:+,.0f}"
            elif state == PARK_RECOMMENDED and healthy >= HEALTHY_MONTHS_TO_RECOVER:
                state, since = ACTIVE, month
                why = f"{healthy} consecutive healthy months"

        entry = {"month": month, "pnl": None if pnl is None else round(float(pnl), 2),
                 "signal": signal, "state": state}
        if why:
            entry["why"] = why
        history.append(entry)

    return {"state": state, "warn_streak": warn, "healthy_streak": healthy,
            "since": since, "history": history[-HISTORY_CAP:]}


def diff_transitions(old_history: Sequence[dict], result: dict,
                     display: str, reported_through: Optional[str]) -> List[str]:
    """Transition lines to surface for THIS run.

    Two kinds, both derived by comparing the fresh trajectory against the
    stored one: transitions in months never reported before, and REVISED
    transitions in already-reported months whose state changed because data
    was backfilled or corrected (the case the old cursor design could not
    represent at all).
    """
    old_by_month = {h["month"]: h for h in old_history}
    lines: List[str] = []
    prev = ACTIVE
    for h in result["history"]:
        month, state = h["month"], h["state"]
        if state != prev:
            old = old_by_month.get(month)
            fresh = reported_through is None or month > reported_through
            if fresh:
                lines.append(f"{display}: {prev} → {state} "
                             f"({month}: {h.get('why', 'state change')})")
            elif old is not None and old.get("state") != state:
                lines.append(f"{display}: {prev} → {state} "
                             f"({month}: {h.get('why', 'state change')}) "
                             "[REVISED — data backfilled or corrected]")
        prev = state
    return lines


# ── Ledger entries ────────────────────────────────────────────────────────

def new_entry(display: str) -> dict:
    return {
        "display": display,
        "state": ACTIVE,
        "since": None,
        "warn_streak": 0,
        "healthy_streak": 0,
        "reported_through": None,   # last month whose transitions were shown
        "park": None,               # {"source","reason","since"} or None
        "history": [],
    }


def apply_replay(entry: dict, result: dict, display: str,
                 latest_complete: Optional[str]) -> List[str]:
    """Fold a fresh `replay` result into a ledger entry; return transitions."""
    lines = diff_transitions(entry.get("history", []), result, display,
                             entry.get("reported_through"))
    entry["display"] = display
    entry["state"] = result["state"]
    entry["since"] = result["since"]
    entry["warn_streak"] = result["warn_streak"]
    entry["healthy_streak"] = result["healthy_streak"]
    entry["history"] = result["history"]
    if latest_complete is not None:
        entry["reported_through"] = latest_complete
    return lines


def apply_park_overlay(entry: dict, sentinel_reason: Optional[str],
                       month: str) -> List[str]:
    """Reconcile the PARKED overlay with what the runner sentinels say now.

    A sentinel park is DERIVED: present file → parked, absent file →
    un-parked, every run. An operator park is left untouched here.
    """
    park = entry.get("park")
    lines: List[str] = []
    if sentinel_reason:
        already = bool(park) and park.get("source") == SENTINEL
        if not already:
            lines.append(f"{entry['display']}: PARKED by runner sentinel "
                         f"({month}: {sentinel_reason})")
        entry["park"] = {"source": SENTINEL, "reason": sentinel_reason,
                         "since": park["since"] if already else month}
    elif park and park.get("source") == SENTINEL:
        # The file is gone: revive automatically (pre-machine behaviour).
        lines.append(f"{entry['display']}: un-parked ({month}: runner "
                     "sentinel removed) — health state resumes")
        entry["park"] = None
    return lines


def set_operator_park(entry: dict, reason: str, month: str) -> List[str]:
    park = entry.get("park")
    if park and park.get("source") == SENTINEL:
        raise ValueError(
            f"{entry['display']} is parked by a runner sentinel "
            f"({park['reason']}) — that file is the authority; remove it to "
            "revive the strategy. An operator park would be overwritten on "
            "the next run.")
    if park and park.get("source") == OPERATOR:
        entry["park"]["reason"] = reason        # refresh, no duplicate line
        return []
    entry["park"] = {"source": OPERATOR, "reason": reason, "since": month}
    return [f"{entry['display']}: PARKED by operator ({month}: {reason})"]


def clear_operator_park(entry: dict, month: str) -> List[str]:
    park = entry.get("park")
    if not park:
        return []
    if park.get("source") == SENTINEL:
        raise ValueError(
            f"{entry['display']} is parked by a runner sentinel "
            f"({park['reason']}), not by an operator — remove that file to "
            "revive it; --unpark cannot override a sentinel.")
    entry["park"] = None
    return [f"{entry['display']}: un-parked by operator ({month}) — health "
            f"state resumes at {entry['state']}"]


def verdict_line(entry: dict) -> str:
    """Scoreboard verdict-column rendering for an entry."""
    park = entry.get("park")
    if park:
        src = "sentinel" if park.get("source") == SENTINEL else "operator"
        return f"PARKED [{src}] ({park.get('reason') or src})"
    state, since = entry["state"], entry.get("since")
    if state == PARK_RECOMMENDED:
        return f"PARK RECOMMENDED{f' since {since}' if since else ''}"
    if state == MONITORING:
        return f"MONITORING ({entry['warn_streak']} losing month(s))"
    if not entry.get("history"):
        return "ACTIVE (no complete-month data yet)"
    return "ACTIVE"


# ── Ledger IO ─────────────────────────────────────────────────────────────

def load_ledger(path: Path) -> dict:
    """Load the ledger, or an empty one when absent.

    Every other failure is LOUD (Rule 12): silently restarting each strategy
    at ACTIVE would erase decay memory, the one thing this file exists to
    keep. Corrupt JSON propagates; a wrong version or a malformed shape
    raises ValueError naming the problem rather than surfacing as a bare
    KeyError deep in a caller.
    """
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return {"version": LEDGER_VERSION, "strategies": {}}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ledger must be a JSON object, "
                         f"got {type(data).__name__}")
    if data.get("version") != LEDGER_VERSION:
        raise ValueError(
            f"{path}: unsupported ledger version {data.get('version')!r} "
            f"(this build writes v{LEDGER_VERSION}). Move the file aside to "
            "rebuild it from the monthly series — the replay reconstructs "
            "every state; only the reported-transition markers are lost.")
    if not isinstance(data.get("strategies"), dict):
        raise ValueError(f"{path}: ledger is missing a 'strategies' object "
                         "— refusing to treat a malformed ledger as empty.")
    return data


def save_ledger(path: Path, ledger: dict) -> None:
    """Atomic write (tmp + os.replace), same discipline as _save_best_params."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2))
    os.replace(tmp, path)
