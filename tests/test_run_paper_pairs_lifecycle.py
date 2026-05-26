"""Tests for run_paper_pairs runner lifecycle: per-tick `tick_one`
TickOutcome (H1 attempted_execution + H3 errored), SIGTERM signal handler
installation (H2), and the HeartbeatTracker silent-fail counter (H3).

`tick_one` returns a TickOutcome NamedTuple with two booleans:
  - attempted_execution: drives per-attempt state persist
  - errored: drives silent-fail heartbeat (True iff every op that ran
    raised — see TickOutcome docstring in run_paper_pairs.py)

State-file persistence semantics (atomicity, durability, fsync ordering)
live in test_run_paper_pairs_state.py; this file is just about *when* the
runner triggers a persist, *how* it tears down on signals, and *when* it
escalates a systemic silent fail.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from run_paper_pairs import (
    HeartbeatTracker,
    install_signal_handlers,
    tick_one,
)


@pytest.fixture
def log():
    return logging.getLogger("test_run_paper_pairs_lifecycle")


def _strategy(sa: str = "A", sb: str = "B"):
    """A minimal strategy mock; tests override scan/rehedge behaviour."""
    s = MagicMock()
    s.symbol_a, s.symbol_b = sa, sb
    s.scan_and_propose.return_value = []
    s.check_and_rehedge.return_value = []
    return s


# ──────────────────────────────────────────────────────────
# H1 — tick_one.attempted_execution contract
# ──────────────────────────────────────────────────────────

class TestTickOneAttemptedExecution:
    def test_false_when_no_proposals_and_no_rehedge(self, log):
        assert tick_one(_strategy(), log).attempted_execution is False

    def test_true_on_scan_fill(self, log):
        s = _strategy()
        s.scan_and_propose.return_value = [MagicMock()]
        assert tick_one(s, log).attempted_execution is True
        s.execute_proposals.assert_called_once()

    def test_true_on_rehedge_fill(self, log):
        s = _strategy()
        s.check_and_rehedge.return_value = [MagicMock()]
        assert tick_one(s, log).attempted_execution is True
        s.execute_proposals.assert_called_once()

    def test_true_when_scan_attempts_then_rehedge_raises(self, log):
        """A scan execute-attempt followed by a failed rehedge check still
        returns attempted_execution=True — the scan's state mutations (if
        any) must be persisted promptly. The rehedge failure is logged
        separately and swallowed."""
        s = _strategy()
        s.scan_and_propose.return_value = [MagicMock()]
        s.check_and_rehedge.side_effect = RuntimeError("kite quote 500")
        assert tick_one(s, log).attempted_execution is True

    def test_false_when_scan_raises_before_execute(self, log):
        """If scan_and_propose raises before execute_proposals is reached
        (and rehedge produces nothing), no execution was attempted → no
        persist."""
        s = _strategy()
        s.scan_and_propose.side_effect = RuntimeError("screener crashed")
        out = tick_one(s, log)
        assert out.attempted_execution is False
        s.execute_proposals.assert_not_called()

    def test_halt_all_returns_idle(self, log):
        s = _strategy()
        out = tick_one(s, log, halt_all=True)
        assert out.attempted_execution is False
        assert out.errored is False
        s.scan_and_propose.assert_not_called()
        s.check_and_rehedge.assert_not_called()

    def test_halt_new_entries_skips_scan_but_runs_rehedge(self, log):
        """A pair with an open leg must still be able to exit even when
        HALT_NEW_ENTRIES is set. Rehedge execute-attempt must still
        return attempted_execution=True so the exit gets persisted
        immediately."""
        s = _strategy()
        s.scan_and_propose.return_value = [MagicMock()]   # would execute if called
        s.check_and_rehedge.return_value = [MagicMock()]
        assert tick_one(s, log, halt_new_entries=True).attempted_execution is True
        s.scan_and_propose.assert_not_called()
        s.check_and_rehedge.assert_called_once()


# ──────────────────────────────────────────────────────────
# H3 — tick_one.errored contract
# ──────────────────────────────────────────────────────────

class TestTickOneErrored:
    def test_false_when_no_op_raises(self, log):
        """Quiet ticks (no proposals, no rehedge) aren't errored — they're
        just nothing-to-do. Don't confuse idleness with failure."""
        assert tick_one(_strategy(), log).errored is False

    def test_true_when_both_ops_raise(self, log):
        """Both scan and rehedge raise → obvious failure signal."""
        s = _strategy()
        s.scan_and_propose.side_effect = RuntimeError("screener crashed")
        s.check_and_rehedge.side_effect = RuntimeError("kite quote 500")
        assert tick_one(s, log).errored is True

    def test_true_when_scan_raises_and_rehedge_returns_trivially(self, log):
        """The FLAT-position-with-dead-kite case: scan calls kite and
        raises; rehedge sees position=FLAT and returns [] without
        touching the failing dependency. errored must still be True —
        otherwise a runner with only flat pairs can't detect a dead
        token (the heartbeat would never trip because rehedge
        'succeeded' by short-circuiting). See pair_trading.py:250."""
        s = _strategy()
        s.scan_and_propose.side_effect = RuntimeError("kite token expired")
        # rehedge sees FLAT → returns [] without exercising kite.
        s.check_and_rehedge.return_value = []
        assert tick_one(s, log).errored is True

    def test_true_when_scan_returns_trivially_and_rehedge_raises(self, log):
        """The held-position-with-dead-kite case (orphan strategies): scan
        sees position!=FLAT and returns [] at pair_trading.py:226 without
        touching kite; rehedge calls kite and raises. errored must still
        be True — otherwise a book of held positions plus dead token
        would never trip the heartbeat."""
        s = _strategy()
        # scan returns trivially (mimicking the non-FLAT short-circuit).
        s.scan_and_propose.return_value = []
        s.check_and_rehedge.side_effect = RuntimeError("kite token expired")
        assert tick_one(s, log).errored is True

    def test_false_when_halt_all_idle(self, log):
        """halt_all = no ops ran = no error signal, no success signal.
        Idle ticks must not pollute the heartbeat counter."""
        assert tick_one(_strategy(), log, halt_all=True).errored is False

    def test_true_when_halt_new_entries_and_only_rehedge_raises(self, log):
        """Under HALT_NEW_ENTRIES only rehedge runs. If it raises,
        errored=True."""
        s = _strategy()
        s.check_and_rehedge.side_effect = RuntimeError("kite quote 500")
        assert tick_one(s, log, halt_new_entries=True).errored is True


