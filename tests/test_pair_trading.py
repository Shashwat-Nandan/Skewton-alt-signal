"""Tests for the pair trading strategy — z-score, entry/exit logic, mode dispatch, fill handling."""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.pair_trading import (
    PairLeg,
    PairState,
    PairTradingStrategy,
)
from trade_proposer import TradeProposal


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────

def _make_strategy(
    *, mode: str = "paper",
    hedge_ratio: float = 0.5,
    entry_z: float = 2.0,
    exit_z: float = 0.5,
    stop_z: float = 4.0,
    max_entry_z: float = 5.0,
    safety_buffer: float = 0.75,
    spread_history=None,
    max_leg_notional=None,
    lots_per_leg: int = 1,
    min_edge_multiplier: float = 0.0,
) -> PairTradingStrategy:
    """Build a PairTradingStrategy with __init__ bypassed — fully controllable for unit tests.

    min_edge_multiplier defaults to 0.0 (hurdle disabled) so entry/exit/sizing
    tests aren't accidentally blocked by friction-vs-edge accounting. Tests
    that exercise the hurdle set it explicitly.
    """
    s = PairTradingStrategy.__new__(PairTradingStrategy)
    s.kite = MagicMock()
    s.config = MagicMock()
    s.config_path = "config.ini"
    s.mode = mode
    s.symbol_a = "AAA"
    s.symbol_b = "BBB"
    s.hedge_ratio = hedge_ratio
    s.entry_z = entry_z
    s.exit_z = exit_z
    s.stop_z = stop_z
    s.max_entry_z = max_entry_z
    s.safety_buffer = safety_buffer
    s.lookback_days = 30
    s.lots_per_leg = lots_per_leg
    s.max_holding_days = 10
    s.max_leg_notional = max_leg_notional
    s.min_edge_multiplier = min_edge_multiplier
    s.total_capital = 500_000
    # H5: default cooldown disabled in unit tests so existing entry/exit
    # tests keep their pre-H5 behaviour. Cooldown-specific tests opt in by
    # mutating s.stop_cooldown_minutes after construction.
    s.stop_cooldown_minutes = 0
    s._pending_exit_reason = None
    # H19: no injected NFO dump by default; helpers that need
    # _resolve_futures or legs_expire_on lookups mock kite.instruments
    # directly so the lazy-fetch path still works.
    s._nfo_instruments_cache = None
    # H6: trading-day time-stop reads holidays.csv via _holidays(). Default
    # to an empty set so tests don't depend on the on-disk file's contents.
    s._holidays_cache = set()
    # H8/H13: optional callbacks default to None so tests don't trip on
    # missing attrs (constructor bypassed by __new__).
    s._kite_refresh = None
    s._book_notional_fn = None
    s.max_book_notional = None
    # M-S3: debounce defaults to 1 so existing single-tick exit tests
    # remain valid. Dedicated debounce tests opt in by setting >1.
    s.exit_debounce_ticks = 1
    # M-B5: backoff counters start clean (no skip in effect).
    s._place_order_fail_streak = 0
    s._place_order_skip_ticks_left = 0
    s._place_order_skip_window = 5
    # Marketable-LIMIT pad (2026-06-11): live orders price at LTP ± this %.
    s.limit_protection_pct = 0.25
    # Audit 1.1: no runner-injected panel by default — seed tests that
    # exercise the injected path set _spread_panel explicitly.
    s._spread_panel = None
    # M-B2: tests that assert exact fill prices default to 0bp slip;
    # dedicated slippage tests opt in by setting paper_slippage_bps.
    s.paper_slippage_bps = 0.0
    s.state = PairState()
    s._spread_history = list(spread_history) if spread_history is not None else []
    s._cached_futures = {
        "AAA": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                "expiry": "2026-04-28", "instrument_token": 111},
        "BBB": {"tradingsymbol": "BBB26APRFUT", "lot_size": 200,
                "expiry": "2026-04-28", "instrument_token": 222},
    }
    s._clock = lambda: datetime(2026, 4, 21, 10, 30)
    # session-start P&L baseline — normally set in __init__; tests bypass it.
    s._session_start_realized = 0.0
    s._session_start_unrealized = 0.0
    return s


# ──────────────────────────────────────────────────────────
# Z-score
# ──────────────────────────────────────────────────────────

class TestZScore:
    def test_empty_history_returns_none(self):
        s = _make_strategy(spread_history=[])
        assert s._z_score(100.0) is None

    def test_too_thin_history_returns_none(self):
        s = _make_strategy(spread_history=[10.0] * 5)
        assert s._z_score(10.0) is None

    def test_zero_std_returns_none(self):
        # Constant history → std = 0 → z is undefined
        s = _make_strategy(spread_history=[5.0] * 50)
        assert s._z_score(5.0) is None

    def test_known_z_value(self):
        # Build a history with mean=0, std=1 → spread of 2 should give z≈2.
        # Under seed-only z (2026-05-13), `spread_now` is never in history
        # so no exclusion is needed.
        history = [-1.0, 1.0] * 25  # mean 0, std 1
        s = _make_strategy(spread_history=history)
        z = s._z_score(2.0)
        assert z is not None
        assert abs(z - 2.0) < 0.05

    def test_recent_window_only(self):
        # Old observations far from new ones — z should reflect the recent window
        s = _make_strategy(
            spread_history=[1000.0] * 200 + [0.0] * 60,
            entry_z=2.0,
        )
        s.lookback_days = 30  # recent window: last 30 of [0.0]*60
        z = s._z_score(0.0)
        # All recent observations are 0 → std = 0 → returns None
        assert z is None


# ──────────────────────────────────────────────────────────
# _observe_spread: seed-only z (no intraday mutation)
# ──────────────────────────────────────────────────────────

class TestObserveSpreadSeedOnly:
    """
    The rolling z-window is daily-only — seeded once at __init__ from
    bhavcopy and untouched intraday. 2026-05-13 incident: appending every
    minute-tick that moved the spread by >1 paisa collapsed the rolling
    std and made `|z|=2` fire on intraday noise. Whatever happens during
    the session, `_spread_history` and the z it produces must stay
    pinned to the daily baseline.
    """

    def _stub_quotes(self, s, price_a, price_b):
        def fake_quote(syms):
            sym = syms[0]
            return {sym: {"last_price": price_a if "AAA" in sym else price_b}}
        s.kite.quote = fake_quote

    def test_observe_spread_does_not_mutate_history(self):
        seed = [float(x) for x in range(-30, 30)]
        s = _make_strategy(hedge_ratio=0.5, spread_history=list(seed))
        self._stub_quotes(s, 1000.0, 2000.0)
        for _ in range(50):
            s._observe_spread()
        assert s._spread_history == seed

    def test_session_z_is_pinned_to_seed(self):
        """200 intraday ticks at the same price → z is identical to first call."""
        s = _make_strategy(
            hedge_ratio=0.5,
            spread_history=[float(x) for x in range(-30, 30)],
        )
        self._stub_quotes(s, 1000.0, 2010.0)  # spread = -5 throughout
        spread_first, _ = s._observe_spread()
        z_first = s._z_score(spread_first)
        for _ in range(200):
            s._observe_spread()
        spread_after, _ = s._observe_spread()
        z_after = s._z_score(spread_after)
        assert z_first is not None and z_after is not None
        assert abs(z_first - z_after) < 1e-12


# ──────────────────────────────────────────────────────────
# Cost hurdle — refuse entries with insufficient expected edge
# ──────────────────────────────────────────────────────────

class TestCostHurdle:
    """
    2026-05-13 paper session: 28 round-trips across 3 pairs, ~₹39k realized
    losses, ~₹39k transaction costs — strategy was firing on z-crossings
    whose expected ₹ move was smaller than friction. The hurdle refuses
    entries where expected_gain < multiplier × round_trip_cost.
    """

    def _stub_quotes(self, s, price_a, price_b):
        def fake_quote(syms):
            sym = syms[0]
            return {sym: {"last_price": price_a if "AAA" in sym else price_b}}
        s.kite.quote = fake_quote

    def test_hurdle_blocks_low_edge_entry(self):
        # Tiny rolling std → small expected ₹ move → fails the 1.5× cost hurdle.
        # Spread history with std ≈ 0.02 → at |z|≈2.5 → expected Δspread ≈ 0.035
        # × qty_a_shares (100) = ₹3.5 expected gain vs ~₹100+ round-trip cost.
        s = _make_strategy(
            hedge_ratio=0.5,
            exit_z=0.75,
            min_edge_multiplier=1.5,
            spread_history=[0.0, 0.04] * 30,  # mean ≈0.02, std ≈0.02
        )
        # spread = 1000 - 0.5*2000.1 = -0.05 → ~3σ below mean → would enter
        self._stub_quotes(s, 1000.0, 2000.1)
        proposals = s.scan_and_propose()
        assert proposals == []
        assert s.state.position == "FLAT"

    def test_hurdle_allows_high_edge_entry(self):
        # Wide rolling std → large expected ₹ move → easily clears 1.5× cost.
        # Rolling window is the last lookback_days=30 of history → values 0..29:
        # mean=14.5, std≈8.66. z must land in the entry band and below
        # max_entry_z=5.0; price_b=2025 gives spread=-12.5 → z≈-3.12.
        s = _make_strategy(
            hedge_ratio=0.5,
            exit_z=0.75,
            min_edge_multiplier=1.5,
            spread_history=[float(x) for x in range(-30, 30)],
        )
        self._stub_quotes(s, 1000.0, 2025.0)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2

    def test_hurdle_disabled_with_zero_multiplier(self):
        # Same low-edge setup as test_hurdle_blocks_low_edge_entry, but
        # multiplier=0 → no hurdle → entry fires regardless of friction.
        s = _make_strategy(
            hedge_ratio=0.5,
            exit_z=0.75,
            min_edge_multiplier=0.0,
            spread_history=[0.0, 0.04] * 30,
        )
        self._stub_quotes(s, 1000.0, 2000.1)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2


# ──────────────────────────────────────────────────────────
# Entry logic
# ──────────────────────────────────────────────────────────

