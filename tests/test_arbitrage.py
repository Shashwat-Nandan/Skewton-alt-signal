"""Tests for the arbitrage strategy — fair-value math, signal/calendar generation, fill handling."""
from __future__ import annotations

import logging as _logging
import math
import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.arbitrage import (
    ArbitrageState,
    ArbitrageStrategy,
    CalendarLeg,
    CalendarTrade,
)
from core.trade_proposer import TradeProposal


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────

def _make_strategy(
    *, mode: str = "paper",
    risk_free_rate: float = 0.07,
    dividend_yield: float = 0.0,
    dividend_yields=None,
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
    s.dividend_yields = dict(dividend_yields or {})
    s.basis_entry_annual = basis_entry_annual
    s.basis_min_dte = 3
    s.calendar_entry_annual = calendar_entry_annual
    s.calendar_exit_annual = calendar_exit_annual
    s.calendar_max_holding_days = 15
    s.calendar_min_dte_near = 4
    s.calendar_max_leg_basis = calendar_max_leg_basis
    # Disabled by default in tests (like calendar_max_leg_basis above) so the
    # directional/threshold tests keep exercising exactly the logic they were
    # written for; the rupee-hurdle/debounce tests enable them explicitly.
    s.calendar_cost_hurdle_mult = 0.0
    s.calendar_exit_debounce_ticks = 1
    # calendar_entry_min_dte is a @property (= max(min_dte, max_hold+2) = 17
    # with the values above) — entry-test snaps use dte_near=25 to clear it.
    s.calendar_stop_loss_mult = 0.0
    s.disable_calendar = disable_calendar
    s.lots_per_leg = 1
    s.max_open_calendars = 5
    s.calendar_margin_pct = 0.06
    s.calendar_crossing_mult = 0.0      # #233: measure-only by default
    s.max_leg_notional = None
    s.total_capital = 500_000
    s.state = ArbitrageState()
    s._session_start_realized = 0.0
    s._session_start_unrealized = 0.0
    s._instrument_cache = None
    s._ts_to_name = {}
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
            # 25 ≥ the derived entry window (max_hold 15 + 2 = 17) so these
            # tests exercise the gates they were written for, not the window.
            "near_price": near_px, "dte_near": 25,
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

    def test_lot_mismatch_skips_calendar(self):
        # Audit 2026-06-17: near/next lot sizes differ (lot revision) → the
        # 1-lot-each spread wouldn't share-offset; skip rather than open an
        # un-offset outright stub.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        snap = self._snap(carry_diff=-0.04)
        snap["next"]["lot_size"] = 125          # near=100, next=125 → mismatch
        assert s._build_calendar_entry(snap) == []

    def test_calendar_margin_is_spread_aware(self):
        # Audit 2026-06-17: calendar legs are margined as one-leg notional ×
        # calendar_margin_pct, split across the two legs — NOT 0.20×notional
        # per leg. near=100 next=101 lot=100 qty=1 → one_leg_notional=10,100;
        # leg_margin = 10,100 × 0.06 / 2 = 303.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        props = s._build_calendar_entry(self._snap(carry_diff=-0.04))
        assert len(props) == 2
        for p in props:
            assert p.margin_required == pytest.approx(303.0)
            # Far below the old per-leg 0.20×notional (~₹2,000).
            assert p.margin_required < p.price * p.lot_size * 0.20

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

    # ── CONVERGE streak debounce (efficiency review 2026-07-05 §2.3) ──────
    # WHY: the carry_diff quote flickers intraday; honoring a SINGLE
    # converged print minutes after entry buys a full round-trip cost for
    # near-zero capture (June 2026: 64 round trips, ₹658 net on ₹93,963 of
    # costs). The guard must filter one-print noise WITHOUT pinning a
    # genuinely-converged spread — there is no stop-loss exit in this
    # strategy, so any hold-based suppression carries open re-divergence
    # risk. Mirrors pair_trading's mean_revert_streak (M-S3).
    def test_converge_single_print_is_debounced(self):
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        s.calendar_exit_debounce_ticks = 3
        trade = self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        assert s.check_and_rehedge() == []      # print 1/3: no exit
        assert trade.converge_streak == 1

    def test_converge_streak_reaching_threshold_exits(self):
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        s.calendar_exit_debounce_ticks = 3
        self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        assert s.check_and_rehedge() == []      # 1/3
        assert s.check_and_rehedge() == []      # 2/3
        exits = s.check_and_rehedge()           # 3/3 → banked
        assert len(exits) == 2
        assert all("CONVERGE" in p.rationale for p in exits)

    def test_streak_resets_when_diff_prints_back_outside_band(self):
        # Noise looks like: converged print, then back out. Two such episodes
        # must never accumulate into an exit — that would be the same
        # one-noisy-print churn with extra steps.
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        s.calendar_exit_debounce_ticks = 2
        trade = self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        assert s.check_and_rehedge() == []      # 1/2
        s._observe_universe = lambda: [self._snap(carry_diff=0.05)]
        assert s.check_and_rehedge() == []      # back outside → reset
        assert trade.converge_streak == 0
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        assert s.check_and_rehedge() == []      # 1/2 again, NOT 2/2

    def test_expiry_exit_is_never_debounced(self):
        # The streak guards against churn, not against safety: a leg about
        # to expire must square off even on its first converged print
        # (cash-settlement risk trumps cost).
        s = _make_strategy(mode="paper")
        s.calendar_exit_debounce_ticks = 5
        self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(dte_near=1, carry_diff=0.001)]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("EXPIRY" in p.rationale for p in exits)

    def test_converge_streak_survives_serialize_restore(self):
        # A mid-streak restart (runner crash between ticks) must not reset
        # the count to a value that fires the exit early; old blobs without
        # the key restore to 0 (fresh streak, conservative).
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        s.calendar_exit_debounce_ticks = 3
        trade = self._open_calendar(s)
        trade.converge_streak = 2
        blob = s.serialize_state()
        s2 = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        s2.restore_state(blob)
        assert s2.state.open_calendars["AAA"].converge_streak == 2
        del blob["open_calendars"][0]["converge_streak"]   # pre-debounce blob
        s3 = _make_strategy(mode="paper")
        s3.restore_state(blob)
        assert s3.state.open_calendars["AAA"].converge_streak == 0


