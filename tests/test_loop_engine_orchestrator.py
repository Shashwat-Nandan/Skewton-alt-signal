"""Tests for loop_engine.orchestrator — the five-stage skeleton (Phase 1).

Rule 9: the orchestrator's whole job in Phase 1 is to thread the compounding
memory around a wrapped maker+execute session. So the load-bearing tests are:
(1) the session result is written into STATE.md (write-last) AND the lessons an
earlier session paid for survive that write — the orchestrator is exactly where a
careless rewrite could drop them; (2) the checker/risk SEAMS are recorded as
deferred so a reader can see the loop is not yet complete (paper §VII) and so
Phase 2/4 flipping them to real is observable; (3) an engine that errors still
gets a summary written (fail-loud — Rule 12) instead of crashing the loop.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loop_engine import memory
from loop_engine.orchestrator import (
    CHECK_DEFERRED,
    LoopOrchestrator,
    SessionOutcome,
    dry_run_engine,
)


def _fake_engine(status="ok", k=120.0, m=80.0):
    def engine(today=None):
        return SessionOutcome(
            status=status,
            exit_code=0 if status != "error" else 1,
            eod={
                "date": (today or date.today()).isoformat(),
                "total_kalman_rupees": k,
                "total_ma_rupees": m,
                "kalman_minus_ma_rupees": round(k - m, 2),
            },
        )

    return engine


def test_session_result_is_written_to_state(tmp_path):
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path)
    orch.run_session(engine=_fake_engine(k=120.0, m=80.0), today=date(2026, 6, 28))

    last_run = memory.read_state("kt", root=tmp_path).last_run
    assert last_run["status"] == "ok"
    assert last_run["kalman_rupees"] == "120.0"
    assert last_run["ma_rupees"] == "80.0"
    assert last_run["kalman_minus_ma_rupees"] == "40.0"


def test_session_preserves_prior_lessons(tmp_path):
    """THE load-bearing invariant at the orchestrator level: running a session
    must not wipe lessons earlier sessions accumulated (paper §IV compounding)."""
    memory.append_lesson("kt", "chop bleeds the trend book", on_date=date(2026, 1, 1),
                         root=tmp_path)

    LoopOrchestrator(strategy="kt", state_root=tmp_path).run_session(
        engine=_fake_engine(), today=date(2026, 6, 28))

    lessons = memory.read_state("kt", root=tmp_path).lessons
    assert lessons == ["2026-01-01: chop bleeds the trend book"]


def test_unwired_checker_is_recorded_as_deferred(tmp_path):
    """With no checker injected, stage 3 stays deferred and STATE.md SAYS so, so
    the incompleteness is visible (Phase-1 behaviour preserved)."""
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path,
                            risk_halt_path=tmp_path / "no_halt")
    outcome = orch.run_session(engine=_fake_engine(), today=date(2026, 6, 28))

    assert outcome.checker == CHECK_DEFERRED
    assert memory.read_state("kt", root=tmp_path).last_run["checker"] == CHECK_DEFERRED


def test_risk_seam_observes_the_kill_switch_flag(tmp_path):
    """Phase 4: the orchestrator REPORTS the isolated monitor's kill switch (reads
    the flag), it does not run the monitor inline (§VII-D)."""
    halt = tmp_path / "HALT_NEW_ENTRIES"
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path, risk_halt_path=halt)

    out_ok = orch.run_session(engine=_fake_engine(), today=date(2026, 6, 28))
    assert out_ok.risk == "ok"

    halt.touch()                                    # the separate monitor trips it
    out_halt = orch.run_session(engine=_fake_engine(), today=date(2026, 6, 29))
    assert out_halt.risk == "HALT_NEW_ENTRIES"
    assert memory.read_state("kt", root=tmp_path).last_run["risk"] == "HALT_NEW_ENTRIES"


def test_risk_seam_default_mode_reports_shared_operator_flag(tmp_path, monkeypatch):
    """The runner's entry gate ORs the scoped flag with the shared operator-owned
    HALT_NEW_ENTRIES, so risk() must not report 'ok' while the shared flag is set
    — that false green is the 2026-07-15 silent-freeze shape (Rule 12). Scoped
    takes precedence when both exist (it names the monitor's own trip)."""
    import core.runner_common as rc
    monkeypatch.setattr(rc, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(rc, "HALT_NEW_ENTRIES_PATH", tmp_path / "HALT_NEW_ENTRIES")
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path)   # no injected path

    out_ok = orch.run_session(engine=_fake_engine(), today=date(2026, 7, 23))
    assert out_ok.risk == "ok"

    (tmp_path / "HALT_NEW_ENTRIES").touch()          # operator's fleet-wide halt
    out_shared = orch.run_session(engine=_fake_engine(), today=date(2026, 7, 24))
    assert out_shared.risk == "HALT_NEW_ENTRIES"

    (tmp_path / "HALT_NEW_ENTRIES_kt").touch()       # monitor's scoped trip wins
    out_scoped = orch.run_session(engine=_fake_engine(), today=date(2026, 7, 27))
    assert out_scoped.risk == "HALT_NEW_ENTRIES_kt"


def test_errored_session_still_writes_a_summary(tmp_path):
    """Fail-loud (Rule 12): a failed maker session is recorded, not swallowed."""
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path)
    outcome = orch.run_session(engine=_fake_engine(status="error"), today=date(2026, 6, 28))

    assert outcome.status == "error"
    last_run = memory.read_state("kt", root=tmp_path).last_run
    assert last_run["status"] == "error"
    assert last_run["exit_code"] == "1"


