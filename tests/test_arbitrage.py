"""Tests for the arbitrage strategy — fair-value math, signal/calendar generation, fill handling."""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.arbitrage import (
    ArbitrageState,
    ArbitrageStrategy,
    CalendarLeg,
    CalendarTrade,
)
from trade_proposer import TradeProposal


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────

def _make_strategy(
    *, mode: str = "paper",
    risk_free_rate: float = 0.07,
    dividend_yield: float = 0.0,
    basis_entry_annual: float = 0.015,
    calendar_entry_annual: float = 0.020,
    calendar_exit_annual: float = 0.005,
    calendar_max_leg_basis: float = 9.99,   # disabled by default in tests
    disable_calendar: bool = False,
    universe=None,
) -> ArbitrageStrategy:
    s = ArbitrageStrategy.__new__(ArbitrageStrategy)
    s.kite = MagicMock()
    s.config = MagicMock()
    s.config_path = "config.ini"
    s.mode = mode
    s.universe = list(universe) if universe is not None else ["AAA", "BBB"]
    s.risk_free_rate = risk_free_rate
    s.dividend_yield = dividend_yield
    s.basis_entry_annual = basis_entry_annual
    s.basis_min_dte = 3
    s.calendar_entry_annual = calendar_entry_annual
    s.calendar_exit_annual = calendar_exit_annual
    s.calendar_max_holding_days = 15
    s.calendar_min_dte_near = 4
    s.calendar_max_leg_basis = calendar_max_leg_basis
    s.disable_calendar = disable_calendar
    s.lots_per_leg = 1
    s.max_open_calendars = 5
    s.max_leg_notional = None
    s.total_capital = 500_000
    s.state = ArbitrageState()
    s._instrument_cache = None
    s._clock = lambda: datetime(2026, 4, 17, 10, 30)
    return s


# ──────────────────────────────────────────────────────────
# Carry math
# ──────────────────────────────────────────────────────────

class TestCarryMath:
    def test_fair_future_zero_dte(self):
        s = _make_strategy()
        assert s._fair_future(100.0, 0) == 100.0

    def test_fair_future_30_days(self):
        s = _make_strategy(risk_free_rate=0.07, dividend_yield=0.0)
        # F* = 100 · exp(0.07 · 30/365)
        expected = 100.0 * math.exp(0.07 * 30 / 365)
        assert abs(s._fair_future(100.0, 30) - expected) < 1e-9

    def test_dividend_yield_lowers_fair(self):
        s_no_div = _make_strategy(dividend_yield=0.0)
        s_with_div = _make_strategy(dividend_yield=0.05)
        assert s_with_div._fair_future(100.0, 60) < s_no_div._fair_future(100.0, 60)

    def test_basis_zero_at_fair(self):
        s = _make_strategy()
        spot = 100.0
        dte = 30
        fut_at_fair = s._fair_future(spot, dte)
        assert abs(s._annualized_basis(spot, fut_at_fair, dte)) < 1e-9

    def test_basis_positive_when_fut_rich(self):
        s = _make_strategy()
        # 1% above fair on a 30-day contract → annualized ≈ 12%
        spot = 100.0
        dte = 30
        fut = s._fair_future(spot, dte) * 1.01
        assert s._annualized_basis(spot, fut, dte) > 0.10

    def test_implied_carry_matches_fair(self):
        s = _make_strategy(risk_free_rate=0.07)
        # F1, F2 both at fair → implied carry = r
        spot = 100.0
        f1 = s._fair_future(spot, 30)
        f2 = s._fair_future(spot, 60)
        carry = s._implied_carry(f1, f2, 30, 60)
        assert abs(carry - 0.07) < 1e-9

    def test_implied_carry_degenerate(self):
        s = _make_strategy()
        assert s._implied_carry(0.0, 100.0, 30, 60) is None
        assert s._implied_carry(100.0, 100.0, 60, 30) is None  # next before near
        assert s._implied_carry(100.0, 100.0, 30, 30) is None  # same dte


# ──────────────────────────────────────────────────────────
# Basis signal generation (signals-only mode)
# ──────────────────────────────────────────────────────────