# ──────────────────────────────────────────────────────────
# Rupee cost hurdle at entry (efficiency review 2026-07-05 §2.3/E2)
# ──────────────────────────────────────────────────────────
# WHY: the % gate (calendar_entry_annual) is blind to whether the carry can
# be MONETIZED — a thin-notional spread can pass 5% annualized while its
# harvestable rupees over the holding window are smaller than the four-leg
# round-trip cost. These tests pin the gate's unit: rupees, not percent.
class TestCalendarCostHurdle:
    def _snap(self, near_px, next_px, lot, carry_diff, dte_near=11):
        return {
            "symbol": "AAA", "spot": near_px - 0.5,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": lot,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": near_px, "dte_near": dte_near,
            "next": {"tradingsymbol": "AAA26MAYFUT", "lot_size": lot,
                     "expiry": "2026-05-26", "instrument_token": 2},
            "next_price": next_px, "dte_next": 39,
            "basis_annual": 0.0, "basis_annual_next": 0.0,
            "carry_implied": 0.10, "carry_diff": carry_diff,
        }

    def test_thin_notional_passes_percent_gate_but_fails_rupee_gate(self):
        # 6% annualized clears the 5% gate, but on a ₹10k-notional lot held
        # ≤11 days the harvest is a few rupees vs ~₹100+ of costs.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = 2.0
        assert s._build_calendar_entry(
            self._snap(near_px=100.0, next_px=101.0, lot=100,
                       carry_diff=0.06)) == []

    def test_fat_carry_on_big_notional_clears_the_hurdle(self):
        # ₹1M notional at a huge diff: harvest ≫ 2× round-trip cost. The
        # magnitude is deliberately extreme so the assertion stays robust to
        # small cost-model revisions.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = 2.0
        props = s._build_calendar_entry(
            self._snap(near_px=2000.0, next_px=2020.0, lot=500,
                       carry_diff=0.50))
        assert len(props) == 2

    def test_zero_mult_disables_the_hurdle(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = 0.0
        props = s._build_calendar_entry(
            self._snap(near_px=100.0, next_px=101.0, lot=100,
                       carry_diff=0.06))
        assert len(props) == 2

    def test_gate_consults_estimate_transaction_cost(self, monkeypatch):
        # The gate must price costs through the SHARED estimate_transaction_cost
        # (the same function the fills book) — not a private formula. Proven by
        # substitution, not by re-deriving the arithmetic (which would share
        # any bug with the code under test): with the cost model forced huge,
        # even the fat-carry fixture must be refused; forced to zero, even a
        # marginal one must pass.
        import strategies.taleb_karpathy as tk
        s = _make_strategy(mode="paper", calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = 2.0
        fat = self._snap(near_px=2000.0, next_px=2020.0, lot=500,
                         carry_diff=0.50)
        monkeypatch.setattr(tk, "estimate_transaction_cost",
                            lambda *a, **k: 1e12)
        assert s._build_calendar_entry(fat) == []
        monkeypatch.setattr(tk, "estimate_transaction_cost",
                            lambda *a, **k: 0.0)
        thin = self._snap(near_px=100.0, next_px=101.0, lot=100,
                          carry_diff=0.06)
        assert len(s._build_calendar_entry(thin)) == 2


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


# ──────────────────────────────────────────────────────────
# Regression: prefix-collision in symbol attribution (LT vs LTIM)
# ──────────────────────────────────────────────────────────

class TestSymbolAttribution:
    def _prop(self, ts, side="BUY", qty=1, price=100.0):
        return TradeProposal(
            tradingsymbol=ts, instrument_token=1, strike=0.0,
            expiry="2026-04-28" if "APR" in ts else "2026-05-26",
            option_type="FUT", lot_size=100, quantity=qty, price=price,
            transaction_type=side, iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=0.0, rationale="test",
        )

    def test_authoritative_map_resolves_ltim_correctly(self):
        # In a NIFTY-50 universe LT and LTIM both exist; the prior
        # startswith-on-first-match would route LTIM26APRFUT → LT.
        s = _make_strategy(universe=["LT", "LTIM"])
        s._ts_to_name = {"LTIM26APRFUT": "LTIM", "LT26APRFUT": "LT"}
        assert s._symbol_from_tradingsymbol("LTIM26APRFUT") == "LTIM"
        assert s._symbol_from_tradingsymbol("LT26APRFUT") == "LT"

    def test_longest_prefix_fallback_when_map_unpopulated(self):
        # If _build_fut_index hasn't run yet the map is empty; the fallback
        # must still route to LTIM (longest match), not LT.
        s = _make_strategy(universe=["LT", "LTIM"])
        # _ts_to_name intentionally empty
        assert s._symbol_from_tradingsymbol("LTIM26APRFUT") == "LTIM"
        assert s._symbol_from_tradingsymbol("LT26APRFUT") == "LT"

    def test_apply_fill_routes_ltim_independent_of_lt(self):
        # Direct correctness check on _apply_fill: an LTIM leg must NOT
        # land in an LT trade. This is the load-bearing assertion that
        # protects the full state machine from prefix collision.
        s = _make_strategy(universe=["LT", "LTIM"])
        s._ts_to_name = {
            "LT26APRFUT": "LT", "LT26MAYFUT": "LT",
            "LTIM26APRFUT": "LTIM", "LTIM26MAYFUT": "LTIM",
        }
        s._apply_fill(self._prop("LT26APRFUT", "BUY"))
        s._apply_fill(self._prop("LT26MAYFUT", "SELL"))
        s._apply_fill(self._prop("LTIM26APRFUT", "BUY"))
        s._apply_fill(self._prop("LTIM26MAYFUT", "SELL"))
        assert set(s.state.open_calendars) == {"LT", "LTIM"}
        assert len(s.state.open_calendars["LT"].legs) == 2
        assert len(s.state.open_calendars["LTIM"].legs) == 2


# ──────────────────────────────────────────────────────────
# Regression: per-trade realized_pnl on closed_trades
# ──────────────────────────────────────────────────────────

class TestPerTradePnL:
    def _prop(self, ts, side, qty=1, price=100.0):
        return TradeProposal(
            tradingsymbol=ts, instrument_token=1, strike=0.0,
            expiry="2026-04-28" if "APR" in ts else "2026-05-26",
            option_type="FUT", lot_size=100, quantity=qty, price=price,
            transaction_type=side, iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=0.0, rationale="test",
        )

    def test_two_round_trips_record_independent_realized_pnl(self):
        # Open + close AAA at +₹200 gross. Then open + close BBB at +₹400 gross.
        # The OLD bug: closed_trades[1].realized_pnl ≈ closed_trades[0].realized_pnl
        # + bbb_delta (running total). The fix: each row contains only its own
        # contribution.
        s = _make_strategy(mode="paper", universe=["AAA", "BBB"])
        s._ts_to_name = {
            "AAA26APRFUT": "AAA", "AAA26MAYFUT": "AAA",
            "BBB26APRFUT": "BBB", "BBB26MAYFUT": "BBB",
        }
        # Round trip 1 — AAA, +₹200 gross spread move (each leg ₹1×100 lot).
        s._apply_fill(self._prop("AAA26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("AAA26APRFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "BUY", price=100.0))
        # Round trip 2 — BBB, +₹400 gross (₹2×100 each leg).
        s._apply_fill(self._prop("BBB26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("BBB26MAYFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("BBB26APRFUT", "SELL", price=102.0))
        s._apply_fill(self._prop("BBB26MAYFUT", "BUY", price=99.0))

        assert len(s.state.closed_trades) == 2
        aaa, bbb = s.state.closed_trades
        # Per-trade realized = gross spread move - per-trade transaction costs.
        # gross AAA = 200; gross BBB = 400.
        assert aaa["symbol"] == "AAA"
        assert bbb["symbol"] == "BBB"
        # AAA cleared 200 gross; net must be roughly 200 - aaa.transaction_costs.
        assert abs(aaa["realized_pnl"] - (200.0 - aaa["transaction_costs"])) < 1e-6
        # BBB independently cleared 400 gross; net ≈ 400 - bbb.transaction_costs.
        assert abs(bbb["realized_pnl"] - (400.0 - bbb["transaction_costs"])) < 1e-6
        # And the bug-shape we are guarding against: the second row is NOT
        # the running total of both trades' P&L.
        assert bbb["realized_pnl"] != aaa["realized_pnl"] + bbb["realized_pnl"]
        # Cumulative totals on state should still equal the sum of the per-trade rows.
        assert abs(
            s.state.realized_pnl - (aaa["realized_pnl"] + bbb["realized_pnl"])
        ) < 1e-6

    def test_overlapping_trades_record_independent_realized_pnl(self):
        # OVERLAP case (the 2026-07-01 arbitrage session: many spreads open on the
        # SAME tick). Open AAA and BBB *both* first, THEN close them. The old
        # global-counter-minus-baseline approach contaminated each row with the
        # OTHER trade's realized+costs booked during its lifetime — BBB's row
        # absorbed AAA's +₹200, inflating BBB's gross to ₹600. Local per-trade
        # accumulation keeps each row its own (AAA=200 gross, BBB=400 gross).
        s = _make_strategy(mode="paper", universe=["AAA", "BBB"])
        s._ts_to_name = {
            "AAA26APRFUT": "AAA", "AAA26MAYFUT": "AAA",
            "BBB26APRFUT": "BBB", "BBB26MAYFUT": "BBB",
        }
        # Open BOTH (now overlapping), then close AAA (+200 gross), then BBB (+400).
        s._apply_fill(self._prop("AAA26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("BBB26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("BBB26MAYFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("AAA26APRFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("BBB26APRFUT", "SELL", price=102.0))
        s._apply_fill(self._prop("BBB26MAYFUT", "BUY", price=99.0))

        assert len(s.state.closed_trades) == 2
        aaa, bbb = s.state.closed_trades
        assert aaa["symbol"] == "AAA" and bbb["symbol"] == "BBB"
        # Each row's GROSS (realized + its own costs) must be its OWN spread move.
        # The overlap bug inflated BBB's gross to 600 (absorbing AAA's +200).
        assert abs((aaa["realized_pnl"] + aaa["transaction_costs"]) - 200.0) < 1e-6
        assert abs((bbb["realized_pnl"] + bbb["transaction_costs"]) - 400.0) < 1e-6
        # Rows must not double-count: their sum equals the global realized total.
        assert abs(
            s.state.realized_pnl - (aaa["realized_pnl"] + bbb["realized_pnl"])
        ) < 1e-6


# ──────────────────────────────────────────────────────────
# Per-symbol dividend yield
# ──────────────────────────────────────────────────────────

class TestPerSymbolDividendYield:
    def test_parse_yield_map_basic(self):
        m = ArbitrageStrategy._parse_yield_map(
            "ITC=0.04,COALINDIA=0.06, hindunilvr =0.025"
        )
        assert m == {"ITC": 0.04, "COALINDIA": 0.06, "HINDUNILVR": 0.025}

    def test_parse_yield_map_drops_garbage(self):
        # Malformed entries are warned-and-skipped, not fatal.
        m = ArbitrageStrategy._parse_yield_map("ITC=0.04,oops,COALINDIA=notanumber,=0.05,")
        assert m == {"ITC": 0.04}

    def test_get_yield_falls_back_to_default(self):
        s = _make_strategy(dividend_yield=0.01, dividend_yields={"ITC": 0.04})
        assert s._get_dividend_yield("ITC") == 0.04
        assert s._get_dividend_yield("RELIANCE") == 0.01

    def test_fair_future_uses_per_symbol_q(self):
        # With q=4% the fair future on ITC should be lower than the q=0 default.
        s = _make_strategy(risk_free_rate=0.07, dividend_yield=0.0,
                           dividend_yields={"ITC": 0.04})
        f_default = s._fair_future(100.0, 60, "RELIANCE")
        f_itc = s._fair_future(100.0, 60, "ITC")
        assert f_itc < f_default
        # Sanity: f_itc = 100 · exp((0.07 - 0.04) · 60/365)
        assert abs(f_itc - 100.0 * math.exp(0.03 * 60 / 365)) < 1e-9

    def test_basis_uses_per_symbol_q(self):
        # Same fut price; basis on a high-q name should be higher than on a q=0 name.
        s = _make_strategy(risk_free_rate=0.07, dividend_yield=0.0,
                           dividend_yields={"ITC": 0.04})
        spot, fut, dte = 100.0, 102.0, 60
        b_default = s._annualized_basis(spot, fut, dte, "RELIANCE")
        b_itc = s._annualized_basis(spot, fut, dte, "ITC")
        assert b_itc > b_default


# ──────────────────────────────────────────────────────────
# Spot fallback suppresses the basis arm
# ──────────────────────────────────────────────────────────

class TestSpotFallbackSuppression:
    def test_basis_skipped_when_spot_is_fallback(self):
        # When spot is back-discounted from the near future (cash quote
        # missing), basis_annual is structurally zero by construction.
        # The arm must skip rather than emit a noise signal.
        s = _make_strategy(mode="paper", basis_entry_annual=0.001)
        snap = {
            "symbol": "AAA", "spot": 100.0, "spot_is_fallback": True,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 102.0, "dte_near": 30,
            "next": None, "next_price": None, "dte_next": None,
            # If the snapshot ever leaks a non-zero basis through the
            # fallback path (it shouldn't), the gate must still suppress.
            "basis_annual": 0.50, "basis_annual_next": None,
            "carry_implied": None, "carry_diff": None,
        }
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert proposals == []

    def test_basis_fires_with_real_spot(self):
        # Same snapshot but spot_is_fallback=False — gate passes.
        s = _make_strategy(mode="paper", basis_entry_annual=0.001)
        snap = {
            "symbol": "AAA", "spot": 100.0, "spot_is_fallback": False,
            "near": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 102.0, "dte_near": 30,
            "next": None, "next_price": None, "dte_next": None,
            "basis_annual": 0.50, "basis_annual_next": None,
            "carry_implied": None, "carry_diff": None,
        }
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2


# ──────────────────────────────────────────────────────────
# Rolled-out leg pricing logs WARN (regression for 2d)
# ──────────────────────────────────────────────────────────

class TestRolledLegPricing:
    def test_exit_warns_when_leg_not_in_snapshot(self, caplog):
        # Open a calendar, then build an exit against a snapshot whose `near`
        # / `next` no longer carry the leg's tradingsymbol — simulating the
        # case where the contract has rolled off the instrument list.
        import logging
        s = _make_strategy(mode="paper")
        trade = CalendarTrade(
            symbol="AAA", position="SHORT_CALENDAR",
            entry_time=datetime(2026, 4, 1, 10, 0),
            entry_carry_diff=0.04,
            legs=[
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26APRFUT",
                            expiry="2026-04-28", lot_size=100, quantity=1,
                            entry_price=100.0, current_price=99.5),
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26MAYFUT",
                            expiry="2026-05-26", lot_size=100, quantity=-1,
                            entry_price=101.0, current_price=101.5),
            ],
        )
        # Snapshot's "near" is now the May contract (April rolled off);
        # the April leg in our trade record won't match.
        snap = {
            "symbol": "AAA", "spot": 100.0,
            "near": {"tradingsymbol": "AAA26MAYFUT", "lot_size": 100,
                     "expiry": "2026-05-26", "instrument_token": 2},
            "near_price": 101.5, "dte_near": 26,
            "next": None, "next_price": None, "dte_next": None,
            "basis_annual": 0.0,
            "carry_implied": None, "carry_diff": None,
        }
        with caplog.at_level(logging.WARNING, logger="strategies.arbitrage"):
            exits = s._build_calendar_exit(trade, snap, "MAX_HOLD")
        assert len(exits) == 2
        # The April leg should have been priced at last-known current_price (99.5)
        apr = next(p for p in exits if "APR" in p.tradingsymbol)
        assert apr.price == 99.5
        # And we logged about it.
        assert any("not in current snapshot" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────
# Cross-session state persistence (serialize ↔ restore)
# ──────────────────────────────────────────────────────────

class TestStatePersistence:
    """A restart in the middle of a multi-day calendar spread must not abandon
    the open position nor double-count its P&L. serialize_state() →
    json round-trip → restore_state() must reproduce the book exactly — that's
    the contract the paper runner relies on every tick and every morning."""

    def _strategy_with_open_spread(self):
        s = _make_strategy()
        # An open SHORT_CALENDAR on AAA: short near (Apr), long next (May).
        trade = CalendarTrade(
            symbol="AAA",
            position="SHORT_CALENDAR",
            entry_time=datetime(2026, 4, 15, 10, 0),
            entry_carry_diff=0.031,
            legs=[
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26APRFUT",
                            expiry="2026-04-30", lot_size=50, quantity=-1,
                            entry_price=101.0, current_price=100.5),
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26MAYFUT",
                            expiry="2026-05-28", lot_size=50, quantity=1,
                            entry_price=102.0, current_price=102.4),
            ],
            realized=-21.0,
            costs=21.0,
        )
        s.state.open_calendars = {"AAA": trade}
        s.state.realized_pnl = -42.0
        s.state.unrealized_pnl = 95.0
        s.state.total_transaction_costs = 42.0
        s.state.closed_trades = [{
            "symbol": "BBB",
            "exit_time": datetime(2026, 4, 14, 15, 20),
            "entry_time": datetime(2026, 4, 10, 9, 30),
            "entry_carry_diff": 0.025,
            "realized_pnl": 310.0,
            "transaction_costs": 42.0,
            "position": "LONG_CALENDAR",
        }]
        # Transient fields that must NOT survive serialisation.
        s.state.last_basis_snapshot = [{"symbol": "AAA", "basis_annual": 0.04}]
        s.state.pending_entry_diff = {"CCC": 0.05}
        return s

    def test_roundtrip_preserves_open_spread_and_pnl(self):
        import json

        src = self._strategy_with_open_spread()
        blob = json.loads(json.dumps(src.serialize_state(), default=str))

        dst = _make_strategy()
        dst.restore_state(blob)

        # Scalar P&L preserved exactly.
        assert dst.state.realized_pnl == -42.0
        assert dst.state.unrealized_pnl == 95.0
        assert dst.state.total_transaction_costs == 42.0

        # Open spread fully reconstructed, including signed leg quantities and
        # the per-trade realized/costs accumulators (without which closed_trades
        # would lose this trade's own P&L attribution at close).
        assert set(dst.state.open_calendars) == {"AAA"}
        t = dst.state.open_calendars["AAA"]
        assert t.position == "SHORT_CALENDAR"
        assert t.entry_time == datetime(2026, 4, 15, 10, 0)
        assert t.entry_carry_diff == pytest.approx(0.031)
        assert t.realized == -21.0
        assert t.costs == 21.0
        assert [(l.tradingsymbol, l.quantity, l.entry_price) for l in t.legs] == [
            ("AAA26APRFUT", -1, 101.0),
            ("AAA26MAYFUT", 1, 102.0),
        ]

        # Closed-trade datetimes survive the round-trip as datetimes (not str).
        assert len(dst.state.closed_trades) == 1
        ct = dst.state.closed_trades[0]
        assert ct["exit_time"] == datetime(2026, 4, 14, 15, 20)
        assert ct["entry_time"] == datetime(2026, 4, 10, 9, 30)
        assert ct["realized_pnl"] == 310.0

    def test_transient_fields_not_persisted(self):
        import json

        src = self._strategy_with_open_spread()
        blob = json.loads(json.dumps(src.serialize_state(), default=str))
        assert "last_basis_snapshot" not in blob
        assert "pending_entry_diff" not in blob

        dst = _make_strategy()
        dst.restore_state(blob)
        # Restore must leave the transient fields at their fresh defaults, not
        # carry stale values from the source session.
        assert dst.state.last_basis_snapshot == []
        assert dst.state.pending_entry_diff == {}

    def test_restore_rejects_wrong_strategy_blob(self):
        dst = _make_strategy()
        with pytest.raises(ValueError):
            dst.restore_state({
                "strategy": "pair_trading",
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_transaction_costs": 0.0,
            })

    def test_restore_old_format_reconstructs_opening_cost_attribution(self):
        # Migration: an OLD-format state file has baseline_* keys and NO per-trade
        # realized/costs. Restore must reconstruct each still-open calendar's
        # opening-cost attribution from its legs — otherwise its eventual closed
        # row over-states net P&L by the opening costs. Legs are open (nothing
        # realized yet), so realized-so-far == -(opening costs).
        from strategies.taleb_karpathy import estimate_transaction_cost

        dst = _make_strategy()
        old_blob = {
            "strategy": dst.name,
            "realized_pnl": -30.0,
            "unrealized_pnl": 0.0,
            "total_transaction_costs": 30.0,
            "closed_trades": [],
            "open_calendars": [{
                "symbol": "AAA",
                "position": "SHORT_CALENDAR",
                "entry_time": "2026-04-15T10:00:00",
                "entry_carry_diff": 0.03,
                "baseline_realized": -30.0,   # old-format keys, no realized/costs
                "baseline_costs": 30.0,
                "legs": [
                    {"symbol": "AAA", "tradingsymbol": "AAA26APRFUT",
                     "expiry": "2026-04-30", "lot_size": 50, "quantity": -1,
                     "entry_price": 101.0, "current_price": 101.0},
                    {"symbol": "AAA", "tradingsymbol": "AAA26MAYFUT",
                     "expiry": "2026-05-28", "lot_size": 50, "quantity": 1,
                     "entry_price": 102.0, "current_price": 102.0},
                ],
            }],
        }
        dst.restore_state(old_blob)
        t = dst.state.open_calendars["AAA"]
        expected = (
            estimate_transaction_cost(101.0, 1, 50, "SELL", instrument_type="FUT")
            + estimate_transaction_cost(102.0, 1, 50, "BUY", instrument_type="FUT")
        )
        assert t.costs == pytest.approx(expected)
        assert t.realized == pytest.approx(-expected)


# ──────────────────────────────────────────────────────────
# unrealized_pnl stays consistent with the open book (no phantom MTM)
# ──────────────────────────────────────────────────────────

class TestUnrealizedConsistency:
    """unrealized_pnl must reflect ONLY currently-open legs. The bug: it was
    maintained only inside _update_unrealized (called from check_and_rehedge,
    which early-returns on an empty book), so closing the last spread left a
    phantom mark frozen into unrealized_pnl — poisoning the EOD report, the
    dashboard net/cumulative, the daily-loss breaker, and the persisted state.
    """

    def _prop(self, ts, side, qty=1, price=100.0):
        return TradeProposal(
            tradingsymbol=ts, instrument_token=1, strike=0.0,
            expiry="2026-04-28" if "APR" in ts else "2026-05-26",
            option_type="FUT", lot_size=100, quantity=qty, price=price,
            transaction_type=side, iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=0.0, rationale="test",
        )

    def test_closing_last_spread_zeroes_unrealized(self):
        s = _make_strategy(mode="paper")
        s._apply_fill(self._prop("AAA26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL", price=101.0))
        # Simulate an intraday mark that moved unrealized away from zero, the
        # way _update_unrealized would on a live tick. Use asymmetric marks so
        # the long/short legs don't cancel to a coincidental zero.
        trade = s.state.open_calendars["AAA"]
        trade.legs[0].current_price = trade.legs[0].entry_price + 5.0
        trade.legs[1].current_price = trade.legs[1].entry_price + 1.0
        s._recompute_unrealized_from_open_legs()
        assert s.state.unrealized_pnl != 0.0  # mark is live

        # Close both legs → book is now empty.
        s._apply_fill(self._prop("AAA26APRFUT", "SELL", price=106.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "BUY", price=95.0))
        assert "AAA" not in s.state.open_calendars
        # The phantom-MTM bug would leave unrealized at its last open value;
        # the fix recomputes from the (now empty) open book → exactly 0.
        assert s.state.unrealized_pnl == 0.0

    def test_unrealized_reflects_only_remaining_open_spread(self):
        s = _make_strategy(mode="paper")
        for sym in ("AAA", "BBB"):
            s._apply_fill(self._prop(f"{sym}26APRFUT", "BUY", price=100.0))
            s._apply_fill(self._prop(f"{sym}26MAYFUT", "SELL", price=100.0))
        # Mark BBB's legs to a known unrealized; AAA stays flat.
        for leg in s.state.open_calendars["BBB"].legs:
            leg.current_price = leg.entry_price + (2.0 if leg.quantity > 0 else -2.0)
        s._recompute_unrealized_from_open_legs()
        # Close AAA only.
        s._apply_fill(self._prop("AAA26APRFUT", "SELL", price=100.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "BUY", price=100.0))
        # unrealized must now equal BBB's mark alone: each leg +2 * 100 * 1lot,
        # both legs same sign of contribution = +400 total.
        expected = sum(
            (l.current_price - l.entry_price) * l.quantity * l.lot_size
            for l in s.state.open_calendars["BBB"].legs
        )
        assert s.state.unrealized_pnl == pytest.approx(expected)


# ──────────────────────────────────────────────────────────
# Per-tick observation cache works under the live runner's clock
# ──────────────────────────────────────────────────────────

class TestObserveCache:
    def test_obs_tick_id_dedupes_within_tick(self):
        s = _make_strategy()
        calls = []
        s._observe_universe_uncached = lambda: (calls.append(1) or [])
        s._obs_tick_id = 1
        s._observe_universe()
        s._observe_universe()
        assert len(calls) == 1  # one fetch shared across the tick
        s._obs_tick_id = 2
        s._observe_universe()
        assert len(calls) == 2  # next tick re-fetches


# ──────────────────────────────────────────────────────────
# Session baseline is captured by restore_state (not the runner)
# ──────────────────────────────────────────────────────────

class TestSessionBaseline:
    def test_restore_captures_baseline_so_session_delta_is_zero(self):
        s = _make_strategy()
        # A restored book that has already earned ₹5000 cumulative.
        s.restore_state({
            "strategy": "arbitrage",
            "realized_pnl": 5000.0,
            "unrealized_pnl": 300.0,
            "total_transaction_costs": 120.0,
        })
        report = s.generate_eod_report()
        # Cumulative is reported as-is...
        assert report["realized_pnl"] == 5000.0
        # ...but the session delta is ~0 right after restore — NOT 5000. Without
        # restore_state capturing the baseline, any caller of generate_eod_report
        # would report the whole restored book as a single day's P&L.
        assert report["session_realized_delta"] == 0.0
        assert report["session_unrealized_delta"] == 0.0

    def test_capture_baseline_is_idempotent_to_current_pnl(self):
        s = _make_strategy()
        s.state.realized_pnl = 1000.0
        s.state.unrealized_pnl = -50.0
        s._capture_session_baseline()
        assert s._session_start_realized == 1000.0
        assert s._session_start_unrealized == -50.0


# ──────────────────────────────────────────────────────────
# EOD report leg contract (dashboard reads "qty")
# ──────────────────────────────────────────────────────────

class TestEodReportLegContract:
    def _prop(self, ts, side):
        return TradeProposal(
            tradingsymbol=ts, instrument_token=1, strike=0.0,
            expiry="2026-04-28" if "APR" in ts else "2026-05-26",
            option_type="FUT", lot_size=100, quantity=1, price=100.0,
            transaction_type=side, iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=0.0, rationale="test",
        )

    def test_open_calendar_legs_use_qty_key(self):
        # The frontend ArbitragePage reads l["qty"]; pin that contract so a
        # future rename to "quantity" can't silently render every leg as 0.
        s = _make_strategy(mode="paper")
        s._apply_fill(self._prop("AAA26APRFUT", "BUY"))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL"))
        report = s.generate_eod_report()
        leg = report["open_calendars"][0]["legs"][0]
        assert "qty" in leg
        assert "tradingsymbol" in leg


class TestLiveStatusHandling:
    """Audit 2026-06-10 task 0.3 (C-1, arbitrage copy): same blacklist bug
    as taleb — only FAILED skips _apply_fill, so PENDING/REJECTED book
    fills. Intended contract: _apply_fill on COMPLETE only. xfail markers
    come off with task 1.2."""

    def _run_with_status(self, status):
        s = _make_strategy(mode="live")
        s._live_execute = lambda p: {"order_id": "X1", "status": status, "mode": "live"}
        calls = []
        s._apply_fill = lambda prop, result=None: calls.append(prop)
        prop = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=1, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0.01, margin_required=20000,
            rationale="calendar leg",
        )
        s.execute_proposals([prop])
        return calls

    def test_pending_does_not_apply_fill(self):
        assert self._run_with_status("PENDING") == []

    def test_rejected_does_not_apply_fill(self):
        assert self._run_with_status("REJECTED") == []

    def test_failed_does_not_apply_fill(self):
        assert self._run_with_status("FAILED") == []

    def test_complete_applies_fill(self):
        assert len(self._run_with_status("COMPLETE")) == 1

    def _leg_prop(self):
        return TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=1, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0.01, margin_required=20000,
            rationale="calendar leg",
        )

    def test_live_execute_delegates_to_shared_executor(self):
        # Audit 1.2 step 2: the refusal is gone — _live_execute hands the
        # proposal to the shared KiteOrderExecutor and rebinds the kite
        # client so a runner-side token refresh propagates.
        from unittest.mock import MagicMock
        s = _make_strategy(mode="live")
        executor = MagicMock()
        executor.execute.return_value = {
            "order_id": "X1", "status": "COMPLETE", "filled_lots": 1,
            "average_price": 1001.0, "mode": "live",
        }
        s._live_order_executor = executor
        prop = self._leg_prop()
        result = s._live_execute(prop)
        executor.execute.assert_called_once_with(prop)
        assert executor.kite is s.kite
        assert result["status"] == "COMPLETE"

    def test_order_executor_wiring(self):
        # The lazily-built executor carries arbitrage's identity: per-
        # symbol tags via _symbol_from_tradingsymbol, NFO, the cached
        # instruments dump, and the 0.25 default pad.
        from strategies.order_executor import KiteOrderExecutor
        s = _make_strategy(mode="live")
        # the fixture's config is a MagicMock; emulate "no override in
        # config.ini" so the 0.25 fallback is what's under test
        s.config.getfloat = lambda *a, fallback=None: fallback
        ex = s._order_executor()
        assert isinstance(ex, KiteOrderExecutor)
        assert ex._tag_for(self._leg_prop()) == "arb-AAA"
        assert ex.exchange == "NFO"
        assert ex.limit_protection_pct == 0.25
        assert s._order_executor() is ex  # built once

    def test_apply_fill_books_at_actual_average_price(self):
        # Audit 1.2 step 2: live fills book at the executor's reported
        # average_price, not the proposal quote. Paper results carry no
        # average_price → prop.price (pinned elsewhere).
        s = _make_strategy(mode="paper")
        s._apply_fill(self._leg_prop(), {"average_price": 1003.5})
        trade = next(iter(s.state.open_calendars.values()))
        assert trade.legs[0].entry_price == 1003.5
        assert trade.legs[0].current_price == 1003.5


