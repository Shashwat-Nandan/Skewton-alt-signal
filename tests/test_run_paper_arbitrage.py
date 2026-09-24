"""Tests for the arbitrage paper runner's wiring — the bits that, if wrong,
silently corrupt state rather than throwing: file-name namespacing (so the
arbitrage runner can't share a lock/state file with the pair runner) and the
trading-day gate (so it no-ops on weekends/holidays instead of trading)."""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from runners import run_paper_arbitrage as arb
from runners import run_paper_pairs as pairs


class TestFilenameNamespacing:
    """The arbitrage runner MUST NOT share a lock or state file with the pair
    runner. They run concurrently on the same data_cache/; a shared lock would
    make them block each other, and a shared state file would make each clobber
    the other's positions. This is the invariant the separate templates buy."""

    def test_state_file_is_arbitrage_namespaced(self):
        p = arb.state_file_path("baseline")
        assert p.name == "arbitrage_paper_state_baseline.json"
        # Distinct from the pair runner's state file.
        assert p != pairs.state_file_path("baseline")

    def test_lock_file_distinct_from_pairs(self):
        assert arb.LOCK_FILE_TEMPLATE != pairs.LOCK_FILE_TEMPLATE
        assert "arbitrage" in arb.LOCK_FILE_TEMPLATE

    def test_daily_loss_flag_distinct_from_pairs(self):
        # A loss breach in one strategy must not freeze entries in the other.
        assert arb.HALT_ARB_DAILY_LOSS_PATH != pairs.HALT_DAILY_LOSS_PATH

    def test_operator_kill_switches_are_shared(self):
        # The manual HALT_ALL / HALT_NEW_ENTRIES switches ARE intentionally
        # shared — an operator kill switch should stop everything.
        assert arb.HALT_ALL_PATH == pairs.HALT_ALL_PATH
        assert arb.HALT_NEW_ENTRIES_PATH == pairs.HALT_NEW_ENTRIES_PATH

    def test_eod_sidecar_naming(self, tmp_path, monkeypatch):

        class _Strat:
            name = "arbitrage"

            def generate_eod_report(self):
                return {"realized_pnl": 1.0, "unrealized_pnl": 2.0}

        monkeypatch.setattr(arb, "DATA_CACHE", tmp_path)
        import logging
        log = logging.getLogger("test")
        arb.write_eod_sidecar(_Strat(), date(2026, 4, 17), log, system="baseline")
        assert (tmp_path / "arbitrage_paper_eod_2026-04-17.json").exists()
        arb.write_eod_sidecar(_Strat(), date(2026, 4, 17), log, system="persistent")
        assert (tmp_path / "arbitrage_paper_persistent_eod_2026-04-17.json").exists()


class TestTradingDayGate:
    """The runner reuses the pair runner's is_trading_day gate. Encode the
    intent: a Saturday and an NSE holiday both no-op; a normal weekday trades."""

    def test_weekend_is_not_a_trading_day(self):
        ok, reason = arb.is_trading_day(date(2026, 4, 18), set())  # Saturday
        assert not ok and "weekend" in reason

    def test_holiday_is_not_a_trading_day(self):
        holiday = date(2026, 4, 14)  # Tuesday, treated as a holiday here
        ok, reason = arb.is_trading_day(holiday, {holiday})
        assert not ok and "holiday" in reason

    def test_normal_weekday_is_a_trading_day(self):
        ok, _ = arb.is_trading_day(date(2026, 4, 17), set())  # Friday
        assert ok


from types import SimpleNamespace
from unittest.mock import MagicMock


class _FakeStrategy:
    """Minimal stand-in exposing only what the runner helpers touch."""
    name = "arbitrage"

    def __init__(self):
        self.client = MagicMock()
        self.state = SimpleNamespace(
            open_calendars={}, last_basis_snapshot=[],
            realized_pnl=0.0, unrealized_pnl=0.0,
        )
        self._session_start_realized = 0.0
        self._session_start_unrealized = 0.0
        self._observed = []

    def _observe_universe(self):
        return self._observed