def test_injected_checker_pass_is_recorded(tmp_path):
    """Phase 2: a passing verdict is recorded as 'pass' in STATE.md."""
    from loop_engine.checker import CheckResult

    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path,
                            checker=lambda outcome: CheckResult(passed=True, n_obs=600))
    outcome = orch.run_session(engine=_fake_engine(), today=date(2026, 6, 28))

    assert outcome.checker == "pass"
    assert memory.read_state("kt", root=tmp_path).last_run["checker"] == "pass"


def test_injected_checker_reject_records_failing_gates(tmp_path):
    """A killed candidate records the failing gate into STATE.md, and the
    pre-existing lessons survive the write (the spam policy itself is exercised in
    the Phase-3 retro tests)."""
    from loop_engine.checker import CheckResult, GateOutcome

    memory.append_lesson("kt", "pre-existing lesson", on_date=date(2026, 1, 1),
                         root=tmp_path)
    bad = CheckResult(
        passed=False, n_obs=120,
        gates=[GateOutcome("sharpe", 0.2, 1.5, ">=", False)],
    )
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path, checker=lambda o: bad)
    outcome = orch.run_session(engine=_fake_engine(), today=date(2026, 6, 28))

    assert outcome.checker.startswith("REJECT")
    assert "sharpe" in outcome.checker
    state = memory.read_state("kt", root=tmp_path)
    assert state.last_run["checker"].startswith("REJECT")
    assert "2026-01-01: pre-existing lesson" in state.lessons   # preserved


def test_no_checker_stays_deferred(tmp_path):
    """Phase-1 behaviour is preserved when no checker is injected."""
    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path)
    outcome = orch.run_session(engine=_fake_engine(), today=date(2026, 6, 28))
    assert outcome.checker == CHECK_DEFERRED


def test_raising_engine_still_writes_an_error_summary(tmp_path):
    """#1 fail-loud: an engine that RAISES (e.g. the runner blows up) must still be
    recorded as an error session, never crash the loop with no STATE.md write."""
    def boom(today=None):
        raise RuntimeError("runner exploded")

    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path,
                            risk_halt_path=tmp_path / "no_halt")
    outcome = orch.run_session(engine=boom, today=date(2026, 6, 28))

    assert outcome.status == "error"
    assert memory.read_state("kt", root=tmp_path).last_run["status"] == "error"


def test_raising_checker_does_not_drop_the_session(tmp_path):
    """#1: a checker that RAISES (e.g. load_daily_closes FileNotFoundError) must
    not prevent write_memory — the full day's P&L is still recorded."""
    def bad_checker(outcome):
        raise FileNotFoundError("no NIFTY data on host")

    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path,
                            risk_halt_path=tmp_path / "no_halt", checker=bad_checker)
    outcome = orch.run_session(engine=_fake_engine(k=120.0, m=80.0), today=date(2026, 6, 28))

    last_run = memory.read_state("kt", root=tmp_path).last_run
    assert outcome.checker.startswith("ERROR")
    assert last_run["kalman_rupees"] == "120.0"        # session P&L survived
    assert last_run["status"] == "ok"


def test_no_session_status_skips_the_checker(tmp_path):
    """#5: a non-trading day (engine returns status='no_session') must not run the
    expensive verifier or fabricate a holiday verdict."""
    ran = []

    def engine(today=None):
        return SessionOutcome(status="no_session", exit_code=0, eod=None)

    def checker(outcome):
        ran.append(True)
        from loop_engine.checker import CheckResult
        return CheckResult(passed=True, n_obs=600)

    orch = LoopOrchestrator(strategy="kt", state_root=tmp_path,
                            risk_halt_path=tmp_path / "no_halt", checker=checker)
    outcome = orch.run_session(engine=engine, today=date(2026, 6, 28))

    assert outcome.checker == "skipped:no_session"
    assert ran == []                                   # verifier not invoked


def test_dry_run_engine_touches_no_kite_and_zeroes_pnl():
    """The CI/local engine must run without a broker session and report a zeroed,
    well-formed EOD so the orchestration path is exercisable offline."""
    outcome = dry_run_engine(today=date(2026, 6, 28))
    assert outcome.status == "dry_run"
    assert outcome.eod["total_kalman_rupees"] == 0.0
    assert outcome.eod["kalman_minus_ma_rupees"] == 0.0