# ──────────────────────────────────────────────────────────
# Review 2026-07-11: stop-loss, expiry-safe window, ledger integrity
# ──────────────────────────────────────────────────────────

class TestReview20260711:
    """WHY these exist (Rule 9): the forward book's multi-day bleeders ran
    −₹11k…−₹22k against ~₹2-4k entry-time expectations with NO stop below the
    CONVERGE exit, and the JUN-2026 roll produced ±₹16k closed rows priced at
    last-known marks with no flag distinguishing them from verified fills.
    Each test would fail if the corresponding guard were removed."""

    def _open_calendar(self, s, symbol="AAA", entry_time=None, **trade_kw):
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
            **trade_kw,
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
            "basis_annual": 0.0, "basis_annual_next": 0.0,
            "carry_implied": 0.07, "carry_diff": 0.05,
        }
        snap.update(overrides)
        return snap

    # ── STOP_LOSS (thesis invalidation) ────────────────────────────────

    def test_stop_loss_fires_when_mtm_breaches_expected_harvest(self):
        s = _make_strategy(mode="paper")
        s.calendar_stop_loss_mult = 1.0
        self._open_calendar(s, expected_harvest=2000.0)
        # SHORT_CALENDAR: short far leg. Far rallies 101→131 → MTM −3000.
        s._observe_universe = lambda: [self._snap(next_price=131.0)]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("STOP_LOSS" in p.rationale for p in exits)

    def test_stop_loss_holds_above_the_line(self):
        s = _make_strategy(mode="paper")
        s.calendar_stop_loss_mult = 1.0
        self._open_calendar(s, expected_harvest=2000.0)
        # MTM −1500 > −(1.0 × 2000): thesis not yet invalidated → hold.
        s._observe_universe = lambda: [self._snap(next_price=116.0)]
        assert s.check_and_rehedge() == []

    def test_stop_loss_zero_mult_disables(self):
        s = _make_strategy(mode="paper")
        s.calendar_stop_loss_mult = 0.0
        self._open_calendar(s, expected_harvest=2000.0)
        s._observe_universe = lambda: [self._snap(next_price=131.0)]
        assert s.check_and_rehedge() == []

    def test_stop_loss_legacy_trade_falls_back_to_entry_diff(self):
        # Pre-2026-07-11 trades (the 5 currently open on the host) have no
        # expected_harvest; the stop must still cover them via the
        # entry_carry_diff reconstruction, not silently skip them.
        s = _make_strategy(mode="paper")
        s.calendar_stop_loss_mult = 1.0
        self._open_calendar(s, expected_harvest=None)
        # fallback expected = (0.04−0.005) × 10,100 × 15/365 ≈ ₹14.5
        s._observe_universe = lambda: [self._snap(next_price=131.0)]  # MTM −3000
        exits = s.check_and_rehedge()
        assert len(exits) == 2 and all("STOP_LOSS" in p.rationale for p in exits)

    # ── Expiry-safe window ──────────────────────────────────────────────

    def test_entry_blocked_inside_expiry_window(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        # derived window = max(4, 15 + 2) = 17 via the property
        snap = self._snap(dte_near=11, carry_diff=0.05)
        s._observe_universe = lambda: [snap]
        props = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        assert props == [], "dte_near=11 < 17 must not open a 15-day hold"

    def test_entry_allowed_with_full_runway(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        snap = self._snap(dte_near=20, carry_diff=0.05)
        s._observe_universe = lambda: [snap]
        props = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        assert len(props) == 2

    def test_entry_min_dte_computed_from_max_hold(self):
        # Real __init__ wiring (the fixture bypasses it): the window must
        # follow max_hold so shortening the hold widens the entry window.
        s = ArbitrageStrategy(kite=MagicMock(), config_path="/dev/null", mode="paper")
        assert s.calendar_entry_min_dte == max(
            s.calendar_min_dte_near, s.calendar_max_holding_days + 2)

    def test_expiry_force_exit_now_at_dte_2(self):
        s = _make_strategy(mode="paper")
        self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(dte_near=2)]
        exits = s.check_and_rehedge()
        assert len(exits) == 2 and all("EXPIRY" in p.rationale for p in exits)

    # ── Ledger integrity ────────────────────────────────────────────────

    def test_closed_row_carries_exit_metadata(self):
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        self._open_calendar(s, expected_harvest=1234.0)
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        exits = s.check_and_rehedge()          # CONVERGE (debounce=1)
        s.execute_proposals(exits)
        row = s.state.closed_trades[-1]
        assert row["exit_reason"] == "CONVERGE"
        assert row["exit_carry_diff"] == pytest.approx(0.001)
        assert row["expected_harvest"] == pytest.approx(1234.0)
        assert row["pnl_verified"] is True
        # entry 04-15 10:00 → clock 04-17 10:30 ≈ 2.02 days
        assert row["held_days"] == pytest.approx(2.02, abs=0.01)

    def test_fallback_priced_exit_marks_pnl_unverified(self):
        # Near leg missing from the snapshot (rolled off) → exit prices it at
        # last-known and the closed row must say so.
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        self._open_calendar(s)
        s._observe_universe = lambda: [self._snap(near=None, carry_diff=0.001)]
        exits = s.check_and_rehedge()
        s.execute_proposals(exits)
        row = s.state.closed_trades[-1]
        assert row["exit_reason"] == "CONVERGE"
        assert row["pnl_verified"] is False

    def test_entry_records_expected_harvest_on_trade(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        snap = self._snap(dte_near=20, carry_diff=0.05)
        s._observe_universe = lambda: [snap]
        s.execute_proposals(s.scan_and_propose())
        trade = s.state.open_calendars["AAA"]
        # (0.05 − 0.005) × 10,100 × min(19, 15)/365
        assert trade.expected_harvest == pytest.approx(
            0.045 * 10_100 * 15 / 365.0, rel=1e-6)

    def test_serialize_restore_roundtrips_new_fields_and_defaults(self):
        s = _make_strategy(mode="paper")
        self._open_calendar(s, expected_harvest=999.0)
        s.state.open_calendars["AAA"].pnl_verified = False
        blob = s.serialize_state()
        fresh = _make_strategy(mode="paper")
        fresh.restore_state(blob)
        t = fresh.state.open_calendars["AAA"]
        assert t.expected_harvest == pytest.approx(999.0)
        assert t.pnl_verified is False
        # Pre-upgrade blob without the keys → safe defaults.
        for tb in blob["open_calendars"]:
            tb.pop("expected_harvest"); tb.pop("pnl_verified")
        legacy = _make_strategy(mode="paper")
        legacy.restore_state(blob)
        t2 = legacy.state.open_calendars["AAA"]
        assert t2.expected_harvest is None and t2.pnl_verified is True

    def test_restore_warns_on_ledger_drift(self, caplog):
        import logging as _logging
        s = _make_strategy(mode="paper")
        s.state.realized_pnl = 500.0
        s.state.closed_trades.append({
            "symbol": "AAA", "exit_time": datetime(2026, 4, 16, 15, 0),
            "entry_time": datetime(2026, 4, 15, 10, 0),
            "entry_carry_diff": 0.04, "realized_pnl": 100.0,
            "transaction_costs": 50.0, "position": "SHORT_CALENDAR",
        })
        blob = s.serialize_state()
        fresh = _make_strategy(mode="paper")
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            fresh.restore_state(blob)
        assert any("LEDGER DRIFT" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────
# Code-review 2026-07-11 fixes: builder parity, None-quote guard,
# latch reset, stop boundary
# ──────────────────────────────────────────────────────────

class TestReviewFixes20260711:
    """Each test pins a fix from the 2026-07-11 code review and fails if the
    guard is removed (Rule 9)."""

    # Reuse TestReview20260711's fixtures via composition.
    _open_calendar = TestReview20260711._open_calendar
    _snap = TestReview20260711._snap

    # ── F1: backtest __new__ builders must satisfy every dereference ──────

    def test_backtest_builder_supports_scan_and_stop_paths(self):
        # backtest_arbitrage.make_strategy bypasses __init__ via __new__; a
        # new __init__-only attribute silently killed every backtest tick
        # (AttributeError swallowed per-tick → clean 0-trade result). The
        # builder must produce an instance whose scan/rehedge paths run.
        from research import backtest_arbitrage as ba
        import pandas as pd
        from datetime import date as _date
        panel = pd.DataFrame([
            {"date": pd.Timestamp("2026-01-05"), "symbol": "AAA",
             "tradingsymbol": "AAA26JANFUT", "lot_size": 100,
             "expiry": _date(2026, 1, 29), "close": 100.0, "spot": 99.5},
            {"date": pd.Timestamp("2026-01-05"), "symbol": "AAA",
             "tradingsymbol": "AAA26FEBFUT", "lot_size": 100,
             "expiry": _date(2026, 2, 26), "close": 101.0, "spot": 99.5},
        ])
        s = ba.make_strategy(
            ba.MockKiteArb(panel), ["AAA"],
            risk_free_rate=0.07, dividend_yield=0.0, basis_entry_annual=9.99,
            calendar_entry_annual=0.02, calendar_exit_annual=0.005,
            calendar_max_holding_days=15, calendar_min_dte_near=4,
            calendar_max_leg_basis=9.99, basis_min_dte=99, lots_per_leg=1,
            max_open_calendars=5, max_leg_notional=None)
        assert s.calendar_entry_min_dte == 17          # property, not attr
        assert s.calendar_stop_loss_mult == 1.0        # set by the builder
        # AST sweep: EVERY attribute ArbitrageStrategy.__init__ assigns must
        # exist on the __new__-built instance, so the NEXT __init__ addition
        # fails here instead of dying as a swallowed per-tick AttributeError
        # (this sweep is what exposed calendar_margin_pct as already missing
        # since 2026-06-17 — the backtest had been silently dead for weeks).
        import ast
        import inspect
        from strategies import arbitrage as _arb_mod
        tree = ast.parse(inspect.getsource(_arb_mod))
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "ArbitrageStrategy")
        init = next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == "__init__")
        assigned = {t.attr for n in ast.walk(init) for t in ast.walk(n)
                    if isinstance(t, ast.Attribute) and isinstance(t.ctx, ast.Store)
                    and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = {a for a in assigned if not hasattr(s, a)}
        assert not missing, f"make_strategy misses __init__ attrs: {missing}"
        # Fat carry on big notional so the builder's hardcoded 2.0x cost
        # hurdle can't mask the path under test (mirrors the fat-carry
        # fixture in TestCalendarCostHurdle).
        s._observe_universe = lambda: [self._snap(
            dte_near=25, carry_diff=0.50, near_price=2000.0, next_price=2020.0)]
        props = s.scan_and_propose()                   # must not AttributeError
        assert len([p for p in props if p.option_type == "FUT"]) == 2
        self._open_calendar(s, expected_harvest=2000.0)
        s.check_and_rehedge()                          # must not AttributeError

    def test_backtest_all_ticks_failing_raises(self, monkeypatch):
        # Rule 12: a systematic per-tick failure (the AttributeError class of
        # bug) must raise at the end, not return a clean flat 0-trade result
        # that sweeps then treat as a measurement.
        from research import backtest_arbitrage as ba
        import pandas as pd
        from datetime import date as _date
        days = [pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-06")]
        rows = []
        for d in days:
            for ts, exp, px in (("AAA26JANFUT", _date(2026, 1, 29), 100.0),
                                ("AAA26FEBFUT", _date(2026, 2, 26), 101.0)):
                rows.append({"date": d, "symbol": "AAA", "tradingsymbol": ts,
                             "lot_size": 100, "expiry": exp, "close": px,
                             "spot": 99.5})
        panel = pd.DataFrame(rows)

        def _boom(self):
            raise AttributeError("simulated missing-attribute bug")
        monkeypatch.setattr(ArbitrageStrategy, "scan_and_propose", _boom)
        with pytest.raises(RuntimeError, match="ticks raised"):
            ba.run_backtest(panel)

    # ── F3: None next-month quote must not abort the tick ─────────────────

    def test_none_next_price_holds_mark_instead_of_crashing(self):
        s = _make_strategy(mode="paper")
        trade = self._open_calendar(s)
        far = trade.legs[1]
        far.current_price = 103.0                      # last known mark
        snap = self._snap(carry_diff=0.05)
        snap["next_price"] = None                      # transient quote gap
        s._observe_universe = lambda: [snap]
        s.check_and_rehedge()                          # used to TypeError
        assert far.current_price == 103.0, "stale mark must hold, not become None/0"

    # ── F4: pnl_verified latch resets per exit attempt ─────────────────────

    def test_pnl_verified_latch_resets_on_clean_exit(self):
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        trade = self._open_calendar(s)
        trade.pnl_verified = False       # a previous aborted attempt latched it
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        s.execute_proposals(s.check_and_rehedge())     # clean CONVERGE exit
        assert s.state.closed_trades[-1]["pnl_verified"] is True, \
            "a clean exit must not inherit a stale False latch"

    # ── F8: legacy-fallback stop boundary (pins the reconstruction) ───────

    def test_legacy_stop_boundary_pins_fallback_formula(self):
        # fallback expected = (0.04 − 0.005) × 10,100 × 15/365 ≈ ₹14.53.
        # Just above the line holds; just below fires. A ~100x inflation or
        # deflation of the reconstruction breaks one of the two assertions.
        s = _make_strategy(mode="paper")
        s.calendar_stop_loss_mult = 1.0
        self._open_calendar(s, expected_harvest=None)
        # far leg short 1 lot of 100: mtm = (px − 101) × (−100)
        s._observe_universe = lambda: [self._snap(next_price=101.10)]  # mtm −10
        assert s.check_and_rehedge() == [], "−10 > −14.53: must hold"
        s._observe_universe = lambda: [self._snap(next_price=101.20)]  # mtm −20
        exits = s.check_and_rehedge()
        assert len(exits) == 2 and all("STOP_LOSS" in p.rationale for p in exits)


# ──────────────────────────────────────────────────────────
# Issue #222: quoted depth logged at fill time
# ──────────────────────────────────────────────────────────

def _quote(bid, ask, ltp, bid_qty=500, ask_qty=400):
    """A Kite quote row shaped like the live feed's."""
    return {
        "last_price": ltp,
        "depth": {
            "buy": [{"price": bid, "quantity": bid_qty, "orders": 3},
                    {"price": bid - 1, "quantity": 900, "orders": 5}],
            "sell": [{"price": ask, "quantity": ask_qty, "orders": 2},
                     {"price": ask + 1, "quantity": 700, "orders": 4}],
        },
    }


class TestDepthLogging:
    """WHY these exist (Rule 9): the paper book fills at last_price and the
    cost model charges a flat 2bps of slippage per side, while the measured
    far-month touch is 8-21bps wide. Re-pricing the 38 verified trades at the
    real touch turned +₹68,496 into −₹33,469 — the whole live/no-live question
    turns on a number we were not recording. Each test fails if the touch stops
    reaching the closed_trades row, or starts claiming a spread it never
    measured."""

    _open_calendar = TestReview20260711._open_calendar
    _snap = TestReview20260711._snap

    def _snap_with_depth(self, near=(99.9, 100.1), far=(100.7, 101.3), **kw):
        return self._snap(
            near_quote=ArbitrageStrategy._touch(_quote(near[0], near[1], 100.0)),
            next_quote=ArbitrageStrategy._touch(_quote(far[0], far[1], 101.0)),
            **kw)

    # ── the extractor ────────────────────────────────────────────────────

    def test_touch_reads_depth_one_not_the_whole_book(self):
        t = ArbitrageStrategy._touch(_quote(99.9, 100.1, 100.0,
                                            bid_qty=500, ask_qty=400))
        # Depth-2 rows (99.9−1 / 100.1+1) must not leak into the touch: an
        # order crosses level 1, so a wider level would understate nothing
        # and overstate everything.
        assert t == {"bid": 99.9, "ask": 100.1, "bid_qty": 500,
                     "ask_qty": 400, "ltp": 100.0}

    @pytest.mark.parametrize("quote, why", [
        (None, "no quote at all"),
        ({"last_price": 100.0}, "no depth — the backtest's MockKiteArb feed"),
        ({"last_price": 100.0, "depth": {"buy": [], "sell": []}}, "empty book"),
        ({"last_price": 100.0,
          "depth": {"buy": [{"price": 0, "quantity": 0}],
                    "sell": [{"price": 100.1, "quantity": 5}]}}, "no bid"),
        ({"last_price": 100.0,
          "depth": {"buy": [{"price": 100.5, "quantity": 5}],
                    "sell": [{"price": 100.1, "quantity": 5}]}}, "crossed book"),
        ({"last_price": 100.0,
          "depth": {"buy": [{"price": 100.1, "quantity": 5}],
                    "sell": [{"price": 100.1, "quantity": 5}]}}, "locked book"),
    ])
    def test_touch_is_none_when_the_book_is_not_measurable(self, quote, why):
        # None must mean "not measurable", never "zero spread" — a re-pricing
        # analysis that read an unusable book as free would reproduce exactly
        # the optimism this issue exists to remove (Rule 12).
        assert ArbitrageStrategy._touch(quote) is None, why

    def test_snapshot_carries_the_touch_from_the_live_feed(self):
        # End-to-end through the real _observe_universe_uncached: the depth
        # kite.quote() already returns was being thrown away here.
        s = _make_strategy(mode="paper", universe=["AAA"])
        s.kite.instruments.return_value = [
            {"name": "AAA", "tradingsymbol": "AAA26APRFUT", "instrument_type": "FUT",
             "expiry": date(2026, 4, 28), "lot_size": 100, "segment": "NFO-FUT"},
            {"name": "AAA", "tradingsymbol": "AAA26MAYFUT", "instrument_type": "FUT",
             "expiry": date(2026, 5, 26), "lot_size": 100, "segment": "NFO-FUT"},
        ]
        s.kite.quote.side_effect = lambda keys: {
            "NSE:AAA": {"last_price": 99.5},
            "NFO:AAA26APRFUT": _quote(99.9, 100.1, 100.0),
            "NFO:AAA26MAYFUT": _quote(100.7, 101.3, 101.0),
        }
        snap = s._observe_universe_uncached()[0]
        assert snap["near_quote"]["ask"] == 100.1
        assert snap["next_quote"]["bid"] == 100.7

    # ── the ledger row ───────────────────────────────────────────────────

    def test_closed_row_carries_both_ends_of_every_leg(self):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02,
                           calendar_exit_annual=0.005)
        s._observe_universe = lambda: [self._snap_with_depth(
            dte_near=20, carry_diff=0.05)]
        s.execute_proposals(s.scan_and_propose())
        # Exit a tick later on a DIFFERENT book — the row must record the
        # touch each end actually crossed, not the entry's twice.
        s._observe_universe = lambda: [self._snap_with_depth(
            near=(99.5, 99.7), far=(100.2, 100.8), carry_diff=0.001)]
        s.execute_proposals(s.check_and_rehedge())

        q = s.state.closed_trades[-1]["leg_quotes"]
        assert set(q) == {"AAA26APRFUT", "AAA26MAYFUT"}
        assert q["AAA26APRFUT"]["entry"]["ask"] == 100.1
        assert q["AAA26APRFUT"]["exit"]["ask"] == 99.7
        assert q["AAA26MAYFUT"]["entry"]["bid"] == 100.7
        assert q["AAA26MAYFUT"]["exit"]["bid"] == 100.2
        # The spread this trade would have paid, from the row alone: the
        # point of the whole exercise.
        entry = q["AAA26APRFUT"]["entry"]
        assert entry["ask"] - entry["bid"] == pytest.approx(0.2)

    def test_row_is_still_written_when_the_feed_has_no_depth(self):
        # The backtest and any signals-only feed carry no depth. That must
        # degrade to "not measured", never break the ledger.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02,
                           calendar_exit_annual=0.005)
        s._observe_universe = lambda: [self._snap(dte_near=20, carry_diff=0.05)]
        s.execute_proposals(s.scan_and_propose())
        s._observe_universe = lambda: [self._snap(carry_diff=0.001)]
        s.execute_proposals(s.check_and_rehedge())

        row = s.state.closed_trades[-1]
        assert row["realized_pnl"] is not None
        assert row["leg_quotes"]["AAA26APRFUT"] == {"entry": None, "exit": None}

    def test_exit_touch_is_restamped_on_a_later_attempt(self):
        # Same reason pnl_verified re-stamps per attempt (F4): a debounced or
        # rejected attempt must not leave its stale touch standing in place of
        # the tick that actually fills.
        s = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        trade = self._open_calendar(s)
        # First attempt: the far leg has rolled out of the snapshot — no touch.
        s._observe_universe = lambda: [self._snap_with_depth(
            carry_diff=0.001, next=None, next_price=None)]
        s.check_and_rehedge()
        assert trade.leg_quotes["AAA26MAYFUT"]["exit"] is None
        # Second attempt, clean book: the recorded touch must be this one.
        s._observe_universe = lambda: [self._snap_with_depth(
            far=(100.2, 100.8), carry_diff=0.001)]
        s.execute_proposals(s.check_and_rehedge())
        assert s.state.closed_trades[-1]["leg_quotes"]["AAA26MAYFUT"]["exit"]["ask"] \
            == 100.8

    def test_entry_touch_survives_the_session_boundary(self):
        # A calendar opened today usually exits days later, so the entry touch
        # has to round-trip through the state file or the closed row can never
        # carry both ends.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s._observe_universe = lambda: [self._snap_with_depth(
            dte_near=20, carry_diff=0.05)]
        s.execute_proposals(s.scan_and_propose())

        fresh = _make_strategy(mode="paper", calendar_exit_annual=0.005)
        fresh.restore_state(s.serialize_state())
        restored = fresh.state.open_calendars["AAA"]
        assert restored.leg_quotes["AAA26APRFUT"]["entry"]["bid"] == 99.9

        fresh._observe_universe = lambda: [self._snap_with_depth(carry_diff=0.001)]
        fresh.execute_proposals(fresh.check_and_rehedge())
        q = fresh.state.closed_trades[-1]["leg_quotes"]["AAA26APRFUT"]
        assert q["entry"]["bid"] == 99.9 and q["exit"] is not None

    def test_a_restored_trade_does_not_share_sub_dicts_with_the_blob(self):
        # Restoring from a dict the caller still holds — a reconcile or repair
        # script, not the runner's JSON round-trip — must copy the per-leg
        # ends, or a later edit to the blob silently rewrites a live trade's
        # recorded touch.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s._observe_universe = lambda: [self._snap_with_depth(
            dte_near=20, carry_diff=0.05)]
        s.execute_proposals(s.scan_and_propose())
        blob = s.serialize_state()
        fresh = _make_strategy(mode="paper")
        fresh.restore_state(blob)
        blob["open_calendars"][0]["leg_quotes"]["AAA26APRFUT"]["entry"] = None
        assert fresh.state.open_calendars["AAA"].leg_quotes[
            "AAA26APRFUT"]["entry"] is not None

    def test_the_serialized_blob_is_not_a_window_into_live_state(self):
        # The other direction of the same aliasing: a caller that edits the
        # blob it got back — a reconcile or repair script — must not reach
        # through into the open trade's recorded touches.
        s = _make_strategy(mode="paper", calendar_entry_annual=0.02)
        s._observe_universe = lambda: [self._snap_with_depth(
            dte_near=20, carry_diff=0.05)]
        s.execute_proposals(s.scan_and_propose())
        blob = s.serialize_state()
        blob["open_calendars"][0]["leg_quotes"]["AAA26APRFUT"]["entry"] = None
        assert s.state.open_calendars["AAA"].leg_quotes[
            "AAA26APRFUT"]["entry"] is not None

    def test_restore_of_a_pre_222_blob_reads_as_not_measured(self):
        s = _make_strategy(mode="paper")
        self._open_calendar(s)
        blob = s.serialize_state()
        del blob["open_calendars"][0]["leg_quotes"]      # written before #222
        fresh = _make_strategy(mode="paper")
        fresh.restore_state(blob)
        assert fresh.state.open_calendars["AAA"].leg_quotes == {}