class TestReconcileWithBroker:
    """Live mode must refuse to start when the persisted book disagrees with the
    broker; paper mode must skip entirely."""

    def _leg(self, ts, qty, lot):
        return SimpleNamespace(tradingsymbol=ts, quantity=qty, lot_size=lot)

    def _trade(self, legs):
        return SimpleNamespace(legs=legs)

    def test_paper_mode_skips(self):
        strat = _FakeStrategy()
        # No broker call should happen in paper mode.
        arb.reconcile_with_broker(strat, "paper", _log())
        strat.client.positions.assert_not_called()

    def test_live_mismatch_refuses_to_start(self):
        strat = _FakeStrategy()
        strat.state.open_calendars = {
            "AAA": self._trade([self._leg("AAA26APRFUT", -1, 100)])
        }
        # Broker reports a DIFFERENT share count for the leg.
        strat.client.positions.return_value = {
            "net": [{"exchange": "NFO", "tradingsymbol": "AAA26APRFUT", "quantity": 0}]
        }
        with pytest.raises(RuntimeError, match="reconciliation FAILED"):
            arb.reconcile_with_broker(strat, "live", _log())

    def test_live_match_starts(self):
        strat = _FakeStrategy()
        strat.state.open_calendars = {
            "AAA": self._trade([self._leg("AAA26APRFUT", -1, 100)])
        }
        # Broker agrees: -1 lot * 100 = -100 shares.
        strat.client.positions.return_value = {
            "net": [{"exchange": "NFO", "tradingsymbol": "AAA26APRFUT", "quantity": -100}]
        }
        arb.reconcile_with_broker(strat, "live", _log())  # no raise


class TestEmptyUniverseHeartbeat:
    """A dead token yields empty quotes (swallowed by _safe_quote) and no raise,
    so scan returns []. tick_once must still flag the tick as errored so the
    silent-fail heartbeat can catch the blind 'dead trader'."""

    def test_empty_universe_marks_errored(self):
        strat = _FakeStrategy()
        strat.scan_and_propose = lambda: []   # sets nothing; last_basis_snapshot stays []
        strat.check_and_rehedge = lambda: []
        attempted, errored = arb.tick_once(strat, _log())
        assert errored is True
        assert attempted is False

    def test_nonempty_universe_not_errored(self):
        strat = _FakeStrategy()
        strat.state.last_basis_snapshot = [{"symbol": "AAA"}]
        strat.scan_and_propose = lambda: []
        strat.check_and_rehedge = lambda: []
        attempted, errored = arb.tick_once(strat, _log())
        assert errored is False


class TestExpiryFlattenGuard:
    def test_detects_leg_expiring_on_or_before_today(self):
        trade = SimpleNamespace(legs=[
            SimpleNamespace(tradingsymbol="AAA26APRFUT", expiry="2026-04-17"),
            SimpleNamespace(tradingsymbol="AAA26MAYFUT", expiry="2026-05-28"),
        ])
        out = arb._legs_expiring_on_or_before(trade, date(2026, 4, 17))
        assert out == ["AAA26APRFUT"]

    def test_no_expiry_when_all_legs_future(self):
        trade = SimpleNamespace(legs=[
            SimpleNamespace(tradingsymbol="AAA26MAYFUT", expiry="2026-05-28"),
        ])
        assert arb._legs_expiring_on_or_before(trade, date(2026, 4, 17)) == []

    def test_unparseable_expiry_is_flagged(self):
        trade = SimpleNamespace(legs=[
            SimpleNamespace(tradingsymbol="AAA26APRFUT", expiry="not-a-date"),
        ])
        assert arb._legs_expiring_on_or_before(trade, date(2026, 4, 17)) == ["AAA26APRFUT"]


def _log():
    import logging
    return logging.getLogger("test-arb-runner")


class TestEntryWarmup:
    """WHY (Rule 9, issue #228): at 09:15 the far-month book is not formed. On
    2026-09-11 seven calendars opened in the very first tick — six against a far
    leg with NO two-sided book. The strategy-side gates catch those, but not
    entering an unformed market at all is the cheaper guard, and it must never
    suppress EXITS: a position already held has to stay manageable from tick 1."""

    def test_warmup_suppresses_entries_but_not_exits(self):
        import runners.run_paper_arbitrage as R
        calls = {"scan": 0, "rehedge": 0}

        class _S:
            state = type("st", (), {"last_basis_snapshot": [1]})()
            def scan_and_propose(self):
                calls["scan"] += 1; return []
            def check_and_rehedge(self):
                calls["rehedge"] += 1; return []
            def execute_proposals(self, p): pass

        R.tick_once(_S(), _log(), halt_all=False, halt_new_entries=True)
        assert calls == {"scan": 0, "rehedge": 1}, \
            "warmup must block entries and leave exits running"

    def test_the_warmup_window_is_measured_from_the_open(self):
        import runners.run_paper_arbitrage as R
        from datetime import datetime, timedelta
        assert R.ENTRY_WARMUP_MINUTES >= 1
        open_ts = datetime(2026, 9, 11, 9, 15)
        entry_open = open_ts + timedelta(minutes=R.ENTRY_WARMUP_MINUTES)
        # The 09-11 cluster fired at 09:15:18 — inside any warmup >= 1 minute.
        assert datetime(2026, 9, 11, 9, 15, 18) < entry_open

