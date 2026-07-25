"""Tests for core/level_registry.py — persistent level registry (A2).

These encode WHY the registry is correct (Rule 9): re-deriving a drifting level
must NOT reset its age or duplicate it (persistence is the whole point); a price
touch tests every level in the zone, not just the nearest (L4 semantics); a
defended level survives pruning while an abandoned one ages out; and the lunch
window must be excluded from node derivation or a 12:15 lull mints a fake level
daily. The 20-session replay pins byte-level determinism (the A2 gate).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.level_registry import (
    Level,
    LevelRegistry,
    ingest_session,
    session_nodes,
)
# Aliased so pytest doesn't try to collect the `Test`-prefixed dataclass.
from core.level_registry import TestOutcome as Outcome
from core.market_profile import (
    Bar,
    CompositeProfile,
    DayIndicators,
    compute_day_profile,
    market_generated_indicators,
)

_D = datetime(2026, 7, 13, 15, 30)


def _bar(h, m, o, hi, lo, c, v):
    return Bar(ts=datetime(2026, 7, 13, h, m), open=o, high=hi, low=lo, close=c, volume=v)


def _pbar(ts, p, vol):
    """A bar living entirely inside the tick bin [p, p+1) → volume lands in the
    single bin whose mid is p+0.5 (tick_size=1.0). Keeps node-price arithmetic
    exact in the derivation tests."""
    return Bar(ts=ts, open=p + 0.5, high=p + 0.9, low=p + 0.1, close=p + 0.5, volume=vol)


def _indicators(**kw) -> DayIndicators:
    """A DayIndicators with every field defaulted, overridable per test."""
    base = dict(
        day=_D.date(), open_type="open_auction", day_shape="normal",
        profile_skew="balanced", balance_state="inside", in_balance=True,
        range_ext_up=False, range_ext_down=False, range_ext_first="none",
        excess_high=False, excess_low=False, poor_high=False, poor_low=False,
        single_print_count=0, single_print_levels=[],
        one_timeframing="none", one_timeframing_run=0,
        open=100.0, close=100.0, high=112.0, low=95.0,
        poc=100.5, vah=102.5, val=98.5, ib_high=101.0, ib_low=99.0,
    )
    base.update(kw)
    return DayIndicators(**base)


# ──────────────────────────────────────────────────────────
# Level record
# ──────────────────────────────────────────────────────────

class TestLevel:
    def test_register_test_increments_and_tracks_last(self):
        lvl = Level(price=100.0, source="session_poc", instrument="X", created_at=_D)
        t1 = Outcome(ts=_D, mfe=5.0, mae=1.0, absorbed=True)
        t2 = Outcome(ts=_D + timedelta(days=1), mfe=3.0, mae=2.0, absorbed=False)
        lvl.register_test(t1)
        lvl.register_test(t2)
        assert lvl.test_count == 2
        assert lvl.last_tested_at == t2.ts   # the later one

    def test_staleness_decays_and_a_test_refreshes_it(self):
        born = datetime(2026, 7, 1, 15, 30)
        lvl = Level(price=100.0, source="session_poc", instrument="X", created_at=born)
        now = born + timedelta(days=10)
        assert lvl.staleness(now, half_life_days=10.0) == pytest.approx(0.5)
        # A test 1 day before `now` resets the clock → nearly fresh again.
        lvl.register_test(Outcome(ts=now - timedelta(days=1), mfe=1, mae=0, absorbed=True))
        assert lvl.staleness(now, half_life_days=10.0) > 0.9


# ──────────────────────────────────────────────────────────
# Registry mutation
# ──────────────────────────────────────────────────────────

class TestUpsert:
    def test_same_source_within_tol_recenters_price_but_keeps_age(self):
        reg = LevelRegistry(price_tol=5.0)
        born = datetime(2026, 7, 10, 15, 30)
        reg.upsert(100.0, "weekly_vah", "X", born)
        # Re-derived 3 pts away a week later — same persisting level, but its
        # price tracks the current edge while identity/age is preserved.
        again = reg.upsert(103.0, "weekly_vah", "X", born + timedelta(days=7))
        assert len(reg) == 1
        assert again.created_at == born          # age preserved (not reset)
        assert again.price == 103.0              # re-centered to the current edge

    def test_beyond_tol_mints_a_new_level(self):
        reg = LevelRegistry(price_tol=5.0)
        reg.upsert(100.0, "weekly_vah", "X", _D)
        reg.upsert(120.0, "weekly_vah", "X", _D)
        assert len(reg) == 2

    def test_different_source_same_price_stays_separate(self):
        # A value-area edge and a coincident volume node are distinct evidence.
        reg = LevelRegistry(price_tol=5.0)
        reg.upsert(100.0, "weekly_vah", "X", _D)
        reg.upsert(100.0, "session_hvn", "X", _D)
        assert len(reg) == 2

    def test_different_instrument_stays_separate(self):
        reg = LevelRegistry(price_tol=5.0)
        reg.upsert(100.0, "weekly_vah", "X", _D)
        reg.upsert(100.0, "weekly_vah", "Y", _D)
        assert len(reg) == 2

    def test_unknown_source_raises(self):
        reg = LevelRegistry()
        with pytest.raises(ValueError):
            reg.upsert(100.0, "not_a_source", "X", _D)


class TestRecordTest:
    def test_touch_tests_every_level_in_the_zone(self):
        # Manual annotation: a touch at 100.5 within tol=2 of two clustered
        # levels (100 and 102) tests BOTH; the far level (110) is untouched.
        reg = LevelRegistry(price_tol=2.0)
        reg.upsert(100.0, "weekly_vah", "X", _D)
        reg.upsert(102.0, "session_hvn", "X", _D)
        reg.upsert(110.0, "session_poc", "X", _D)
        hit = reg.record_test(100.5, _D, "X", mfe=4.0, mae=1.0, absorbed=True)
        assert {round(l.price) for l in hit} == {100, 102}
        counts = {l.source: l.test_count for l in reg.all_levels()}
        assert counts == {"weekly_vah": 1, "session_hvn": 1, "session_poc": 0}

    def test_touch_off_every_level_records_nothing(self):
        reg = LevelRegistry(price_tol=2.0)
        reg.upsert(100.0, "weekly_vah", "X", _D)
        assert reg.record_test(200.0, _D, "X", mfe=1, mae=1, absorbed=True) == []
        assert reg.all_levels()[0].test_count == 0


class TestPrune:
    def test_abandoned_level_ages_out_but_defended_one_survives(self):
        born = datetime(2026, 7, 1, 15, 30)
        reg = LevelRegistry(price_tol=2.0)
        old = reg.upsert(100.0, "weekly_vah", "X", born)   # never tested again
        held = reg.upsert(200.0, "session_poc", "X", born)
        now = born + timedelta(days=20)
        # `held` was defended 2 days ago → stays fresh.
        held.register_test(Outcome(ts=now - timedelta(days=2), mfe=1, mae=0, absorbed=True))
        dropped = reg.prune_stale(now, max_age_days=10.0)
        assert dropped == 1
        survivors = {l.source for l in reg.all_levels()}
        assert survivors == {"session_poc"}
        assert old  # referenced; asserts nothing but documents intent


# ──────────────────────────────────────────────────────────
# Persistence & determinism
# ──────────────────────────────────────────────────────────

class TestPersistence:
    def test_round_trip_is_byte_stable(self):
        reg = LevelRegistry(price_tol=3.0)
        reg.upsert(100.0, "weekly_vah", "X", _D)
        lvl = reg.upsert(105.0, "session_lvn", "X", _D)
        lvl.register_test(Outcome(ts=_D, mfe=2.0, mae=1.0, absorbed=False))
        blob = reg.to_dict()
        again = LevelRegistry.from_dict(blob).to_dict()
        assert json.dumps(again, sort_keys=True) == json.dumps(blob, sort_keys=True)

    def test_serialization_order_is_insertion_independent(self):
        # Two registries with the same levels added in opposite orders must
        # serialize identically — the 20-session replay depends on it.
        a = LevelRegistry(price_tol=2.0)
        a.upsert(110.0, "session_poc", "X", _D)
        a.upsert(100.0, "weekly_vah", "X", _D)
        b = LevelRegistry(price_tol=2.0)
        b.upsert(100.0, "weekly_vah", "X", _D)
        b.upsert(110.0, "session_poc", "X", _D)
        assert a.to_dict() == b.to_dict()

    def test_save_load_via_durable_write(self, tmp_path):
        reg = LevelRegistry(price_tol=2.0)
        reg.upsert(100.0, "weekly_vah", "X", _D)
        path = tmp_path / "levels.json"
        reg.save(path)
        loaded = LevelRegistry.load(path)
        assert loaded.to_dict() == reg.to_dict()


# ──────────────────────────────────────────────────────────
# Derivation & the lunch rule
# ──────────────────────────────────────────────────────────

def _three_shelf_session(*, lunch_shelf_vol):
    """Single-bin shelves at 100 (morning), 105 (transacts through LUNCH), and
    110 (afternoon). When the 105 shelf is heavy it is a REAL acceptance node;
    a hard lunch-exclude erases it and mints a spurious LVN at 105 (the §2.5
    inversion), whereas including or deweighting keeps it from being a rejection.
    """
    d = datetime(2026, 7, 13)
    bars = []
    for hh, mm in [(9, 15), (9, 30), (9, 45), (10, 0), (10, 30), (11, 0)]:
        bars.append(_pbar(d.replace(hour=hh, minute=mm), 100, 500))
    for hh, mm in [(12, 0), (12, 30), (13, 0)]:      # inside 11:30–13:30
        bars.append(_pbar(d.replace(hour=hh, minute=mm), 105, lunch_shelf_vol))
    for hh, mm in [(13, 45), (14, 0), (14, 30), (15, 0), (15, 15)]:
        bars.append(_pbar(d.replace(hour=hh, minute=mm), 110, 500))
    return bars


def _near(prices, target, tol=1.0):
    return any(abs(p - target) <= tol for p in prices)


class TestDerivation:
    def test_hard_exclude_mints_a_spurious_lunch_lvn_that_include_does_not(self):
        # A REAL shelf that happens to transact through lunch: dropping every
        # lunch bar (weight=0) erases its volume and turns it into a valley —
        # a fake session_lvn at 105 the strategy would trade against. Keeping it
        # (weight=1) leaves 105 an acceptance shelf, not a rejection. This is the
        # §2.5 inversion the review flagged; deweight is the guard.
        bars = _three_shelf_session(lunch_shelf_vol=500)
        hvn_incl, lvn_incl = session_nodes(bars, lunch_weight=1.0, tick_size=1.0,
                                           smoothing_bins=0, min_prominence=0.2)
        _, lvn_excl = session_nodes(bars, lunch_weight=0.0, tick_size=1.0,
                                    smoothing_bins=0, min_prominence=0.2)
        assert _near(lvn_excl, 105.5)          # hard-exclude invents the LVN
        assert not _near(lvn_incl, 105.5)      # included, 105 is not a rejection
        assert _near(hvn_incl, 105.5)          # it is an acceptance shelf

    def test_default_deweight_does_not_mint_a_lunch_lvn(self):
        # The shipped default (deweight, not drop) must NOT flag 105 as a
        # rejection level — a discounted lunch shelf asserts no level either way.
        bars = _three_shelf_session(lunch_shelf_vol=500)
        _, lvn = session_nodes(bars, tick_size=1.0, smoothing_bins=0,
                               min_prominence=0.2)   # default lunch_weight=0.25
        assert not _near(lvn, 105.5)

    def test_index_zero_volume_bars_yield_no_nodes(self):
        # An index carries no volume, so its volume profile is empty.
        t0 = datetime(2026, 7, 13, 9, 15)
        bars = [Bar(ts=t0 + timedelta(minutes=5 * i),
                    open=100, high=101, low=99, close=100, volume=0)
                for i in range(10)]
        assert session_nodes(bars, tick_size=1.0) == ([], [])

    def test_ingest_maps_every_supplied_source(self):
        # Verify the FULL 1:1 mapping (Rule 9): every flag/field that is set must
        # mint its source, and every flag that is NOT set must mint nothing.
        bars = _three_shelf_session(lunch_shelf_vol=500)
        prof = compute_day_profile(bars, tick_size=1.0)   # session_poc/vah/val + ib
        composite = CompositeProfile(
            bins=[], poc=100.5, vah=102.5, val=98.5, high=112.0, low=95.0,
            total_tpos=0, total_volume=0)
        ind = _indicators(excess_high=True, poor_low=True,
                          single_print_count=1, single_print_levels=[107.5])
        reg = LevelRegistry(price_tol=1.0)
        ingest_session(reg, instrument="X", created_at=_D, node_tick_size=1.0,
                       day_profile=prof, indicators=ind, composite=composite,
                       session_bars=bars)
        sources = {l.source for l in reg.all_levels()}
        expected = {
            "weekly_vah", "weekly_val", "composite_poc",
            "session_poc", "session_vah", "session_val", "ib_high", "ib_low",
            "excess_high", "poor_low", "single_print",
            "session_hvn", "session_lvn",
        }
        assert expected <= sources, f"missing: {expected - sources}"
        # Un-set flags must mint nothing — a regression that always-emits would
        # slip past a subset check.
        assert "excess_low" not in sources
        assert "poor_high" not in sources

    def test_ingest_with_only_a_day_profile_still_works(self):
        bars = _three_shelf_session(lunch_shelf_vol=500)
        prof = compute_day_profile(bars, tick_size=1.0)
        reg = LevelRegistry(price_tol=1.0)
        ingest_session(reg, instrument="X", created_at=_D, day_profile=prof)
        assert len(reg) >= 1


# ──────────────────────────────────────────────────────────
# The A2 gate — 20-session deterministic replay
# ──────────────────────────────────────────────────────────

def _replay_20_sessions():
    """Ingest 20 synthetic sessions into a fresh registry, return its JSON."""
    reg = LevelRegistry(price_tol=2.0)
    for d in range(20):
        day = datetime(2026, 6, 1, 15, 30) + timedelta(days=d)
        # A drifting session: POC walks up 1 pt/day so most days mint a new
        # level, some coincide within tol (persistence exercised).
        base = 100.0 + d
        t0 = day.replace(hour=9, minute=15)
        bars = [Bar(ts=t0 + timedelta(minutes=5 * i),
                    open=base, high=base + 2, low=base - 2, close=base, volume=400)
                for i in range(12)]
        prof = compute_day_profile(bars, tick_size=1.0)
        ind = market_generated_indicators(bars, tick_size=1.0)
        ingest_session(reg, instrument="NIFTY", created_at=day,
                       day_profile=prof, indicators=ind, session_bars=bars,
                       node_tick_size=1.0)
        # A deterministic touch each day at the POC.
        reg.record_test(prof.poc, day, "NIFTY", mfe=1.0, mae=0.5, absorbed=True)
    return json.dumps(reg.to_dict(), sort_keys=True)


class TestReplayGate:
    def test_twenty_session_replay_is_deterministic(self):
        assert _replay_20_sessions() == _replay_20_sessions()

    def test_replay_books_exactly_one_test_per_session_no_over_count(self):
        # Hand annotation: 20 days each mint one distinct level (prices ≥1 apart)
        # and fire exactly one touch at it. With a tight tol each touch hits that
        # one level, so the total booked must be EXACTLY 20 — a `>= 20` assertion
        # would pass even if record_test double-counted a level (Rule 9).
        reg = LevelRegistry(price_tol=0.01)
        for d in range(20):
            day = datetime(2026, 6, 1, 15, 30) + timedelta(days=d)
            price = 100.0 + d
            reg.upsert(price, "session_poc", "NIFTY", day)
            hit = reg.record_test(price, day, "NIFTY", mfe=1, mae=0, absorbed=True)
            assert len(hit) == 1, f"day {d}: touch hit {len(hit)} levels, expected 1"
        booked = sum(l.test_count for l in reg.all_levels())
        assert booked == 20   # exactly one per day, no over-count