# Issue #222: entry-batch atomicity + margin precheck
# ──────────────────────────────────────────────────────────

def _leg(symbol, month, side, lots=1, price=1000.0):
    return TradeProposal(
        tradingsymbol=f"{symbol}26{month}FUT", instrument_token=1, strike=0,
        expiry="2026-04-28" if month == "APR" else "2026-05-26",
        option_type="FUT", lot_size=100, quantity=lots, price=price,
        transaction_type=side, iv=0, bid_ask_spread_pct=0.01,
        margin_required=20_000, rationale="calendar leg",
    )


class _Executor:
    """Stub _live_execute: returns REJECTED for the named contracts."""

    def __init__(self, reject=()):
        self.reject = set(reject)
        self.placed = []

    def __call__(self, prop):
        self.placed.append((prop.tradingsymbol, prop.transaction_type))
        if prop.tradingsymbol in self.reject:
            return {"order_id": None, "status": "REJECTED", "error": "margin"}
        return {"order_id": "X", "status": "COMPLETE", "mode": "live",
                "average_price": prop.price}


class TestEntryBatchAtomicity:
    """WHY these exist (Rule 9): execute_proposals booked each leg's fill
    independently, so leg 2 rejecting after leg 1 filled left a NAKED single
    future — an outright ~₹650k directional position that no exit path in this
    strategy manages, on a book whose whole thesis is that the two legs hedge
    each other. Paper never saw it because _paper_execute always returns
    COMPLETE. Each test fails if the guard is removed."""

    def _live(self, **kw):
        s = _make_strategy(mode="live", **kw)
        # Margin gate out of the way unless a test is exercising it: None is
        # the "balance unreadable → proceed" path.
        s._available_margin = lambda: None
        return s

    def test_partial_entry_is_reversed_to_flat(self):
        s = self._live()
        s._live_execute = ex = _Executor(reject={"AAA26MAYFUT"})
        s.execute_proposals([_leg("AAA", "APR", "BUY"),
                             _leg("AAA", "MAY", "SELL")])
        assert s.state.open_calendars == {}, \
            "a half-filled calendar must not survive as a naked leg"
        # The reversal is the opposite side of the leg that DID fill.
        assert ("AAA26APRFUT", "SELL") in ex.placed

    def test_partial_entry_logs_critical(self, caplog):
        s = self._live()
        s._live_execute = _Executor(reject={"AAA26MAYFUT"})
        with caplog.at_level(_logging.CRITICAL, logger="strategies.arbitrage"):
            s.execute_proposals([_leg("AAA", "APR", "BUY"),
                                 _leg("AAA", "MAY", "SELL")])
        assert any("ENTRY BATCH PARTIAL FILL" in r.message for r in caplog.records)

    def test_reversal_row_is_marked_in_the_ledger(self):
        s = self._live()
        s._live_execute = _Executor(reject={"AAA26MAYFUT"})
        s.execute_proposals([_leg("AAA", "APR", "BUY"),
                             _leg("AAA", "MAY", "SELL")])
        row = s.state.closed_trades[-1]
        assert row["exit_reason"] == "UNWIND_PARTIAL_BATCH", \
            "a cost-only scratch must not read as a traded-and-exited calendar"
        assert row["realized_pnl"] < 0        # two round-trip costs, no edge

    def test_failed_reversal_screams(self, caplog):
        # Both the entry leg's pair AND the unwind reject: the position is
        # genuinely naked and the operator has to square it by hand. Fail loud
        # (Rule 12) — never let this look like a clean skip.
        s = self._live()
        s._live_execute = _Executor(reject={"AAA26MAYFUT", "AAA26APRFUT"})
        # First call fills APR, the reversal of APR then rejects.
        calls = {"n": 0}

        def _exec(prop):
            calls["n"] += 1
            if prop.tradingsymbol == "AAA26APRFUT" and calls["n"] == 1:
                return {"order_id": "X", "status": "COMPLETE",
                        "average_price": prop.price}
            return {"order_id": None, "status": "REJECTED", "error": "boom"}

        s._live_execute = _exec
        with caplog.at_level(_logging.CRITICAL, logger="strategies.arbitrage"):
            s.execute_proposals([_leg("AAA", "APR", "BUY"),
                                 _leg("AAA", "MAY", "SELL")])
        assert any("NAKED LEG IN MARKET" in r.message for r in caplog.records)
        assert s.state.open_calendars["AAA"].legs, \
            "the unreversed leg must stay on the book, not vanish silently"

    def test_clean_entry_is_untouched(self):
        s = self._live()
        s._live_execute = ex = _Executor()
        s.execute_proposals([_leg("AAA", "APR", "BUY"),
                             _leg("AAA", "MAY", "SELL")])
        assert len(s.state.open_calendars["AAA"].legs) == 2
        assert len(ex.placed) == 2, "no reversal orders on a clean batch"

    def test_atomicity_is_per_calendar_not_per_tick(self):
        # One scan tick proposes entries for two underlyings. BBB's second leg
        # rejects; AAA is a clean, unrelated spread and must NOT be unwound.
        s = self._live()
        s._live_execute = _Executor(reject={"BBB26MAYFUT"})
        s.execute_proposals([
            _leg("AAA", "APR", "BUY"), _leg("AAA", "MAY", "SELL"),
            _leg("BBB", "APR", "BUY"), _leg("BBB", "MAY", "SELL"),
        ])
        assert len(s.state.open_calendars["AAA"].legs) == 2
        assert "BBB" not in s.state.open_calendars

    def test_a_half_filled_exit_is_not_reversed(self):
        # An exit that half-fills leaves a position we still OWN — re-buying
        # the leg we just closed would re-open risk. The next tick's
        # check_and_rehedge re-proposes the remainder instead.
        s = self._live()
        s._live_execute = _Executor()
        s.execute_proposals([_leg("AAA", "APR", "BUY"),
                             _leg("AAA", "MAY", "SELL")])
        s._live_execute = ex = _Executor(reject={"AAA26MAYFUT"})
        ex.placed.clear()
        s.execute_proposals([_leg("AAA", "APR", "SELL"),
                             _leg("AAA", "MAY", "BUY")])
        assert [p for p in ex.placed if p == ("AAA26APRFUT", "BUY")] == [], \
            "the closed leg must not be re-bought"
        assert len(s.state.open_calendars["AAA"].legs) == 1


