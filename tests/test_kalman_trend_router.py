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


def _write_runner_state(cache: Path, positions):
    """positions = list of (symbol, kalman_pos, ma_pos) → live runner state file."""
    cache.joinpath("kalman_trend_runner_state.json").write_text(json.dumps({
        "updated": "2026-06-26T11:00:00",
        "instruments": [
            {"symbol": s, "kalman": {"pos": kp}, "ma": {"pos": mp}}
            for (s, kp, mp) in positions
        ],
    }))


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
    def test_surfaces_latest_session_ab_performance(self, client):
        cache: Path = client._cache
        # An older session too — the endpoint must return the LATEST.
        _write_eod(cache, date(2026, 6, 25), [
            {"symbol": "NIFTY", "kalman": _book("kalman"), "ma": _book("ma")}], tot_k=1.0)
        _write_eod(cache, date(2026, 6, 26), [
            {"symbol": "NIFTY",
             "kalman": _book("kalman", realized_rupees=900.0, n_trades=4),
             "ma": _book("ma", realized_rupees=300.0, n_trades=3)},
            {"symbol": "BANKNIFTY",
             "kalman": _book("kalman", realized_rupees=-200.0, n_trades=2),
             "ma": _book("ma", realized_rupees=100.0, n_trades=2)},
        ], tot_k=700.0, tot_m=400.0)

        r = client.get(f"/api/kalman-trend?end={END}")
        assert r.status_code == 200
        body = r.json()

        assert body["latest_date"] == "2026-06-26"
        assert body["n_sessions_recorded"] == 2
        assert body["total_kalman_rupees"] == 700.0   # recomputed: 900 + (−200)
        assert body["kalman_minus_ma_rupees"] == 300.0

        nifty = next(i for i in body["instruments"] if i["symbol"] == "NIFTY")
        assert nifty["edge_rupees"] == 600.0          # 900 − 300, the A/B's point

    def test_positions_come_from_live_runner_state_not_eod(self, client):
        """#1: the EOD sidecar force-closes (open_pos always 0); the tab must show
        the LIVE intraday positions from kalman_trend_runner_state.json."""
        cache: Path = client._cache
        # EOD sidecar is flat (as it always is post-force-close)...
        _write_eod(cache, date(2026, 6, 26), [
            {"symbol": "NIFTY", "kalman": _book("kalman", open_pos=0), "ma": _book("ma", open_pos=0)}])
        # ...but the live runner state holds a Kalman long / MA short.
        _write_runner_state(cache, [("NIFTY", 1, -1)])

        nifty = next(i for i in client.get(f"/api/kalman-trend?end={END}").json()["instruments"]
                     if i["symbol"] == "NIFTY")
        assert nifty["kalman"]["open_pos"] == 1
        assert nifty["ma"]["open_pos"] == -1

    def test_session_trades_surface_from_the_sidecar(self, client):
        """Per-day view: each book's session_trades + session net ₹ from the EOD
        sidecar pass through, so the dashboard can list today's fills alongside the
        cumulative aggregate."""
        cache: Path = client._cache
        kal = _book("kalman", realized_rupees=900.0, n_trades=4)
        kal["session_realized_rupees"] = 7152.0
        kal["session_trades"] = [
            {"side": -1, "entry_price": 24132.0, "exit_price": 24030.56,
             "pnl_points": 101.44, "pnl_rupees": 7608.0, "reason": "target"},
            {"side": 1, "entry_price": 23891.0, "exit_price": 23884.92,
             "pnl_points": -6.08, "pnl_rupees": -456.0, "reason": "stop"},
        ]
        _write_eod(cache, date(2026, 6, 26), [
            {"symbol": "NIFTY", "kalman": kal, "ma": _book("ma")}])

        nifty = next(i for i in client.get(f"/api/kalman-trend?end={END}").json()["instruments"]
                     if i["symbol"] == "NIFTY")
        assert nifty["kalman"]["session_realized_rupees"] == 7152.0
        trades = nifty["kalman"]["session_trades"]
        assert len(trades) == 2
        assert trades[0]["reason"] == "target" and trades[0]["pnl_rupees"] == 7608.0
        assert trades[1]["side"] == 1 and trades[1]["pnl_rupees"] == -456.0
        assert nifty["ma"]["session_trades"] == []          # MA book had no fills

    def test_old_sidecar_without_session_fields_defaults_empty(self, client):
        """Sessions recorded before this shipped lack session_trades; the tab must
        show them aggregate-only (empty list / 0.0), never 500."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 6, 26), [
            {"symbol": "NIFTY",
             "kalman": _book("kalman", realized_rupees=900.0, n_trades=4),
             "ma": _book("ma")}])
        nifty = next(i for i in client.get(f"/api/kalman-trend?end={END}").json()["instruments"]
                     if i["symbol"] == "NIFTY")
        assert nifty["kalman"]["session_trades"] == []
        assert nifty["kalman"]["session_realized_rupees"] == 0.0
        assert nifty["kalman"]["realized_rupees"] == 900.0  # aggregate still shows

    def test_null_fields_do_not_500(self, client):
        """#3: a sidecar with explicit JSON null instruments/totals must not crash."""
        cache: Path = client._cache
        cache.joinpath("kalman_trend_eod_2026-06-26.json").write_text(json.dumps({
            "date": "2026-06-26", "instruments": None,
            "total_kalman_rupees": None, "total_ma_rupees": None,
        }))
        r = client.get(f"/api/kalman-trend?end={END}")
        assert r.status_code == 200
        assert r.json()["instruments"] == []

    def test_totals_recomputed_from_surviving_rows(self, client):
        """#5: a dropped malformed row must drop from the headline totals too, so
        the table always sums to the metric cards."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 6, 26), [
            "not-a-dict",  # dropped
            {"symbol": "NIFTY", "kalman": _book("kalman", realized_rupees=5.0), "ma": _book("ma")},
        ], tot_k=999.0, tot_m=0.0)  # writer's bogus total must NOT leak through
        body = client.get(f"/api/kalman-trend?end={END}").json()
        assert [i["symbol"] for i in body["instruments"]] == ["NIFTY"]
        assert body["total_kalman_rupees"] == 5.0     # recomputed from survivor, not 999

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

    def test_loop_memory_returned_without_any_eod_sidecar(self, client):
        """#2 (backend side): loop status + lessons are returned independent of the
        EOD report, so the UI can show the verdict before the first session."""
        state_root: Path = client._state_root
        memory.write_run_summary("kalman_trend", root=state_root,
                                 status="ok", checker="pass", risk="ok")
        memory.append_lesson("kalman_trend", "first lesson", root=state_root)

        body = client.get(f"/api/kalman-trend?end={END}").json()
        assert body["latest_date"] is None            # no EOD yet
        assert body["loop"]["checker"] == "pass"      # ...but loop memory present
        assert any("first lesson" in le for le in body["lessons"])

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
