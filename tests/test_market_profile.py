"""Tests for the Market Profile compute module + bars storage + API smoke."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from market_profile import (
    Bar,
    auto_tick_size,
    compute_composite,
    compute_day_profile,
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
