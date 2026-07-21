"""core/intrabar.py — open-aware long-exit adjudication.

Each test encodes a fill-honesty rule the equity-swing P&L depends on
(shared by research/backtest_varsity_equity.py and the run_equity_swing
paper path — see the 2026-07-21 NautilusTrader-eval §4.7 adoption).
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.intrabar import adjudicate_long_exit


class TestGapOpens:
    def test_open_below_stop_fills_at_open_not_stop(self):
        """A gap-down through the stop cannot fill at the stop price — the
        first tradable print is the open. Filling at the stop would book
        gap-down losses too small (the flattering-fill bias the legacy
        elif chain had when the stop was also inside the day's range)."""
        assert adjudicate_long_exit(88.0, 96.0, 87.0, stop=95.0, target=110.0) \
            == ("SL_HIT", 88.0)

    def test_open_above_target_books_target_even_when_stop_in_range(self):
        """THE headline defect: a day opening beyond the target has already
        filled the resting target sell at the open — a later intra-day slide
        to the stop is irrelevant. The legacy chain checked stop-in-range
        first and booked SL_HIT on exactly this bar, which both understated
        P&L and (in the forward paper record) suppressed target hits."""
        assert adjudicate_long_exit(112.0, 113.0, 94.0, stop=95.0, target=110.0) \
            == ("TARGET_HIT", 112.0)

    def test_open_exactly_at_levels_counts_as_gap_fill(self):
        assert adjudicate_long_exit(95.0, 100.0, 94.0, stop=95.0, target=110.0) \
            == ("SL_HIT", 95.0)
        assert adjudicate_long_exit(110.0, 111.0, 100.0, stop=95.0, target=110.0) \
            == ("TARGET_HIT", 110.0)


class TestIntraBarTouches:
    def test_both_reachable_intrabar_is_adjudicated_pessimistically(self):
        """Daily OHLC cannot order intra-bar touches. When the open sits
        between the levels and the range spans both, assuming the target
        hit first would systematically flatter every stop-and-reverse day;
        the house bias is honest fills, so the stop wins the race."""
        assert adjudicate_long_exit(100.0, 111.0, 94.0, stop=95.0, target=110.0) \
            == ("SL_HIT", 95.0)

    def test_only_touched_level_fires_at_its_own_price(self):
        """An intra-bar touch (no gap) fills at the level itself — the
        resting order's price — not at open/close, else slippage appears
        from nowhere on ordinary days."""
        assert adjudicate_long_exit(100.0, 101.0, 94.0, stop=95.0, target=110.0) \
            == ("SL_HIT", 95.0)
        assert adjudicate_long_exit(100.0, 111.0, 99.0, stop=95.0, target=110.0) \
            == ("TARGET_HIT", 110.0)

    def test_neither_level_reachable_returns_none(self):
        """No phantom exits: a bar that touches neither level must leave
        the position open for the trail/time-stop checks the caller owns."""
        assert adjudicate_long_exit(100.0, 105.0, 96.0, stop=95.0, target=110.0) is None


class TestDegradedInputs:
    def test_nan_open_falls_through_to_touch_checks(self):
        """Some cached daily rows carry NaN opens (proxy-data gaps). NaN
        comparisons are False, so the gap branches must not fire and the
        touch checks must still adjudicate — a raise here would kill the
        whole session's exit scan for every symbol after the bad one."""
        assert adjudicate_long_exit(math.nan, 101.0, 94.0, stop=95.0, target=110.0) \
            == ("SL_HIT", 95.0)
        assert adjudicate_long_exit(math.nan, 105.0, 96.0, stop=95.0, target=110.0) is None

    def test_open_contradicting_bar_range_is_ignored(self):
        """Split/proxy-data corruption can leave an open outside the bar's
        own [low, high]. Trusting it would book fills at prices the market
        never traded (a phantom stop-out at 88 against a 140-150 bar), so
        an inconsistent open must be ignored and only level touches count
        — here neither level is touched, so the position is held."""
        assert adjudicate_long_exit(88.0, 150.0, 140.0, stop=95.0, target=200.0) is None

    def test_gap_fill_without_usable_open_prices_at_the_low(self):
        """Whole bar below the stop with no usable open: the stop level
        itself (above the day's high) is physically unfillable — legacy
        booked it anyway, flattering exactly the big gap-down days that
        hurt most. With the open unknown, the fill takes the bar's LOW,
        the pessimistic in-range print for a seller. Symmetric for a
        whole-bar-above-target fill."""
        assert adjudicate_long_exit(math.nan, 80.0, 75.0, stop=95.0, target=110.0) \
            == ("SL_HIT", 75.0)
        assert adjudicate_long_exit(math.nan, 120.0, 112.0, stop=95.0, target=110.0) \
            == ("TARGET_HIT", 112.0)

    def test_corrupt_levels_fail_loud(self):
        """target <= stop is not a market scenario — it is corrupt position
        state (bad DB restore / manual edit). Silently adjudicating it
        labeled losing exits TARGET_HIT, inflating the win-rate the
        scoreboard and decay machine consume; the contract is to raise."""
        with pytest.raises(ValueError):
            adjudicate_long_exit(100.0, 105.0, 96.0, stop=110.0, target=95.0)
        with pytest.raises(ValueError):
            adjudicate_long_exit(100.0, 105.0, 96.0, stop=100.0, target=100.0)


class TestEffectiveStop:
    """The caller passes its ratcheted trail as ``stop`` (a higher stop).
    These assert the adjudicator treats it as any other stop — the varsity
    exit path relabels the reason to TRAIL_STOP."""

    def test_bar_opening_below_the_trail_fills_at_open(self):
        """A bar OPENING below the ratcheted stop fills at the open — under
        the legacy in-range-only check this bar exited nothing and the
        position rode below its own trail indefinitely."""
        assert adjudicate_long_exit(100.0, 101.0, 98.0, stop=105.0, target=120.0) \
            == ("SL_HIT", 100.0)

    def test_bar_trading_down_through_trail_fills_at_trail(self):
        """The headline of finding #1: with the trail at 105 and a bar
        spanning [94,106], the effective stop is 105 — price cannot reach
        94 without crossing 105 first, so the fill is 105, not the lower
        initial-SL region."""
        assert adjudicate_long_exit(106.0, 106.0, 94.0, stop=105.0, target=120.0) \
            == ("SL_HIT", 105.0)
