"""Tests for loop_engine.risk_monitor — the isolated kill switch (Phase 4).

Rule 9: the monitor's reason to exist is to fire when the maker is bleeding
(paper §III-E) WITHOUT sharing the maker's fate (§VII-D), and to FAIL CLOSED, not
open. So the load-bearing tests are: (1) it reads realized P&L straight from the
runner state file as numbers (no maker import → no shared drift); (2) it tracks
each A/B book SEPARATELY (summing them is not a real equity) and trips on the
worst single-book drawdown; (3) an UNREADABLE runner state skips the poll without
a spurious ₹0-collapse trip; (4) a CORRUPT monitor state trips the kill switch
(fail closed) rather than silently losing the high-water mark; (5) the trip is
idempotent.
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


def _peaks_file(path, peaks):
    path.write_text(json.dumps({"peaks": peaks}))


# ──────────────────────────────────────────────────────────────────────────
# read_book_equities — per book, fail-closed on unreadable
# ──────────────────────────────────────────────────────────────────────────
def test_equities_are_per_book_not_summed(tmp_path):
    state = tmp_path / "runner.json"
    _write_runner_state(state, [("NIFTY", 10, -4, 75), ("BANKNIFTY", 2, 1, 15)])
    eq = rm.read_book_equities(state)
    # kept separate, never summed: 10*75, -4*75, 2*15, 1*15
    assert eq == {"NIFTY:kalman": 750.0, "NIFTY:ma": -300.0,
                  "BANKNIFTY:kalman": 30.0, "BANKNIFTY:ma": 15.0}


def test_missing_runner_state_is_unknown_not_zero(tmp_path):
    """Absent file → None (unknown), NOT 0.0 — so the caller fails closed instead
    of reading a phantom ₹0 collapse."""
    assert rm.read_book_equities(tmp_path / "absent.json") is None


def test_null_book_numbers_are_unknown(tmp_path):
    """A present-but-null realized_points must not crash (float(None)) nor be read
    as 0 — the whole read returns None (unknown)."""
    state = tmp_path / "runner.json"
    state.write_text(json.dumps({"instruments": [
        {"symbol": "NIFTY", "kalman": {"realized_points": None, "lot_size": 75}}]}))
    assert rm.read_book_equities(state) is None


# ──────────────────────────────────────────────────────────────────────────
# evaluate (pure) — per-book peaks, worst-book breach
# ──────────────────────────────────────────────────────────────────────────
def test_fresh_books_at_zero_do_not_trip():
    r = rm.evaluate({"NIFTY:kalman": 0.0, "NIFTY:ma": 0.0}, None, 20000)
    assert not r.breached and r.worst_drawdown == 0.0


def test_worst_single_book_drawdown_breaches():
    # kalman down 25k from its 30k peak; ma flat. Worst book = NIFTY:kalman.
    r = rm.evaluate({"NIFTY:kalman": 5000.0, "NIFTY:ma": 0.0},
                    {"NIFTY:kalman": 30000.0, "NIFTY:ma": 0.0}, 20000)
    assert r.breached and r.worst_book == "NIFTY:kalman" and r.worst_drawdown == 25000.0


def test_offsetting_books_do_not_mask_a_single_book_blowup():
    """Summing would net these to a small number; per-book correctly trips on the
    one book that blew up."""
    r = rm.evaluate({"NIFTY:kalman": -25000.0, "NIFTY:ma": 25000.0},
                    {"NIFTY:kalman": 0.0, "NIFTY:ma": 0.0}, 20000)
    assert r.breached and r.worst_book == "NIFTY:kalman"


def test_new_high_raises_that_books_peak():
    r = rm.evaluate({"NIFTY:kalman": 40000.0}, {"NIFTY:kalman": 30000.0}, 20000)
    assert r.peaks["NIFTY:kalman"] == 40000.0 and not r.breached


# ──────────────────────────────────────────────────────────────────────────
# poll_once — fail-closed IO + kill switch + incident lesson
# ──────────────────────────────────────────────────────────────────────────
def test_breach_trips_halt_and_logs_incident(tmp_path):
    runner, monitor, halt = (tmp_path / "runner.json", tmp_path / "monitor.json",
                             tmp_path / "HALT_NEW_ENTRIES")
    _peaks_file(monitor, {"NIFTY:kalman": 30000.0, "NIFTY:ma": 0.0})
    _write_runner_state(runner, [("NIFTY", 0, 0, 75)])      # kalman 0 → dd 30k

    r = rm.poll_once(rm.RiskConfig(20000), runner_state=runner, monitor_state=monitor,
                     halt_path=halt, strategy="kt", state_root=tmp_path)

    assert r.breached and halt.exists()
    assert any("RISK KILL" in le and "NIFTY:kalman" in le
               for le in memory.read_state("kt", root=tmp_path).lessons)


def test_default_halt_flag_is_scoped_per_strategy_not_fleet_wide(tmp_path, monkeypatch):
    """The monitor polices ONE strategy's paper book, so its default kill switch
    must be the scoped HALT_NEW_ENTRIES_<strategy> — never the shared
    HALT_NEW_ENTRIES, which halts entries for EVERY runner. On 2026-07-15 a
    ₹25,358 kalman_trend paper drawdown tripped the shared flag and silently
    froze the LIVE pair runner's entries for ~6.5 sessions."""
    import core.runner_common as rc
    monkeypatch.setattr(rc, "DATA_CACHE", tmp_path)
    # HALT_NEW_ENTRIES_PATH is bound at import time, so patching DATA_CACHE
    # alone would not redirect it — and a regression to the shared-flag default
    # would then touch the REAL data_cache/HALT_NEW_ENTRIES on the deploy host.
    # Patch it too, so the fleet-flag assertion below is load-bearing.
    monkeypatch.setattr(rc, "HALT_NEW_ENTRIES_PATH", tmp_path / "HALT_NEW_ENTRIES")
    runner, monitor = tmp_path / "runner.json", tmp_path / "monitor.json"
    _peaks_file(monitor, {"NIFTY:kalman": 30000.0, "NIFTY:ma": 0.0})
    _write_runner_state(runner, [("NIFTY", 0, 0, 75)])      # kalman 0 → dd 30k

    r = rm.poll_once(rm.RiskConfig(20000), runner_state=runner, monitor_state=monitor,
                     strategy="kt", state_root=tmp_path)    # halt_path defaulted

    assert r.breached
    assert (tmp_path / "HALT_NEW_ENTRIES_kt").exists()
    assert not (tmp_path / "HALT_NEW_ENTRIES").exists()     # fleet flag untouched


