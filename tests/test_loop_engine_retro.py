"""Tests for loop_engine.retro — the compounding self-improvement mechanism (Phase 3).

Rule 9: the retro's value is COMPOUNDING WITHOUT SPAM (paper §IV). So the
load-bearing tests are: (1) a notable change (checker verdict flip, operational
incident) writes exactly ONE lesson; (2) repeating the same verdict writes NOTHING
(else the genuinely load-bearing lessons drown); (3) the deferred (unwired)
checker never manufactures lessons; (4) lessons accumulate newest-first and are
read first on the next run — the whole point of the file.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loop_engine import memory
from loop_engine import retro as rt
from loop_engine.orchestrator import LoopOrchestrator, SessionOutcome


def _state(**last_run):
    return memory.LoopState(last_run=dict(last_run))


# ──────────────────────────────────────────────────────────────────────────
# build_lesson policy (pure)
# ──────────────────────────────────────────────────────────────────────────
def test_verdict_transition_is_a_lesson():
    lesson = rt.build_lesson({"checker": "pass"}, status="ok", checker="REJECT: sharpe 0.2>=1.5")
    assert lesson is not None
    assert "pass→REJECT" in lesson


def test_unchanged_verdict_is_not_a_lesson():
    """A repeat of the same verdict adds nothing — this is the anti-spam guard."""
    assert rt.build_lesson({"checker": "REJECT: x"}, status="ok", checker="REJECT: x") is None


def test_deferred_checker_never_makes_a_lesson():
    """An unwired stage 3 (Phase-1 default) must not generate verdict lessons."""
    assert rt.build_lesson({}, status="ok", checker="deferred:phase2") is None


def test_first_real_verdict_is_a_lesson_even_with_no_prior():
    lesson = rt.build_lesson({}, status="ok", checker="REJECT: sharpe 0.2>=1.5")
    assert lesson is not None and "None→REJECT" in lesson


def test_incident_status_is_a_lesson_once():
    assert rt.build_lesson({"status": "ok"}, status="error", checker="deferred:phase2")
    # already-in-incident → no repeat
    assert rt.build_lesson({"status": "error"}, status="error", checker="deferred:phase2") is None


def test_lesson_carries_pnl_context():
    eod = {"total_kalman_rupees": 120.0, "total_ma_rupees": 80.0, "kalman_minus_ma_rupees": 40.0}
    lesson = rt.build_lesson({}, status="ok", checker="pass", eod=eod)
    assert "Δ 40.0" in lesson


# ──────────────────────────────────────────────────────────────────────────
# run_retro persists to STATE.md
# ──────────────────────────────────────────────────────────────────────────
def test_run_retro_appends_only_when_notable(tmp_path):
    prior = _state(checker="pass")
    lesson = rt.run_retro("kt", prior, status="ok", checker="REJECT: x",
                          on_date=date(2026, 6, 28), root=tmp_path)
    assert lesson is not None
    assert memory.read_state("kt", root=tmp_path).lessons[0].endswith("pass→REJECT: x")

    # nothing notable → no new lesson
    none = rt.run_retro("kt", _state(checker="REJECT: x"), status="ok",
                        checker="REJECT: x", root=tmp_path)
    assert none is None
    assert len(memory.read_state("kt", root=tmp_path).lessons) == 1


# ──────────────────────────────────────────────────────────────────────────
# Integrated: orchestrator over consecutive sessions
# ──────────────────────────────────────────────────────────────────────────
def _engine(today=None):
    return SessionOutcome(status="ok", exit_code=0, eod={
        "total_kalman_rupees": 0.0, "total_ma_rupees": 0.0, "kalman_minus_ma_rupees": 0.0})


def test_stable_strategy_compounds_one_lesson_then_stays_silent(tmp_path):
    """Two identical REJECT sessions → exactly ONE transition lesson, not two.
    This is the compounding-without-spam property the paper rests on (§IV)."""
    from loop_engine.checker import CheckResult, GateOutcome

    bad = CheckResult(passed=False, n_obs=120,
                      gates=[GateOutcome("sharpe", 0.2, 1.5, ">=", False)])
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path, checker=lambda o: bad)

    orch.run_session(engine=_engine, today=date(2026, 6, 28))
    orch.run_session(engine=_engine, today=date(2026, 6, 29))

    lessons = memory.read_state("kt", root=tmp_path).lessons
    transition = [le for le in lessons if "REJECT" in le]
    assert len(transition) == 1, lessons


def test_verdict_recovery_writes_a_second_lesson(tmp_path):
    """REJECT then pass is a real regime change → a second lesson (read first)."""
    from loop_engine.checker import CheckResult, GateOutcome

    results = iter([
        CheckResult(passed=False, n_obs=120, gates=[GateOutcome("sharpe", 0.2, 1.5, ">=", False)]),
        CheckResult(passed=True, n_obs=600),
    ])
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path,
                            checker=lambda o: next(results))
    orch.run_session(engine=_engine, today=date(2026, 6, 28))
    orch.run_session(engine=_engine, today=date(2026, 6, 29))

    lessons = memory.read_state("kt", root=tmp_path).lessons
    assert "→pass" in lessons[0]                          # newest-first, read first
    assert any("→REJECT" in le for le in lessons)
