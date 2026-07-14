"""Tests for the Market Profile compute module + bars storage + API smoke."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from market_profile import (
    Bar,
    auto_tick_size,
    compute_composite,
    compute_day_profile,
    indicators_to_dict,
    market_generated_indicators,
    period_letter,
    split_by_day,
)


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────

def _bars(spec: List[tuple], start: datetime = datetime(2026, 4, 17, 9, 15)) -> List[Bar]:
    """Build a Bar list from (open, high, low, close, [volume]) tuples spaced 30 minutes apart."""
    out = []
    for i, t in enumerate(spec):
        if len(t) == 4:
            o, h, l, c = t
            v = 0
        else:
            o, h, l, c, v = t
        out.append(Bar(ts=start + timedelta(minutes=30 * i),
                       open=o, high=h, low=l, close=c, volume=v))
    return out


# ──────────────────────────────────────────────────────────
# Tick / period helpers
# ──────────────────────────────────────────────────────────

class TestTickSize:
    def test_empty_returns_default(self):
        assert auto_tick_size([]) == 0.05

    def test_zero_range_returns_default(self):
        assert auto_tick_size([100, 100, 100]) == 0.05

    def test_small_range_picks_small_tick(self):
        # 5 wide → target 0.1 → snap to 0.10
        assert auto_tick_size([100, 105]) == 0.10

    def test_large_range_picks_large_tick(self):
        # 5000 wide → target 100 → snap to 100
        assert auto_tick_size([10000, 15000]) == 100


class TestPeriodLetter:
    def test_uppercase(self):
        assert period_letter(0) == "A"
        assert period_letter(25) == "Z"

    def test_lowercase_after_z(self):
        assert period_letter(26) == "a"
        assert period_letter(51) == "z"

    def test_doubled_letters(self):
        assert period_letter(52) == "AA"
        assert period_letter(77) == "ZZ"


# ──────────────────────────────────────────────────────────
# Day profile
# ──────────────────────────────────────────────────────────

class TestDayProfile:
    def test_empty_returns_none(self):
        assert compute_day_profile([]) is None

    def test_single_bar_zero_range(self):
        # Pathological: high == low. We still return a single-bin profile.
        p = compute_day_profile(_bars([(100, 100, 100, 100, 50)]))
        assert p is not None
        assert len(p.bins) == 1
        assert p.poc == p.vah == p.val == 100
        assert p.total_tpos == 1

    def test_known_poc_and_va(self):
        # 6 periods, mid concentrated around 1106-1115.
        bars = _bars([
            (1100, 1110, 1095, 1105, 100),
            (1105, 1115, 1100, 1112, 200),
            (1112, 1120, 1108, 1118, 150),
            (1118, 1118, 1102, 1104, 180),
            (1104, 1108, 1095, 1098, 120),
            (1098, 1106, 1095, 1102, 90),
        ])
        p = compute_day_profile(bars, tick_size=1.0)
        assert p is not None
        # Hand-computed: bin 1106 is hit by every period that touches the
        # 1105–1115 band (A,B,C,D,E,F all do once we account for B 1100–1115).
        # Either way, POC must be inside the central 1100-1115 band and the
        # value area must contain >=70% of TPOs.
        assert 1100 < p.poc < 1115
        assert p.val < p.poc <= p.vah
        # Value area covers >=70% of TPOs by construction
        va_tpos = sum(b.tpo_count for b in p.bins if b.in_value_area)
        assert va_tpos / p.total_tpos >= 0.70 - 1e-9

    def test_initial_balance_first_two_periods(self):
        bars = _bars([
            (100, 110, 95, 105),    # A: high=110, low=95
            (105, 115, 100, 112),   # B: high=115, low=100
            (112, 120, 108, 118),   # C: outside IB
        ])
        p = compute_day_profile(bars, tick_size=1.0)
        assert p is not None
        assert p.ib_high == 115   # A∪B high
        assert p.ib_low == 95     # A∪B low

    def test_poc_unique(self):
        # Bar that camps at one price
        bars = _bars([
            (100, 100, 100, 100),
            (100, 100, 100, 100),
            (100, 100, 100, 100),
        ])
        p = compute_day_profile(bars, tick_size=1.0)
        assert p is not None
        assert sum(1 for b in p.bins if b.is_poc) == 1

    def test_value_area_pct_sensitivity(self):
        bars = _bars([
            (100, 110, 95, 105, 100),
            (105, 115, 100, 112, 100),
            (112, 120, 108, 118, 100),
            (118, 118, 102, 104, 100),
            (104, 108, 95, 98, 100),
        ])
        p70 = compute_day_profile(bars, tick_size=1.0, value_area_pct=0.70)
        p95 = compute_day_profile(bars, tick_size=1.0, value_area_pct=0.95)
        assert p70 is not None and p95 is not None
        va_70 = sum(1 for b in p70.bins if b.in_value_area)
        va_95 = sum(1 for b in p95.bins if b.in_value_area)
        assert va_95 >= va_70  # more TPOs included → wider VA


# ──────────────────────────────────────────────────────────
# Composite
# ──────────────────────────────────────────────────────────

class TestComposite:
    def test_composite_combines_all_days(self):
        # Two distinct days
        d1 = _bars([(100, 105, 95, 102)], start=datetime(2026, 4, 17, 9, 15))
        d2 = _bars([(110, 115, 105, 112)], start=datetime(2026, 4, 18, 9, 15))
        comp = compute_composite(d1 + d2, tick_size=1.0)
        assert comp is not None
        assert comp.n_days == 2
        assert comp.high == 115
        assert comp.low == 95


# ──────────────────────────────────────────────────────────
# Day-splitting
# ──────────────────────────────────────────────────────────

class TestSplitByDay:
    def test_groups_correctly(self):
        bars = (
            _bars([(100, 105, 95, 102), (102, 108, 100, 106)],
                  start=datetime(2026, 4, 17, 9, 15))
            + _bars([(106, 112, 104, 110)],
                    start=datetime(2026, 4, 18, 9, 15))
        )
        groups = split_by_day(bars)
        assert len(groups) == 2
        assert len(groups[0]) == 2
        assert len(groups[1]) == 1


# ──────────────────────────────────────────────────────────
# Storage layer
# ──────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db(tmp_path):
    """Bind a tmp DB for the duration of a single test, then reset."""
    from backend import db as backend_db
    backend_db.reset_for_tests(tmp_path / "mp_test.db")
    backend_db.init_schema()
    yield backend_db
    backend_db.reset_for_tests()  # release singleton


class TestStorage:
    def test_upsert_and_list_universe(self, fresh_db):
        from backend import bars as bdb
        bdb.upsert_universe("ABC", 111, "NSE", "ABC Ltd")
        bdb.upsert_universe("XYZ", 222, "NSE", "XYZ Ltd")
        rows = bdb.list_universe()
        assert {r["symbol"] for r in rows} == {"ABC", "XYZ"}

    def test_upsert_idempotent(self, fresh_db):
        from backend import bars as bdb
        bdb.upsert_universe("ABC", 111, "NSE", "Old name")
        bdb.upsert_universe("ABC", 111, "NSE", "New name")
        rows = bdb.list_universe()
        assert len(rows) == 1
        assert rows[0]["name"] == "New name"

    def test_insert_bars_idempotent(self, fresh_db):
        from backend import bars as bdb
        bdb.upsert_universe("ABC", 111, "NSE")
        rows = [
            ("2026-04-17T09:30:00", 100.0, 110.0, 95.0, 105.0, 100),
            ("2026-04-17T10:00:00", 105.0, 115.0, 100.0, 112.0, 200),
        ]
        n1 = bdb.insert_bars(111, 30, rows)
        n2 = bdb.insert_bars(111, 30, rows)  # re-insert
        assert n1 == 2
        assert n2 == 0  # PK collision → 0 new rows

    def test_get_bars_bounds(self, fresh_db):
        from backend import bars as bdb
        bdb.upsert_universe("ABC", 111, "NSE")
        bdb.insert_bars(111, 30, [
            ("2026-04-17T09:30:00", 100, 110, 95, 105, 100),
            ("2026-04-17T10:00:00", 105, 115, 100, 112, 200),
            ("2026-04-17T10:30:00", 112, 120, 108, 118, 150),
        ])
        all_bars = bdb.get_bars(111, 30)
        assert len(all_bars) == 3
        bounded = bdb.get_bars(111, 30,
                               from_ts="2026-04-17T10:00:00",
                               to_ts="2026-04-17T10:00:00")
        assert len(bounded) == 1

    def test_latest_bar_ts_and_count(self, fresh_db):
        from backend import bars as bdb
        bdb.upsert_universe("ABC", 111, "NSE")
        bdb.insert_bars(111, 30, [
            ("2026-04-17T09:30:00", 100, 110, 95, 105, 100),
            ("2026-04-17T10:00:00", 105, 115, 100, 112, 200),
        ])
        assert bdb.latest_bar_ts(111, 30) == "2026-04-17T10:00:00"
        assert bdb.count_bars(111, 30) == 2
        assert bdb.latest_bar_ts(999, 30) is None  # missing


# ──────────────────────────────────────────────────────────
# API smoke
# ──────────────────────────────────────────────────────────

class TestApi:
    def test_router_returns_profile(self, fresh_db):
        from fastapi.testclient import TestClient
        from backend.main import app
        from backend import bars as bdb

        bdb.upsert_universe("XYZ", 12345, "NSE", "XYZ Test")
        base = datetime(2026, 4, 17, 9, 15)
        rows = []
        for i, ohlcv in enumerate([
            (100, 110, 95, 105, 100), (105, 115, 100, 112, 200),
            (112, 120, 108, 118, 150), (118, 118, 102, 104, 180),
            (104, 108, 95, 98, 120),   (98, 106, 95, 102, 90),
        ]):
            rows.append(((base + timedelta(minutes=30 * i)).isoformat(), *ohlcv))
        bdb.insert_bars(12345, 30, rows)
        bdb.mark_backfilled("XYZ")

        from tests._helpers import login_client
        c = TestClient(app)
        login_client(c)
        # Symbols list
        r = c.get("/api/market-profile/symbols")
        assert r.status_code == 200
        syms = r.json()
        assert any(s["symbol"] == "XYZ" for s in syms)

        # Profile fetch
        r = c.get("/api/market-profile/XYZ?days=720&tick_size=1.0")
        assert r.status_code == 200
        j = r.json()
        assert j["symbol"] == "XYZ"
        assert j["composite"] is not None
        assert j["composite"]["poc"] > 0
        assert j["composite"]["val"] <= j["composite"]["poc"] <= j["composite"]["vah"]

        # Daily mode includes per-day breakdown
        r = c.get("/api/market-profile/XYZ?days=720&mode=daily&tick_size=1.0")
        assert r.status_code == 200
        j = r.json()
        assert "daily" in j and len(j["daily"]) >= 1
        assert all("ib_high" in d for d in j["daily"])

    def test_router_404_for_unknown_symbol(self, fresh_db):
        from fastapi.testclient import TestClient
        from backend.main import app
        from tests._helpers import login_client
        c = TestClient(app)
        login_client(c)
        r = c.get("/api/market-profile/GHOST")
        assert r.status_code == 404
        assert "bars_universe" in r.json()["detail"]

    def test_router_404_when_no_bars_yet(self, fresh_db):
        from fastapi.testclient import TestClient
        from backend.main import app
        from backend import bars as bdb
        bdb.upsert_universe("ABC", 111, "NSE")
        from tests._helpers import login_client
        c = TestClient(app)
        login_client(c)
        r = c.get("/api/market-profile/ABC")
        assert r.status_code == 404
        assert "No 30m bars" in r.json()["detail"]


# ──────────────────────────────────────────────────────────
# Market-generated indicators (Dalton, Markets in Profile)
#
# Each test reproduces the book's described geometry and asserts the CLASSIFIER
# returns the book's label — so a test fails if the classification logic drifts,
# not merely if it returns "something" (Rule 9).
# ──────────────────────────────────────────────────────────

def _prior(low: float, high: float) -> "object":
    """A one-bar prior-day DayProfile spanning [low, high] for reference tests."""
    return compute_day_profile(
        _bars([((low + high) / 2, high, low, (low + high) / 2, 100)]),
        tick_size=1.0,
    )


class TestOpenType:
    def test_open_drive_up(self):
        # Opens at the low and marches up, never trading back below the open
        # (Fig 8.15 geometry, up direction). Highest-confidence open.
        bars = _bars([
            (100, 102, 100, 101, 50),
            (101, 104, 101, 103, 60),
            (103, 106, 103, 105, 70),
            (105, 108, 105, 107, 80),
            (107, 110, 107, 109, 90),
        ])
        ind = market_generated_indicators(bars)
        assert ind.open_type == "open_drive_up"

    def test_open_drive_down(self):
        # Fig 8.15 as drawn: opens at the high, drives lower all day.
        bars = _bars([
            (110, 110, 108, 109, 50),
            (109, 109, 106, 107, 60),
            (107, 107, 104, 105, 70),
            (105, 105, 102, 103, 80),
            (103, 103, 100, 101, 90),
        ])
        ind = market_generated_indicators(bars)
        assert ind.open_type == "open_drive_down"

    def test_open_test_drive_up(self):
        # Opens 100, first period tests BELOW the prior day's low (95), finds no
        # business, reverses and drives up. The failed test secured the low.
        bars = _bars([
            (100, 101, 94, 99, 50),    # test down to 94 (< prior low 95)
            (99, 103, 98, 102, 60),    # reverse up through the open
            (102, 106, 101, 105, 70),
            (105, 110, 104, 109, 80),
        ])
        ind = market_generated_indicators(bars, prior=_prior(95, 108))
        assert ind.open_type == "open_test_drive_up"

    def test_open_test_drive_down(self):
        # Opens 115, first period pokes ABOVE the prior high (120), sellers step
        # in, reverses and drives lower (Fig 8.18 geometry, down direction).
        bars = _bars([
            (115, 121, 115, 116, 50),   # test up to 121 (> prior high 120)
            (116, 116, 110, 111, 60),   # reverse down through the open
            (111, 112, 105, 106, 70),
            (106, 107, 100, 101, 80),
        ])
        ind = market_generated_indicators(bars, prior=_prior(100, 120))
        assert ind.open_type == "open_test_drive_down"

    def test_open_rejection_reverse_up(self):
        # Drives down off the open, gets rejected (single-print buying tail),
        # reverses and closes up — but WITHOUT testing a prior reference, which
        # is what separates it from an Open-Test-Drive (Fig 8.19).
        bars = _bars([
            (100, 101, 97, 98, 50),    # early low 97, no prior ref reached
            (98, 103, 98, 102, 60),    # reverse up through the open
            (102, 106, 101, 105, 70),
            (105, 108, 104, 107, 80),
        ])
        ind = market_generated_indicators(bars, prior=_prior(90, 110))
        assert ind.open_type == "open_rejection_reverse_up"

    def test_open_auction(self):
        # Rotates above and below the open with no conviction.
        bars = _bars([
            (100, 102, 98, 100, 50),
            (100, 101, 99, 100, 50),
            (100, 102, 98, 101, 50),
            (100, 101, 99, 100, 50),
        ])
        ind = market_generated_indicators(bars)
        assert ind.open_type == "open_auction"


class TestDayShape:
    def test_trend_up(self):
        bars = _bars([
            (100, 102, 100, 101, 50),
            (101, 104, 101, 103, 60),
            (103, 106, 103, 105, 70),
            (105, 108, 105, 107, 80),
            (107, 110, 107, 109, 90),
        ])
        ind = market_generated_indicators(bars)
        assert ind.day_shape == "trend_up"
        assert ind.one_timeframing == "up"

    def test_neutral_two_sided_extension(self):
        # IB (first 2 periods) = 100..105; later periods extend BOTH above and
        # below → two-way indecision.
        bars = _bars([
            (102, 105, 100, 103, 50),   # IB
            (103, 105, 101, 104, 50),   # IB
            (104, 108, 104, 106, 60),   # extend up (108 > 105)
            (106, 106, 97, 99, 60),     # extend down (97 < 100)
        ])
        ind = market_generated_indicators(bars)
        assert ind.range_ext_up and ind.range_ext_down
        assert ind.day_shape == "neutral"

    def test_p_shape_short_covering(self):
        # Fat value up top, thin single-print tail below, and NOT a clean trend
        # (a late dip breaks the higher-low run) — Dalton's p / short-covering.
        bars = _bars([
            (108, 110, 100, 108, 50),   # early spike leaves a 100-107 tail
            (108, 110, 108, 109, 90),
            (109, 110, 108, 109, 90),
            (108, 110, 106, 107, 90),   # dips: breaks the up one-timeframing
        ], )
        ind = market_generated_indicators(bars, tick_size=1.0)
        assert ind.day_shape == "p_shape"
        assert ind.profile_skew == "p"

    def test_b_shape_long_liquidation(self):
        # Fat value at the bottom, thin single-print tail above, non-trending.
        bars = _bars([
            (101, 102, 100, 101, 90),
            (101, 102, 100, 101, 90),
            (101, 109, 101, 102, 50),   # spike up leaves a 103-108 tail
            (101, 102, 100, 101, 90),
        ], )
        ind = market_generated_indicators(bars, tick_size=1.0)
        assert ind.day_shape == "b_shape"
        assert ind.profile_skew == "b"


class TestBalanceState:
    def test_all_six_relationships(self):
        from market_profile import _classify_balance
        # prior value area = [100, 110]
        assert _classify_balance(112, 120, 100, 110) == "higher"
        assert _classify_balance(85, 95, 100, 110) == "lower"
        assert _classify_balance(95, 115, 100, 110) == "outside"
        assert _classify_balance(102, 108, 100, 110) == "inside"
        assert _classify_balance(105, 115, 100, 110) == "overlapping_higher"
        assert _classify_balance(95, 105, 100, 110) == "overlapping_lower"

    def test_unknown_without_prior(self):
        from market_profile import _classify_balance
        assert _classify_balance(100, 110, None, None) == "unknown"

    def test_in_balance_flag(self):
        # Overlapping value = balance; disjoint (higher) = imbalance.
        bal = market_generated_indicators(
            _bars([(105, 108, 103, 106, 50), (106, 109, 104, 107, 50)]),
            prior=_prior(100, 110), tick_size=1.0,
        )
        assert bal.in_balance is True


class TestExcessAndPoor:
    def test_excess_high_single_print_tail(self):
        # One period spikes up leaving a single-print tail; the rest camp low.
        bars = _bars([
            (100, 110, 100, 101, 50),   # tail 102-110 = single prints
            (100, 101, 99, 100, 90),
            (100, 101, 99, 100, 90),
            (100, 101, 99, 100, 90),
        ])
        ind = market_generated_indicators(bars, tick_size=1.0)
        assert ind.excess_high is True
        assert ind.poor_high is False
        assert ind.single_print_count >= 2

    def test_poor_high_multiple_prints_no_tail(self):
        # Several periods print the exact same high — a flat, "poor" high that
        # is likely to be revisited (no excess tail above it).
        bars = _bars([
            (108, 110, 106, 109, 90),
            (108, 110, 106, 109, 90),
            (108, 110, 106, 109, 90),
        ])
        ind = market_generated_indicators(bars, tick_size=1.0)
        assert ind.excess_high is False
        assert ind.poor_high is True


class TestIndicatorsSerialization:
    def test_to_dict_round_trips(self):
        bars = _bars([
            (100, 102, 100, 101, 50),
            (101, 104, 101, 103, 60),
            (103, 106, 103, 105, 70),
        ])
        ind = market_generated_indicators(bars, prior=_prior(95, 108))
        d = indicators_to_dict(ind)
        # Every dataclass field is represented, and the day is ISO-serialized.
        assert d["day"] == "2026-04-17"
        assert d["open_type"] == ind.open_type
        assert d["profile_skew"] in {"p", "b", "balanced"}
        assert set(d) >= {
            "open_type", "day_shape", "profile_skew", "balance_state",
            "in_balance", "range_ext_first", "excess_high", "one_timeframing",
            "poc", "vah", "val",
        }

    def test_empty_bars_returns_none(self):
        assert market_generated_indicators([]) is None
