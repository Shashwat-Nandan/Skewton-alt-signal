"""Tests for the pair trading strategy — z-score, entry/exit logic, mode dispatch, fill handling."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
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
    s.lookback_days = 30
    s.lots_per_leg = lots_per_leg
    s.max_holding_days = 10
    s.max_leg_notional = max_leg_notional
    s.min_edge_multiplier = min_edge_multiplier
    s.total_capital = 500_000
    s.state = PairState()
    s._spread_history = list(spread_history) if spread_history is not None else []
    s._cached_futures = {
        "AAA": {"tradingsymbol": "AAA26APRFUT", "lot_size": 100,
                "expiry": "2026-04-28", "instrument_token": 111},
        "BBB": {"tradingsymbol": "BBB26APRFUT", "lot_size": 200,
                "expiry": "2026-04-28", "instrument_token": 222},
    }
    s._clock = lambda: datetime(2026, 4, 21, 10, 30)
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
        # std≈30 → expected Δspread at z=-2.5, exit_z=0.75 = 1.75*30 = 52.5
        # × qty_a_shares 100 = ₹5,250 expected vs ~₹450 round-trip cost.
        s = _make_strategy(
            hedge_ratio=0.5,
            exit_z=0.75,
            min_edge_multiplier=1.5,
            spread_history=[float(x) for x in range(-30, 30)],  # std ≈ 17
        )
        # spread = 1000 - 0.5*2100 = -50 → well below mean 0 with std~17 → z<-2.5
        self._stub_quotes(s, 1000.0, 2100.0)
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
        """Mock the kite quote calls so _observe_spread returns deterministic prices."""
        def fake_quote(syms):
            assert len(syms) == 1
            sym = syms[0]
            if "AAA" in sym:
                return {sym: {"last_price": price_a}}
            return {sym: {"last_price": price_b}}
        s.kite.quote = fake_quote
        # Prepend a stable history so the z-score is computable
        s._spread_history = [0.0, 1.0] * 30

    def test_no_entry_when_already_in_position(self):
        s = _make_strategy()
        s.state.position = "LONG_SPREAD"
        proposals = s.scan_and_propose()
        assert proposals == []

    def test_long_spread_when_z_below_minus_entry(self):
        # Spread well below the rolling mean → z negative → LONG_SPREAD
        s = _make_strategy(hedge_ratio=0.5)
        # Establish mean ~= 0, std ~= 1 over recent history
        s._spread_history = [-1.0, 1.0] * 30
        # current price puts spread at -5 (way below mean)
        # spread = price_a - β*price_b; pick price_a, price_b so spread = -5
        # 1000 - 0.5*2010 = -5 → price_b = 2010
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
        s = _make_strategy(hedge_ratio=0.5)
        s._spread_history = [-1.0, 1.0] * 30
        # spread = 1000 - 0.5*1990 = +5 (well above mean)
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
    def _open_long_spread(self, s, price_a=1000.0, price_b=2000.0):
        s.state.position = "LONG_SPREAD"
        s.state.entry_time = s._clock()
        s.state.entry_z = -2.5
        s.state.entry_spread = -5.0
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

    def test_no_exit_inside_band(self):
        s = _make_strategy(hedge_ratio=0.5, exit_z=0.5, stop_z=4.0)
        s._spread_history = [-1.0, 1.0] * 30
        self._open_long_spread(s)
        # Spread = 1.5 → z=1.5, between exit_z (0.5) and stop_z (4.0)
        self._set_quote(s, price_a=1000.0, price_b=1997.0)  # spread = 1.5
        proposals = s.check_and_rehedge()
        assert proposals == []


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
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": price_a}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": price_b}}
        )
        s._spread_history = [-1.0, 1.0] * 30

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
        s.kite.quote = lambda syms: (
            {syms[0]: {"last_price": 50.0}} if "AAA" in syms[0]
            else {syms[0]: {"last_price": 100.0}}
        )
        s._spread_history = [-1.0, 1.0] * 30
        # spread = 50 - 10*101 = -960 (way below mean) → LONG_SPREAD entry
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


# ──────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────

class TestRegistry:
    def test_strategy_registered(self):
        from strategies import STRATEGIES, get_strategy
        assert "pair_trading" in STRATEGIES
        assert get_strategy("pair_trading") is PairTradingStrategy