class TestEntry:
    def _seed_priced_quotes(self, s, price_a=1000.0, price_b=2000.0):
        """Mock the kite quote calls so _observe_spread returns deterministic prices.

        Seeds a rolling window with mean=0, std=2 so the canonical test prices
        (price_b ≈ 2010 → spread ≈ -5) land at z ≈ -2.5 — inside the entry band
        and well below max_entry_z=5.0. Narrower seeds would push |z| past the
        2026-05-15 regime-break ceiling.
        """
        def fake_quote(syms):
            assert len(syms) == 1
            sym = syms[0]
            if "AAA" in sym:
                return {sym: {"last_price": price_a}}
            return {sym: {"last_price": price_b}}
        s.kite.quote = fake_quote
        s._spread_history = [-2.0, 2.0] * 30

    def test_no_entry_when_already_in_position(self):
        s = _make_strategy()
        s.state.position = "LONG_SPREAD"
        proposals = s.scan_and_propose()
        assert proposals == []

    def test_long_spread_when_z_below_minus_entry(self):
        # Spread below the rolling mean → z negative → LONG_SPREAD.
        # Helper seeds mean=0, std=2; price_b=2010 → spread=-5 → z=-2.5.
        s = _make_strategy(hedge_ratio=0.5)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        # Leg A is BUY (long the spread = long A)
        a_leg = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b_leg = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a_leg.transaction_type == "BUY"
        # Positive β with LONG_SPREAD → SELL B
        assert b_leg.transaction_type == "SELL"
        # Both legs are FUT
        assert a_leg.option_type == "FUT"
        assert b_leg.option_type == "FUT"

    def test_short_spread_when_z_above_plus_entry(self):
        # Symmetric to the LONG case: price_b=1990 → spread=+5 → z=+2.5.
        s = _make_strategy(hedge_ratio=0.5)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=1990.0)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        a_leg = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b_leg = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a_leg.transaction_type == "SELL"
        assert b_leg.transaction_type == "BUY"

    def test_negative_hedge_ratio_flips_leg_b_side(self):
        # With β<0, LONG_SPREAD wants both legs BUY (since "−β·B" with β<0 means +|β|·B)
        s = _make_strategy(hedge_ratio=-0.5)
        s._spread_history = [-1.0, 1.0] * 30
        # spread = price_a - (-0.5)*price_b = price_a + 0.5*price_b
        # mean of seed = 0, std = 1; want spread ≈ -5 → price_a + 0.5*price_b = -5
        # use price_a = 0, price_b = -10 (negative price isn't realistic but the
        # math is what we're testing); easier: shift mean to a positive value first.
        s._spread_history = [995.0, 1005.0] * 30  # mean 1000, std 5
        # spread for LONG entry: well below 1000 - entry_z*5 = 990
        # set price_a + 0.5*price_b = 980 → price_a = 80, price_b = 1800
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 80.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 1800.0}}
        )
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        a_leg = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b_leg = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a_leg.transaction_type == "BUY"
        # Negative β → both BUY for LONG_SPREAD
        assert b_leg.transaction_type == "BUY"

    def test_no_entry_past_max_entry_z(self):
        # 2026-05-15 RELIANCE/CIPLA: opened at z=-4.22 and stayed pinned past
        # the configured stop band. max_entry_z is the regime-break ceiling —
        # entries past it are refused. (Inside the band, a deep entry gets a
        # widened per-trade stop; that's covered in TestExit.)
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, max_entry_z=5.0)
        # std=2, price_b=2022 → spread=-11 → z=-5.5 (past -max_entry_z).
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2022.0)
        assert s.scan_and_propose() == []
        # Symmetric upside: price_b=1978 → spread=+11 → z=+5.5.
        self._seed_priced_quotes(s, price_a=1000.0, price_b=1978.0)
        assert s.scan_and_propose() == []

    def test_entry_just_inside_max_entry_z_fires_with_widened_stop(self):
        # Counterpart: an entry at |z|=4.5 (inside max_entry_z=5.0) must fire,
        # and the per-trade effective stop must be widened by safety_buffer so
        # the trade isn't insta-stopped at the next tick — the actual
        # RELIANCE/CIPLA failure mode.
        s = _make_strategy(
            hedge_ratio=0.5, entry_z=2.0, stop_z=4.0,
            max_entry_z=5.0, safety_buffer=0.75,
        )
        # std=2, price_b=2018 → spread=-9 → z=-4.5.
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2018.0)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        s.execute_proposals(proposals)
        # effective_stop_z = max(4.0, 4.5 + 0.75) = 5.25 — gives 0.75σ of room
        # past entry before the per-trade stop fires.
        assert s.state.effective_stop_z == pytest.approx(5.25)

    def test_shallow_entry_keeps_global_stop_z(self):
        # Shallow entry at |z|=2.5 → |entry_z|+buffer = 3.25 < stop_z=4.0, so
        # the per-trade stop stays at the global floor. Guards against an
        # over-eager widening that would loosen risk for normal trades.
        s = _make_strategy(
            hedge_ratio=0.5, entry_z=2.0, stop_z=4.0, safety_buffer=0.75,
        )
        # std=2, price_b=2010 → spread=-5 → z=-2.5.
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        s.execute_proposals(s.scan_and_propose())
        assert s.state.effective_stop_z == pytest.approx(4.0)

    def test_hedge_qty_matches_notional(self):
        # Share-count β-weighted sizing (Varsity Ch. 13/14): qty_b_shares should
        # ≈ |β| × qty_a_shares. With β=0.5 and 1 lot of A = 100 shares, the
        # target B is 50 shares — below B's 200-share lot — so the strategy
        # flips the anchor onto B (qty_b=1 → 200 shares) and scales A up to
        # 4 lots (400 shares). Realized ratio 200/400 = 0.5 = β.
        s = _make_strategy(hedge_ratio=0.5)
        s._spread_history = [-1.0, 1.0] * 30
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        proposals = s.scan_and_propose()
        a_leg = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b_leg = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a_leg.quantity == 4
        assert b_leg.quantity == 1
        # Invariant: realized share-count ratio equals β.
        b_shares = b_leg.quantity * b_leg.lot_size
        a_shares = a_leg.quantity * a_leg.lot_size
        assert b_shares / a_shares == 0.5


# ──────────────────────────────────────────────────────────
# Exit logic
# ──────────────────────────────────────────────────────────

class TestExit:
    def _open_long_spread(self, s, price_a=1000.0, price_b=2000.0, entry_z=-2.5):
        s.state.position = "LONG_SPREAD"
        s.state.entry_time = s._clock()
        s.state.entry_z = entry_z
        s.state.entry_spread = entry_z * 1.0  # std=1 for the standard helper
        # Mirror _set_position_from_legs so check_and_rehedge sees the same
        # per-trade stop band a real entry would have produced.
        s.state.effective_stop_z = max(s.stop_z, abs(entry_z) + s.safety_buffer)
        s.state.legs = [
            PairLeg(symbol="AAA", tradingsymbol="AAA26APRFUT", lot_size=100,
                    quantity=1, entry_price=price_a, current_price=price_a),
            PairLeg(symbol="BBB", tradingsymbol="BBB26APRFUT", lot_size=200,
                    quantity=-1, entry_price=price_b, current_price=price_b),
        ]

    def _set_quote(self, s, price_a, price_b):
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": price_a}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": price_b}}
        )

    def test_no_exit_when_flat(self):
        s = _make_strategy()
        proposals = s.check_and_rehedge()
        assert proposals == []

    def test_mean_revert_exit(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0)
        s._spread_history = [-1.0, 1.0] * 30  # mean 0, std 1
        self._open_long_spread(s)
        # Set prices so spread is near 0 → |z| < exit_z
        self._set_quote(s, price_a=1000.0, price_b=2000.0)  # spread = 0
        proposals = s.check_and_rehedge()
        assert len(proposals) == 2
        # Exit proposals reverse the open legs
        a_exit = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b_exit = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a_exit.transaction_type == "SELL"  # closes long
        assert b_exit.transaction_type == "BUY"   # closes short
        assert "MEAN_REVERT" in a_exit.rationale

    def test_stop_exit(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0)
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        # Spread blew out further negative → |z| >= stop_z
        # spread = 1000 - 0.5*2010 = -5  (z = -5)
        self._set_quote(s, price_a=1000.0, price_b=2010.0)
        proposals = s.check_and_rehedge()
        assert len(proposals) == 2
        assert any("STOP" in p.rationale for p in proposals)

    def test_max_hold_exit(self):
        s = _make_strategy()
        s.max_holding_days = 1
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        # Push entry_time back 5 days
        s.state.entry_time = s._clock() - timedelta(days=5)
        self._set_quote(s, price_a=1000.0, price_b=1999.5)  # z neither exit nor stop
        proposals = s.check_and_rehedge()
        assert len(proposals) == 2
        assert any("MAX_HOLD" in p.rationale for p in proposals)

    def test_max_hold_uses_trading_days_skipping_weekend(self):
        # H6: Fri close → Mon close is 1 trading day, not 3 calendar days.
        # max_holding_days=2 must NOT trigger on Monday after a Friday entry.
        from datetime import datetime as dt
        s = _make_strategy()
        s.max_holding_days = 2
        s._holidays_cache = set()  # no holidays in the test window
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        # Friday 2026-05-22 entry, "now" pinned to Monday 2026-05-25 close.
        s.state.entry_time = dt(2026, 5, 22, 15, 30)
        s._clock = lambda: dt(2026, 5, 25, 15, 30)
        self._set_quote(s, price_a=1000.0, price_b=1999.5)
        proposals = s.check_and_rehedge()
        # 1 trading day held (Monday) < max_holding_days=2 → no MAX_HOLD
        assert not any("MAX_HOLD" in p.rationale for p in proposals)

    def test_max_hold_trading_days_excludes_holidays(self):
        # H6: a configured holiday between entry and now must not count.
        from datetime import datetime as dt
        s = _make_strategy()
        s.max_holding_days = 3
        # Mark Tue 2026-05-26 as a holiday → held = Wed + Thu = 2 trading days.
        s._holidays_cache = {date(2026, 5, 26)}
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        s.state.entry_time = dt(2026, 5, 25, 15, 30)  # Monday
        s._clock = lambda: dt(2026, 5, 28, 15, 30)  # Thursday
        self._set_quote(s, price_a=1000.0, price_b=1999.5)
        proposals = s.check_and_rehedge()
        # 2 trading days < 3 → no MAX_HOLD
        assert not any("MAX_HOLD" in p.rationale for p in proposals)

    def test_deep_entry_not_stopped_inside_widened_band(self):
        # 2026-05-15 RELIANCE/CIPLA regression: a position opened at z=-3.8
        # (just inside max_entry_z=5.0) used to fire EXIT_STOP on the next
        # tick because the global stop_z=4.0 was right next to the entry.
        # With per-trade effective_stop_z = max(stop_z, |entry_z| + 0.75) =
        # 4.55, a z=-4.3 reading is inside the band → no exit.
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0, safety_buffer=0.75)
        s._spread_history = [-1.0, 1.0] * 30  # mean 0, std 1
        self._open_long_spread(s, entry_z=-3.8)
        # spread = 1000 - 0.5*2008.6 = -4.3 → z = -4.3 (inside widened band)
        self._set_quote(s, price_a=1000.0, price_b=2008.6)
        assert s.check_and_rehedge() == []

    def test_deep_entry_stops_when_past_effective_stop(self):
        # Counterpart: drift past the widened stop must still fire EXIT_STOP.
        # Guards against a regression that drops the stop entirely.
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0, safety_buffer=0.75)
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s, entry_z=-3.8)
        # effective_stop_z = 4.55; spread = -4.7 → z = -4.7 (past stop)
        self._set_quote(s, price_a=1000.0, price_b=2009.4)
        proposals = s.check_and_rehedge()
        assert len(proposals) == 2
        assert any("STOP" in p.rationale for p in proposals)

    def test_no_exit_inside_band(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0)
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        # Spread = 1.5 → z=1.5, between exit_z (0.5) and stop_z (4.0)
        self._set_quote(s, price_a=1000.0, price_b=1997.0)  # spread = 1.5
        proposals = s.check_and_rehedge()
        assert proposals == []


# ──────────────────────────────────────────────────────────
# H5 — post-STOP cooldown
# ──────────────────────────────────────────────────────────

