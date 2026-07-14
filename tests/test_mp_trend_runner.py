"""Runner-level tests for run_paper_mp: the kill-switch latch and the exit/prior
date helpers introduced by the code-review fixes.

The latch is the load-bearing safety property (Rule 9): once halted, a later
recovery must NOT silently resume entries. The helper tests pin the missing-bar
carry logic so a suspended symbol can't extend a hold undetected.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import run_paper_mp as rp
from market_profile import Bar
from strategies.market_profile_intraday import MPTrendConfig


def _trend_up_day(day: str, base: float) -> List[Bar]:
    """Six strictly-higher-low 30-min bars on `day` → day_shape trend_up."""
    start = datetime.fromisoformat(f"{day}T09:15:00")
    return [Bar(ts=start + timedelta(minutes=30 * i), open=base + i,
                high=base + 2 + i, low=base + i, close=base + 1.5 + i, volume=100)
            for i in range(6)]


@pytest.fixture
def conn(tmp_path):
    from backend import db as backend_db
    backend_db.reset_for_tests(tmp_path / "mp_runner_test.db")
    c = backend_db.get_conn()
    c.executescript(rp.SCHEMA)
    yield c
    backend_db.reset_for_tests()


def _three_trend_up(day: str):
    """per_symbol + sorted_days for 3 names that are all trend_up on `day`."""
    per_symbol = {
        "A": {day: _trend_up_day(day, 100)},
        "B": {day: _trend_up_day(day, 200)},
        "C": {day: _trend_up_day(day, 300)},
    }
    sorted_days = {s: [day] for s in per_symbol}
    return per_symbol, sorted_days


class TestKillLatch:
    def test_opens_when_not_halted(self, conn):
        # Sanity: with no prior halt, a broad-momentum day opens all 3 longs.
        per_symbol, sorted_days = _three_trend_up("2026-05-02")
        rp.process_date(conn, MPTrendConfig(min_signals=3), per_symbol,
                        sorted_days, "2026-05-02")
        row = conn.execute("SELECT n_opened, halted FROM mp_trend_runs "
                           "WHERE run_date='2026-05-02'").fetchone()
        assert row["n_opened"] == 3 and row["halted"] == 0

    def test_prior_halt_latches_and_blocks_entries(self, conn):
        # A prior run halted (e.g. drawdown) — even though check_kill on today's
        # (empty) realized series would NOT halt, the latch must keep us halted
        # and open ZERO despite 3 valid trend_up signals.
        conn.execute(
            "INSERT INTO mp_trend_runs (run_date, n_universe, n_trend_up, "
            "n_opened, n_closed, day_net, cum_net, halted, reason, created_at) "
            "VALUES ('2026-05-01',48,0,0,0,0,-50000,1,'drawdown 61000 >= ...','x')"
        )
        per_symbol, sorted_days = _three_trend_up("2026-05-02")
        rp.process_date(conn, MPTrendConfig(min_signals=3), per_symbol,
                        sorted_days, "2026-05-02")
        row = conn.execute("SELECT n_opened, halted, reason FROM mp_trend_runs "
                           "WHERE run_date='2026-05-02'").fetchone()
        assert row["halted"] == 1
        assert row["n_opened"] == 0            # latched → no entries
        assert "drawdown" in row["reason"]     # original halt reason preserved
        assert conn.execute("SELECT COUNT(*) c FROM mp_trend_positions "
                            "WHERE status='OPEN'").fetchone()["c"] == 0


class TestDateHelpers:
    def test_exit_date_picks_next_available_close(self):
        days = ["2026-05-01", "2026-05-04", "2026-05-05"]
        # Entered 05-01; on 05-04 the exit is 05-04 (skips the 05-02/03 weekend).
        assert rp._exit_date_for(days, "2026-05-01", "2026-05-04") == "2026-05-04"

    def test_exit_date_none_when_symbol_has_no_bar_since_entry(self):
        # Symbol suspended after entry: no trading date in (entry, D] → carry.
        days = ["2026-05-01"]
        assert rp._exit_date_for(days, "2026-05-01", "2026-05-08") is None

    def test_exit_date_not_same_day(self):
        days = ["2026-05-01"]
        assert rp._exit_date_for(days, "2026-05-01", "2026-05-01") is None

    def test_prior_date(self):
        days = ["2026-05-01", "2026-05-04", "2026-05-05"]
        assert rp._prior_date(days, "2026-05-05") == "2026-05-04"
        assert rp._prior_date(days, "2026-05-01") is None   # nothing before first

    def test_calendar_gap(self):
        assert rp._calendar_gap("2026-05-01", "2026-05-08") == 7