class TestBasisSignals:
    def test_basis_below_threshold_no_signal(self):
        s = _make_strategy(mode="signals", basis_entry_annual=0.05)
        snap = {
            "symbol": "AAA", "spot": 100.0,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 100.5, "dte_near": 30,
            "next": None, "next_price": None, "dte_next": None,
            "basis_annual": 0.005,    # below threshold
            "carry_implied": None, "carry_diff": None,
        }
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert proposals == []

    def test_basis_above_threshold_emits_pair(self):
        s = _make_strategy(mode="signals", basis_entry_annual=0.01)
        snap = {
            "symbol": "AAA", "spot": 100.0,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 102.0,           # rich → cash-and-carry
            "dte_near": 30,
            "next": None, "next_price": None, "dte_next": None,
            "basis_annual": 0.20,
            "carry_implied": None, "carry_diff": None,
        }
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        kinds = {p.option_type for p in proposals}
        assert kinds == {"FUT_BASIS", "CASH"}
        # Fut leg should be SELL (rich), cash leg should be BUY
        fut = next(p for p in proposals if p.option_type == "FUT_BASIS")
        cash = next(p for p in proposals if p.option_type == "CASH")
        assert fut.transaction_type == "SELL"
        assert cash.transaction_type == "BUY"

    def test_basis_paper_mode_also_emits_basis_signal(self):
        # Basis is now emitted in every mode (used as a basis-monitoring service).
        # The proposals are routed to _emit_signal in execute_proposals so paper
        # state is never mutated.
        s = _make_strategy(mode="paper", basis_entry_annual=0.01)
        snap = {
            "symbol": "AAA", "spot": 100.0,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 105.0, "dte_near": 30,
            "next": None, "next_price": None, "dte_next": None,
            "basis_annual": 0.60, "basis_annual_next": None,
            "carry_implied": None, "carry_diff": None,
        }
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        # Both legs flagged as signals-only (CASH and FUT_BASIS).
        assert {p.option_type for p in proposals} == {"CASH", "FUT_BASIS"}

    def test_basis_signal_routed_to_emit_in_paper_mode(self, tmp_path, monkeypatch):
        # Verify paper-mode dispatch sends BOTH basis legs through _emit_signal
        # — it must never call paper/live execute or touch state.
        s = _make_strategy(mode="paper", basis_entry_annual=0.01)
        # Stub out the file-write path so we don't depend on log_dir resolution.
        emitted = []
        s._emit_signal = lambda p: emitted.append(p) or {"status": "SIGNAL_LOGGED"}
        # paper_execute should NOT be reached for basis legs.
        called = {"paper": 0, "live": 0}
        s._paper_execute = lambda p: called.__setitem__("paper", called["paper"] + 1) or {}
        s._live_execute = lambda p: called.__setitem__("live", called["live"] + 1) or {}
        snap = {
            "symbol": "AAA", "spot": 100.0,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 105.0, "dte_near": 30,
            "next": None, "next_price": None, "dte_next": None,
            "basis_annual": 0.60, "basis_annual_next": None,
            "carry_implied": None, "carry_diff": None,
        }
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        s.execute_proposals(proposals)
        assert len(emitted) == 2
        assert called == {"paper": 0, "live": 0}


# ──────────────────────────────────────────────────────────
# Calendar entry
# ──────────────────────────────────────────────────────────

