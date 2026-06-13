"""
Audit 2026-06-10 task 2.6 / deferred 2.1: run_paper.py gained a silent-
dead-trader heartbeat. Its input is tick()'s new bool return, so pin that
contract — a tick is "ok" only when neither scan nor rehedge swallowed an
exception. If tick() always returned True (the old behavior), a token-
expired session would tick all day failing silently and still exit 0.
"""
import logging
from unittest.mock import MagicMock

import run_paper

log = logging.getLogger("test")


def _hedger():
    h = MagicMock()
    h.scan_and_propose.return_value = []
    h.check_and_rehedge.return_value = []
    return h


def test_tick_ok_when_both_succeed():
    assert run_paper.tick(_hedger(), log) is True


def test_tick_not_ok_when_scan_raises():
    h = _hedger()
    h.scan_and_propose.side_effect = RuntimeError("token expired")
    assert run_paper.tick(h, log) is False


def test_tick_not_ok_when_rehedge_raises():
    h = _hedger()
    h.check_and_rehedge.side_effect = RuntimeError("kite down")
    assert run_paper.tick(h, log) is False


def test_tick_executes_proposals_when_present():
    h = _hedger()
    h.scan_and_propose.return_value = ["p1"]
    assert run_paper.tick(h, log) is True
    h.execute_proposals.assert_called_with(["p1"])
