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
    s.disable_calendar = disable_calendar
    s.lots_per_leg = 1
    s.max_open_calendars = 5
    s.calendar_margin_pct = 0.06
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
            _baseline_realized=-21.0,
            _baseline_costs=21.0,
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
        # the per-trade baselines (without which closed_trades would record the
        # running cumulative instead of the trade delta at close).
        assert set(dst.state.open_calendars) == {"AAA"}
        t = dst.state.open_calendars["AAA"]
        assert t.position == "SHORT_CALENDAR"
        assert t.entry_time == datetime(2026, 4, 15, 10, 0)
        assert t.entry_carry_diff == pytest.approx(0.031)
        assert t._baseline_realized == -21.0
        assert t._baseline_costs == 21.0
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