class TestCalendarEntry:
    def _snap(self, symbol="AAA", carry_diff=0.03, near_px=100.0, next_px=101.0,
              basis_near=0.0, basis_next=0.0):
        return {
            "symbol": symbol, "spot": 99.5,
            "near": {"tradingsymbol": f"{symbol}26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": near_px, "dte_near": 11,
            "next": {"tradingsymbol": f"{symbol}26MAYFUT", "lot_size": 100,
                     "expiry": "2026-05-26", "instrument_token": 2},
            "next_price": next_px, "dte_next": 39,
            "basis_annual": basis_near, "basis_annual_next": basis_next,
            "carry_implied": 0.10, "carry_diff": carry_diff,
        }

    def test_below_threshold_no_entry(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.05)
        s._observe_universe = lambda: [self._snap(carry_diff=0.01)]
        assert s.scan_and_propose() == []

    def test_positive_diff_short_calendar(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s._observe_universe = lambda: [self._snap(carry_diff=0.03)]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        # carry_diff > 0 → next is rich → SELL F2 + BUY F1
        near = next(p for p in proposals if "APR" in p.tradingsymbol)
        nxt = next(p for p in proposals if "MAY" in p.tradingsymbol)
        assert near.transaction_type == "BUY"
        assert nxt.transaction_type == "SELL"

    def test_negative_diff_long_calendar(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s._observe_universe = lambda: [self._snap(carry_diff=-0.04)]
        proposals = s.scan_and_propose()
        near = next(p for p in proposals if "APR" in p.tradingsymbol)
        nxt = next(p for p in proposals if "MAY" in p.tradingsymbol)
        assert near.transaction_type == "SELL"
        assert nxt.transaction_type == "BUY"

    def test_max_open_cap_blocks_new(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s.max_open_calendars = 1
        s.state.open_calendars["XXX"] = CalendarTrade(
            symbol="XXX", position="LONG_CALENDAR",
            entry_time=datetime(2026, 4, 17), entry_carry_diff=0.0, legs=[],
        )
        s._observe_universe = lambda: [self._snap(symbol="AAA", carry_diff=0.03)]
        assert s.scan_and_propose() == []

    def test_already_open_blocks_new(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s.state.open_calendars["AAA"] = CalendarTrade(
            symbol="AAA", position="LONG_CALENDAR",
            entry_time=datetime(2026, 4, 17), entry_carry_diff=0.0, legs=[],
        )
        s._observe_universe = lambda: [self._snap(symbol="AAA", carry_diff=0.03)]
        assert s.scan_and_propose() == []

    def test_max_leg_notional_skip(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s.max_leg_notional = 1000  # 1 lot of 100 @ 100 = 10,000 → too big
        s._observe_universe = lambda: [self._snap(carry_diff=0.05, near_px=100.0, next_px=100.0)]
        assert s.scan_and_propose() == []

    def test_basis_gate_blocks_dividend_pinned_name(self):
        # Either leg with |basis_annual| above the gate → calendar skipped.
        # Push basis_entry_annual very high so the basis-monitor arm doesn't
        # fire and pollute the assertion (basis signals are always-on now).
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02,
                           calendar_max_leg_basis=0.10,
                           basis_entry_annual=9.99)
        # Near leg basis -50% ann. (RVNL/MUTHOOTFIN-like dividend artifact)
        s._observe_universe = lambda: [self._snap(carry_diff=0.05, basis_near=-0.50)]
        assert s.scan_and_propose() == []
        # Same on the next leg
        s._observe_universe = lambda: [self._snap(carry_diff=0.05, basis_next=-0.30)]
        assert s.scan_and_propose() == []

    def test_basis_gate_passes_clean_legs(self):
        # Both legs within ±10% basis → calendar fires.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02,
                           calendar_max_leg_basis=0.10,
                           basis_entry_annual=9.99)   # silence basis arm
        s._observe_universe = lambda: [
            self._snap(carry_diff=0.05, basis_near=0.05, basis_next=-0.05)
        ]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        assert {p.option_type for p in proposals} == {"FUT"}

    def test_basis_gate_skips_when_next_leg_basis_missing(self):
        # If basis_annual_next is None we conservatively skip — the gate cannot be evaluated.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02,
                           calendar_max_leg_basis=0.10)
        snap = self._snap(carry_diff=0.05)
        snap["basis_annual_next"] = None
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_disable_calendar_blocks_all_calendar_entries(self):
        # In disable_calendar mode no calendar proposals are produced, even
        # when carry_diff would otherwise trigger entry. Basis signals should
        # still flow through — this is the basis-monitoring deployment.
        s = _make_strategy(
            mode="paper", calendar_entry_annual=0.02,
            disable_calendar=True, basis_entry_annual=0.01,
        )
        snap = self._snap(carry_diff=0.05, basis_near=0.0, basis_next=0.0)
        # Force a basis dislocation at the same time so we can prove only
        # the calendar arm is gated, not the basis arm.
        snap["basis_annual"] = 0.50
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        kinds = {p.option_type for p in proposals}
        assert "FUT" not in kinds          # no calendar legs
        assert kinds == {"CASH", "FUT_BASIS"}


# ──────────────────────────────────────────────────────────
# Calendar exit logic
# ──────────────────────────────────────────────────────────

class TestCalendarExit:
    def _open_calendar(self, s, symbol="AAA", entry_time=None):
        trade = CalendarTrade(
            symbol=symbol, position="SHORT_CALENDAR",
            entry_time=entry_time or datetime(2026, 4, 15, 10, 0),
            entry_carry_diff=0.04,
            legs=[
                CalendarLeg(symbol=symbol, tradingsymbol=f"{symbol}26APRFUT",
                            expiry="2026-04-28", lot_size=100, quantity=1,
                            entry_price=100.0, current_price=100.0),
                CalendarLeg(symbol=symbol, tradingsymbol=f"{symbol}26MAYFUT",
                            expiry="2026-05-26", lot_size=100, quantity=-1,
                            entry_price=101.0, current_price=101.0),
            ],
        )
        s.state.open_calendars[symbol] = trade
        return trade

    def _snap(self, **overrides):
        snap = {
            "symbol": "AAA", "spot": 99.5,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 100.0, "dte_near": 11,
            "next": {"tradingsymbol": "AAA26MAYFUT", "lot_size": 100,
                     "expiry": "2026-05-26", "instrument_token": 2},
            "next_price": 101.0, "dte_next": 39,
            "basis_annual": 0.0,
            "carry_implied": 0.07, "carry_diff": 0.0,
        }
        snap.update(overrides)
        return snap

    def test_converge_exit(self):
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("CONVERGE" in p.rationale for p in exits)

    def test_max_hold_exit(self):
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        s.calendar_max_holding_days = 1
        # Force the entry into the past so held_days exceeds the cap.
        self._open_calendar(s, entry_time=datetime(2026, 4, 1, 10, 0))
        s._observe_universe = lambda: [self._snap(carry_diff=0.05)]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("MAX_HOLD" in p.rationale for p in exits)

    def test_expiry_force_exit(self):
        s = _make_strategy(mode="paper")
        self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(dte_near=1, carry_diff=0.05)]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("EXPIRY" in p.rationale for p in exits)


# ──────────────────────────────────────────────────────────
# Fill handling
# ──────────────────────────────────────────────────────────

class TestFillHandling:
    def _prop(self, ts, side, qty=1, price=100.0):
        return TradeProposal(
            tradingsymbol=ts, instrument_token=1, strike=0.0,
            expiry="2026-04-28" if "APR" in ts else "2026-05-26",
            option_type="FUT", lot_size=100, quantity=qty, price=price,
            transaction_type=side, iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=0.0, rationale="test",
        )

    def test_open_two_legs_creates_calendar(self):
        s = _make_strategy(mode="paper")
        s._apply_fill(self._prop("AAA26APRFUT", "BUY"))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL"))
        assert "AAA" in s.state.open_calendars
        trade = s.state.open_calendars["AAA"]
        assert len(trade.legs) == 2
        assert trade.position == "SHORT_CALENDAR"  # short the later (May), long earlier (Apr)

    def test_close_realizes_pnl(self):
        s = _make_strategy(mode="paper")
        s._apply_fill(self._prop("AAA26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL", price=101.0))
        # Now close at favorable prices: near up 1, next down 1 → +200 per leg
        s._apply_fill(self._prop("AAA26APRFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "BUY", price=100.0))
        # Two ₹100 gains × 100 lot × 1 lot = ₹200 gross, minus 4× FUT cost (~₹80 each)
        # The exact value isn't important — verify book is flat and trade archived.
        assert "AAA" not in s.state.open_calendars
        assert len(s.state.closed_trades) == 1