class TestStrategyMediums:
    """M-S1..M-S4 — restore-time std drift warning, exit debounce,
    per-trade P&L recording."""

    def _seed_priced_quotes(self, s, price_a=1000.0, price_b=2000.0):
        def fake_quote(symbols):
            sym = symbols[0]
            if "AAA" in sym:
                return {sym: {"last_price": price_a}}
            return {sym: {"last_price": price_b}}
        s.kite.quote = fake_quote

    def _open_long_spread(self, s, entry_z=-2.5, price_a=1000.0, price_b=2000.0):
        s.state.position = "LONG_SPREAD"
        s.state.entry_time = s._clock()
        s.state.entry_z = entry_z
        s.state.entry_spread = entry_z * 1.0
        s.state.effective_stop_z = max(s.stop_z, abs(entry_z) + s.safety_buffer)
        s.state.legs = [
            PairLeg(symbol="AAA", tradingsymbol="AAA26APRFUT", lot_size=100,
                    quantity=1, entry_price=price_a, current_price=price_a),
            PairLeg(symbol="BBB", tradingsymbol="BBB26APRFUT", lot_size=200,
                    quantity=-1, entry_price=price_b, current_price=price_b),
        ]

    # M-S1: std-drift warning on restore
    def test_restore_warns_on_std_drift(self, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="strategies.pair_trading")
        s = _make_strategy(hedge_ratio=0.5)
        # Today's seed has std = 1.0 (alternating ±1.0)
        s._spread_history = [-1.0, 1.0] * 30
        # Saved state: entry_z = -3.0 against an OLD distribution where
        # std was much larger (so the SAME entry_spread implies a much
        # less extreme z under today's tighter std).
        blob = {
            "pair": ["AAA", "BBB"],
            "hedge_ratio": 0.5,
            "state": {
                "position": "LONG_SPREAD",
                "entry_z": -3.0,
                "entry_time": None,
                "entry_spread": -1.5,  # under today's std=1 this is z=-1.5
                "effective_stop_z": 4.5,
                "legs": [
                    {"symbol": "AAA", "tradingsymbol": "AAA26APRFUT",
                     "lot_size": 100, "quantity": 1, "entry_price": 1000.0},
                    {"symbol": "BBB", "tradingsymbol": "BBB26APRFUT",
                     "lot_size": 200, "quantity": -1, "entry_price": 2000.0},
                ],
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_transaction_costs": 0.0,
            },
        }
        s.restore_state(blob)
        assert any("M-S1" in r.message and "drifted" in r.message
                   for r in caplog.records)

    def test_restore_no_warn_when_drift_small(self, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="strategies.pair_trading")
        s = _make_strategy(hedge_ratio=0.5)
        s._spread_history = [-1.0, 1.0] * 30
        # entry_z=-2.5 and entry_spread=-2.5 → today recomputes to -2.5
        # (mean=0, std=1) → 0 drift.
        blob = {
            "pair": ["AAA", "BBB"], "hedge_ratio": 0.5,
            "state": {
                "position": "LONG_SPREAD", "entry_z": -2.5,
                "entry_time": None, "entry_spread": -2.5,
                "effective_stop_z": 4.0,
                "legs": [
                    {"symbol": "AAA", "tradingsymbol": "AAA26APRFUT",
                     "lot_size": 100, "quantity": 1, "entry_price": 1000.0},
                    {"symbol": "BBB", "tradingsymbol": "BBB26APRFUT",
                     "lot_size": 200, "quantity": -1, "entry_price": 2000.0},
                ],
                "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "total_transaction_costs": 0.0,
            },
        }
        s.restore_state(blob)
        assert not any("M-S1" in r.message for r in caplog.records)

    # M-S3: exit debounce
    def test_exit_debounce_holds_first_tick(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0)
        s.exit_debounce_ticks = 2
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        # spread = 1000 - 0.5*2000 = 0 → z=0, inside exit band
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2000.0)
        proposals = s.check_and_rehedge()
        # First in-band tick must NOT exit — streak only at 1
        assert proposals == []
        assert s.state.mean_revert_streak == 1

    def test_exit_debounce_fires_on_second_tick(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0)
        s.exit_debounce_ticks = 2
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2000.0)
        s.check_and_rehedge()  # tick 1 — no exit
        proposals = s.check_and_rehedge()  # tick 2 — exit
        assert any("MEAN_REVERT" in p.rationale for p in proposals)

    def test_exit_debounce_resets_on_out_of_band(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0)
        s.exit_debounce_ticks = 2
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        # Tick 1: inside band — streak goes to 1, no exit.
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2000.0)
        s.check_and_rehedge()
        assert s.state.mean_revert_streak == 1
        # Tick 2: bounce outside band — streak resets to 0.
        self._seed_priced_quotes(s, price_a=1000.0, price_b=1998.0)  # spread=1, z=1
        s.check_and_rehedge()
        assert s.state.mean_revert_streak == 0

    # M-S4: per-trade P&L
    def test_record_close_writes_per_trade_pnl(self):
        s = _make_strategy(hedge_ratio=0.5)
        # Simulate prior trade left realized=500 cumulative
        s.state.realized_pnl = 500.0
        s.state.total_transaction_costs = 100.0
        s.state.realized_at_entry = 500.0
        s.state.tx_costs_at_entry = 100.0
        # Now this trade adds 300 realized, 50 costs
        s.state.realized_pnl = 800.0
        s.state.total_transaction_costs = 150.0
        s.state.entry_time = s._clock()
        s.state.entry_z = -2.5
        s.state.entry_spread = -2.5
        s.state.position = "LONG_SPREAD"
        s._record_close()
        row = s.state.closed_trades[-1]
        # Per-trade delta, not cumulative
        assert row["realized_pnl"] == 300.0
        assert row["transaction_costs"] == 50.0
        # Cumulative preserved as a separate column
        assert row["cumulative_realized_pnl"] == 800.0

    def test_entry_baseline_captured_before_entry_fills(self):
        # Code-review follow-up 2026-07-11: the M-S4 baseline must be the
        # headline BEFORE the entry legs' fills book their costs. The old
        # _set_position_from_legs placement snapshotted after the fills, so
        # every closed row structurally excluded its own entry costs and the
        # per-trade ledger could never sum to the headline.
        s = _make_strategy(hedge_ratio=0.5)
        s.state.realized_pnl = 1000.0  # prior trades
        s.state.total_transaction_costs = 200.0
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2018.0)
        s._spread_history = [-2.0, 2.0] * 30   # std=2 → z=-4.5, inside bands
        s.execute_proposals(s.scan_and_propose())
        assert s.state.position != "FLAT"
        assert s.state.realized_at_entry == 1000.0, "baseline = PRE-fill headline"
        assert s.state.realized_pnl < 1000.0, "entry costs booked after baseline"
        assert s.state.tx_costs_at_entry == 200.0

    def test_round_trip_ledger_reconciles_with_headline(self):
        # THE identity the baseline fix exists for: after a full open→close
        # round trip, Σ closed-row deltas == headline realized (entry costs,
        # rehedges and exit all inside one delta window). Fails under the
        # pre-2026-07-11 semantics by exactly the entry costs.
        s = _make_strategy(hedge_ratio=0.5)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2018.0)
        s._spread_history = [-2.0, 2.0] * 30
        s.execute_proposals(s.scan_and_propose())
        assert s.state.position != "FLAT"
        exits = s._build_exit_proposals(
            reason="MAX_HOLD", z=0.0, prices={"AAA": 1000.0, "BBB": 2000.0})
        s.execute_proposals(exits)
        assert s.state.position == "FLAT" and len(s.state.closed_trades) == 1
        ledger = sum(r["realized_pnl"] for r in s.state.closed_trades)
        assert ledger == pytest.approx(s.state.realized_pnl), \
            "per-trade ledger must sum to the headline (incl. entry costs)"

    def test_restore_self_anchors_legacy_state_then_warns_on_new_drift(self, caplog):
        import logging as _logging
        # Legacy state (pre-anchor, rows exclude entry costs → headline ≠
        # ledger): the FIRST restore self-anchors silently — the live book
        # must not cry wolf over known history. A LATER headline change with
        # no matching row (surgery / bug) must warn.
        s = _make_strategy(hedge_ratio=0.5)
        s.state.realized_pnl = 500.0
        s.state.closed_trades = [{
            "exit_time": s._clock(), "entry_time": s._clock(),
            "entry_z": -2.5, "entry_spread": -5.0,
            "realized_pnl": 100.0, "transaction_costs": 50.0,
            "cumulative_realized_pnl": 500.0, "position": "LONG_SPREAD",
        }]
        assert s.state.ledger_anchor is None      # never restored = legacy
        blob = s.serialize_state()

        first = _make_strategy(hedge_ratio=0.5)
        with caplog.at_level(_logging.WARNING):
            first.restore_state(blob)
        assert not any("LEDGER DRIFT" in r.message for r in caplog.records), \
            "first restore must self-anchor, not warn about known history"
        assert first.state.ledger_anchor == pytest.approx(400.0)

        # Surgery after anchoring: headline moves, no row explains it.
        first.state.realized_pnl += 123.0
        blob2 = first.serialize_state()
        second = _make_strategy(hedge_ratio=0.5)
        caplog.clear()
        with caplog.at_level(_logging.WARNING):
            second.restore_state(blob2)
        assert any("LEDGER DRIFT" in r.message for r in caplog.records), \
            "post-anchor drift must warn on the live book"


