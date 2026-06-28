"""Tests for loop_engine.risk_monitor — the isolated kill switch (Phase 4).

Rule 9: the monitor's reason to exist is that it fires when the maker is bleeding
(paper §III-E) WITHOUT sharing the maker's fate (§VII-D). So the load-bearing
tests are: (1) it reads realized P&L straight from the runner state file as
numbers (no maker import → no shared drift); (2) a drawdown-from-peak past the
threshold trips HALT_NEW_ENTRIES and logs a hard incident — and a fresh book at 0
does NOT spuriously trip before it has made a high; (3) the trip is idempotent
(no duplicate halt / lesson spam while still breached).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loop_engine import memory
from loop_engine import risk_monitor as rm


def _write_runner_state(path, books):
    """books = list of (symbol, kalman_realized_pts, ma_realized_pts, lot)."""
    instruments = [
        {"symbol": s,
         "kalman": {"realized_points": kp, "lot_size": lot},
         "ma": {"realized_points": mp, "lot_size": lot}}
        for (s, kp, mp, lot) in books
    ]
    path.write_text(json.dumps({"instruments": instruments}))


# ──────────────────────────────────────────────────────────────────────────
# read_book_equity
# ──────────────────────────────────────────────────────────────────────────
def test_equity_sums_realized_across_books_in_rupees(tmp_path):
    state = tmp_path / "runner.json"
    # NIFTY: kalman +10pts ma -4pts @75 lot; BANKNIFTY: kalman +2 ma +1 @15
    _write_runner_state(state, [("NIFTY", 10, -4, 75), ("BANKNIFTY", 2, 1, 15)])
    # (10-4)*75 + (2+1)*15 = 450 + 45 = 495
    assert rm.read_book_equity(state) == 495.0


def test_missing_runner_state_is_zero_equity(tmp_path):
    assert rm.read_book_equity(tmp_path / "absent.json") == 0.0


# ──────────────────────────────────────────────────────────────────────────
# evaluate (pure)
# ──────────────────────────────────────────────────────────────────────────
def test_fresh_book_at_zero_does_not_trip():
    """No prior peak → peak seeds at current equity → drawdown 0 → no false kill."""
    r = rm.evaluate(equity=0.0, prior_peak=None, threshold_rupees=20000)
    assert not r.breached and r.peak == 0.0


def test_drawdown_from_peak_breaches_threshold():
    r = rm.evaluate(equity=5000.0, prior_peak=30000.0, threshold_rupees=20000)
    assert r.drawdown == 25000.0 and r.breached


def test_new_high_raises_the_peak_no_breach():
    r = rm.evaluate(equity=40000.0, prior_peak=30000.0, threshold_rupees=20000)
    assert r.peak == 40000.0 and r.drawdown == 0.0 and not r.breached


# ──────────────────────────────────────────────────────────────────────────
# poll_once — IO + kill switch + incident lesson
# ──────────────────────────────────────────────────────────────────────────
def test_breach_trips_halt_and_logs_incident(tmp_path):
    runner = tmp_path / "runner.json"
    monitor = tmp_path / "monitor.json"
    halt = tmp_path / "HALT_NEW_ENTRIES"
    monitor.write_text(json.dumps({"peak": 30000.0}))      # an earlier high
    _write_runner_state(runner, [("NIFTY", 0, 0, 75)])      # equity now 0 → dd 30k

    r = rm.poll_once(rm.RiskConfig(20000), runner_state=runner, monitor_state=monitor,
                     halt_path=halt, strategy="kt", state_root=tmp_path)

    assert r.breached
    assert halt.exists()                                    # kill switch tripped
    lessons = memory.read_state("kt", root=tmp_path).lessons
    assert any("RISK KILL" in le for le in lessons)


def test_no_breach_does_not_trip_and_persists_peak(tmp_path):
    runner = tmp_path / "runner.json"
    monitor = tmp_path / "monitor.json"
    halt = tmp_path / "HALT_NEW_ENTRIES"
    _write_runner_state(runner, [("NIFTY", 100, 0, 75)])    # equity 7500, a new high

    r = rm.poll_once(rm.RiskConfig(20000), runner_state=runner, monitor_state=monitor,
                     halt_path=halt, strategy="kt", state_root=tmp_path)

    assert not r.breached and not halt.exists()
    assert json.loads(monitor.read_text())["peak"] == 7500.0
    assert memory.read_state("kt", root=tmp_path).lessons == []


def test_trip_is_idempotent_no_duplicate_lesson(tmp_path):
    """While still breached the monitor must not re-touch the flag or spam lessons."""
    runner = tmp_path / "runner.json"
    monitor = tmp_path / "monitor.json"
    halt = tmp_path / "HALT_NEW_ENTRIES"
    monitor.write_text(json.dumps({"peak": 30000.0}))
    _write_runner_state(runner, [("NIFTY", 0, 0, 75)])

    cfg = rm.RiskConfig(20000)
    kw = dict(runner_state=runner, monitor_state=monitor, halt_path=halt,
              strategy="kt", state_root=tmp_path)
    rm.poll_once(cfg, **kw)
    rm.poll_once(cfg, **kw)                                 # still breached

    lessons = memory.read_state("kt", root=tmp_path).lessons
    assert sum("RISK KILL" in le for le in lessons) == 1


# ──────────────────────────────────────────────────────────────────────────
# config from committed SKILL.md
# ──────────────────────────────────────────────────────────────────────────
def test_threshold_loaded_from_committed_skill():
    assert rm.RiskConfig.from_skill("kalman_trend").kill_switch_drawdown_rupees == 20000.0