class TestMarginPrecheck:
    """WHY (Rule 9): leg 2 rejecting on margin AFTER leg 1 filled is the main
    way a calendar goes naked, and this book never asked the broker for a
    margin number at all. Refusing the batch is free; a mid-batch reject costs
    a reversal at market."""

    def _live(self):
        s = _make_strategy(mode="live")
        s._live_execute = _Executor()
        return s

    def _props(self):
        return [_leg("AAA", "APR", "BUY"), _leg("AAA", "MAY", "SELL")]

    def _margins(self, net):
        return {"equity": {"net": net,
                           "available": {"live_balance": net, "collateral": 0}}}

    def _basket(self, total):
        return {"initial": {"total": total}, "final": {"total": total}}

    def test_batch_is_refused_when_the_broker_cannot_fund_it(self):
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(50_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._basket(70_000))
        s.execute_proposals(self._props())
        assert s._live_execute.placed == [], "nothing may be placed"
        assert s.state.open_calendars == {}

    def test_batch_proceeds_when_funded(self):
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(500_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._basket(70_000))
        s.execute_proposals(self._props())
        assert len(s.state.open_calendars["AAA"].legs) == 2

    def test_gate_uses_the_peak_not_the_settled_basket_figure(self):
        # Legs are placed sequentially, so the pre-benefit requirement is what
        # has to clear. Taking `final` alone would wave through a batch that
        # rejects on leg 2.
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(100_000))
        s.kite.basket_order_margins = MagicMock(return_value={
            "initial": {"total": 200_000}, "final": {"total": 70_000}})
        s.execute_proposals(self._props())
        assert s._live_execute.placed == []

    def test_broker_quote_beats_the_static_estimate(self):
        # The Σ estimate is 2 × ₹20k = ₹40k; the broker says ₹200k. Gating on
        # the estimate would place a batch the account cannot fund — the whole
        # reason this calls basket_order_margins.
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(100_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._basket(200_000))
        s.execute_proposals(self._props())
        assert s._live_execute.placed == []

    def test_margins_flake_does_not_block_the_book(self):
        # Don't-block-on-flake (pair_trading H15): the reversal is the backstop
        # for a post-fact reject; a broken margins() must not halt trading.
        s = self._live()
        s.kite.margins = MagicMock(side_effect=RuntimeError("net down"))
        s.execute_proposals(self._props())
        assert len(s.state.open_calendars["AAA"].legs) == 2

    def test_basket_flake_falls_back_to_the_estimate(self):
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(30_000))
        s.kite.basket_order_margins = MagicMock(side_effect=RuntimeError("boom"))
        s.execute_proposals(self._props())
        # Σ estimate ₹40k × headroom is NOT applied to the fallback, but ₹40k
        # already exceeds ₹30k available → refused rather than placed blind.
        assert s._live_execute.placed == []

    def test_vacuous_basket_quote_falls_back_to_the_estimate(self):
        # Zeroed totals from a degraded RMS response would pass any gate
        # trivially; trusting them would book a phantom-zero requirement.
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(30_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._basket(0))
        s.execute_proposals(self._props())
        assert s._live_execute.placed == []

    def test_paper_never_calls_the_broker(self):
        s = _make_strategy(mode="paper")
        s.kite.margins = MagicMock(side_effect=AssertionError("paper called margins()"))
        s.kite.basket_order_margins = MagicMock(
            side_effect=AssertionError("paper called basket_order_margins()"))
        s.execute_proposals(self._props())
        assert len(s.state.open_calendars["AAA"].legs) == 2

    def test_exits_are_never_prechecked(self):
        # We already own the position; refusing to exit on a margin reading
        # would trap the book in a trade it has decided to leave.
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(500_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._basket(70_000))
        s.execute_proposals(self._props())
        s.kite.margins = MagicMock(side_effect=AssertionError("exit was prechecked"))
        s.execute_proposals([_leg("AAA", "APR", "SELL"), _leg("AAA", "MAY", "BUY")])
        assert s.state.open_calendars == {}


class TestMarginPrecheckReviewFixes:
    """WHY (Rule 9): the first cut of the gate was ported from pair_trading
    verbatim and inherited two assumptions that invert for a calendar. Both
    were caught in review of PR #224; each test fails if the fix is reverted."""

    def _live(self):
        s = _make_strategy(mode="live")
        s._live_execute = _Executor()
        return s

    def _props(self, symbol="AAA"):
        return [_leg(symbol, "APR", "BUY"), _leg(symbol, "MAY", "SELL")]

    def _margins(self, net):
        return {"equity": {"net": net, "available": {}}}

    # ── the peak is one leg outright, NOT the un-netted sum of both ────────

    def _calendar_basket(self):
        # Shaped on the real 2026-09-07 TECHM quote: initial is the fully
        # un-netted sum of both legs, final carries the spread benefit.
        return {"initial": {"total": 207_159}, "final": {"total": 33_284},
                "orders": [{"total": 103_500}, {"total": 103_659}]}

    def test_funded_calendar_is_not_refused_on_the_unnetted_sum(self):
        # ₹150k funds a spread whose real peak is one ₹103.7k leg (×1.05 =
        # ₹108.8k). Gating on initial (₹207k) would refuse it — ~6x the
        # netted requirement, defeating the calendar benefit the basket call
        # exists to capture.
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(150_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._calendar_basket())
        s.execute_proposals(self._props())
        assert len(s.state.open_calendars["AAA"].legs) == 2

    def test_peak_still_gates_when_one_leg_alone_is_unaffordable(self):
        # ₹90k cannot carry the ₹103.7k first leg, even though the settled
        # basket figure (₹33.3k) would fit — gating on `final` alone would
        # place a batch whose leg 1 rejects.
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(90_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._calendar_basket())
        s.execute_proposals(self._props())
        assert s._live_execute.placed == []

    def test_missing_per_leg_totals_fall_back_to_the_conservative_shape(self):
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(150_000))
        s.kite.basket_order_margins = MagicMock(return_value={
            "initial": {"total": 207_159}, "final": {"total": 33_284}})
        s.execute_proposals(self._props())
        assert s._live_execute.placed == [], \
            "without per-leg totals the gate must stay conservative"

    # ── balance read once per call, decremented across batches ────────────

    def test_balance_is_decremented_across_batches_in_one_tick(self):
        # A single scan proposes entries for two underlyings. ₹150k funds the
        # first (₹108.8k with headroom) but not both; re-reading margins() per
        # group would let the second through against a balance that does not
        # yet reflect the first — the exact mid-batch reject this gate exists
        # to prevent.
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(150_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._calendar_basket())
        s.execute_proposals(self._props("AAA") + self._props("BBB"))
        assert "AAA" in s.state.open_calendars
        assert "BBB" not in s.state.open_calendars
        assert s.kite.margins.call_count == 1, "balance must be read once per call"

    def test_both_batches_pass_when_the_balance_actually_covers_them(self):
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(500_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._calendar_basket())
        s.execute_proposals(self._props("AAA") + self._props("BBB"))
        assert len(s.state.open_calendars) == 2

    def test_no_broker_call_at_all_when_the_tick_is_exits_only(self):
        s = self._live()
        s.kite.margins = MagicMock(return_value=self._margins(500_000))
        s.kite.basket_order_margins = MagicMock(return_value=self._calendar_basket())
        s.execute_proposals(self._props())
        s.kite.margins.reset_mock()
        s.execute_proposals([_leg("AAA", "APR", "SELL"), _leg("AAA", "MAY", "BUY")])
        assert s.kite.margins.call_count == 0


