"""Tests for the /pair-paper-compare endpoint.

The endpoint reads on-disk EOD JSON sidecars written by run_paper_pairs.py
(``data_cache/pair_paper{,_<system>}_eod_<date>.json``) and merges them into
a per-day + aggregate comparison. These tests stub the data_cache dir and
write synthetic sidecars to exercise the merge logic.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db, run_manager as rm
from backend.routers import pair_paper_compare as ppc_router


def _write_eod(
    cache: Path,
    d: date,
    system: str,
    pairs: list[dict],
) -> Path:
    """Write a synthetic EOD sidecar matching run_paper_pairs.write_eod_sidecar()
    format. Returns the path written."""
    if system == "baseline":
        filename = f"pair_paper_eod_{d.isoformat()}.json"
    else:
        filename = f"pair_paper_{system}_eod_{d.isoformat()}.json"
    path = cache / filename
    payload = {
        "date": d.isoformat(),
        "generated_at": f"{d.isoformat()}T15:25:00",
        "system": system,
        "pairs": pairs,
    }
    path.write_text(json.dumps(payload))
    return path


def _pair_report(
    a: str, b: str, *, realized: float, n_trades: int = 1, costs: float = 100.0,
) -> dict:
    """Per-pair EOD record matching strategies.pair_trading.generate_eod_report()."""
    return {
        "strategy": "pair_trading",
        "pair": [a, b],
        "hedge_ratio": 1.0,
        "position": "FLAT",
        "current_z": 0.4,
        "entry_z": 2.0,
        "realized_pnl": realized,
        "unrealized_pnl": 0.0,
        "transaction_costs": costs,
        "n_closed_trades": n_trades,
        "spread_history_size": 60,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with auth session + DATA_CACHE pointed at tmp_path."""
    from tests._helpers import login_client
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")

    # Each test writes synthetic EODs into its own cache.
    cache = tmp_path / "data_cache"
    cache.mkdir()
    monkeypatch.setattr(ppc_router, "DATA_CACHE", cache)

    app = create_app()
    with TestClient(app) as c:
        login_client(c)
        c._cache = cache  # stash for tests to write into
        yield c
    db.reset_for_tests(None)


# Use a fixed Friday (2026-05-15 is a real trading-day Friday) as `end`, so
# `_collect_trading_days(end, 5)` returns Mon 2026-05-11 → Fri 2026-05-15.
END = "2026-05-15"
EXPECTED_DAYS = [
    date(2026, 5, 11),
    date(2026, 5, 12),
    date(2026, 5, 13),
    date(2026, 5, 14),
    date(2026, 5, 15),
]


