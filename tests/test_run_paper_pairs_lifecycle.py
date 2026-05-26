"""Tests for run_paper_pairs runner lifecycle: per-tick `tick_one` return
contract (H1 — drives per-attempt state persist).

`tick_one` returns True when `execute_proposals` was called (scan or
rehedge produced proposals). It does NOT guarantee that broker-side fills
happened — execute_proposals may return non-COMPLETE for every leg, or
reverse a partial entry batch back to FLAT, and still return normally.
Persisting in those "attempted but no-op" cases is harmless and the
safer side to err on.

State-file persistence semantics (atomicity, durability, fsync ordering)
live in test_run_paper_pairs_state.py.
"""
from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from run_paper_pairs import tick_one


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
# H1 — tick_one return contract
# ──────────────────────────────────────────────────────────

class TestTickOneReturnsFilled:
    def test_returns_false_when_no_proposals_and_no_rehedge(self, log):
        assert tick_one(_strategy(), log) is False

    def test_returns_true_on_scan_fill(self, log):
        s = _strategy()
        s.scan_and_propose.return_value = [MagicMock()]
        assert tick_one(s, log) is True
        s.execute_proposals.assert_called_once()

    def test_returns_true_on_rehedge_fill(self, log):
        s = _strategy()
        s.check_and_rehedge.return_value = [MagicMock()]
        assert tick_one(s, log) is True
        s.execute_proposals.assert_called_once()

    def test_returns_true_when_scan_attempts_then_rehedge_raises(self, log):
        """A scan execute-attempt followed by a failed rehedge check still
        returns True — the scan's state mutations (if any) must be
        persisted promptly. The rehedge failure is logged separately and
        swallowed."""
        s = _strategy()
        s.scan_and_propose.return_value = [MagicMock()]
        s.check_and_rehedge.side_effect = RuntimeError("kite quote 500")
        assert tick_one(s, log) is True

    def test_returns_false_when_scan_raises_before_execute(self, log):
        """If scan_and_propose raises before execute_proposals is reached
        (and rehedge produces nothing), no execution was attempted → no
        persist."""
        s = _strategy()
        s.scan_and_propose.side_effect = RuntimeError("screener crashed")
        assert tick_one(s, log) is False
        s.execute_proposals.assert_not_called()

    def test_halt_all_returns_false_without_calling_strategy(self, log):
        s = _strategy()
        assert tick_one(s, log, halt_all=True) is False
        s.scan_and_propose.assert_not_called()
        s.check_and_rehedge.assert_not_called()

    def test_halt_new_entries_skips_scan_but_runs_rehedge(self, log):
        """A pair with an open leg must still be able to exit even when
        HALT_NEW_ENTRIES is set. Rehedge execute-attempt must still
        return True so the exit gets persisted immediately."""
        s = _strategy()
        s.scan_and_propose.return_value = [MagicMock()]   # would execute if called
        s.check_and_rehedge.return_value = [MagicMock()]
        assert tick_one(s, log, halt_new_entries=True) is True
        s.scan_and_propose.assert_not_called()
        s.check_and_rehedge.assert_called_once()