class TestBrokerMediums:
    """M-B1..M-B5 — paper validate, paper slippage, expiry-day refusal,
    kite-exception specificity, place_order backoff."""

    def _live_strategy(self):
        s = _make_strategy(mode="live")
        s.kite.VARIETY_REGULAR = "regular"
        s.kite.TRANSACTION_TYPE_BUY = "BUY"
        s.kite.TRANSACTION_TYPE_SELL = "SELL"
        s.kite.PRODUCT_NRML = "NRML"
        s.kite.ORDER_TYPE_MARKET = "MARKET"
        s.kite.VALIDITY_DAY = "DAY"
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 10_000_000.0}},
        })
        return s

    def _prop(self, qty=1, txn="BUY", price=1000.0,
              tradingsymbol="AAA26APRFUT", expiry="2026-04-28"):
        return TradeProposal(
            tradingsymbol=tradingsymbol, instrument_token=111, strike=0,
            expiry=expiry, option_type="FUT", lot_size=100,
            quantity=qty, price=price, transaction_type=txn,
            iv=0, bid_ask_spread_pct=0.0, margin_required=20000,
            rationale="entry",
        )

    # M-B1: paper applies validate_order
    def test_paper_rejects_nan_price(self):
        s = _make_strategy(mode="paper")
        prop = self._prop(price=float("nan"))
        result = s._paper_execute(prop)
        assert result["status"] == "FAILED"
        assert "validation" in result["error"]

    # M-B2: paper slip is applied per direction
    def test_paper_buy_pays_above_ltp_with_slip(self):
        s = _make_strategy(mode="paper")
        s.paper_slippage_bps = 10.0  # 10bp = 0.10%
        result = s._paper_execute(self._prop(txn="BUY", price=1000.0))
        # 1000 * (1 + 0.001) = 1001.0
        assert abs(result["average_price"] - 1001.0) < 1e-6

    def test_paper_sell_hits_below_ltp_with_slip(self):
        s = _make_strategy(mode="paper")
        s.paper_slippage_bps = 10.0
        result = s._paper_execute(self._prop(txn="SELL", price=2000.0))
        # 2000 * (1 - 0.001) = 1998.0
        assert abs(result["average_price"] - 1998.0) < 1e-6

    def test_paper_zero_slip_matches_ltp(self):
        s = _make_strategy(mode="paper")
        s.paper_slippage_bps = 0.0
        result = s._paper_execute(self._prop(price=1234.5))
        assert result["average_price"] == 1234.5

    # M-B3: expiry-day refuse
    def test_refuses_entry_when_expiry_is_today(self):
        from datetime import datetime as dt
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, exit_z=0.5,
                           max_leg_notional=2_000_000.0, lots_per_leg=1)
        # Pin clock to 2026-04-28
        s._clock = lambda: dt(2026, 4, 28, 14, 0)
        # Both legs' cached expiry = today
        s._cached_futures = {
            "AAA": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                    "expiry": "2026-04-28", "instrument_token": 111},
            "BBB": {"tradingsymbol": "BBB26APRFUT", "lot_size": 200,
                    "expiry": "2026-04-28", "instrument_token": 222},
        }
        s._spread_history = [-1.0, 1.0] * 30 + [-5.0]
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 1000.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 2010.0}}
        )
        proposals = s.scan_and_propose()
        assert proposals == []

    def test_allows_entry_when_expiry_is_not_today(self):
        from datetime import datetime as dt
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, exit_z=0.5,
                           max_leg_notional=2_000_000.0, lots_per_leg=1,
                           min_edge_multiplier=0.0)
        s._clock = lambda: dt(2026, 4, 21, 14, 0)
        # Expiry next week
        s._cached_futures = {
            "AAA": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                    "expiry": "2026-04-28", "instrument_token": 111},
            "BBB": {"tradingsymbol": "BBB26APRFUT", "lot_size": 200,
                    "expiry": "2026-04-28", "instrument_token": 222},
        }
        s._spread_history = [-1.0, 1.0] * 30 + [-5.0]
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 1000.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 2010.0}}
        )
        proposals = s.scan_and_propose()
        assert len(proposals) == 2

    # M-B4: distinguish exception classes
    def test_network_exception_retries_once(self):
        from strategies.pair_trading import _NetworkException
        s = self._live_strategy()
        calls = {"n": 0}

        def fake_place(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _NetworkException("transient")
            return "ORD-RETRY"

        s.kite.place_order = fake_place
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        s.execute_proposals([self._prop()])
        assert calls["n"] == 2  # original + retry
        assert len(s.state.legs) == 1

    def test_order_exception_does_not_retry(self):
        from strategies.pair_trading import _OrderException
        s = self._live_strategy()
        calls = {"n": 0}

        def fake_place(*a, **kw):
            calls["n"] += 1
            raise _OrderException("margin shortfall")

        s.kite.place_order = fake_place
        s.execute_proposals([self._prop()])
        # Broker-side reject: no retry, single call
        assert calls["n"] == 1
        assert s.state.legs == []

    # M-B5: place_order backoff
    def test_consecutive_failures_arm_backoff(self):
        s = self._live_strategy()
        # Force 3 consecutive non-COMPLETE results
        s.kite.place_order = MagicMock(side_effect=RuntimeError("broken"))
        for _ in range(3):
            s.execute_proposals([self._prop()])
        assert s._place_order_skip_ticks_left == 5
        # 4th call: backoff short-circuits BEFORE place_order is hit
        call_count_before = s.kite.place_order.call_count
        s.execute_proposals([self._prop()])
        assert s.kite.place_order.call_count == call_count_before

    def test_backoff_burns_one_tick_per_execute_call_not_per_leg(self):
        # Bug guard (review-found 2026-05-28): the cooldown decrement was
        # inside _live_execute, so a 2-leg pair entry consumed 2 skip-ticks
        # per tick. Decrement is now in execute_proposals — once per call
        # regardless of leg count.
        s = self._live_strategy()
        # Pre-arm cooldown to 5 ticks
        s._place_order_skip_ticks_left = 5
        s._place_order_fail_streak = 0
        # Two-leg batch in a single call
        a = self._prop()
        b = self._prop()
        b.tradingsymbol = "BBB26APRFUT"
        s.execute_proposals([a, b])
        # One call → one tick burned, not two
        assert s._place_order_skip_ticks_left == 4

    def test_backoff_last_tick_does_not_immediately_rearm(self):
        # Bug guard: when skip_ticks decremented from 1 → 0 inside the
        # tick, the resulting FAILED outcome used to increment fail_streak
        # → potentially rearm the cooldown on the very next FAILED tick.
        # Now the streak is cleared when the cooldown ends.
        s = self._live_strategy()
        s._place_order_skip_ticks_left = 1
        s._place_order_fail_streak = 0
        s.kite.place_order = MagicMock(side_effect=RuntimeError("broker still flaky"))
        # This call: decrement 1 → 0, fail_streak cleared. The FAILED
        # outcome from the actual place_order then increments to 1 (not 3).
        s.execute_proposals([self._prop()])
        assert s._place_order_skip_ticks_left == 0
        assert s._place_order_fail_streak == 1  # NOT >= threshold

    def test_backoff_clears_on_complete(self):
        s = self._live_strategy()
        # Two failures, then success
        seq = [
            RuntimeError("broken"),
            RuntimeError("broken"),
            "ORD-OK",
        ]

        def fake_place(*a, **kw):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v

        s.kite.place_order = fake_place
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        for _ in range(3):
            s.execute_proposals([self._prop()])
        # COMPLETE on the 3rd attempt clears the streak (it was only 2)
        assert s._place_order_fail_streak == 0
        assert s._place_order_skip_ticks_left == 0


class TestBookNotionalCap:
    """H13: total Σ open_notional ceiling across all runners. Refuses new
    entries when current book is already at or above cap."""

    def _seed_priced_quotes(self, s, price_a=1000.0, price_b=2010.0):
        def fake_quote(symbols):
            sym = symbols[0]
            if "AAA" in sym:
                return {sym: {"last_price": price_a}}
            return {sym: {"last_price": price_b}}
        s.kite.quote = fake_quote

    def test_no_cap_allows_entry(self):
        # Cap unset → callback never called; entry proceeds.
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=2_000_000.0,
                           lots_per_leg=1, entry_z=2.0)
        s._spread_history = [-1.0, 1.0] * 30 + [-5.0]
        self._seed_priced_quotes(s)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2

    def test_book_at_cap_refuses_new_entry(self):
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=2_000_000.0,
                           lots_per_leg=1, entry_z=2.0)
        s._spread_history = [-1.0, 1.0] * 30 + [-5.0]
        s.max_book_notional = 1_000_000.0
        s._book_notional_fn = lambda: 1_100_000.0  # above cap
        self._seed_priced_quotes(s)
        proposals = s.scan_and_propose()
        assert proposals == []

    def test_book_below_cap_allows_entry(self):
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=2_000_000.0,
                           lots_per_leg=1, entry_z=2.0)
        s._spread_history = [-1.0, 1.0] * 30 + [-5.0]
        s.max_book_notional = 5_000_000.0
        s._book_notional_fn = lambda: 1_000_000.0  # well below cap
        self._seed_priced_quotes(s)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2

    def test_callback_exception_does_not_block_entry(self):
        # H13: a flaky callback (e.g. IO error reading sibling state file)
        # must NOT silently halt trading. Fall through with book=0.0 and
        # log the failure. The cap still applies if cap=0 implies "I know
        # there is at least this much" — but the conservative call here is
        # to keep entries flowing so a transient bug doesn't freeze the
        # strategy. Symmetric with H5/H7 fail-loud-but-don't-deadlock.
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=2_000_000.0,
                           lots_per_leg=1, entry_z=2.0)
        s._spread_history = [-1.0, 1.0] * 30 + [-5.0]
        s.max_book_notional = 1_000_000.0
        s._book_notional_fn = lambda: (_ for _ in ()).throw(IOError("boom"))
        self._seed_priced_quotes(s)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2

    def test_aggregate_helper_reads_pair_state(self, tmp_path):
        # H13: the cross-runner aggregator reads both pair- and taleb-shape
        # paper-state files in data_cache/.
        import json
        from strategies.pair_trading import _aggregate_book_notional
        (tmp_path / "pair_paper_state_baseline.json").write_text(json.dumps({
            "pairs": [{
                "state": {"legs": [
                    {"entry_price": 1000.0, "quantity": 2, "lot_size": 100},
                    {"entry_price": 2000.0, "quantity": -1, "lot_size": 200},
                ]},
            }],
        }))
        (tmp_path / "taleb_paper_state.json").write_text(json.dumps({
            "positions": [{
                "legs": [
                    {"entry_price": 50.0, "quantity": 4, "lot_size": 50},
                ],
            }],
        }))
        total = _aggregate_book_notional(tmp_path)
        # 1000*2*100 + 2000*1*200 + 50*4*50 = 200k + 400k + 10k = 610_000
        assert total == 610_000.0

    def test_aggregate_helper_handles_malformed_file(self, tmp_path):
        from strategies.pair_trading import _aggregate_book_notional
        (tmp_path / "pair_paper_state_bad.json").write_text("not json{")
        (tmp_path / "pair_paper_state_ok.json").write_text(
            '{"pairs":[{"state":{"legs":[]}}]}'
        )
        # Should not raise; bad file is skipped.
        assert _aggregate_book_notional(tmp_path) == 0.0


class TestStopCooldown:
    """A pair that stops out at z=4.2 must not re-enter on the very next
    tick. The cooldown arms ONLY on STOP — MEAN_REVERT and MAX_HOLD allow
    immediate re-entry. The gate persists across state save/restore."""

    def _stage_long_spread(self, s, entry_z=-2.5, price_a=1000.0, price_b=2000.0):
        s.state.position = "LONG_SPREAD"
        s.state.entry_time = s._clock()
        s.state.entry_z = entry_z
        s.state.entry_spread = entry_z * 1.0
        s.state.effective_stop_z = max(s.stop_z, abs(entry_z) + s.safety_buffer)
        s.state.legs = [
            PairLeg(symbol="AAA", tradingsymbol="AAA26APRFUT", lot_size=100,
                    quantity=1, entry_price=price_a, current_price=price_a),
            PairLeg(symbol="BBB", tradingsymbol="BBB26APRFUT", lot_size=200,
                    quantity=-1, entry_price=price_b, current_price=price_b),
        ]

    def _force_stop_then_close(self, s):
        """Call _build_exit_proposals(STOP, …), then run execute_proposals on
        the returned legs so the book actually flattens — that's the path
        that promotes the reason onto state."""
        proposals = s._build_exit_proposals(
            reason="STOP", z=4.2,
            prices={"AAA": 1010.0, "BBB": 2000.0},
        )
        s.execute_proposals(proposals)

    def test_stop_arms_cooldown_and_blocks_immediate_reentry(self):
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, stop_z=4.0,
                           spread_history=[-1.0, 1.0] * 30)
        s.stop_cooldown_minutes = 60
        self._stage_long_spread(s)
        self._force_stop_then_close(s)

        assert s.state.position == "FLAT"
        assert s.state.last_exit_reason == "STOP"
        assert s.state.last_exit_time == s._clock()
        # Even at a clean re-entry z, the gate must hold.
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 1000.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 1994.0}}  # spread=3 → z=3.0 entry signal
        )
        assert s.scan_and_propose() == []

    def test_reentry_allowed_after_cooldown_elapses(self):
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, stop_z=4.0,
                           spread_history=[-1.0, 1.0] * 30)
        s.stop_cooldown_minutes = 60
        self._stage_long_spread(s)
        self._force_stop_then_close(s)
        # Jump the clock past the cooldown window.
        original_clock = s._clock()
        s._clock = lambda: original_clock + timedelta(minutes=61)
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 1000.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 1994.0}}
        )
        proposals = s.scan_and_propose()
        assert len(proposals) == 2, "Cooldown elapsed — re-entry should fire"

    def test_mean_revert_exit_does_not_arm_cooldown(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0,
                           spread_history=[-1.0, 1.0] * 30)
        s.stop_cooldown_minutes = 60
        self._stage_long_spread(s)
        proposals = s._build_exit_proposals(
            reason="MEAN_REVERT", z=0.0,
            prices={"AAA": 1000.0, "BBB": 2000.0},
        )
        s.execute_proposals(proposals)
        assert s.state.last_exit_reason == "MEAN_REVERT"
        # _is_in_stop_cooldown gates only on reason=='STOP', so no block.
        assert s._is_in_stop_cooldown() is False

    def test_zero_minutes_disables_cooldown(self):
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, stop_z=4.0,
                           spread_history=[-1.0, 1.0] * 30)
        s.stop_cooldown_minutes = 0
        self._stage_long_spread(s)
        self._force_stop_then_close(s)
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 1000.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 1994.0}}
        )
        assert len(s.scan_and_propose()) == 2

    def test_cooldown_persists_through_state_roundtrip(self):
        """STOP at 14:30 IST → state saved → restored next morning. The
        cooldown should still gate re-entry until 60 min from the STOP."""
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, stop_z=4.0,
                           spread_history=[-1.0, 1.0] * 30)
        s.stop_cooldown_minutes = 60
        self._stage_long_spread(s)
        self._force_stop_then_close(s)
        blob = s.serialize_state()

        # Rebuild and restore.
        s2 = _make_strategy(hedge_ratio=0.5, entry_z=2.0, stop_z=4.0,
                            spread_history=[-1.0, 1.0] * 30)
        s2.stop_cooldown_minutes = 60
        s2._clock = lambda: s._clock() + timedelta(minutes=30)  # next morning, 30 min in
        s2.restore_state(blob)
        assert s2.state.last_exit_reason == "STOP"
        # 30 min < 60 min cooldown → still blocked.
        assert s2._is_in_stop_cooldown() is True
        # Advance another 31 min → past cooldown.
        s2._clock = lambda: s._clock() + timedelta(minutes=61)
        assert s2._is_in_stop_cooldown() is False

    def test_old_state_without_cooldown_keys_restores_clean(self):
        """Backwards-compatibility: a pre-H5 state file (no last_exit_* keys)
        must restore as 'no active cooldown', not blow up."""
        s = _make_strategy(hedge_ratio=0.5, entry_z=2.0, stop_z=4.0,
                           spread_history=[-1.0, 1.0] * 30)
        s.stop_cooldown_minutes = 60
        blob = {
            "pair": ["AAA", "BBB"],
            "hedge_ratio": 0.5,
            "state": {
                "position": "FLAT",
                "entry_z": 0.0,
                "entry_time": None,
                "entry_spread": 0.0,
                "effective_stop_z": 0.0,
                "legs": [],
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_transaction_costs": 0.0,
                "closed_trades": [],
                # NOTE: deliberately missing last_exit_time / last_exit_reason
            },
        }
        s.restore_state(blob)
        assert s.state.last_exit_time is None
        assert s.state.last_exit_reason is None
        assert s._is_in_stop_cooldown() is False


# ──────────────────────────────────────────────────────────
# Mode dispatch / signals JSONL
# ──────────────────────────────────────────────────────────