class TestPairPaperCompare:
    def test_happy_path_both_systems_with_data(self, client):
        cache: Path = client._cache
        # Baseline trades 2 pairs on day 1; persistent trades 1 pair (CIPLA/ITC)
        # on days 1 and 2. Day 3 only baseline produces an EOD.
        _write_eod(cache, EXPECTED_DAYS[0], "baseline", [
            _pair_report("RELIANCE", "TCS", realized=5000.0),
            _pair_report("CIPLA", "ITC", realized=-1500.0),
        ])
        _write_eod(cache, EXPECTED_DAYS[1], "baseline", [
            _pair_report("RELIANCE", "TCS", realized=2000.0, n_trades=2),
        ])
        _write_eod(cache, EXPECTED_DAYS[0], "persistent", [
            _pair_report("CIPLA", "ITC", realized=3000.0),
        ])
        _write_eod(cache, EXPECTED_DAYS[1], "persistent", [
            _pair_report("CIPLA", "ITC", realized=1500.0, n_trades=2),
        ])

        r = client.get(f"/pair-paper-compare?days=5&end={END}")
        assert r.status_code == 200
        body = r.json()
        assert body["start_date"] == EXPECTED_DAYS[0].isoformat()
        assert body["end_date"] == EXPECTED_DAYS[-1].isoformat()
        assert body["systems"] == ["baseline", "persistent"]

        # Aggregate: baseline 5000 + (-1500) + 2000 = 5500 over 2 days w/ data.
        # Persistent: 3000 + 1500 = 4500 over 2 days.
        agg = {row["system"]: row for row in body["aggregate"]}
        assert agg["baseline"]["net_pnl"] == pytest.approx(5500.0)
        assert agg["baseline"]["n_days_with_data"] == 2
        assert agg["baseline"]["n_unique_pairs"] == 2
        # Day 0: 1 + 1 trades; Day 1: 2 trades. Total 4.
        assert agg["baseline"]["n_closed_trades"] == 4
        assert agg["baseline"]["avg_per_day"] == pytest.approx(2750.0)
        assert agg["persistent"]["net_pnl"] == pytest.approx(4500.0)
        assert agg["persistent"]["n_days_with_data"] == 2
        assert agg["persistent"]["n_unique_pairs"] == 1

        # Daily rows: 5 entries, 3 days with no files.
        assert len(body["daily"]) == 5
        day_by_date = {row["date"]: row for row in body["daily"]}
        d0 = day_by_date[EXPECTED_DAYS[0].isoformat()]
        assert d0["systems"]["baseline"]["net_pnl"] == pytest.approx(3500.0)
        assert d0["systems"]["baseline"]["n_pairs"] == 2
        assert d0["systems"]["persistent"]["net_pnl"] == pytest.approx(3000.0)
        # Day 3+ have no files for either system.
        assert day_by_date[EXPECTED_DAYS[2].isoformat()]["systems"]["baseline"] is None
        assert day_by_date[EXPECTED_DAYS[4].isoformat()]["systems"]["persistent"] is None

        # Per-pair: RELIANCE/TCS only baseline, CIPLA/ITC in both.
        per_pair = {r["pair"]: r for r in body["per_pair"]}
        assert per_pair["RELIANCE/TCS"]["traded_by"] == "only baseline"
        assert per_pair["RELIANCE/TCS"]["by_system"]["baseline"] == pytest.approx(7000.0)
        assert per_pair["RELIANCE/TCS"]["by_system"]["persistent"] is None
        assert per_pair["CIPLA/ITC"]["traded_by"] == "BOTH"
        assert per_pair["CIPLA/ITC"]["by_system"]["baseline"] == pytest.approx(-1500.0)
        assert per_pair["CIPLA/ITC"]["by_system"]["persistent"] == pytest.approx(4500.0)

    def test_no_eod_files_returns_empty_window(self, client):
        # No sidecars at all — endpoint must still 200 with zero-filled aggregate.
        r = client.get(f"/pair-paper-compare?days=5&end={END}")
        assert r.status_code == 200
        body = r.json()
        assert len(body["daily"]) == 5
        for row in body["daily"]:
            assert row["systems"]["baseline"] is None
            assert row["systems"]["persistent"] is None
        for agg in body["aggregate"]:
            assert agg["net_pnl"] == 0.0
            assert agg["n_days_with_data"] == 0
            assert agg["n_unique_pairs"] == 0
            assert agg["avg_per_day"] == 0.0
        assert body["per_pair"] == []

    def test_only_baseline_has_data(self, client):
        # First-day-after-deploy realistic state: persistent runner hasn't fired yet.
        cache: Path = client._cache
        _write_eod(cache, EXPECTED_DAYS[0], "baseline", [
            _pair_report("RELIANCE", "TCS", realized=1000.0),
        ])
        r = client.get(f"/pair-paper-compare?days=5&end={END}")
        body = r.json()
        agg = {row["system"]: row for row in body["aggregate"]}
        assert agg["baseline"]["net_pnl"] == pytest.approx(1000.0)
        assert agg["persistent"]["net_pnl"] == 0.0
        assert agg["persistent"]["n_days_with_data"] == 0

        per_pair = {r["pair"]: r for r in body["per_pair"]}
        assert per_pair["RELIANCE/TCS"]["traded_by"] == "only baseline"

    def test_malformed_json_is_skipped(self, client):
        cache: Path = client._cache
        # Valid baseline + corrupted persistent.
        _write_eod(cache, EXPECTED_DAYS[0], "baseline", [
            _pair_report("RELIANCE", "TCS", realized=1000.0),
        ])
        (cache / f"pair_paper_persistent_eod_{EXPECTED_DAYS[0].isoformat()}.json").write_text(
            "{this is not valid json"
        )

        r = client.get(f"/pair-paper-compare?days=5&end={END}")
        assert r.status_code == 200
        body = r.json()
        day0 = body["daily"][0]
        assert day0["systems"]["baseline"]["net_pnl"] == pytest.approx(1000.0)
        # Malformed persistent must appear as missing, not 500.
        assert day0["systems"]["persistent"] is None

    def test_weekends_excluded_from_date_range(self, client):
        # `end=2026-05-17` is a Sunday. _collect_trading_days(end=Sunday, days=5)
        # should return Mon..Fri of the previous week — Sat 5/16 and Sun 5/17
        # never appear in the daily rows.
        cache: Path = client._cache
        _write_eod(cache, EXPECTED_DAYS[-1], "baseline", [
            _pair_report("RELIANCE", "TCS", realized=500.0),
        ])
        r = client.get(f"/pair-paper-compare?days=5&end=2026-05-17")
        assert r.status_code == 200
        body = r.json()
        dates = [row["date"] for row in body["daily"]]
        assert "2026-05-16" not in dates  # Saturday
        assert "2026-05-17" not in dates  # Sunday
        # All five returned dates must be weekdays.
        for d_str in dates:
            assert date.fromisoformat(d_str).weekday() < 5

    def test_per_pair_sorted_by_max_single_system_pnl_desc(self, client):
        # Biggest per-system P&L wins the top spot regardless of sign distribution
        # across systems.
        cache: Path = client._cache
        _write_eod(cache, EXPECTED_DAYS[0], "baseline", [
            _pair_report("A", "B", realized=10000.0),     # max=10000
            _pair_report("C", "D", realized=-50000.0),    # max=-50000
            _pair_report("E", "F", realized=500.0),       # max=500
        ])
        _write_eod(cache, EXPECTED_DAYS[0], "persistent", [
            _pair_report("C", "D", realized=2000.0),      # max=2000 (raises C/D to 2000)
        ])
        r = client.get(f"/pair-paper-compare?days=5&end={END}")
        body = r.json()
        order = [row["pair"] for row in body["per_pair"]]
        # A/B (max=10000), then C/D (max=2000), then E/F (max=500)
        assert order == ["A/B", "C/D", "E/F"]

    def test_invalid_end_date_returns_400(self, client):
        r = client.get("/pair-paper-compare?days=3&end=not-a-date")
        assert r.status_code == 400
        assert "end" in r.json()["detail"].lower()

    def test_single_system_param_returns_400(self, client):
        r = client.get(f"/pair-paper-compare?days=3&systems=baseline")
        assert r.status_code == 400
        assert "at least 2" in r.json()["detail"]

    def test_days_above_max_returns_422(self, client):
        # FastAPI Query(ge=1, le=30) enforces the bound — 31 should 422.
        r = client.get(f"/pair-paper-compare?days=31")
        assert r.status_code == 422

    def test_days_below_min_returns_422(self, client):
        r = client.get(f"/pair-paper-compare?days=0")
        assert r.status_code == 422

    def test_unknown_system_returns_empty_data_not_error(self, client):
        # Querying for a system that has no EOD files anywhere must not 500 —
        # it should look like a system that hasn't started running yet.
        cache: Path = client._cache
        _write_eod(cache, EXPECTED_DAYS[0], "baseline", [
            _pair_report("A", "B", realized=1000.0),
        ])
        r = client.get(f"/pair-paper-compare?days=5&end={END}&systems=baseline,nonsense")
        assert r.status_code == 200
        agg = {row["system"]: row for row in r.json()["aggregate"]}
        assert agg["baseline"]["net_pnl"] == pytest.approx(1000.0)
        assert agg["nonsense"]["net_pnl"] == 0.0
        assert agg["nonsense"]["n_days_with_data"] == 0