class TestUnwindRowDirection:
    """WHY (Rule 9): _apply_fill only finalizes `position` once BOTH legs are
    on the trade, which never happens for a half-filled entry — so the unwind
    row archived as the CalendarTrade default and a rejected SHORT_CALENDAR
    was counted on the long side by anything segmenting closed_trades by
    direction (sweeps, the decay ledger)."""

    def test_unwound_short_calendar_is_not_recorded_as_long(self):
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor(reject={"AAA26MAYFUT"})
        # BUY near + SELL far = SHORT_CALENDAR, per _apply_fill's convention.
        s.execute_proposals([_leg("AAA", "APR", "BUY"),
                             _leg("AAA", "MAY", "SELL")])
        assert s.state.closed_trades[-1]["position"] == "SHORT_CALENDAR"

    def test_unwound_long_calendar_is_recorded_as_long(self):
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor(reject={"AAA26MAYFUT"})
        s.execute_proposals([_leg("AAA", "APR", "SELL"),
                             _leg("AAA", "MAY", "BUY")])
        assert s.state.closed_trades[-1]["position"] == "LONG_CALENDAR"


class TestUnwindRowQuoteShape:
    """WHY (Rule 9): the reversal path closes a trade through _apply_fill
    directly, never through _build_calendar_exit — the only place an exit
    touch is stamped. Without an explicit stamp the unwind row carries
    {"entry": ...} and no "exit" key, where every other closed row has both
    ends: a re-pricing scorer doing q["exit"] raises KeyError, and one doing
    q.get("exit") silently keeps a cost-only scratch in the spread sample.
    Interaction between the two halves of #222 — invisible until both landed."""

    def _entry_with_depth(self):
        near = _leg("AAA", "APR", "BUY")
        far = _leg("AAA", "MAY", "SELL")
        return [near, far]

    def test_unwound_row_carries_both_keys_for_every_leg(self):
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor(reject={"AAA26MAYFUT"})
        # Entry touch present for the leg that filled, as a real entry would.
        s.state.pending_entry_quotes["AAA26APRFUT"] = {
            "bid": 99.9, "ask": 100.1, "bid_qty": 5, "ask_qty": 5, "ltp": 100.0}
        s.execute_proposals(self._entry_with_depth())

        q = s.state.closed_trades[-1]["leg_quotes"]["AAA26APRFUT"]
        assert set(q) == {"entry", "exit"}, \
            "an unwind row must have the same shape as every other closed row"
        assert q["entry"]["ask"] == 100.1        # the entry touch is preserved
        assert q["exit"] is None                 # never quoted for an exit

    def test_the_stamp_does_not_overwrite_a_real_exit_touch(self):
        # setdefault, not assignment: if a leg ever did go through
        # _build_calendar_exit before landing here, its measured touch wins.
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor(reject={"AAA26MAYFUT"})
        real = {"bid": 1.0, "ask": 2.0, "bid_qty": 1, "ask_qty": 1, "ltp": 1.5}

        orig = s._apply_fill

        def _seed(prop, result=None):
            orig(prop, result)
            trade = s.state.open_calendars.get("AAA")
            if trade is not None:
                trade.leg_quotes.setdefault(
                    "AAA26APRFUT", {})["exit"] = real

        s._apply_fill = _seed
        s.execute_proposals(self._entry_with_depth())
        assert s.state.closed_trades[-1][
            "leg_quotes"]["AAA26APRFUT"]["exit"] == real


class TestUniverseResolution:
    """WHY (Rule 9, issue #226): a universe symbol with no futures was skipped
    by the scan loop exactly like a symbol with no signal, so a typo, a rename,
    a delisting and an F&O exit were indistinguishable from a quiet day.
    TATAMOTORS and LTIM sat in the list for 10.5 and 6.4 months on that basis."""

    def _instruments(self, names):
        return [{"name": n, "tradingsymbol": f"{n}26APRFUT",
                 "instrument_type": "FUT", "expiry": date(2026, 4, 28),
                 "lot_size": 100, "segment": "NFO-FUT"} for n in names]

    def test_missing_symbol_is_named_in_a_warning(self, caplog):
        s = _make_strategy(mode="paper", universe=["AAA", "DELISTED"])
        s.kite.instruments.return_value = self._instruments(["AAA"])
        s.kite.quote.return_value = {}
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            s._observe_universe_uncached()
        assert any("DELISTED" in r.getMessage() for r in caplog.records), \
            "a count alone is not actionable — the warning must name the symbol"

    def test_it_does_not_refuse_to_run(self, caplog):
        # Operator decision 2026-09-09: warn everywhere, refuse nowhere. A
        # delisting must not stop the book managing what it already holds.
        s = _make_strategy(mode="paper", universe=["AAA", "DELISTED"])
        s.kite.instruments.return_value = self._instruments(["AAA"])
        s.kite.quote.return_value = {}
        s._observe_universe_uncached()          # must not raise

    def test_warned_once_per_session_not_once_per_tick(self, caplog):
        # The scan runs every few seconds all session; a per-tick warning would
        # bury the thing it is trying to surface.
        s = _make_strategy(mode="paper", universe=["AAA", "DELISTED"])
        s.kite.instruments.return_value = self._instruments(["AAA"])
        s.kite.quote.return_value = {}
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            for _ in range(5):
                s._observe_universe_uncached()
        hits = [r for r in caplog.records if "DELISTED" in r.getMessage()]
        assert len(hits) == 1, f"expected exactly one warning, got {len(hits)}"

    def test_a_fully_resolvable_universe_is_silent(self, caplog):
        s = _make_strategy(mode="paper", universe=["AAA"])
        s.kite.instruments.return_value = self._instruments(["AAA", "EXTRA"])
        s.kite.quote.return_value = {}
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            s._observe_universe_uncached()
        assert not [r for r in caplog.records if "universe" in r.getMessage()]


class TestOpenCalendarStaysObservable:
    """WHY (Rule 9, review of PR #227): check_and_rehedge is the ONLY exit path
    and it needs a snapshot to fire EXPIRY (cash-settlement), MAX_HOLD or
    STOP_LOSS. Snapshots came only from `self.universe`, so removing a departed
    symbol from the list — exactly what scripts/reconcile_universe.py tells the
    operator to do — orphaned any open calendar on it, silently, all the way to
    settlement."""

    _open_calendar = TestReview20260711._open_calendar

    def _snap_for(self, symbol):
        return {
            "symbol": symbol, "spot": 99.5,
            "near": {"tradingsymbol": f"{symbol}26APRFUT", "lot_size": 100,
                     "expiry": "2026-04-28", "instrument_token": 1},
            "near_price": 100.0, "dte_near": 1,          # inside the EXPIRY zone
            "next": {"tradingsymbol": f"{symbol}26MAYFUT", "lot_size": 100,
                     "expiry": "2026-05-26", "instrument_token": 2},
            "next_price": 101.0, "dte_next": 29,
            "basis_annual": 0.0, "basis_annual_next": 0.0,
            "carry_implied": 0.07, "carry_diff": 0.05,
        }

    def test_an_off_universe_symbol_still_reaches_its_expiry_exit(self):
        # Given a snapshot, the exit path itself does not care about the
        # universe. The separate test below is the one that pins the union
        # actually producing that snapshot — this one would pass without it.
        s = _make_strategy(mode="paper", universe=["BBB"])   # AAA was removed
        self._open_calendar(s, symbol="AAA")
        s._observe_universe = lambda: [self._snap_for("AAA")]
        exits = s.check_and_rehedge()
        assert len(exits) == 2 and all("EXPIRY" in p.rationale for p in exits), \
            "an open calendar must still reach its expiry force-exit"

    def test_the_scan_covers_held_symbols_outside_the_universe(self):
        s = _make_strategy(mode="paper", universe=["BBB"])
        self._open_calendar(s, symbol="AAA")
        s.kite.instruments.return_value = []
        scanned = []
        # _observe_universe_uncached iterates the union; with no instruments it
        # returns early, so assert on the union it builds rather than output.
        s.kite.instruments.return_value = [
            {"name": n, "tradingsymbol": f"{n}26APRFUT", "instrument_type": "FUT",
             "expiry": date(2026, 4, 28), "lot_size": 100, "segment": "NFO-FUT"}
            for n in ("AAA", "BBB")]
        s.kite.quote.side_effect = lambda keys: scanned.extend(keys) or {}
        s._observe_universe_uncached()
        assert any("AAA" in k for k in scanned), \
            "the held symbol must be quoted even though it left the universe"

    def test_an_unpriceable_open_calendar_screams_once(self, caplog):
        s = _make_strategy(mode="paper", universe=["BBB"])
        self._open_calendar(s, symbol="AAA")
        s._observe_universe = lambda: []          # no snapshot at all
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            for _ in range(3):
                s.check_and_rehedge()
        hits = [r for r in caplog.records if "OPEN CALENDAR AAA" in r.getMessage()]
        assert len(hits) == 1, \
            f"expected exactly one warning per symbol per session, got {len(hits)}"

    def test_a_held_off_universe_symbol_cannot_be_re_entered(self):
        # The entry gate already requires `symbol not in open_calendars`; this
        # pins that observing extra symbols does not widen what we may OPEN.
        s = _make_strategy(mode="paper", universe=["BBB"], calendar_entry_annual=0.001)
        self._open_calendar(s, symbol="AAA")
        s._observe_universe = lambda: [self._snap_for("AAA")]
        entries = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        assert entries == [], "an off-universe symbol may be exited, never entered"


# ──────────────────────────────────────────────────────────
# Issue #228: price from the book, not the print
# ──────────────────────────────────────────────────────────