class TestModeDispatch:
    def test_signals_mode_emits_jsonl_and_skips_state(self, tmp_path, monkeypatch):
        s = _make_strategy(mode="signals")
        # Point _emit_signal at tmp dir by stubbing config.get for log_dir
        s.config.get = MagicMock(return_value=str(tmp_path))
        prop = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=1, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0, margin_required=20000,
            rationale="test entry",
        )
        results = s.execute_proposals([prop])
        assert len(results) == 1
        assert results[0]["status"] == "SIGNAL_LOGGED"
        assert results[0]["mode"] == "signals"
        # State must remain pristine
        assert s.state.position == "FLAT"
        assert s.state.legs == []
        assert s.state.realized_pnl == 0.0
        # JSONL was written
        files = list(tmp_path.glob("signals-*.jsonl"))
        assert len(files) == 1
        record = json.loads(files[0].read_text().strip())
        assert record["strategy"] == "pair_trading"
        assert record["tradingsymbol"] == "AAA26APRFUT"

    def test_paper_mode_updates_state(self):
        s = _make_strategy(mode="paper")
        prop = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=1, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0, margin_required=20000,
            rationale="test entry",
        )
        results = s.execute_proposals([prop])
        assert results[0]["status"] == "COMPLETE"
        assert results[0]["mode"] == "paper"
        assert len(s.state.legs) == 1
        assert s.state.legs[0].symbol == "AAA"
        assert s.state.legs[0].quantity == 1
        # Costs deducted from realized_pnl
        assert s.state.total_transaction_costs > 0
        assert s.state.realized_pnl < 0  # only costs so far


# ──────────────────────────────────────────────────────────
# Fill handling / position netting
# ──────────────────────────────────────────────────────────

class TestApplyFill:
    def test_close_removes_leg_and_books_pnl(self):
        s = _make_strategy(mode="paper")
        # Open
        open_prop = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=1, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0, margin_required=20000, rationale="open",
        )
        s.execute_proposals([open_prop])
        assert len(s.state.legs) == 1

        # Close at +50 → realized = 50 * 1 * 100 = 5000 (minus costs)
        close_prop = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=1, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1050.0, transaction_type="SELL",
            iv=0, bid_ask_spread_pct=0, margin_required=0, rationale="close",
        )
        s.execute_proposals([close_prop])
        assert len(s.state.legs) == 0
        # 5000 minus round-trip costs should still be solidly positive
        assert s.state.realized_pnl > 4000

    def test_partial_close_books_pnl_on_closed_portion(self):
        s = _make_strategy(mode="paper")
        # Open 3 lots
        s.state.legs = [PairLeg(
            symbol="AAA", tradingsymbol="AAA26APRFUT", lot_size=100,
            quantity=3, entry_price=1000.0, current_price=1000.0,
        )]
        # Close 1 of 3 lots at 1100 → realized = 100 * 1 * 100 = 10,000 minus costs
        close_prop = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=1, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1100.0, transaction_type="SELL",
            iv=0, bid_ask_spread_pct=0, margin_required=0, rationale="partial close",
        )
        s._apply_fill(close_prop)
        assert len(s.state.legs) == 1
        assert s.state.legs[0].quantity == 2  # 3 - 1
        assert s.state.realized_pnl > 9000


# ──────────────────────────────────────────────────────────
# EOD report
# ──────────────────────────────────────────────────────────

class TestNotionalCap:
    """A high-β pair can deploy huge notional with lots_per_leg=1 — the cap
    scales both legs down (preserving the hedge ratio) or skips the entry."""

    def _seed_priced_quotes(self, s, price_a=1000.0, price_b=2000.0):
        # mean=0, std=2 so price_b=2010 → spread=-5 → z=-2.5, inside the
        # entry band and below max_entry_z=5.0.
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": price_a}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": price_b}}
        )
        s._spread_history = [-2.0, 2.0] * 30

    def test_no_cap_means_no_change(self):
        # max_leg_notional=None disables the cap entirely. Share-count sizing
        # decides: β=0.5, 1 lot of A (100 shares) wants 50 shares of B but
        # B's lot is 200, so the anchor flips to B (qty_b=1, 200 shares)
        # and qty_a scales up to 4 (400 shares) to preserve ratio = β = 0.5.
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=None)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        a = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a.quantity == 4
        assert b.quantity == 1

    def test_cap_scales_high_beta_down(self):
        # β=10, lots_per_leg=1, price_a=100, lot_a=100 → notional_a = 10k
        # target_notional_b = 10 * 10k = 100k. Cap at 50k → scale = 0.5
        # qty_a scales to round(1*0.5)=1 (but 1 lot still gives notional_a=10k OK)
        # target_notional_b after scaling = 10 * 10k = 100k > 50k still
        # Hmm: scaling qty_a doesn't help when the OUTSIZE leg is B and qty_a is already 1.
        # What we want: the cap forces a refusal because 1 lot of A implies 100k of B.
        # Use price_a = 50 so 1 lot of A = 5k notional, β=10 → target B = 50k = cap exactly.
        s = _make_strategy(hedge_ratio=10.0, max_leg_notional=50_000.0, lots_per_leg=1)
        # Seed mean=0, std=400 so spread=-960 → z=-2.4 (inside entry band, below
        # max_entry_z=5.0). A tighter std would put |z| past the regime-break
        # ceiling and the gate would refuse before the cap logic runs.
        s._spread_history = [-400.0, 400.0] * 30
        # spread = 50 - 10*101 = -960 → LONG_SPREAD entry
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 50.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 101.0}}
        )
        proposals = s.scan_and_propose()
        # 1 lot of A → notional 5k; β=10 → target B = 50k; cap 50k OK; entry succeeds
        assert len(proposals) == 2
        a = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a.quantity == 1
        # qty_b = round(50k / (200*101)) = round(2.475) = 2
        assert b.quantity == 2

    def test_cap_skips_entry_when_one_lot_busts_it(self):
        # 1 lot of A alone is 100k; cap 50k → must refuse
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=50_000.0, lots_per_leg=1)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        # spread far below mean → would normally enter LONG_SPREAD
        proposals = s.scan_and_propose()
        assert proposals == []
        assert s.state.position == "FLAT"

    def test_cap_scales_lots_per_leg_down(self):
        # lots_per_leg=10, β=0.5, prices 1000/2010, lots 100/200.
        # Share-count sizing: qty_a=10, target_b_shares = 0.5*10*100 = 500,
        # qty_b = round(500/200) = round(2.5) = 2 (Python banker's rounding).
        # Natural notionals: A=10*100*1000=1,000k, B=2*200*2010=804k → max=1,000k.
        # 1-lot gate needs cap ≥ max(one_lot_a=100k, one_lot_b=402k) = 402k.
        # Cap 500k → scale=0.5 (exact) → qty_a=5, qty_b=1. The proportional
        # scale-down preserves the share-count ratio (200/500=0.4 ≠ β here
        # only because the post-scale qty_b is floor'd at 1 lot — see
        # `max(..., 1)` in the cap branch); this is the documented behaviour.
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=500_000.0, lots_per_leg=10)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        a = next(p for p in proposals if p.tradingsymbol == "AAA26APRFUT")
        b = next(p for p in proposals if p.tradingsymbol == "BBB26APRFUT")
        assert a.quantity == 5
        assert b.quantity == 1
        # Invariant: post-cap, neither leg's deployed notional exceeds the cap.
        assert a.quantity * a.lot_size * a.price <= 500_000
        assert b.quantity * b.lot_size * b.price <= 500_000

    def test_clamp_above_2x_emits_warning(self, caplog):
        # H12: when the notional cap forces a >2× downscale, log a WARNING
        # so the operator notices a too-large --lots-per-leg vs cap.
        # lots_per_leg=20, β=0.5 → qty_a=20, qty_b=5. Natural notionals:
        # A=20*100*1000=2,000k, B=5*200*2010=2,010k → max=2,010k.
        # Cap 500k → scale ≈ 0.249 (>4× downscale) → warning expected.
        import logging
        caplog.set_level(logging.WARNING, logger="strategies.pair_trading")
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=500_000.0, lots_per_leg=20)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        s.scan_and_propose()
        assert any("notional cap clamped lots" in r.message for r in caplog.records)

    def test_clamp_below_2x_no_warning(self, caplog):
        # H12: a mild clamp (<2×) is routine high-β rebalancing — no warning.
        import logging
        caplog.set_level(logging.WARNING, logger="strategies.pair_trading")
        # lots_per_leg=10, cap 800k. Natural max ≈ 1000k → scale=0.8 (<2× ↓).
        s = _make_strategy(hedge_ratio=0.5, max_leg_notional=800_000.0, lots_per_leg=10)
        self._seed_priced_quotes(s, price_a=1000.0, price_b=2010.0)
        s.scan_and_propose()
        assert not any("notional cap clamped lots" in r.message for r in caplog.records)


class TestEODReport:
    def test_eod_report_keys(self):
        s = _make_strategy()
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 1000.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 2000.0}}
        )
        s._spread_history = [-1.0, 1.0] * 30
        report = s.generate_eod_report()
        assert report["strategy"] == "pair_trading"
        assert report["pair"] == ("AAA", "BBB")
        assert report["position"] == "FLAT"
        assert "current_z" in report
        assert "realized_pnl" in report
        assert "spread_history_size" in report

    def test_session_deltas_start_at_zero_when_baseline_matches_state(self):
        s = _make_strategy()
        s.kite.quote = lambda syms: {syms[0]: {"last_price": 1000.0}}
        s._spread_history = [-1.0, 1.0] * 30
        # Simulate restored state: counters are non-zero, but session
        # baseline matches them (re-baselined in restore_state).
        s.state.realized_pnl = 5000.0
        s.state.unrealized_pnl = 300.0
        s._session_start_realized = 5000.0
        s._session_start_unrealized = 300.0
        report = s.generate_eod_report()
        assert report["realized_pnl"] == 5000.0
        assert report["session_realized_delta"] == 0.0
        assert report["session_unrealized_delta"] == 0.0

    def test_session_deltas_capture_this_session_change(self):
        s = _make_strategy()
        s.kite.quote = lambda syms: {syms[0]: {"last_price": 1000.0}}
        s._spread_history = [-1.0, 1.0] * 30
        # Baseline = yesterday's close. Today added another 2000 realised
        # and 100 unrealised — those are the per-session deltas.
        s._session_start_realized = 5000.0
        s._session_start_unrealized = 300.0
        s.state.realized_pnl = 7000.0
        s.state.unrealized_pnl = 400.0
        report = s.generate_eod_report()
        assert report["session_realized_delta"] == pytest.approx(2000.0)
        assert report["session_unrealized_delta"] == pytest.approx(100.0)


# ──────────────────────────────────────────────────────────
# Cross-session persistence (serialize_state / restore_state)
# ──────────────────────────────────────────────────────────