def test_unreadable_runner_state_skips_poll_no_spurious_trip(tmp_path):
    """After a high, a momentarily-missing runner state must NOT read as a ₹0
    collapse and trip — fail closed = skip, peaks preserved (§the spurious-trip bug)."""
    runner, monitor, halt = (tmp_path / "absent.json", tmp_path / "monitor.json",
                             tmp_path / "HALT_NEW_ENTRIES")
    _peaks_file(monitor, {"NIFTY:kalman": 50000.0})

    r = rm.poll_once(rm.RiskConfig(20000), runner_state=runner, monitor_state=monitor,
                     halt_path=halt, strategy="kt", state_root=tmp_path)

    assert not r.evaluable and not r.breached and not halt.exists()
    assert memory.read_state("kt", root=tmp_path).lessons == []


def test_corrupt_monitor_state_fails_closed_and_trips(tmp_path):
    """A corrupt peak file (high-water mark lost) must FAIL CLOSED — trip the kill
    switch + log, never silently reseed and let a real drawdown go untripped."""
    runner, monitor, halt = (tmp_path / "runner.json", tmp_path / "monitor.json",
                             tmp_path / "HALT_NEW_ENTRIES")
    monitor.write_text("{not valid json")
    _write_runner_state(runner, [("NIFTY", 0, 0, 75)])

    r = rm.poll_once(rm.RiskConfig(20000), runner_state=runner, monitor_state=monitor,
                     halt_path=halt, strategy="kt", state_root=tmp_path)

    assert r.breached and halt.exists()
    assert any("RISK MONITOR FAULT" in le for le in memory.read_state("kt", root=tmp_path).lessons)


def test_no_breach_persists_peaks_and_is_quiet(tmp_path):
    runner, monitor, halt = (tmp_path / "runner.json", tmp_path / "monitor.json",
                             tmp_path / "HALT_NEW_ENTRIES")
    _write_runner_state(runner, [("NIFTY", 100, 0, 75)])    # kalman 7500, a new high

    r = rm.poll_once(rm.RiskConfig(20000), runner_state=runner, monitor_state=monitor,
                     halt_path=halt, strategy="kt", state_root=tmp_path)

    assert not r.breached and not halt.exists()
    assert json.loads(monitor.read_text())["peaks"]["NIFTY:kalman"] == 7500.0
    assert memory.read_state("kt", root=tmp_path).lessons == []


def test_trip_is_idempotent_no_duplicate_lesson(tmp_path):
    runner, monitor, halt = (tmp_path / "runner.json", tmp_path / "monitor.json",
                             tmp_path / "HALT_NEW_ENTRIES")
    _peaks_file(monitor, {"NIFTY:kalman": 30000.0, "NIFTY:ma": 0.0})
    _write_runner_state(runner, [("NIFTY", 0, 0, 75)])

    cfg = rm.RiskConfig(20000)
    kw = dict(runner_state=runner, monitor_state=monitor, halt_path=halt,
              strategy="kt", state_root=tmp_path)
    rm.poll_once(cfg, **kw)
    rm.poll_once(cfg, **kw)                                 # still breached

    assert sum("RISK KILL" in le for le in memory.read_state("kt", root=tmp_path).lessons) == 1


# ──────────────────────────────────────────────────────────────────────────
# config from SKILL.md — non-default value proves it's actually read (Rule 9)
# ──────────────────────────────────────────────────────────────────────────
def test_threshold_is_actually_parsed_from_skill_not_default(tmp_path):
    (tmp_path / "kt").mkdir()
    (tmp_path / "kt" / "SKILL.md").write_text(
        "## Rules\n- kill_switch_drawdown_rupees: 12345\n", encoding="utf-8")
    cfg = rm.RiskConfig.from_skill("kt", root=tmp_path)
    assert cfg.kill_switch_drawdown_rupees == 12345.0     # != the 20000 default


def test_committed_skill_threshold_parses():
    assert rm.RiskConfig.from_skill("kalman_trend").kill_switch_drawdown_rupees == 20000.0