class TestPriceFromTheBook:
    """WHY (Rule 9): the strategy priced its signal, its fills and its marks off
    `last_price`. On a far-month single-stock future that print goes stale, and
    a stale print does not merely add noise — it INVERTS the term structure.

    On 2026-09-09, GRASIM's OCT print sat 22 points below its own bid. The
    strategy saw the far month 9 points cheaper than the near (carry_diff
    −10.63%, threshold 5%) when it was in fact 16 points dearer (real −0.73%).
    Two entries fired that the real book put nowhere near the bar; re-priced at
    the touch the day went +₹9,778 paper → −₹6,224 live."""

    def _q(self, ltp, bid, ask, qty=250):
        return {"last_price": ltp,
                "depth": {"buy": [{"price": bid, "quantity": qty}],
                          "sell": [{"price": ask, "quantity": qty}]}}

    # ── the primitive ────────────────────────────────────────────────────
    def test_mid_is_used_when_there_is_a_book(self):
        q = self._q(3300.8, 3323.0, 3328.2)
        assert ArbitrageStrategy._book_price(q, ArbitrageStrategy._touch(q)) == pytest.approx(3325.6)

    def test_last_price_is_the_fallback_without_depth(self):
        # The backtest's MockKiteArb and any signals-only feed publish no
        # depth; their behaviour must not change.
        q = {"last_price": 100.0}
        assert ArbitrageStrategy._book_price(q, ArbitrageStrategy._touch(q)) == 100.0

    def test_no_quote_no_price(self):
        assert ArbitrageStrategy._book_price(None, None) is None
        assert ArbitrageStrategy._book_price({}, None) is None

    # ── the regression, on the real 2026-09-09 quotes ────────────────────
    def _grasim_strategy(self, *, near, far):
        s = _make_strategy(mode="paper", universe=["GRASIM"],
                           calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = 0.0
        s.kite.instruments.return_value = [
            {"name": "GRASIM", "tradingsymbol": "GRASIM26SEPFUT",
             "instrument_type": "FUT", "expiry": date(2026, 9, 29),
             "lot_size": 250, "segment": "NFO-FUT"},
            {"name": "GRASIM", "tradingsymbol": "GRASIM26OCTFUT",
             "instrument_type": "FUT", "expiry": date(2026, 10, 27),
             "lot_size": 250, "segment": "NFO-FUT"},
        ]
        s._clock = lambda: datetime(2026, 9, 9, 11, 0)   # 20d / 48d to expiry
        s.kite.quote.side_effect = lambda keys: {
            "NSE:GRASIM": {"last_price": 3300.0},
            "NFO:GRASIM26SEPFUT": near,
            "NFO:GRASIM26OCTFUT": far,
        }
        return s

    def test_the_2026_09_09_phantom_entry_no_longer_fires(self):
        # The exact books recorded that morning. carry_diff from the print was
        # −10.63%; from the book it is −0.73%, nowhere near the 5% bar.
        s = self._grasim_strategy(near=self._q(3310.0, 3308.2, 3311.1),
                                  far=self._q(3300.8, 3323.0, 3328.2))
        assert [p for p in s.scan_and_propose() if p.option_type == "FUT"] == [], \
            "an entry driven by a stale far-month print must not fire"

    def test_a_real_dislocation_still_fires(self):
        # Same shape, but the BOOK itself is dislocated rather than the print:
        # the far month genuinely trades below the near. The fix must not have
        # simply disabled the strategy.
        s = self._grasim_strategy(near=self._q(3310.0, 3308.2, 3311.1),
                                  far=self._q(3240.0, 3238.0, 3242.0))
        props = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        assert len(props) == 2, "a genuine book dislocation must still trade"

    def test_the_signal_is_priced_off_the_book_not_the_print(self):
        s = self._grasim_strategy(near=self._q(3310.0, 3308.2, 3311.1),
                                  far=self._q(3300.8, 3323.0, 3328.2))
        snap = s._observe_universe_uncached()[0]
        assert snap["near_price"] == pytest.approx(3309.65)   # mid, not the print
        assert snap["next_price"] == pytest.approx(3325.60)   # mid, not the print

    def test_fills_and_marks_use_the_same_price_as_the_signal(self):
        # A price nothing can transact at must not drive the signal, the fill
        # or the mark. Booking a fill at a print 22 points outside the book
        # would be incoherent once we have decided it is not a price.
        s = self._grasim_strategy(near=self._q(3310.0, 3308.2, 3311.1),
                                  far=self._q(3240.0, 3238.0, 3242.0))
        props = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        by_ts = {p.tradingsymbol: p.price for p in props}
        assert by_ts["GRASIM26SEPFUT"] == pytest.approx(3309.65)
        assert by_ts["GRASIM26OCTFUT"] == pytest.approx(3240.00)

    # ── fail loud about the bad data ─────────────────────────────────────
    def test_a_stale_print_is_reported_once_with_its_numbers(self, caplog):
        s = self._grasim_strategy(near=self._q(3310.0, 3308.2, 3311.1),
                                  far=self._q(3300.8, 3323.0, 3328.2))
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            for _ in range(3):
                s._observe_universe_uncached()
        hits = [r for r in caplog.records if "STALE PRINT" in r.getMessage()]
        assert len(hits) == 1, f"once per contract per session, got {len(hits)}"
        msg = hits[0].getMessage()
        assert "GRASIM26OCTFUT" in msg and "3300.80" in msg and "3323.00" in msg

    def test_a_print_inside_the_book_is_not_flagged(self, caplog):
        s = self._grasim_strategy(near=self._q(3310.0, 3308.2, 3311.1),
                                  far=self._q(3325.0, 3323.0, 3328.2))
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            s._observe_universe_uncached()
        assert not [r for r in caplog.records if "STALE PRINT" in r.getMessage()]


class TestUnformedBookGates:
    """WHY (Rule 9, issue #228 follow-up): #229's mid-pricing was ALREADY live
    in the working tree on 2026-09-11 and did not stop that day's cluster.
    Seven calendars opened in the 09:15 tick — six with NO two-sided far book
    (so `_book_price` fell back to the stale print the fix was about) and two
    against books 2.39% and 2.70% wide, whose mid is not a price either. Both
    measurable ones lost money crossing: −₹4,241 and −₹5,797.

    `_implied_carry` annualizes over the ~28-day inter-expiry gap, so a 0.4%
    error in the far leg is a full 5% of carry — the entire entry threshold."""

    def _q(self, ltp, bid, ask, qty=250):
        return {"last_price": ltp,
                "depth": {"buy": [{"price": bid, "quantity": qty}],
                          "sell": [{"price": ask, "quantity": qty}]}}

    def _strategy(self, *, near, far, universe=("AAA",)):
        s = _make_strategy(mode="paper", universe=list(universe),
                           calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = 0.0
        s.kite.instruments.return_value = [
            {"name": "AAA", "tradingsymbol": "AAA26SEPFUT", "instrument_type": "FUT",
             "expiry": date(2026, 9, 29), "lot_size": 250, "segment": "NFO-FUT"},
            {"name": "AAA", "tradingsymbol": "AAA26OCTFUT", "instrument_type": "FUT",
             "expiry": date(2026, 10, 27), "lot_size": 250, "segment": "NFO-FUT"},
        ]
        s._clock = lambda: datetime(2026, 9, 11, 9, 15)
        s.kite.quote.side_effect = lambda keys: {
            "NSE:AAA": {"last_price": 3300.0},
            "NFO:AAA26SEPFUT": near, "NFO:AAA26OCTFUT": far,
        }
        return s

    # ── a book too wide to be a price ────────────────────────────────────
    def test_a_wide_book_is_not_a_price(self):
        # INDUSINDBK's real far book at 09:15 on 2026-09-11.
        q = self._q(1000.80, 985.30, 1012.30)
        assert ArbitrageStrategy._book_price(q, ArbitrageStrategy._touch(q)) is None

    def test_a_normal_book_still_prices(self):
        q = self._q(3325.0, 3323.0, 3328.2)          # 0.16% — typical far month
        assert ArbitrageStrategy._book_price(
            q, ArbitrageStrategy._touch(q)) == pytest.approx(3325.6)

    def test_a_wide_book_is_still_RECORDED(self):
        # Measurement stays permissive while pricing turns strict: #222's four
        # weeks of depth data must keep the pathological books, or the study
        # loses exactly the cases that matter.
        q = self._q(1000.80, 985.30, 1012.30)
        t = ArbitrageStrategy._touch(q)
        assert t is not None and t["bid"] == 985.30 and t["ask"] == 1012.30

    def test_a_wide_far_book_blocks_the_entry(self):
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far=self._q(3240.0, 3200.0, 3280.0))   # 1.24% wide
        assert [p for p in s.scan_and_propose() if p.option_type == "FUT"] == []

    # ── one leg on a book, the other on a print ──────────────────────────
    def test_mixed_basis_blocks_the_entry(self):
        # The 2026-09-11 shape: near has depth, far has none, so the far leg
        # silently falls back to a print two sessions old.
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far={"last_price": 3240.0})
        assert [p for p in s.scan_and_propose() if p.option_type == "FUT"] == [], \
            "legs priced on different bases must not open a calendar"

    def test_mixed_basis_is_reported(self, caplog):
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far={"last_price": 3240.0})
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            for _ in range(3):
                s._observe_universe_uncached()
        hits = [r for r in caplog.records
                if "comparable basis" in r.getMessage()
                and "CONVERGE suppressed" not in r.getMessage()]
        assert len(hits) == 1, "once per symbol per session"

    def test_a_depthless_feed_is_not_mixed(self):
        # The backtest's MockKiteArb publishes no depth at all: BOTH legs use
        # prints, which is consistent, and must keep trading exactly as before.
        s = self._strategy(near={"last_price": 3310.0}, far={"last_price": 3240.0})
        props = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        assert len(props) == 2, "a depthless feed must be unaffected"

    def test_both_legs_on_good_books_still_trade(self):
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far=self._q(3240.0, 3238.0, 3242.0))
        assert len([p for p in s.scan_and_propose()
                    if p.option_type == "FUT"]) == 2

    def _open_aaa(self, s, dte_near_snap=None):
        s.state.open_calendars["AAA"] = CalendarTrade(
            symbol="AAA", position="SHORT_CALENDAR",
            entry_time=datetime(2026, 9, 11, 9, 0), entry_carry_diff=0.10,
            legs=[CalendarLeg(symbol="AAA", tradingsymbol="AAA26SEPFUT",
                              expiry="2026-09-29", lot_size=250, quantity=1,
                              entry_price=3310.0, current_price=3310.0),
                  CalendarLeg(symbol="AAA", tradingsymbol="AAA26OCTFUT",
                              expiry="2026-10-27", lot_size=250, quantity=-1,
                              entry_price=3240.0, current_price=3240.0)])

    def test_a_SAFETY_exit_survives_untrusted_pricing(self):
        # The point of the gate is that untrusted pricing must never TRAP a
        # position. MAX_HOLD does not read carry_diff, so it must still fire.
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far={"last_price": 3240.0})
        self._open_aaa(s)
        s.calendar_max_holding_days = 0.0       # force MAX_HOLD
        assert len(s.check_and_rehedge()) == 2, \
            "a safety exit must fire regardless of pricing basis"

    def test_CONVERGE_is_suppressed_on_untrusted_pricing(self):
        # CONVERGE reads the very carry_diff the entry gate refuses to trust.
        # A far leg that loses its book for a few ticks would otherwise close
        # a spread that has not converged, on a print-driven number.
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far={"last_price": 3240.0})
        self._open_aaa(s)
        s.calendar_exit_annual = 9.99           # everything "converged"
        s.calendar_exit_debounce_ticks = 1
        assert s.check_and_rehedge() == [], \
            "a discretionary exit must not fire on a number we do not trust"

    def test_CONVERGE_fires_normally_when_both_legs_have_books(self):
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far=self._q(3240.0, 3238.0, 3242.0))
        self._open_aaa(s)
        s.calendar_exit_annual = 9.99
        s.calendar_exit_debounce_ticks = 1
        assert len(s.check_and_rehedge()) == 2

    def test_a_wide_NEAR_book_does_not_orphan_an_open_calendar(self):
        # THE regression from the review of PR #229: `near_px is None ->
        # continue` dropped the symbol from the snapshot entirely, so an open
        # calendar got no EXPIRY, no MAX_HOLD and no STOP_LOSS — the same
        # orphaning #227 fixed, re-entered by a different door.
        s = self._strategy(near=self._q(3310.0, 3280.0, 3340.0),   # 1.8% wide
                           far=self._q(3240.0, 3238.0, 3242.0))
        self._open_aaa(s)
        snaps = s._observe_universe_uncached()
        assert snaps, "a wide near book must not make the symbol unobservable"
        s.calendar_max_holding_days = 0.0
        assert len(s.check_and_rehedge()) == 2, \
            "the position must still reach its safety exit"

    def test_an_untrusted_exit_is_not_booked_as_verified_pnl(self):
        # The ledger must not record a fill and a P&L at a price the same code
        # calls untradable — #222's live-readiness decision reads these rows.
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far={"last_price": 3240.0})
        self._open_aaa(s)
        s.calendar_max_holding_days = 0.0
        s.execute_proposals(s.check_and_rehedge())
        assert s.state.closed_trades[-1]["pnl_verified"] is False

    def test_a_wide_book_is_reported(self, caplog):
        # Zero entries for a session must not look the same as a quiet market.
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1),
                           far=self._q(3240.0, 3200.0, 3280.0))
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            for _ in range(3):
                s._observe_universe_uncached()
        hits = [r for r in caplog.records if "WIDE BOOK" in r.getMessage()]
        assert len(hits) == 1 and "AAA26OCTFUT" in hits[0].getMessage()

    def test_a_missing_far_quote_is_not_called_mixed(self, caplog):
        # A throttled or failed far quote is "nothing to compare", not "mixed"
        # — and mis-reporting it burns the once-per-session warning that a
        # genuine bookless leg later in the day would need.
        s = self._strategy(near=self._q(3310.0, 3308.2, 3311.1), far=None)
        s.kite.quote.side_effect = lambda keys: {
            "NSE:AAA": {"last_price": 3300.0},
            "NFO:AAA26SEPFUT": self._q(3310.0, 3308.2, 3311.1),
        }
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            s._observe_universe_uncached()
        assert not [r for r in caplog.records
                    if "comparable basis" in r.getMessage()]


class TestStopIgnoresEntryFriction:
    """WHY (Rule 9, issue #231): in LIVE both legs are crossed adversely to
    enter, so a calendar's MTM is negative the instant it fills, before the
    market moves at all. STOP_LOSS compared that raw MTM against
    −1×expected_harvest, so a trade entered near the 5% gate was stopped out on
    its FIRST tick for a guaranteed round-trip loss. Paper never showed it,
    because there the fill price IS the mark — it would have appeared on the
    first live session as a cluster of instant stop-outs that looked like
    'the strategy is just losing'."""

    LOT = 250

    def _touch(self, bid, ask):
        return {"bid": bid, "ask": ask, "bid_qty": self.LOT,
                "ask_qty": self.LOT, "ltp": (bid + ask) / 2}

    def _live_entered_trade(self, s, *, costs=500.0, expected=800.0):
        """A SHORT_CALENDAR entered the way LIVE enters: buy the near at its
        ask, sell the far at its bid, with the entry touches recorded."""
        near_touch, far_touch = self._touch(1000.0, 1001.5), self._touch(1010.0, 1013.0)
        trade = CalendarTrade(
            symbol="AAA", position="SHORT_CALENDAR",
            entry_time=datetime(2026, 9, 11, 10, 0), entry_carry_diff=0.06,
            expected_harvest=expected, costs=costs, realized=-costs,
            legs=[CalendarLeg(symbol="AAA", tradingsymbol="AAA26SEPFUT",
                              expiry="2026-09-29", lot_size=self.LOT, quantity=1,
                              entry_price=1001.5,      # crossed to the ask
                              current_price=1000.75),  # marked at the mid
                  CalendarLeg(symbol="AAA", tradingsymbol="AAA26OCTFUT",
                              expiry="2026-10-27", lot_size=self.LOT, quantity=-1,
                              entry_price=1010.0,      # crossed to the bid
                              current_price=1011.5)])
        trade.leg_quotes = {"AAA26SEPFUT": {"entry": near_touch},
                            "AAA26OCTFUT": {"entry": far_touch}}
        s.state.open_calendars["AAA"] = trade
        return trade

    def _snap(self, near_px=1000.75, far_px=1011.5):
        """Marks both legs at their mids — i.e. no market movement since the
        entry. Without a real snapshot check_and_rehedge skips the symbol
        entirely and every assertion here would pass vacuously."""
        return {
            "symbol": "AAA", "spot": 1000.0,
            "near": {"tradingsymbol": "AAA26SEPFUT", "lot_size": self.LOT,
                     "expiry": "2026-09-29", "instrument_token": 1},
            "near_price": near_px, "dte_near": 18,
            "next": {"tradingsymbol": "AAA26OCTFUT", "lot_size": self.LOT,
                     "expiry": "2026-10-27", "instrument_token": 2},
            "next_price": far_px, "dte_next": 46,
            "basis_annual": 0.0, "basis_annual_next": 0.0,
            "carry_implied": 0.13, "carry_diff": 0.06,
            "near_quote": self._touch(1000.0, 1001.5),
            "next_quote": self._touch(1010.0, 1013.0),
            "pricing_trusted": True, "near_basis": "book", "next_basis": "book",
        }

    def _strategy(self, near_px=1000.75, far_px=1011.5):
        s = _make_strategy(mode="paper")
        s.calendar_stop_loss_mult = 1.0
        s.calendar_max_holding_days = 99
        s.calendar_exit_annual = 0.005          # 0.06 is nowhere near CONVERGE
        s._clock = lambda: datetime(2026, 9, 11, 10, 1)
        s._observe_universe = lambda: [self._snap(near_px, far_px)]
        return s

    def test_the_fixture_actually_observes_the_symbol(self):
        # Guard against the vacuous pass: if check_and_rehedge skipped AAA for
        # want of a snapshot, every "no exit" assertion below would be
        # meaningless. Force the stop and prove it CAN fire here.
        s = self._strategy(far_px=1016.5)
        t = self._live_entered_trade(s)
        t.expected_harvest = 1.0
        assert len(s.check_and_rehedge()) == 2

    def test_friction_is_costs_plus_the_spread_actually_crossed(self):
        s = self._strategy()
        trade = self._live_entered_trade(s)
        # near: |1001.50 − 1000.75| × 250 = 187.50 ; far: |1010 − 1011.50| × 250 = 375
        assert ArbitrageStrategy._entry_friction(trade) == pytest.approx(500 + 562.5)

    def test_a_freshly_filled_live_calendar_does_not_stop_itself_out(self):
        s = self._strategy()
        self._live_entered_trade(s)
        # No market movement: MTM is exactly −friction, which must read as
        # "nothing has happened", not "thesis invalidated".
        assert s.check_and_rehedge() == [], \
            "entry friction alone must never trip the stop"

    def test_a_genuine_adverse_move_still_stops(self):
        # The guard must not have disabled the stop: push the short far leg
        # 5 points against us and it has to fire.
        s = self._strategy(far_px=1016.5)
        self._live_entered_trade(s)
        exits = s.check_and_rehedge()
        assert len(exits) == 2 and all("STOP_LOSS" in p.rationale for p in exits)

    def test_a_trade_with_no_recorded_touch_falls_back_to_costs(self):
        # Paper fills AT the mid, and trades opened before #223 have no touch
        # at all. Both reduce to the cost term — the conservative direction,
        # and it must not raise.
        s = self._strategy()
        trade = self._live_entered_trade(s)
        trade.leg_quotes = {}
        assert ArbitrageStrategy._entry_friction(trade) == pytest.approx(500.0)
        s.check_and_rehedge()          # must not raise

    def test_the_ledger_mtm_is_untouched(self):
        # The invariant the previous review wrote: the stop fires on exactly
        # the number the ledger reports. The adjustment is on the THRESHOLD,
        # so unrealized_pnl must be unchanged by any of this.
        s = self._strategy()
        trade = self._live_entered_trade(s)
        s._recompute_unrealized_from_open_legs()
        assert s.state.unrealized_pnl == pytest.approx(-562.5)
        assert sum(ArbitrageStrategy._leg_mtm(l) for l in trade.legs) \
            == pytest.approx(-562.5)