class TestSerializeRestore:
    """The 2026-05-19 rebuild removed the EOD flatten. Open positions now
    survive across sessions via serialize_state/restore_state. Roundtrip
    correctness is load-bearing — a partial restore would silently start
    a strategy fresh and abandon a real position."""

    def _build_held_position(self) -> PairTradingStrategy:
        """A strategy with an OPEN LONG_SPREAD position, two legs, some
        realized P&L and a couple of closed trades."""
        s = _make_strategy(hedge_ratio=0.5)
        s.state.position = "LONG_SPREAD"
        s.state.entry_z = -2.10
        s.state.entry_time = datetime(2026, 5, 19, 15, 2, 19)
        s.state.entry_spread = 845.70
        s.state.effective_stop_z = 4.0
        s.state.legs = [
            PairLeg(symbol="AAA", tradingsymbol="AAA26MAYFUT",
                    lot_size=100, quantity=2,
                    entry_price=1327.00, current_price=1323.20),
            PairLeg(symbol="BBB", tradingsymbol="BBB26MAYFUT",
                    lot_size=200, quantity=-1,
                    entry_price=311.25, current_price=310.05),
        ]
        s.state.realized_pnl = -800.0
        s.state.unrealized_pnl = -50.0
        s.state.total_transaction_costs = 800.0
        s.state.closed_trades = [
            {"exit_time": datetime(2026, 5, 18, 12, 0, 0),
             "entry_time": datetime(2026, 5, 17, 10, 0, 0),
             "entry_z": 2.5, "entry_spread": 100.0,
             "realized_pnl": 1000.0, "transaction_costs": 400.0,
             "position": "SHORT_SPREAD"},
        ]
        return s

    def test_roundtrip_open_position(self):
        s1 = self._build_held_position()
        blob = s1.serialize_state()

        s2 = _make_strategy(hedge_ratio=0.5)
        s2.restore_state(blob)

        assert s2.state.position == "LONG_SPREAD"
        assert s2.state.entry_z == pytest.approx(-2.10)
        assert s2.state.entry_time == datetime(2026, 5, 19, 15, 2, 19)
        assert s2.state.entry_spread == pytest.approx(845.70)
        assert s2.state.effective_stop_z == pytest.approx(4.0)
        assert len(s2.state.legs) == 2
        leg_a = next(l for l in s2.state.legs if l.symbol == "AAA")
        assert leg_a.tradingsymbol == "AAA26MAYFUT"
        assert leg_a.quantity == 2
        assert leg_a.entry_price == pytest.approx(1327.00)
        assert s2.state.realized_pnl == pytest.approx(-800.0)
        assert s2.state.total_transaction_costs == pytest.approx(800.0)
        assert len(s2.state.closed_trades) == 1
        # closed_trades datetimes roundtrip back to datetime
        assert isinstance(s2.state.closed_trades[0]["entry_time"], datetime)

    def test_restore_rebaselines_session_deltas(self):
        s1 = self._build_held_position()
        blob = s1.serialize_state()
        s2 = _make_strategy(hedge_ratio=0.5)
        s2.restore_state(blob)
        # After restore, session baseline = restored cumulative figures, so
        # generate_eod_report's session_realized_delta starts at 0.
        assert s2._session_start_realized == pytest.approx(-800.0)
        assert s2._session_start_unrealized == pytest.approx(-50.0)

    def test_restore_rejects_pair_mismatch(self):
        s1 = self._build_held_position()
        blob = s1.serialize_state()

        s_wrong = _make_strategy(hedge_ratio=0.5)
        s_wrong.symbol_a = "XXX"
        s_wrong.symbol_b = "YYY"
        with pytest.raises(ValueError, match="does not match"):
            s_wrong.restore_state(blob)

    def test_restore_does_not_overwrite_hedge_ratio(self):
        # The runner — not the strategy — decides whether to honour the
        # saved β. Strategy-level restore must leave hedge_ratio alone.
        s1 = self._build_held_position()  # hedge_ratio = 0.5
        blob = s1.serialize_state()

        s2 = _make_strategy(hedge_ratio=0.6)   # today's screener β differs
        s2.restore_state(blob)
        assert s2.hedge_ratio == 0.6  # unchanged

    def test_flat_state_roundtrip(self):
        s1 = _make_strategy()
        # FLAT, no legs, no realized
        blob = s1.serialize_state()
        s2 = _make_strategy()
        s2.restore_state(blob)
        assert s2.state.position == "FLAT"
        assert s2.state.legs == []
        assert s2.state.realized_pnl == 0.0


# ──────────────────────────────────────────────────────────
# legs_expire_on (expiry-day force-flatten helper)
# ──────────────────────────────────────────────────────────

class TestLegsExpireOn:
    def _strategy_with_legs(self, leg_tradingsymbol: str = "AAA26MAYFUT"):
        s = _make_strategy()
        s.state.position = "LONG_SPREAD"
        s.state.legs = [
            PairLeg(symbol="AAA", tradingsymbol=leg_tradingsymbol,
                    lot_size=100, quantity=1,
                    entry_price=1000.0, current_price=1000.0),
        ]
        return s

    def test_returns_false_when_flat(self):
        from datetime import date as d
        s = _make_strategy()  # FLAT, no legs
        assert s.legs_expire_on(d(2026, 5, 28)) is False

    def test_true_when_leg_expiry_matches_today(self):
        from datetime import date as d
        s = self._strategy_with_legs("AAA26MAYFUT")
        s.kite.instruments = lambda seg: [
            {"tradingsymbol": "AAA26MAYFUT", "expiry": "2026-05-28"},
            {"tradingsymbol": "BBB26MAYFUT", "expiry": "2026-05-28"},
        ]
        assert s.legs_expire_on(d(2026, 5, 28)) is True

    def test_false_when_leg_expiry_is_not_today(self):
        from datetime import date as d
        s = self._strategy_with_legs("AAA26MAYFUT")
        s.kite.instruments = lambda seg: [
            {"tradingsymbol": "AAA26MAYFUT", "expiry": "2026-05-28"},
        ]
        assert s.legs_expire_on(d(2026, 5, 20)) is False

    def test_raises_after_retries_on_instruments_failure(self):
        """H18: when held legs are present and instruments('NFO') keeps
        failing, legs_expire_on must raise rather than silently return
        False — silent False on real expiry day means carrying a contract
        into cash settlement. The retry path tries 3× with backoff before
        giving up. We monkey-patch time.sleep to skip the waits."""
        from datetime import date as d
        import strategies.pair_trading as pt_mod

        s = self._strategy_with_legs("AAA26MAYFUT")
        s._nfo_instruments_cache = None  # force fetch
        call_count = {"n": 0}

        def _raise(*a, **k):
            call_count["n"] += 1
            raise RuntimeError("network down")
        s.kite.instruments = _raise

        sleeps: list[float] = []
        orig_sleep = pt_mod.time.sleep
        pt_mod.time.sleep = lambda secs: sleeps.append(secs)
        try:
            with pytest.raises(RuntimeError, match="3 consecutive times"):
                s.legs_expire_on(d(2026, 5, 28))
        finally:
            pt_mod.time.sleep = orig_sleep

        assert call_count["n"] == 3, f"expected 3 attempts, got {call_count['n']}"
        # 1s then 2s backoffs between attempts; no sleep after the final attempt.
        assert sleeps == [1.0, 2.0], f"unexpected backoff schedule: {sleeps}"

    def test_raises_on_empty_instruments_dump(self):
        """H18: kite.instruments('NFO') succeeding but returning [] is
        treated the same as a fetch failure — we cannot verify whether
        held legs expire today, so refuse to silently return False."""
        from datetime import date as d
        s = self._strategy_with_legs("AAA26MAYFUT")
        s._nfo_instruments_cache = None
        s.kite.instruments = lambda seg: []
        with pytest.raises(RuntimeError, match="empty list"):
            s.legs_expire_on(d(2026, 5, 28))


# ──────────────────────────────────────────────────────────
# H19 — injected NFO instruments cache
# ──────────────────────────────────────────────────────────

class TestNfoInstrumentsCache:
    """H19: the runner pre-fetches kite.instruments('NFO') once and injects
    it into every strategy. With the injection, _get_nfo_instruments must
    never touch kite.instruments(); without it, the lazy fallback path
    fetches on first use and caches in-instance for subsequent calls."""

    def test_injected_cache_bypasses_kite_instruments(self):
        from datetime import date as d
        rows = [
            {"tradingsymbol": "AAA26MAYFUT", "expiry": "2026-05-28",
             "name": "AAA", "instrument_type": "FUT"},
        ]
        s = _make_strategy()
        s._nfo_instruments_cache = rows  # simulate runner injection
        # Make kite.instruments explode if called — proves the injection
        # path is wired through both call sites.
        def _explode(*a, **k):
            raise AssertionError("kite.instruments() called despite "
                                  "injected cache being present")
        s.kite.instruments = _explode

        # legs_expire_on uses the cache directly.
        s.state.legs = [
            PairLeg(symbol="AAA", tradingsymbol="AAA26MAYFUT", lot_size=100,
                    quantity=1, entry_price=1000.0, current_price=1000.0),
        ]
        assert s.legs_expire_on(d(2026, 5, 28)) is True

        # _resolve_futures uses the cache too.
        s._cached_futures.clear()  # force the lookup
        s._clock = lambda: datetime(2026, 5, 20, 10, 0)
        fut = s._resolve_futures("AAA")
        assert fut is not None
        assert fut["tradingsymbol"] == "AAA26MAYFUT"

    def test_lazy_fetch_populates_cache_on_first_call(self):
        """Without injection, the first _get_nfo_instruments fetches from
        kite, fills the in-instance cache, and subsequent calls reuse it."""
        rows = [
            {"tradingsymbol": "AAA26MAYFUT", "expiry": "2026-05-28",
             "name": "AAA", "instrument_type": "FUT"},
        ]
        s = _make_strategy()
        s._nfo_instruments_cache = None
        fetch_count = [0]
        def _instr(seg):
            fetch_count[0] += 1
            return rows
        s.kite.instruments = _instr

        assert s._get_nfo_instruments() == rows
        assert s._get_nfo_instruments() == rows  # cache hit
        assert fetch_count[0] == 1, (
            f"expected 1 fetch, got {fetch_count[0]} — lazy cache not working"
        )

    def test_lazy_fetch_failure_returns_empty(self):
        """A kite.instruments exception must return [] (so callers
        short-circuit) and NOT corrupt the cache to a partial state."""
        s = _make_strategy()
        s._nfo_instruments_cache = None
        def _raise(*a, **k):
            raise RuntimeError("network down")
        s.kite.instruments = _raise
        assert s._get_nfo_instruments() == []
        assert s._nfo_instruments_cache is None  # not poisoned


# ──────────────────────────────────────────────────────────
# Live-mode order confirmation (C1) and entry-batch atomicity (C2)
# ──────────────────────────────────────────────────────────

class TestLiveExecuteConfirmation:
    """C1: state must NOT mutate unless the order actually filled. Pre-fix,
    _live_execute returned status=PENDING and execute_proposals mutated
    state as if the LIMIT had filled — phantom positions on day 1 live."""

    def _live_strategy(self):
        s = _make_strategy(mode="live")
        # Provide Kite enum constants the live path references
        s.kite.VARIETY_REGULAR = "regular"
        s.kite.TRANSACTION_TYPE_BUY = "BUY"
        s.kite.TRANSACTION_TYPE_SELL = "SELL"
        s.kite.PRODUCT_NRML = "NRML"
        s.kite.ORDER_TYPE_MARKET = "MARKET"
        s.kite.VALIDITY_DAY = "DAY"
        # H15: tests for execution semantics aren't margin-precheck tests,
        # so default to a permissive margin response.
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 10_000_000.0}},
        })
        return s

    def _prop(self, qty=1, txn="BUY"):
        return TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=111, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=qty, price=1000.0, transaction_type=txn,
            iv=0, bid_ask_spread_pct=0.01, margin_required=20000,
            rationale="entry",
        )

    def test_complete_books_position_at_actual_fill_price(self):
        s = self._live_strategy()
        s.kite.place_order = MagicMock(return_value="ORD-1")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1003.5},
        ])
        s.execute_proposals([self._prop()])
        assert len(s.state.legs) == 1
        # Cost basis must reflect the ACTUAL fill, not the proposal price.
        assert s.state.legs[0].entry_price == 1003.5

    def test_rejected_does_not_book_position(self):
        s = self._live_strategy()
        s.kite.place_order = MagicMock(return_value="ORD-2")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "REJECTED", "filled_quantity": 0, "average_price": 0},
        ])
        s.kite.cancel_order = MagicMock()
        s.execute_proposals([self._prop()])
        assert s.state.legs == []
        assert s.state.position == "FLAT"

    def test_place_order_exception_does_not_book_position(self):
        s = self._live_strategy()
        s.kite.place_order = MagicMock(side_effect=RuntimeError("net down"))
        s.execute_proposals([self._prop()])
        assert s.state.legs == []
        assert s.state.position == "FLAT"

    def test_partial_fill_at_lot_boundary_is_failed(self):
        # Requested 2 lots (200 shares), got 50 → fractional lot → FAILED.
        s = self._live_strategy()
        s.kite.place_order = MagicMock(return_value="ORD-3")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 50, "average_price": 1000.0},
        ])
        s.execute_proposals([self._prop(qty=2)])
        assert s.state.legs == []

    def test_partial_fill_at_full_lot_is_failed(self):
        # H7: requested 2 lots (200 shares), got 100 (1 full lot) → FAILED.
        # Pre-fix this booked a 1-lot leg, breaking the pair hedge ratio.
        s = self._live_strategy()
        s.kite.place_order = MagicMock(return_value="ORD-4")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        s.execute_proposals([self._prop(qty=2)])
        assert s.state.legs == []

    def test_zero_fill_marked_complete_is_failed(self):
        # H7: COMPLETE status with filled_quantity=0 (paranoid broker quirk)
        # must still be refused — no leg booked. Zero fill → no broker
        # position to reverse, so place_order called exactly once.
        s = self._live_strategy()
        s.kite.place_order = MagicMock(return_value="ORD-5")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 0, "average_price": 0.0},
        ])
        s.execute_proposals([self._prop(qty=1)])
        assert s.state.legs == []
        assert s.kite.place_order.call_count == 1

    def test_partial_fill_triggers_emergency_reversal(self):
        # H7 follow-up: when broker gives a partial (50 of 200 shares for a
        # 2-lot req on lot=100), we return FAILED AND place an opposite-
        # side reversing MARKET order for the 50 shares so the broker ends
        # flat too. C2 reversal won't see this leg (it's FAILED), so
        # without the inline reverse the partial would be orphaned.
        s = self._live_strategy()
        place_calls = []

        def fake_place(*a, **kw):
            place_calls.append(kw)
            return f"ORD-{len(place_calls)}"

        s.kite.place_order = fake_place
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 50, "average_price": 1000.0},
        ])
        s.execute_proposals([self._prop(qty=2)])
        assert s.state.legs == []
        # Two place_order calls: the original (BUY 200) + reverse (SELL 50)
        assert len(place_calls) == 2
        assert place_calls[1]["transaction_type"] == "SELL"
        assert place_calls[1]["quantity"] == 50
        assert place_calls[1]["tradingsymbol"] == "AAA26APRFUT"

    def test_partial_fill_reverse_failure_logs_critical(self, caplog):
        # H7 follow-up: if the reversing order itself raises, log CRITICAL
        # with "PARTIAL ORPHAN" so notify-failure@ surfaces the leak.
        import logging
        caplog.set_level(logging.CRITICAL, logger="strategies.pair_trading")
        s = self._live_strategy()
        call_counter = {"n": 0}

        def fake_place(*a, **kw):
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return "ORD-1"
            raise RuntimeError("broker down")

        s.kite.place_order = fake_place
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 50, "average_price": 1000.0},
        ])
        s.execute_proposals([self._prop(qty=2)])
        assert any("PARTIAL ORPHAN" in r.message for r in caplog.records)


