"""Compounding retro — the self-improvement mechanism (paper §IV).

After each session the loop writes what it learned back into STATE.md so the next
run reads it first and the rules tighten over time. The paper is explicit that a
retro records "what rule, IF ANY" — so this is deliberately SELECTIVE: a lesson is
appended only when the session CHANGED something worth remembering (the checker
verdict flipped, or an operational incident occurred), never once-per-session.
A stable NO-GO strategy that keeps getting rejected identically yields exactly one
transition lesson, then silence — the cure for the lesson-spam that would drown
the genuinely load-bearing constraints.

Deterministic only (Rule 5): no model synthesises these. LLM lesson synthesis is
the deferred Phase-6 audit's job.
"""
from __future__ import annotations

from datetime import date as _date
from pathlib import Path
from typing import Dict, Optional

from loop_engine import memory

# Statuses that always warrant an incident lesson the first time they appear.
_INCIDENT_STATUSES = {"error", "silent_fail"}


def _pnl_tail(eod: Optional[dict]) -> str:
    if not eod:
        return ""
    return (f"; kalman ₹{eod.get('total_kalman_rupees')} vs "
            f"ma ₹{eod.get('total_ma_rupees')} "
            f"(Δ {eod.get('kalman_minus_ma_rupees')})")


def build_lesson(
    prior_last_run: Dict[str, str],
    *,
    status: str,
    checker: str,
    eod: Optional[dict] = None,
) -> Optional[str]:
    """Return a one-line lesson IF this session changed something, else None.

    `checker` values starting with 'deferred' are not real verdicts (the stage is
    unwired) and never trigger a lesson — only genuine pass/REJECT transitions do.
    """
    reasons = []

    prior_status = prior_last_run.get("status")
    if status in _INCIDENT_STATUSES and prior_status != status:
        reasons.append(f"session status {prior_status}→{status}")

    if not checker.startswith("deferred"):
        prior_checker = prior_last_run.get("checker")
        if checker != prior_checker:
            reasons.append(f"checker verdict {prior_checker}→{checker}")

    if not reasons:
        return None
    return "; ".join(reasons) + _pnl_tail(eod)


def run_retro(
    strategy: str,
    prior: memory.LoopState,
    *,
    status: str,
    checker: str,
    eod: Optional[dict] = None,
    on_date: Optional[_date] = None,
    root: Optional[Path] = None,
) -> Optional[str]:
    """Append a lesson to STATE.md iff the session is notable; return it (or None).

    `prior` MUST be the state captured BEFORE the session's write_run_summary, so
    the comparison is against the previous run, not this one.
    """
    lesson = build_lesson(prior.last_run, status=status, checker=checker, eod=eod)
    if lesson:
        memory.append_lesson(strategy, lesson, on_date=on_date, root=root)
    return lesson