# ──────────────────────────────────────────────────────────
# H3 — HeartbeatTracker
# ──────────────────────────────────────────────────────────

class TestHeartbeatTracker:
    def _make(self, tmp_path, log, threshold: int = 3) -> HeartbeatTracker:
        return HeartbeatTracker(
            threshold=threshold,
            sentinel_path=tmp_path / "silent_fail.flag",
            log=log,
        )

    def test_idle_tick_does_not_increment_counter(self, tmp_path, log):
        """halt_all → n_ran=0 → no signal either way. An operator who
        pauses the book overnight must not trip a false alarm."""
        h = self._make(tmp_path, log)
        for _ in range(10):
            assert h.record_tick(n_ran=0, n_errored=0) is False
        assert h.consecutive_ticks == 0

    def test_below_threshold_does_not_breach(self, tmp_path, log):
        h = self._make(tmp_path, log, threshold=3)
        assert h.record_tick(n_ran=4, n_errored=4) is False
        assert h.record_tick(n_ran=4, n_errored=4) is False
        assert h.consecutive_ticks == 2
        assert not (tmp_path / "silent_fail.flag").exists()

    def test_at_threshold_touches_sentinel_and_breaches(self, tmp_path, log):
        h = self._make(tmp_path, log, threshold=3)
        h.record_tick(n_ran=4, n_errored=4)
        h.record_tick(n_ran=4, n_errored=4)
        assert h.record_tick(n_ran=4, n_errored=4) is True
        assert (tmp_path / "silent_fail.flag").exists()

    def test_one_success_resets_counter(self, tmp_path, log):
        """Mid-streak recovery (e.g. transient kite blip) clears the
        counter — only sustained all-error sequences escalate."""
        h = self._make(tmp_path, log, threshold=3)
        h.record_tick(n_ran=4, n_errored=4)
        h.record_tick(n_ran=4, n_errored=4)
        # 4 ran, 3 errored → one pair succeeded → reset.
        assert h.record_tick(n_ran=4, n_errored=3) is False
        assert h.consecutive_ticks == 0
        # Now would need 3 *more* consecutive all-errored ticks.
        h.record_tick(n_ran=4, n_errored=4)
        h.record_tick(n_ran=4, n_errored=4)
        assert not (tmp_path / "silent_fail.flag").exists()
        assert h.record_tick(n_ran=4, n_errored=4) is True

    def test_breach_persists_after_threshold(self, tmp_path, log):
        """Once the threshold trips, subsequent record_tick calls keep
        returning True — the runner uses this to keep the silent_fail
        flag set if the caller chooses not to break immediately."""
        h = self._make(tmp_path, log, threshold=2)
        h.record_tick(n_ran=4, n_errored=4)
        assert h.record_tick(n_ran=4, n_errored=4) is True
        assert h.record_tick(n_ran=4, n_errored=4) is True

    def test_sentinel_path_creates_missing_parent_dir(self, tmp_path, log):
        """If DATA_CACHE doesn't exist yet for some reason, the sentinel
        touch shouldn't crash the runner — fail-soft on directory create."""
        nested = tmp_path / "deep" / "not_yet" / "silent_fail.flag"
        h = HeartbeatTracker(threshold=1, sentinel_path=nested, log=log)
        assert h.record_tick(n_ran=2, n_errored=2) is True
        assert nested.exists()


# ──────────────────────────────────────────────────────────
# H2 — SIGTERM handler installation
# ──────────────────────────────────────────────────────────

class TestInstallSignalHandlers:
    @pytest.fixture(autouse=True)
    def _restore_sigterm(self):
        """Signal handlers are process-global — save and restore around
        each test so we don't poison other tests in the session."""
        original = signal.getsignal(signal.SIGTERM)
        yield
        signal.signal(signal.SIGTERM, original)

    def test_sigterm_routes_to_default_int_handler(self, log):
        """SIGTERM must be bound to the same handler that Python uses for
        SIGINT (Ctrl+C) — raising KeyboardInterrupt at the next interpreter
        check point. The existing main() `except KeyboardInterrupt:`
        catches both signals through one teardown path."""
        install_signal_handlers(log)
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler

    def test_sigterm_actually_raises_keyboardinterrupt(self, log):
        """End-to-end: after install, sending SIGTERM to this process
        raises KeyboardInterrupt rather than terminating us. This is the
        contract the runner relies on — verify it works on this kernel."""
        install_signal_handlers(log)
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
            # time.sleep is a documented signal check-point: a pending
            # signal interrupts it and the handler's exception propagates
            # out. Don't use a busy `for _ in range(N): pass` here — that
            # relies on per-bytecode signal checks that PEP 659's adaptive
            # interpreter may collapse under optimisation.
            time.sleep(0.05)