class TestTokenRefresh:
    """H8: TokenException on place_order or quote triggers exactly one
    refresh-and-retry. Without a kite_refresh callback, the call fails
    loud. Second failure after refresh is CRITICAL and treated as FAILED."""

    def _live_strategy(self, kite_refresh=None):
        s = _make_strategy(mode="live")
        s.kite.VARIETY_REGULAR = "regular"
        s.kite.TRANSACTION_TYPE_BUY = "BUY"
        s.kite.TRANSACTION_TYPE_SELL = "SELL"
        s.kite.PRODUCT_NRML = "NRML"
        s.kite.ORDER_TYPE_MARKET = "MARKET"
        s.kite.VALIDITY_DAY = "DAY"
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 10_000_000.0}},
        })
        s._kite_refresh = kite_refresh
        return s

    def _prop(self):
        return TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=111, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0.01, margin_required=20000,
            rationale="entry",
        )

    def test_place_order_token_exception_refreshes_and_retries(self):
        from strategies.pair_trading import _TokenException
        # First call raises TokenException; the refreshed kite client's
        # place_order succeeds.
        s = self._live_strategy()
        fresh = MagicMock()
        fresh.VARIETY_REGULAR = "regular"
        fresh.TRANSACTION_TYPE_BUY = "BUY"
        fresh.TRANSACTION_TYPE_SELL = "SELL"
        fresh.PRODUCT_NRML = "NRML"
        fresh.ORDER_TYPE_MARKET = "MARKET"
        fresh.VALIDITY_DAY = "DAY"
        fresh.place_order = MagicMock(return_value="ORD-RETRY")
        fresh.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        s._kite_refresh = MagicMock(return_value=fresh)
        s.kite.place_order = MagicMock(side_effect=_TokenException("expired"))
        s.execute_proposals([self._prop()])
        assert s._kite_refresh.call_count == 1
        # Leg booked from the retry fill
        assert len(s.state.legs) == 1

    def test_place_order_token_exception_without_callback_is_failed(self):
        from strategies.pair_trading import _TokenException
        s = self._live_strategy(kite_refresh=None)
        s.kite.place_order = MagicMock(side_effect=_TokenException("expired"))
        s.execute_proposals([self._prop()])
        assert s.state.legs == []
        assert s.state.position == "FLAT"

    def test_place_order_second_failure_after_refresh_is_failed(self):
        from strategies.pair_trading import _TokenException
        s = self._live_strategy()
        fresh = MagicMock()
        fresh.VARIETY_REGULAR = "regular"
        fresh.TRANSACTION_TYPE_BUY = "BUY"
        fresh.TRANSACTION_TYPE_SELL = "SELL"
        fresh.PRODUCT_NRML = "NRML"
        fresh.ORDER_TYPE_MARKET = "MARKET"
        fresh.VALIDITY_DAY = "DAY"
        fresh.place_order = MagicMock(side_effect=RuntimeError("still broken"))
        s._kite_refresh = MagicMock(return_value=fresh)
        s.kite.place_order = MagicMock(side_effect=_TokenException("expired"))
        s.execute_proposals([self._prop()])
        assert s.state.legs == []

    def test_quote_token_exception_refreshes_and_retries(self):
        from strategies.pair_trading import _TokenException
        s = self._live_strategy()
        fresh = MagicMock()
        fresh.quote = MagicMock(return_value={
            "NFO:AAA26APRFUT": {"last_price": 1234.5},
        })
        s._kite_refresh = MagicMock(return_value=fresh)
        s.kite.quote = MagicMock(side_effect=_TokenException("expired"))
        px = s._get_last_price("AAA26APRFUT")
        assert px == 1234.5
        assert s._kite_refresh.call_count == 1


class TestMarginPrecheck:
    """H15: live entry batches call kite.margins() first and skip the
    batch when available_balance < Σ margin_required. Paper, signals,
    and exits bypass the check."""

    def _live_strategy(self):
        s = _make_strategy(mode="live")
        s.kite.VARIETY_REGULAR = "regular"
        s.kite.TRANSACTION_TYPE_BUY = "BUY"
        s.kite.TRANSACTION_TYPE_SELL = "SELL"
        s.kite.PRODUCT_NRML = "NRML"
        s.kite.ORDER_TYPE_MARKET = "MARKET"
        s.kite.VALIDITY_DAY = "DAY"
        return s

    def _props(self, margin_a=20000, margin_b=30000):
        from trade_proposer import TradeProposal
        a = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=111, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0.01, margin_required=margin_a,
            rationale="entry A",
        )
        b = TradeProposal(
            tradingsymbol="BBB26APRFUT", instrument_token=222, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=2000.0, transaction_type="SELL",
            iv=0, bid_ask_spread_pct=0.01, margin_required=margin_b,
            rationale="entry B",
        )
        return [a, b]

    def test_insufficient_margin_skips_entry(self):
        s = self._live_strategy()
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 10_000.0}},
        })
        s.kite.place_order = MagicMock()  # must not be called
        s.execute_proposals(self._props(margin_a=20_000, margin_b=30_000))
        s.kite.place_order.assert_not_called()
        assert s.state.legs == []

    def test_collateral_counts_toward_available(self):
        # 2026-06-11: a fully pledged account reports live_balance=0 with all
        # usable margin under available.collateral. Futures margin can be
        # posted from collateral, so the precheck must sum both — otherwise a
        # collateral-funded live account is silently entry-disabled forever.
        s = self._live_strategy()
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 0.0, "collateral": 486_000.0}},
        })
        s.kite.place_order = MagicMock(return_value="ORD-OK")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        s.execute_proposals(self._props(margin_a=20_000, margin_b=30_000))
        assert s.kite.place_order.call_count == 2

    def test_cash_plus_collateral_still_insufficient_skips_entry(self):
        # The sum is the gate: cash and collateral together short of the
        # batch requirement must still refuse, or leg B rejects after leg A
        # fills and C2 reversal eats the round-trip cost.
        s = self._live_strategy()
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 10_000.0, "collateral": 15_000.0}},
        })
        s.kite.place_order = MagicMock()  # must not be called
        s.execute_proposals(self._props(margin_a=20_000, margin_b=30_000))
        s.kite.place_order.assert_not_called()
        assert s.state.legs == []

    def test_sufficient_margin_proceeds(self):
        s = self._live_strategy()
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 1_000_000.0}},
        })
        s.kite.place_order = MagicMock(return_value="ORD-OK")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        s.execute_proposals(self._props())
        assert s.kite.place_order.call_count == 2

    def test_margins_failure_lets_order_through(self):
        s = self._live_strategy()
        s.kite.margins = MagicMock(side_effect=RuntimeError("net down"))
        s.kite.place_order = MagicMock(return_value="ORD-OK")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        s.execute_proposals(self._props())
        # margins() flake must not block trading — broker reject + C2 handles it
        assert s.kite.place_order.call_count == 2

    def test_paper_mode_skips_precheck(self):
        # Paper has no broker — margins() not called.
        s = _make_strategy(mode="paper")
        s.kite.margins = MagicMock()
        s.execute_proposals(self._props())
        s.kite.margins.assert_not_called()


class TestProtectiveLimitOrders:
    """2026-06-11: Zerodha's API rejects naked MARKET orders on F&O
    ('Market orders without market protection are not allowed via API') —
    the first live entry batch was rejected on both legs. Live orders must
    go out as marketable LIMITs at LTP padded limit_protection_pct toward
    the aggressive side, so they fill like market orders but stay
    API-legal with slippage bounded at the pad."""

    def _live_strategy(self):
        s = _make_strategy(mode="live")
        s.kite.VARIETY_REGULAR = "regular"
        s.kite.TRANSACTION_TYPE_BUY = "BUY"
        s.kite.TRANSACTION_TYPE_SELL = "SELL"
        s.kite.PRODUCT_NRML = "NRML"
        s.kite.ORDER_TYPE_MARKET = "MARKET"
        s.kite.ORDER_TYPE_LIMIT = "LIMIT"
        s.kite.VALIDITY_DAY = "DAY"
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 10_000_000.0}},
        })
        s.kite.instruments = MagicMock(return_value=[
            {"tradingsymbol": "AAA26APRFUT", "tick_size": 0.05},
        ])
        s.kite.place_order = MagicMock(return_value="ORD-OK")
        s.kite.order_history = MagicMock(return_value=[
            {"status": "COMPLETE", "filled_quantity": 100, "average_price": 1000.0},
        ])
        return s

    def _prop(self, transaction_type="BUY", price=1000.0):
        from trade_proposer import TradeProposal
        return TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=111, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=price, transaction_type=transaction_type,
            iv=0, bid_ask_spread_pct=0.01, margin_required=20000,
            rationale="entry A",
        )

    def test_buy_places_limit_padded_above_ltp(self):
        s = self._live_strategy()
        s.kite.quote = MagicMock(return_value={
            "NFO:AAA26APRFUT": {"last_price": 1000.0},
        })
        s._live_execute(self._prop("BUY"))
        kwargs = s.kite.place_order.call_args.kwargs
        assert kwargs["order_type"] == "LIMIT"
        # 1000 * (1 + 0.25%) = 1002.50, already on a 0.05 tick
        assert kwargs["price"] == 1002.50

    def test_sell_places_limit_padded_below_ltp(self):
        s = self._live_strategy()
        s.kite.quote = MagicMock(return_value={
            "NFO:AAA26APRFUT": {"last_price": 1000.0},
        })
        s._live_execute(self._prop("SELL"))
        kwargs = s.kite.place_order.call_args.kwargs
        assert kwargs["order_type"] == "LIMIT"
        assert kwargs["price"] == 997.50

    def test_pad_rounds_outward_to_tick(self):
        # BUY must round UP to the next tick (more aggressive), never down
        # below the pad: 333.30 * 1.0025 = 334.13325 → 334.15 on 0.05 ticks.
        s = self._live_strategy()
        s.kite.quote = MagicMock(return_value={
            "NFO:AAA26APRFUT": {"last_price": 333.30},
        })
        s._live_execute(self._prop("BUY"))
        assert s.kite.place_order.call_args.kwargs["price"] == 334.15

    def test_quote_failure_falls_back_to_proposal_price(self):
        # The fresh-LTP call failing must not block the order — the
        # proposal's own quote (same tick, seconds old) is the fallback.
        s = self._live_strategy()
        s.kite.quote = MagicMock(side_effect=RuntimeError("quote down"))
        s._live_execute(self._prop("BUY", price=2000.0))
        kwargs = s.kite.place_order.call_args.kwargs
        assert kwargs["order_type"] == "LIMIT"
        assert kwargs["price"] == 2005.00

    def test_emergency_partial_reverse_uses_protective_limit(self):
        # The H7 inline partial-fill reversal hits the same API rule —
        # a MARKET reversal there would be rejected and leave the orphan.
        # Audit 2.2: the reversal now lives in the shared executor; assert it
        # through the executor pair builds (same fake kite, same protective
        # LIMIT semantics).
        s = self._live_strategy()
        s.kite.quote = MagicMock(return_value={
            "NFO:AAA26APRFUT": {"last_price": 1000.0},
        })
        s._order_executor()._emergency_reverse_partial(
            self._prop("BUY"), 50, "ORIG-1")
        kwargs = s.kite.place_order.call_args.kwargs
        assert kwargs["order_type"] == "LIMIT"
        assert kwargs["transaction_type"] == "SELL"  # reverse of BUY
        assert kwargs["price"] == 997.50  # SELL side: padded BELOW LTP


