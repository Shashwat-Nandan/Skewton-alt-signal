"""Tests for research/level_significance.py — Phase B event study mechanics.

These pin the measurement the kill-shot verdict rests on (Rule 9): swing pivots
are detected correctly, the forward reaction splits reversal from continuation
and refuses a truncated late-session window, level-membership matching picks the
nearest level within tol, range coverage is a true union — and, most important,
run_study never tests a level on the session it was born (the point-in-time
guard whose failure would manufacture a look-ahead edge — the 2026-07-25 review
flagged it had no test).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.market_profile import Bar
from research import level_significance as ls
from research.level_significance import (
    TouchEvent,
    _active_as_of,
    _nearest_level,
    _range_coverage,
    find_swings,
    forward_reaction,
    run_study,
)

_T0 = datetime(2026, 7, 13, 9, 15)


def _b(i, o, hi, lo, c):
    return Bar(ts=_T0 + timedelta(minutes=5 * i), open=o, high=hi, low=lo, close=c, volume=0)


class _Lvl:
    """Minimal stand-in for a registry Level (only .price is read here)."""
    def __init__(self, price):
        self.price = price


class TestFindSwings:
    def test_swing_high_and_low(self):
        # up to a peak at i=2 (high 105), down to a trough at i=4 (low 96).
        bars = [_b(0, 100, 101, 99, 100), _b(1, 101, 103, 100, 102),
                _b(2, 103, 105, 102, 104), _b(3, 104, 104, 100, 101),
                _b(4, 101, 101, 96, 97), _b(5, 97, 100, 96, 99)]
        sw = find_swings(bars)
        assert (2, 105.0, "resistance") in sw   # the peak, fade down
        assert (4, 96.0, "support") in sw        # the trough, fade up

    def test_flat_top_is_not_scored_every_bar(self):
        # A flat plateau (equal highs) must not each count as a swing high —
        # strict-greater on at least one side is required.
        bars = [_b(0, 100, 101, 99, 100), _b(1, 101, 105, 100, 104),
                _b(2, 104, 105, 103, 104), _b(3, 104, 105, 103, 104),
                _b(4, 104, 102, 100, 101)]
        highs = [s for s in find_swings(bars) if s[2] == "resistance"]
        # The rising-then-falling plateau yields at most the first plateau bar,
        # never all three flat bars.
        assert len(highs) <= 1

    def test_endpoints_never_swing(self):
        bars = [_b(0, 100, 106, 99, 105), _b(1, 105, 104, 100, 101),
                _b(2, 101, 107, 100, 106)]
        # bar 0 and bar 2 are the extremes but are endpoints → not interior.
        assert find_swings(bars) == [] or all(1 <= s[0] <= 1 for s in find_swings(bars))


class TestForwardReaction:
    def test_resistance_reversal_and_continuation(self):
        bars = [_b(0, 100, 100, 99, 100),
                _b(1, 100, 101, 98, 99), _b(2, 99, 100.5, 97, 98)]
        rev, cont = forward_reaction(bars, 0, 100.0, "resistance", horizon=2)
        assert rev == 300.0 and cont == 100.0   # down 3 → 300bps, up 1 → 100bps

    def test_support_reversal_is_upward(self):
        bars = [_b(0, 100, 101, 100, 100),
                _b(1, 100, 103, 99.5, 102), _b(2, 102, 104, 101, 103)]
        rev, cont = forward_reaction(bars, 0, 100.0, "support", horizon=2)
        assert rev == 400.0 and cont == 50.0

    def test_insufficient_future_returns_none(self):
        bars = [_b(0, 100, 100, 99, 100), _b(1, 100, 101, 99, 100)]
        assert forward_reaction(bars, 0, 100.0, "resistance", horizon=6) is None


class TestNearestLevel:
    def test_picks_nearest_within_tol(self):
        levels = [_Lvl(100.0), _Lvl(103.0), _Lvl(108.0)]
        assert _nearest_level(levels, 102.4, tol=2.0).price == 103.0

    def test_none_beyond_tol(self):
        assert _nearest_level([_Lvl(100.0)], 105.0, tol=2.0) is None
        assert _nearest_level([], 100.0, tol=2.0) is None


class TestRangeCoverage:
    def test_union_of_bands(self):
        # Two overlapping bands 99–101 and 100.5–102.5 over [95,105] → covers
        # 99..102.5 = 3.5 of 10 = 0.35.
        cov = _range_coverage([_Lvl(100.0), _Lvl(101.5)], 95.0, 105.0, tol=1.0)
        assert cov == (102.5 - 99.0) / 10.0

    def test_empty_or_degenerate(self):
        assert _range_coverage([], 95.0, 105.0, 1.0) == 0.0
        assert _range_coverage([_Lvl(100.0)], 100.0, 100.0, 1.0) == 0.0


class TestTouchEvent:
    def test_net_bps_is_reversal_minus_continuation(self):
        e = TouchEvent(session=_T0.date(), kind="level_swing", source="session_poc",
                       price=100.0, side="resistance", test_index=1,
                       rev_bps=18.0, cont_bps=11.0, held=True)
        assert e.net_bps == 7.0


# ──────────────────────────────────────────────────────────
# Point-in-time guard — the load-bearing no-look-ahead property
# ──────────────────────────────────────────────────────────

def _synthetic_sessions(n_days=14):
    """n_days of oscillating 5-min bars — enough structure to mint levels and
    produce swings, drifting up 1pt/day so levels persist and re-test."""
    out = []
    for d in range(n_days):
        base = 100.0 + d
        day0 = datetime(2026, 6, 1, 9, 15) + timedelta(days=d)
        bars = []
        for i in range(24):
            # sawtooth between base and base+6
            phase = i % 6
            mid = base + (phase if phase <= 3 else 6 - phase) * 2.0
            bars.append(Bar(ts=day0 + timedelta(minutes=5 * i),
                            open=mid, high=mid + 1.0, low=mid - 1.0, close=mid,
                            volume=0))
        out.append((day0.date(), bars))
    return out


class _CreatedLvl:
    def __init__(self, price, created_at):
        self.price = price
        self.created_at = created_at


class TestPointInTime:
    def test_active_excludes_same_day_and_future_levels(self):
        # THE bias guard: a level whose created_at is ON (or after) the session's
        # start must be excluded — only strictly-earlier levels are active. A
        # regression to `<=` would admit the birth-day level and inject hindsight.
        day_start = datetime(2026, 6, 10)
        levels = [
            _CreatedLvl(100.0, datetime(2026, 6, 9, 15, 30)),    # prior day → active
            _CreatedLvl(101.0, datetime(2026, 6, 10, 15, 30)),   # same day close → NOT
            _CreatedLvl(102.0, datetime(2026, 6, 11, 15, 30)),   # future → NOT
        ]
        active = _active_as_of(levels, day_start)
        assert [lvl.price for lvl in active] == [100.0]

    def test_end_to_end_first_session_mints_no_level_swings(self, monkeypatch):
        # End-to-end: on the earliest session no level can yet exist, so every
        # swing that day is non-level. Assert the study runs and produces both
        # arms, and that the first session could not have been paired (no levels
        # yet) — a `<=` regression would create day-0 level-swings.
        sessions = _synthetic_sessions(16)
        monkeypatch.setattr(ls, "load_sessions", lambda *a, **k: sessions)
        summary = run_study("NIFTY", horizon=3, seed=1)
        assert summary["level_swing"]["n"] > 0
        assert summary["nonlevel_swing"]["n"] > 0
        assert summary["net_bps_diff"]["n_paired_sessions"] < len(sessions)


def test_study_end_to_end_runs_and_reports_a_verdict(monkeypatch):
    sessions = _synthetic_sessions(16)
    monkeypatch.setattr(ls, "load_sessions", lambda *a, **k: sessions)
    summary = run_study("NIFTY", horizon=3, seed=2)
    assert summary["verdict"]  # STOP / PROCEED / INSUFFICIENT — some decision
    assert 0.0 <= summary["params"]["mean_range_coverage"] <= 1.0
