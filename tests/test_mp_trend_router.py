"""API smoke for the Market-Profile trend book router.

Encodes the two behaviours that matter (Rule 9): the page must render before the
paper runner has ever run (tables absent → empty book, not a 500), and once
rows exist the summary must aggregate them correctly (net = Σ closed net, win
rate over closed, halted reflects the latest run).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def fresh_db(tmp_path):
    from backend import db as backend_db
    backend_db.reset_for_tests(tmp_path / "mp_trend_test.db")
    backend_db.init_schema()
    yield backend_db
    backend_db.reset_for_tests()


def _client():
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from tests._helpers import login_client
    c = TestClient(create_app())
    login_client(c)
    return c


def _seed(conn):
    """Two closed trades (one win, one loss) + one open + two runs."""
    conn.executescript(
        """
        CREATE TABLE mp_trend_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, entry_date TEXT,
            entry_px REAL, qty INTEGER, exit_date TEXT, exit_px REAL,
            gross REAL, cost REAL, net REAL, status TEXT, created_at TEXT);
        CREATE TABLE mp_trend_runs (
            run_date TEXT PRIMARY KEY, n_universe INTEGER, n_trend_up INTEGER,
            n_opened INTEGER, n_closed INTEGER, day_net REAL, cum_net REAL,
            halted INTEGER, reason TEXT, created_at TEXT);
        """
    )
    now = datetime.now().isoformat()
    conn.executemany(
        "INSERT INTO mp_trend_positions (symbol, entry_date, entry_px, qty, "
        "exit_date, exit_px, gross, cost, net, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("A", "2026-05-01", 100.0, 10, "2026-05-02", 106.0, 60, 25, 35, "CLOSED", now),
            ("B", "2026-05-01", 200.0, 5, "2026-05-02", 190.0, -50, 25, -75, "CLOSED", now),
            ("C", "2026-05-02", 300.0, 3, None, None, None, None, None, "OPEN", now),
        ],
    )
    conn.executemany(
        "INSERT INTO mp_trend_runs (run_date, n_universe, n_trend_up, n_opened, "
        "n_closed, day_net, cum_net, halted, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-05-01", 48, 2, 2, 0, 0.0, 0.0, 0, None, now),
            ("2026-05-02", 48, 1, 1, 2, -40.0, -40.0, 0, None, now),
        ],
    )


class TestMpTrendRouter:
    def test_empty_book_before_runner_ran(self, fresh_db):
        # No mp_trend_* tables yet → 200 with a zeroed book, never a 500.
        c = _client()
        r = c.get("/api/mp-trend")
        assert r.status_code == 200
        j = r.json()
        assert j["summary"]["n_closed_trades"] == 0
        assert j["daily"] == [] and j["open_positions"] == []

    def test_aggregates_after_rows_exist(self, fresh_db):
        _seed(fresh_db.get_conn())
        c = _client()
        j = c.get("/api/mp-trend").json()
        s = j["summary"]
        assert s["net_pnl"] == pytest.approx(-40.0)     # 35 + (-75)
        assert s["n_closed_trades"] == 2
        assert s["n_open_positions"] == 1
        assert s["win_rate"] == pytest.approx(0.5)       # 1 of 2 closed > 0
        assert s["latest_date"] == "2026-05-02"
        assert s["halted"] is False
        assert len(j["daily"]) == 2
        assert j["open_positions"][0]["symbol"] == "C"
        assert j["open_positions"][0]["notional"] == pytest.approx(900.0)