class TestEntryBatchAtomicity:
    """C2: if one leg of a two-leg entry fails, the other must be reversed.
    A naked single leg is the worst outcome of a hedged strategy."""

    def _live_strategy(self):
        s = _make_strategy(mode="live")
        s.kite.margins = MagicMock(return_value={
            "equity": {"available": {"live_balance": 10_000_000.0}},
        })
        s.kite.VARIETY_REGULAR = "regular"
        s.kite.TRANSACTION_TYPE_BUY = "BUY"
        s.kite.TRANSACTION_TYPE_SELL = "SELL"
        s.kite.PRODUCT_NRML = "NRML"
        s.kite.ORDER_TYPE_MARKET = "MARKET"
        s.kite.VALIDITY_DAY = "DAY"
        return s

    def _props(self):
        a = TradeProposal(
            tradingsymbol="AAA26APRFUT", instrument_token=111, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=100,
            quantity=1, price=1000.0, transaction_type="BUY",
            iv=0, bid_ask_spread_pct=0.01, margin_required=20000,
            rationale="leg A entry",
        )
        b = TradeProposal(
            tradingsymbol="BBB26APRFUT", instrument_token=222, strike=0,
            expiry="2026-04-28", option_type="FUT", lot_size=200,
            quantity=1, price=2000.0, transaction_type="SELL",
            iv=0, bid_ask_spread_pct=0.01, margin_required=40000,
            rationale="leg B entry",
        )
        return [a, b]

    def test_leg_b_failure_triggers_reversal_of_leg_a(self):
        s = self._live_strategy()
        # Track each place_order call so we can verify the reversal.
        call_log = []

        def place(*a, **kw):
            call_log.append(kw)
            return f"ORD-{len(call_log)}"

        s.kite.place_order = place
        s.kite.cancel_order = MagicMock()

        # 3 calls expected: leg A entry (fills), leg B entry (fails),
        # leg A reversal (fills). Driven by the position in call_log.
        def history(order_id):
            # Order 1 = leg A entry → COMPLETE
            # Order 2 = leg B entry → REJECTED
            # Order 3 = leg A reversal → COMPLETE
            idx = int(order_id.split("-")[1])
            if idx == 1:
                return [{"status": "COMPLETE", "filled_quantity": 100,
                         "average_price": 1000.0}]
            elif idx == 2:
                return [{"status": "REJECTED", "filled_quantity": 0,
                         "average_price": 0}]
            return [{"status": "COMPLETE", "filled_quantity": 100,
                     "average_price": 1001.0}]

        s.kite.order_history = history
        s.execute_proposals(self._props())

        # Three place_order calls — entry A, entry B, reversal A.
        assert len(call_log) == 3
        # Reversal must be opposite direction at the SAME tradingsymbol.
        assert call_log[2]["tradingsymbol"] == "AAA26APRFUT"
        assert call_log[2]["transaction_type"] == "SELL"
        # Final state: flat (the reversal closed leg A; leg B never opened).
        assert s.state.legs == []
        assert s.state.position == "FLAT"

    def test_both_legs_fail_leaves_state_clean(self):
        s = self._live_strategy()
        s.kite.place_order = MagicMock(side_effect=RuntimeError("market closed"))
        s.execute_proposals(self._props())
        assert s.state.legs == []
        assert s.state.position == "FLAT"


# ──────────────────────────────────────────────────────────
# Contract-roll safety (C10) + tradingsymbol reverse-map (H11)
# ──────────────────────────────────────────────────────────

class TestExitUsesLegContract:
    """C10: exit proposals must use the leg's STORED tradingsymbol, not
    today's front-month. Pre-fix, a position held over a contract roll
    would exit on the new front-month — opening a fresh naked position
    while the old-contract leg sat unmanaged."""

    def test_exit_uses_held_contract_after_roll(self):
        s = _make_strategy()
        # State: leg holds the MAY contract (we entered last month)
        s.state.legs = [PairLeg(
            symbol="AAA", tradingsymbol="AAA26MAYFUT", lot_size=100,
            quantity=1, entry_price=1000.0, current_price=1010.0,
        )]
        s.state.position = "LONG_SPREAD"
        # _cached_futures has today's front-month (JUNE), not what we hold
        s._cached_futures = {
            "AAA": {"tradingsymbol": "AAA26JUNFUT", "lot_size": 100,
                    "expiry": "2026-06-25", "instrument_token": 222},
        }
        proposals = s._build_exit_proposals(
            "TEST", 0.0, {"AAA": 1010.0},
        )
        assert len(proposals) == 1
        # Must exit the MAY contract (what we hold), not JUNE (today's front)
        assert proposals[0].tradingsymbol == "AAA26MAYFUT"
        assert proposals[0].transaction_type == "SELL"
        assert proposals[0].lot_size == 100


class TestSymbolFromTradingsymbol:
    """H11: rolled-contract fill matching + fail-loud on unknown."""

    def test_matches_via_state_legs_for_rolled_contract(self):
        s = _make_strategy()
        s.state.legs = [PairLeg(
            symbol="AAA", tradingsymbol="AAA26MAYFUT", lot_size=100,
            quantity=1, entry_price=1000.0, current_price=1000.0,
        )]
        s._cached_futures = {
            "AAA": {"tradingsymbol": "AAA26JUNFUT", "lot_size": 100,
                    "expiry": "2026-06-25", "instrument_token": 222},
        }
        # Exit fill comes back with the MAY tradingsymbol — must resolve
        # to the leg's symbol (AAA), not fall through.
        assert s._symbol_from_tradingsymbol("AAA26MAYFUT") == "AAA"

    def test_matches_via_cache_for_fresh_entry(self):
        s = _make_strategy()  # state.legs empty
        assert s._symbol_from_tradingsymbol("AAA26APRFUT") == "AAA"

    def test_raises_on_unknown_tradingsymbol(self):
        s = _make_strategy()
        with pytest.raises(ValueError, match="Cannot reverse-map"):
            s._symbol_from_tradingsymbol("UNKNOWN26MAYFUT")


# ──────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────

class TestRegistry:
    def test_strategy_registered(self):
        from strategies import STRATEGIES, get_strategy
        assert "pair_trading" in STRATEGIES
        assert get_strategy("pair_trading") is PairTradingStrategy


class TestSpreadPanelInjection:
    """Audit 2026-06-10 task 1.1: the runner preloads ONE bhavcopy panel and
    injects it; _seed_spread_history must consume it without re-reading
    ~520 CSVs per pair (the open blind window), and must keep the per-pair
    column slicing + self-load fallback byte-identical for callers that
    don't inject (backtests, ad-hoc construction)."""

    def _panel(self, n=80):
        import pandas as pd
        idx = pd.date_range("2026-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {"AAA": [100.0 + i for i in range(n)],
             "BBB": [50.0 + 0.5 * i for i in range(n)]},
            index=idx,
        )

    def test_injected_panel_seeds_without_file_read(self, monkeypatch):
        import screen_pairs
        monkeypatch.setattr(
            screen_pairs, "load_front_month_panel",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not re-read bhavcopy when panel injected")),
        )
        s = _make_strategy(hedge_ratio=2.0)
        panel = self._panel()
        s._spread_panel = panel
        s._seed_spread_history()
        expected = (panel["AAA"] - 2.0 * panel["BBB"]).dropna().tolist()
        assert s._spread_history == expected[-s.lookback_days * 3:]

    def test_no_panel_falls_back_to_self_load(self, monkeypatch):
        import screen_pairs
        calls = []
        panel = self._panel()
        def fake_load(universe, **kw):
            calls.append(list(universe))
            return panel
        monkeypatch.setattr(screen_pairs, "load_front_month_panel", fake_load)
        s = _make_strategy(hedge_ratio=2.0)
        assert s._spread_panel is None
        s._seed_spread_history()
        assert calls == [["AAA", "BBB"]]
        assert len(s._spread_history) > 0

    def test_injected_panel_missing_leg_leaves_seed_empty(self):
        # A coverage-dropped symbol is absent from the shared panel; the
        # existing missing-column warning path must fire (empty seed,
        # intraday accumulation) rather than crashing or re-reading files.
        import pandas as pd
        s = _make_strategy()
        s._spread_panel = pd.DataFrame({"AAA": [1.0, 2.0]})
        s._seed_spread_history()
        assert s._spread_history == []


# ──────────────────────────────────────────────────────────
# Real constructor (audit 2026-06-10 task 2.5)
# ──────────────────────────────────────────────────────────

class TestRealConstructor:
    """Every other test bypasses __init__ via __new__. These exercise the
    REAL constructor against the checked-in config_template.ini, so the
    beta-bound refusal and the bhavcopy seeding actually run — the audit's
    "__init__ executed by at least one test" acceptance, plus a guard on
    the |beta| in [0.1, 10] gate that protects leg-B sizing."""

    CONFIG = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "config_template.ini")

    def _panel(self):
        import numpy as np
        import pandas as pd
        idx = pd.date_range("2025-01-01", periods=80, freq="D")
        return pd.DataFrame(
            {"AAA": 100.0 + np.arange(80) * 0.5,
             "BBB": 50.0 + np.arange(80) * 0.2},
            index=idx,
        )

    def test_beta_below_min_is_refused(self):
        # |beta| = 0.05 < HEDGE_RATIO_MIN (0.1): leg B is so small the
        # "hedge" is really leg A alone. Refuse to construct.
        with pytest.raises(ValueError, match="hedge_ratio out of range"):
            PairTradingStrategy(
                kite=MagicMock(), config_path=self.CONFIG, mode="signals",
                symbol_a="AAA", symbol_b="BBB", hedge_ratio=0.05,
            )

    def test_beta_above_max_is_refused(self):
        # |beta| = 12 > HEDGE_RATIO_MAX (10): leg B notional dwarfs leg A.
        with pytest.raises(ValueError, match="hedge_ratio out of range"):
            PairTradingStrategy(
                kite=MagicMock(), config_path=self.CONFIG, mode="signals",
                symbol_a="AAA", symbol_b="BBB", hedge_ratio=12.0,
            )

    def test_missing_beta_is_refused(self):
        # No arg, and config_template has no [pair_trading] hedge_ratio.
        with pytest.raises(ValueError, match="hedge_ratio must be supplied"):
            PairTradingStrategy(
                kite=MagicMock(), config_path=self.CONFIG, mode="signals",
                symbol_a="AAA", symbol_b="BBB", hedge_ratio=None,
            )

    def test_valid_beta_constructs_and_seeds_from_panel(self):
        # Happy path: in-bounds beta, signals mode (no notional cap needed),
        # injected panel so seeding doesn't touch the filesystem. __init__
        # runs end to end.
        s = PairTradingStrategy(
            kite=MagicMock(), config_path=self.CONFIG, mode="signals",
            symbol_a="AAA", symbol_b="BBB", hedge_ratio=0.5,
            spread_panel=self._panel(),
        )
        assert s.symbol_a == "AAA"
        assert s.symbol_b == "BBB"
        assert s.hedge_ratio == 0.5
        # seed = AAA - 0.5*BBB over the 80-row panel
        assert len(s._spread_history) == 80

    def test_non_signals_mode_requires_notional_cap(self):
        # config_template has no [pair_trading] max_leg_notional, so paper/
        # live must refuse rather than run with leg-B sizing uncapped.
        with pytest.raises(ValueError, match="max_leg_notional must be set"):
            PairTradingStrategy(
                kite=MagicMock(), config_path=self.CONFIG, mode="paper",
                symbol_a="AAA", symbol_b="BBB", hedge_ratio=0.5,
                spread_panel=self._panel(),
            )