class TestEntryFrictionIsBounded:
    """WHY (Rule 9, review of PR #232): the first cut computed
    `abs(entry_price - mid)` against the SCAN-tick touch. The executor prices a
    marketable LIMIT off a FRESH ltp padded by limit_protection_pct and polls
    per leg, and the legs are placed sequentially — so everything the book did
    between the scan quote and the second fill landed in 'friction', and
    `abs()` made it additive whichever way it went. Since friction only ever
    LOOSENS the stop, drift and good luck alike were quietly disarming a safety
    exit."""

    LOT = 250

    def _leg(self, qty, entry_price):
        return CalendarLeg(symbol="AAA", tradingsymbol="AAA26SEPFUT",
                           expiry="2026-09-29", lot_size=self.LOT,
                           quantity=qty, entry_price=entry_price,
                           current_price=entry_price)

    _touch = {"bid": 1000.0, "ask": 1002.0, "bid_qty": 250,
              "ask_qty": 250, "ltp": 1001.0}      # mid 1001, half-spread 1.0

    def test_a_long_leg_crossing_to_the_ask_pays_the_half_spread(self):
        cost = ArbitrageStrategy._leg_crossing_cost(self._leg(1, 1002.0), self._touch)
        assert cost == pytest.approx(1.0 * self.LOT)

    def test_a_short_leg_crossing_to_the_bid_pays_the_half_spread(self):
        cost = ArbitrageStrategy._leg_crossing_cost(self._leg(-1, 1000.0), self._touch)
        assert cost == pytest.approx(1.0 * self.LOT)

    def test_a_fill_better_than_the_mid_costs_nothing(self):
        # It also books a positive _leg_mtm. Counting it as friction too would
        # loosen the stop twice for the same good luck.
        assert ArbitrageStrategy._leg_crossing_cost(
            self._leg(1, 1000.5), self._touch) == 0.0
        assert ArbitrageStrategy._leg_crossing_cost(
            self._leg(-1, 1001.5), self._touch) == 0.0

    def test_drift_beyond_the_touch_is_capped_at_the_half_spread(self):
        # Filled 5 points through the ask: 1 point was the spread, 4 were the
        # market moving. Market movement is what the stop MEASURES; laundering
        # it into the bar would disarm the stop exactly on fast entries.
        cost = ArbitrageStrategy._leg_crossing_cost(self._leg(1, 1007.0), self._touch)
        assert cost == pytest.approx(1.0 * self.LOT), \
            "drift must not inflate the bar the stop is judged against"

    def test_no_touch_means_no_crossing_charge(self):
        assert ArbitrageStrategy._leg_crossing_cost(self._leg(1, 1002.0), None) == 0.0


class TestEntryFrictionIsFrozen:
    """WHY (Rule 9, review of PR #232): `trade.costs` is a LIFETIME total.
    execute_proposals deliberately does not reverse a half-filled EXIT, so on
    the next tick the surviving naked leg was judged against a bar still
    carrying the departed leg's costs — a quantity that drifts, not the entry
    measurement the docstring claims."""

    def test_friction_is_frozen_when_the_second_leg_fills(self):
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor()
        s.execute_proposals([_leg("AAA", "APR", "BUY"), _leg("AAA", "MAY", "SELL")])
        trade = s.state.open_calendars["AAA"]
        assert trade.entry_friction is not None
        assert trade.entry_friction == pytest.approx(trade.costs)   # paper-shaped: no touch

    def test_a_later_cost_does_not_move_the_frozen_bar(self):
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor()
        s.execute_proposals([_leg("AAA", "APR", "BUY"), _leg("AAA", "MAY", "SELL")])
        trade = s.state.open_calendars["AAA"]
        frozen = trade.entry_friction
        trade.costs += 5_000.0          # a half-filled exit books more cost
        assert ArbitrageStrategy._entry_friction(trade) == pytest.approx(frozen)

    def test_it_survives_serialize_restore(self):
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor()
        s.execute_proposals([_leg("AAA", "APR", "BUY"), _leg("AAA", "MAY", "SELL")])
        frozen = s.state.open_calendars["AAA"].entry_friction
        fresh = _make_strategy(mode="paper")
        fresh.restore_state(s.serialize_state())
        assert fresh.state.open_calendars["AAA"].entry_friction == pytest.approx(frozen)

    def test_a_legacy_trade_recomputes_instead_of_crashing(self):
        s = _make_strategy(mode="live")
        s._available_margin = lambda: None
        s._live_execute = _Executor()
        s.execute_proposals([_leg("AAA", "APR", "BUY"), _leg("AAA", "MAY", "SELL")])
        trade = s.state.open_calendars["AAA"]
        trade.entry_friction = None                      # pre-#232 blob
        assert ArbitrageStrategy._entry_friction(trade) == pytest.approx(trade.costs)


class TestCrossingAwareHurdle:
    """WHY (Rule 9, issue #233): `calendar_cost_hurdle_mult` models brokerage,
    STT, exchange fees and stamp — and has NO crossing term. Crossing is the
    larger number: at the measured spreads (#223) it is ~0.235% of leg notional
    against an expected harvest of ~0.185% at the 5% gate, so a trade can be
    expected-negative the instant it fills and still clear the gate.

    Default is MEASURE-ONLY (`calendar_crossing_mult = 0.0`). The right
    multiplier is what #222's four weeks of depth data exists to decide;
    setting it now would bake in the assumption the measurement is meant to
    test. So these tests pin two things: the measurement is correct, and it
    changes nothing until an operator turns it on."""

    def _q(self, ltp, bid, ask, qty=250):
        return {"last_price": ltp,
                "depth": {"buy": [{"price": bid, "quantity": qty}],
                          "sell": [{"price": ask, "quantity": qty}]}}

    def _snap(self, near_half=1.0, far_half=2.0, carry_diff=0.30):
        # carry_diff sized so the trade clears the FEE-only hurdle and fails
        # only once crossing is charged — otherwise 'the default changes
        # nothing' is unobservable, because fees alone reject it.
        near_mid, far_mid = 1000.0, 1010.0
        return {
            "symbol": "AAA", "spot": 1000.0,
            "near": {"tradingsymbol": "AAA26SEPFUT", "lot_size": 250,
                     "expiry": "2026-09-29", "instrument_token": 1},
            "near_price": near_mid, "dte_near": 20,
            "next": {"tradingsymbol": "AAA26OCTFUT", "lot_size": 250,
                     "expiry": "2026-10-27", "instrument_token": 2},
            "next_price": far_mid, "dte_next": 48,
            "basis_annual": 0.0, "basis_annual_next": 0.0,
            "carry_implied": carry_diff + 0.07, "carry_diff": carry_diff,
            "near_quote": ArbitrageStrategy._touch(
                self._q(near_mid, near_mid - near_half, near_mid + near_half)),
            "next_quote": ArbitrageStrategy._touch(
                self._q(far_mid, far_mid - far_half, far_mid + far_half)),
            "pricing_trusted": True, "near_basis": "book", "next_basis": "book",
        }

    def test_the_SHIPPED_default_is_measure_only(self):
        # Every other test here sets the multiplier explicitly, and so does
        # _make_strategy — so none of them would notice if the production
        # default changed. Mutation-checking revealed exactly that hole. This
        # pins the promise the PR actually makes: merging #233 does not move
        # entry behaviour until an operator sets the knob.
        import inspect
        src = inspect.getsource(ArbitrageStrategy.__init__)
        assert 'cfg.get("calendar_crossing_mult", 0.0)' in src, \
            "the shipped default must stay 0.0 (measure-only) until #222's " \
            "data decides the multiplier"

    # ── the measurement ──────────────────────────────────────────────────
    def test_it_counts_four_crossings(self):
        # Both legs in, both legs out: 2 x qty x (near_half x lot + far_half x lot).
        snap = self._snap(near_half=1.0, far_half=2.0)
        cost = ArbitrageStrategy._expected_crossing_cost(snap, qty=1,
                                                         near_lot=250, next_lot=250)
        assert cost == pytest.approx(2.0 * (1.0 * 250 + 2.0 * 250))

    def test_it_scales_with_size(self):
        snap = self._snap()
        one = ArbitrageStrategy._expected_crossing_cost(snap, 1, 250, 250)
        two = ArbitrageStrategy._expected_crossing_cost(snap, 2, 250, 250)
        assert two == pytest.approx(2 * one)

    def test_no_book_measures_zero_not_a_guess(self):
        # With no touch there is nothing to measure, and #229 already refuses
        # to ENTER on that basis — so a zero here can never wave through a
        # trade that gate would have stopped.
        snap = self._snap()
        snap["next_quote"] = None
        assert ArbitrageStrategy._expected_crossing_cost(snap, 1, 250, 250) == 0.0

    # ── inert until switched on ──────────────────────────────────────────
    def _strategy(self, crossing_mult):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = 2.0
        s.calendar_crossing_mult = crossing_mult
        s._observe_universe = lambda: [self._snap(near_half=3.0, far_half=6.0)]
        return s

    def test_default_does_not_change_who_gets_in(self):
        # The whole point: merging this must not move entry behaviour before
        # #222's data says what the multiplier should be.
        assert len([p for p in self._strategy(0.0).scan_and_propose()
                    if p.option_type == "FUT"]) == 2

    def test_charging_the_crossing_rejects_the_same_trade(self):
        assert [p for p in self._strategy(1.0).scan_and_propose()
                if p.option_type == "FUT"] == []

    def test_it_says_so_when_it_would_have_rejected(self, caplog):
        # Rule 12: a trade that cannot pay for itself must not pass silently
        # just because the gate is off — that log line IS the deliverable of
        # this change until the operator flips the multiplier.
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            self._strategy(0.0).scan_and_propose()
        assert any("would REJECT" in r.getMessage()
                   for r in caplog.records)

    def test_it_stays_quiet_when_the_trade_clears_either_way(self, caplog):
        s = self._strategy(0.0)
        s._observe_universe = lambda: [self._snap(near_half=0.01, far_half=0.01)]
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            s.scan_and_propose()
        assert not [r for r in caplog.records if "crossing-aware" in r.getMessage()]


class TestCrossingHurdleReviewFixes:
    """WHY (Rule 9, review of PR #234): the first cut shipped a gate that was a
    silent no-op in the conditions it most needed to bite, and a knob that
    another knob could switch off."""

    def _t(self, bid, ask, qty=250):
        return ArbitrageStrategy._touch(
            {"last_price": (bid + ask) / 2,
             "depth": {"buy": [{"price": bid, "quantity": qty}],
                       "sell": [{"price": ask, "quantity": qty}]}})

    def _snap(self, near_q, next_q, carry=0.30):
        return {"symbol": "AAA", "spot": 1000.0,
                "near": {"tradingsymbol": "AAA26SEPFUT", "lot_size": 250,
                         "expiry": "2026-09-29", "instrument_token": 1},
                "near_price": 1000.0, "dte_near": 20,
                "next": {"tradingsymbol": "AAA26OCTFUT", "lot_size": 250,
                         "expiry": "2026-10-27", "instrument_token": 2},
                "next_price": 1010.0, "dte_next": 48,
                "basis_annual": 0.0, "basis_annual_next": 0.0,
                "carry_implied": carry + 0.07, "carry_diff": carry,
                "near_quote": near_q, "next_quote": next_q,
                "pricing_trusted": True,
                "near_basis": "book" if near_q else "print",
                "next_basis": "book" if next_q else "print"}

    def _s(self, snap, *, hurdle=2.0, crossing=0.0):
        s = _make_strategy(mode="paper", calendar_entry_annual=0.05)
        s.calendar_cost_hurdle_mult = hurdle
        s.calendar_crossing_mult = crossing
        s._observe_universe = lambda: [snap]
        return s

    def test_unmeasurable_crossing_is_announced_not_treated_as_free(self, caplog):
        # print+print IS pricing_trusted, so #229 lets it enter — the earlier
        # docstring claimed otherwise. With the charge armed and no book, the
        # gate cannot bite, and that must not be silent.
        s = self._s(self._snap(None, None), crossing=5.0)
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            props = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        assert len(props) == 2, "a depthless feed must still trade (backtest)"
        assert any("UNMEASURABLE" in r.getMessage() for r in caplog.records)

    def test_the_crossing_knob_works_with_the_fee_hurdle_disabled(self):
        # `calendar_cost_hurdle_mult = 0` is documented as disabling the FEE
        # hurdle. It must not also disable a crossing charge the operator
        # explicitly armed.
        snap = self._snap(self._t(997, 1003), self._t(1004, 1016))
        assert [p for p in self._s(snap, hurdle=0.0, crossing=5.0).scan_and_propose()
                if p.option_type == "FUT"] == []

    def test_fee_hurdle_alone_is_unchanged_when_crossing_is_off(self):
        snap = self._snap(self._t(999.9, 1000.1), self._t(1009.9, 1010.1))
        assert len([p for p in self._s(snap, hurdle=2.0, crossing=0.0).scan_and_propose()
                    if p.option_type == "FUT"]) == 2

    def test_the_counterfactual_states_the_knob_accurately(self, caplog):
        # It used to say "not charged" for any partial multiple, and print
        # 0.05 as 0.1 — this line is the input to #222's decision.
        snap = self._snap(self._t(997, 1003), self._t(1004, 1016))
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            self._s(snap, hurdle=2.0, crossing=0.05).scan_and_propose()
        msgs = [r.getMessage() for r in caplog.records if "would REJECT" in r.getMessage()]
        assert msgs and "charged at 0.05x" in msgs[0], msgs
        assert "not charged" not in msgs[0]

    def test_the_counterfactual_does_not_repeat_every_tick(self, caplog):
        snap = self._snap(self._t(997, 1003), self._t(1004, 1016))
        s = self._s(snap, hurdle=2.0, crossing=0.0)
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            for _ in range(5):
                s._obs_tick_id = _          # force a fresh scan each tick
                s.scan_and_propose()
        assert len([r for r in caplog.records if "would REJECT" in r.getMessage()]) == 1

    def test_thin_depth_is_reported_as_a_lower_bound(self, caplog):
        # depth-1 holding less than the order means the half-spread understates
        # what the remainder pays — and #222 calibrates off this number.
        snap = self._snap(self._t(997, 1003, qty=100), self._t(1004, 1016, qty=100))
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            self._s(snap, hurdle=2.0, crossing=0.0).scan_and_propose()
        assert any("LOWER bound" in r.getMessage() for r in caplog.records)

    def test_ample_depth_is_not_reported(self, caplog):
        snap = self._snap(self._t(999.9, 1000.1, qty=5000),
                          self._t(1009.9, 1010.1, qty=5000))
        with caplog.at_level(_logging.WARNING, logger="strategies.arbitrage"):
            self._s(snap, hurdle=2.0, crossing=0.0).scan_and_propose()
        assert not [r for r in caplog.records if "LOWER bound" in r.getMessage()]
