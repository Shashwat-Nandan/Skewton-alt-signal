"""Tests for the Market-Profile trend_up paper strategy logic.

Each test encodes WHY the rule matters, not just that it returns something
(Rule 9): the broad-momentum filter is the whole reason this trade is not a
net loser, and the kill switch is the only thing standing between an
underpowered edge and a silent bleeder.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from market_profile import Bar
from strategies.market_profile_intraday import (
    MPTrendConfig,
    check_kill,
    classify_day_longs,
    position_size,
    trade_pnl,
)


def _trend_up_bars(base: float = 100.0) -> List[Bar]:
    """A clean one-timeframing-up day (strictly higher lows) → day_shape trend_up."""
    start = datetime(2026, 4, 17, 9, 15)
    spec = [(base + i, base + 2 + i, base + i, base + 1.5 + i) for i in range(6)]
    return [Bar(ts=start + timedelta(minutes=30 * k), open=o, high=h, low=l,
                close=c, volume=100) for k, (o, h, l, c) in enumerate(spec)]


def _flat_bars(base: float = 100.0) -> List[Bar]:
    """A rotational day that does not one-timeframe → not trend_up."""
    start = datetime(2026, 4, 17, 9, 15)
    spec = [(base, base + 1, base - 1, base), (base, base + 1, base - 1, base),
            (base, base + 1, base - 1, base), (base, base + 1, base - 1, base)]
    return [Bar(ts=start + timedelta(minutes=30 * k), open=o, high=h, low=l,
                close=c, volume=100) for k, (o, h, l, c) in enumerate(spec)]


class TestBroadMomentumFilter:
    def test_below_K_produces_no_longs(self):
        # 2 trend_up names, K=3 → the count is reported but NO longs are taken.
        # This is the exact condition under which the single-name trade loses;
        # the filter must suppress it.
        bars = {"A": _trend_up_bars(100), "B": _trend_up_bars(200),
                "C": _flat_bars(300), "D": _flat_bars(400)}
        n, longs = classify_day_longs(bars, MPTrendConfig(min_signals=3))
        assert n == 2
        assert longs == []

    def test_at_or_above_K_takes_all_trend_up_names(self):
        bars = {"A": _trend_up_bars(100), "B": _trend_up_bars(200),
                "C": _trend_up_bars(300), "D": _flat_bars(400)}
        n, longs = classify_day_longs(bars, MPTrendConfig(min_signals=3))
        assert n == 3
        assert longs == ["A", "B", "C"]

    def test_thin_days_are_ignored_not_classified(self):
        # A name with fewer than min_periods bars must not count toward breadth.
        thin = {"X": _trend_up_bars(100)[:3]}
        n, longs = classify_day_longs(thin, MPTrendConfig(min_signals=1, min_periods=6))
        assert n == 0 and longs == []


class TestSizingAndPnl:
    def test_equal_weight_whole_shares(self):
        # ₹1,000,000 across 4 names = ₹250,000 each; at ₹500 → 500 shares.
        assert position_size(1_000_000, 4, 500.0) == 500

    def test_pnl_net_of_round_trip_cost(self):
        # 100 sh, 100→105, 25 bps on ₹10,000 notional = ₹25 cost.
        p = trade_pnl(100.0, 105.0, 100, cost_bps=25.0)
        assert p["gross"] == 500.0
        assert p["cost"] == 25.0
        assert p["net"] == 475.0


class TestKillSwitch:
    def test_no_halt_before_min_trades(self):
        cfg = MPTrendConfig(kill_min_trades=20, kill_cum_loss=100)
        assert check_kill([-1000] * 10, cfg).halted is False   # n<20 → immune

    def test_halts_on_cumulative_loss(self):
        cfg = MPTrendConfig(kill_min_trades=20, kill_cum_loss=1000)
        st = check_kill([-100] * 25, cfg)     # cum -2500 <= -1000
        assert st.halted is True
        assert "cum net" in st.reason

    def test_halts_on_drawdown_even_if_still_net_positive(self):
        # Runs up +100k then gives back 80k: cum +20k (no cum-loss trip) but the
        # 80k drawdown exceeds 6% of ₹1M → halt. This is the trigger that stops
        # a decaying edge before it round-trips the whole gain.
        cfg = MPTrendConfig(kill_min_trades=20, kill_cum_loss=1e9,
                            kill_max_drawdown=0.06, capital=1_000_000)
        series = [10_000] * 10 + [-8_000] * 10   # peak 100k, trough 20k, dd 80k
        st = check_kill(series, cfg)
        assert st.halted is True
        assert "drawdown" in st.reason

    def test_no_halt_when_profitable(self):
        cfg = MPTrendConfig(kill_min_trades=20, capital=1_000_000)
        assert check_kill([500] * 30, cfg).halted is False
