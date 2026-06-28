"""Tests for the /kalman-trend endpoint.

Surfaces the loop-engineering pilot for the dashboard: daily positions + the
Kalman-vs-MA A/B performance from the latest EOD sidecar, PLUS the loop-specific
bits (checker verdict, kill-switch status, lessons) from STATE.md. These pin that
the per-instrument Kalman/MA detail and the edge (kalman−ma) are read through, the
most-recent sidecar wins, the loop's STATE.md status + lessons are surfaced, and
the empty (no-session-yet) case is a clean 200 rather than a 500.
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
from backend.routers import kalman_trend as kt_router
from loop_engine import memory


def _book(kind, *, realized_rupees=0.0, n_trades=0, win_rate=None, open_pos=0, n_bars=0):
    """Mirror IntradayTrendStrategy.book_summary()'s shape."""
    return {"signal_kind": kind, "n_trades": n_trades, "realized_points": realized_rupees,
            "realized_rupees": realized_rupees, "win_rate": win_rate,
            "open_pos": open_pos, "n_bars": n_bars}


def _write_eod(cache: Path, d: date, instruments, *, tot_k=0.0, tot_m=0.0):
    path = cache / f"kalman_trend_eod_{d.isoformat()}.json"
    path.write_text(json.dumps({
        "date": d.isoformat(), "system": "kalman_trend_ab",
        "instruments": instruments,
        "total_kalman_rupees": tot_k, "total_ma_rupees": tot_m,
        "kalman_minus_ma_rupees": round(tot_k - tot_m, 2),
    }))
    return path


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests._helpers import login_client
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    cache = tmp_path / "data_cache"
    cache.mkdir()
    state_root = tmp_path / "state"
    monkeypatch.setattr(kt_router, "DATA_CACHE", cache)
    monkeypatch.setattr(kt_router, "STATE_ROOT", state_root)
    app = create_app()
    with TestClient(app) as c:
        login_client(c)
        c._cache = cache
        c._state_root = state_root
        yield c
    db.reset_for_tests(None)


END = "2026-06-26"


class TestKalmanTrend:
    def test_surfaces_latest_session_ab_performance_and_positions(self, client):
        cache: Path = client._cache
        # An older session too — the endpoint must return the LATEST.
        _write_eod(cache, date(2026, 6, 25), [
            {"symbol": "NIFTY", "kalman": _book("kalman"), "ma": _book("ma")}], tot_k=1.0)
        _write_eod(cache, date(2026, 6, 26), [
            {"symbol": "NIFTY",
             "kalman": _book("kalman", realized_rupees=900.0, n_trades=4, open_pos=1),
             "ma": _book("ma", realized_rupees=300.0, n_trades=3, open_pos=0)},
            {"symbol": "BANKNIFTY",
             "kalman": _book("kalman", realized_rupees=-200.0, n_trades=2, open_pos=-1),
             "ma": _book("ma", realized_rupees=100.0, n_trades=2, open_pos=0)},
        ], tot_k=700.0, tot_m=400.0)

        r = client.get(f"/api/kalman-trend?end={END}")
        assert r.status_code == 200
        body = r.json()

        assert body["latest_date"] == "2026-06-26"
        assert body["n_sessions_recorded"] == 2
        assert body["total_kalman_rupees"] == 700.0
        assert body["kalman_minus_ma_rupees"] == 300.0

        nifty = next(i for i in body["instruments"] if i["symbol"] == "NIFTY")
        assert nifty["kalman"]["open_pos"] == 1 and nifty["ma"]["open_pos"] == 0
        assert nifty["edge_rupees"] == 600.0          # 900 − 300, the A/B's point

    def test_surfaces_loop_memory_status_and_lessons(self, client):
        """The loop-specific bits — checker verdict, kill-switch status, lessons —
        must be read from STATE.md, not just the EOD P&L."""
        cache: Path = client._cache
        state_root: Path = client._state_root
        _write_eod(cache, date(2026, 6, 26), [
            {"symbol": "NIFTY", "kalman": _book("kalman"), "ma": _book("ma")}])

        memory.write_run_summary("kalman_trend", root=state_root,
                                 status="ok", checker="REJECT: NIFTY.sharpe 0.41>=1.50",
                                 risk="HALT_NEW_ENTRIES")
        memory.append_lesson("kalman_trend", "checker verdict None→REJECT", root=state_root)

        body = client.get(f"/api/kalman-trend?end={END}").json()
        assert body["loop"]["checker"].startswith("REJECT")
        assert body["loop"]["risk"] == "HALT_NEW_ENTRIES"
        assert any("REJECT" in le for le in body["lessons"])

    def test_empty_when_no_session_yet(self, client):
        """No sidecar + no STATE.md → a clean empty 200, not a 500."""
        body = client.get(f"/api/kalman-trend?end={END}").json()
        assert body["latest_date"] is None
        assert body["instruments"] == []
        assert body["loop"] is None
        assert body["lessons"] == []

    def test_bad_date_is_400(self, client):
        assert client.get("/api/kalman-trend?end=not-a-date").status_code == 400

    def test_malformed_instrument_is_skipped_not_500(self, client):
        cache: Path = client._cache
        _write_eod(cache, date(2026, 6, 26), [
            "not-a-dict",
            {"symbol": "NIFTY", "kalman": _book("kalman", realized_rupees=5.0), "ma": _book("ma")},
        ])
        body = client.get(f"/api/kalman-trend?end={END}").json()
        # the good record survives; the junk one is dropped
        assert [i["symbol"] for i in body["instruments"]] == ["NIFTY"]
