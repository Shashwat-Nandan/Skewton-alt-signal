"""Tests for the /equity/* endpoints.

Mirrors the pair_candidates router style: monkeypatch the file paths
(LOG_DIR / FII_CACHE_DIR) at the router module to a tmp dir so the test
never reads the operator's real logs/ or data_cache/fii_dii/ tree.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db, run_manager as rm
from backend.routers import equity_swing as eq_router


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests._helpers import login_client
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")

    # Redirect file-system reads to a per-test sandbox so we never touch the
    # real logs/ or data_cache/fii_dii/ tree.
    monkeypatch.setattr(eq_router, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(eq_router, "FII_CACHE_DIR", tmp_path / "fii_dii")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    app = create_app()
    with TestClient(app) as c:
        login_client(c)
        yield c
    db.reset_for_tests(None)


def _seed_position(symbol: str, status: str = "OPEN", **overrides) -> int:
    pos = {
        "symbol": symbol, "side": "LONG",
        "entry_dt": "2026-05-01T15:35:00",
        "entry_px": 1000.0, "qty": 10,
        "initial_sl": 950.0, "current_sl": 950.0,
        "target": 1100.0, "atr_at_entry": 25.0,
        "rationale": "trend_up ADX=24 OI=LONG_BUILDUP fii5d=₹+850cr",
        "last_mtm_dt": "2026-05-08T15:35:00",
        "last_mtm_px": 1020.0,
        "high_watermark": 1035.0,
    }
    pos.update(overrides)
    pid = db.insert_equity_position(pos, opened_by_scan="close")
    if status == "CLOSED":
        db.close_equity_position(
            pid,
            exit_dt=overrides.get("exit_dt", "2026-05-09T15:35:00"),
            exit_px=overrides.get("exit_px", 1080.0),
            exit_reason=overrides.get("exit_reason", "TARGET_HIT"),
            pnl=overrides.get("pnl", 800.0),
        )
    return pid


class TestPositions:
    def test_empty(self, client):
        r = client.get("/api/equity/positions")
        assert r.status_code == 200
        assert r.json() == {"positions": []}

    def test_returns_open_and_closed_by_default(self, client):
        _seed_position("INFY", status="OPEN")
        _seed_position("RELIANCE", status="CLOSED")
        r = client.get("/api/equity/positions")
        assert r.status_code == 200
        body = r.json()
        assert len(body["positions"]) == 2
        symbols = {p["symbol"] for p in body["positions"]}
        assert symbols == {"INFY", "RELIANCE"}
        # OPEN should sort first
        assert body["positions"][0]["status"] == "OPEN"

    def test_filter_open(self, client):
        _seed_position("INFY", status="OPEN")
        _seed_position("RELIANCE", status="CLOSED")
        r = client.get("/api/equity/positions?status=open")
        assert r.status_code == 200
        body = r.json()
        assert len(body["positions"]) == 1
        assert body["positions"][0]["symbol"] == "INFY"
        assert body["positions"][0]["status"] == "OPEN"

    def test_filter_closed_carries_exit_fields(self, client):
        _seed_position("RELIANCE", status="CLOSED")
        r = client.get("/api/equity/positions?status=closed")
        assert r.status_code == 200
        body = r.json()
        assert len(body["positions"]) == 1
        p = body["positions"][0]
        assert p["status"] == "CLOSED"
        assert p["exit_px"] == pytest.approx(1080.0)
        assert p["exit_reason"] == "TARGET_HIT"
        assert p["pnl"] == pytest.approx(800.0)

    def test_invalid_status_400(self, client):
        r = client.get("/api/equity/positions?status=garbage")
        assert r.status_code == 400


class TestSignals:
    def _write_jsonl(self, path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

    def test_missing_file_returns_empty(self, client, tmp_path):
        # No logs/signals-<today>.jsonl exists; response has empty signals.
        r = client.get("/api/equity/signals")
        assert r.status_code == 200
        body = r.json()
        assert body["signals"] == []
        assert body["generated_at"] is None

    def test_returns_only_varsity_equity_swing_rows(self, client, tmp_path):
        path = tmp_path / "logs" / "signals-2026-05-08.jsonl"
        self._write_jsonl(path, [
            {
                "timestamp": "2026-05-08T15:35:01",
                "strategy": "varsity_equity_swing",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "quantity": 10,
                "price": 1500.0,
                "rationale": "trend ADX=24",
            },
            # Foreign strategy — must be filtered out
            {
                "timestamp": "2026-05-08T15:35:02",
                "strategy": "pair_trading",
                "tradingsymbol": "RELIANCE",
                "transaction_type": "SELL",
                "quantity": 1, "price": 2500.0,
            },
        ])
        r = client.get("/api/equity/signals?date=2026-05-08")
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == "2026-05-08"
        assert body["generated_at"] is not None
        assert len(body["signals"]) == 1
        s = body["signals"][0]
        assert s["tradingsymbol"] == "INFY"
        assert s["transaction_type"] == "BUY"

    def test_skips_malformed_lines(self, client, tmp_path):
        path = tmp_path / "logs" / "signals-2026-05-08.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "not-json\n"
            + json.dumps({
                "timestamp": "2026-05-08T15:35:01",
                "strategy": "varsity_equity_swing",
                "tradingsymbol": "INFY",
                "transaction_type": "BUY",
                "quantity": 10, "price": 1500.0,
            }) + "\n"
        )
        r = client.get("/api/equity/signals?date=2026-05-08")
        assert r.status_code == 200
        assert len(r.json()["signals"]) == 1

    def test_invalid_date_400(self, client):
        r = client.get("/api/equity/signals?date=not-a-date")
        assert r.status_code == 400


class TestScans:
    def test_empty(self, client):
        r = client.get("/api/equity/scans")
        assert r.status_code == 200
        assert r.json() == {"scans": []}

    def test_returns_recent_first(self, client):
        db.insert_equity_scan(
            scan_dt="2026-05-07T15:35:00", scan_kind="close", mode="paper",
            n_signals=2, n_trades=1, n_open_positions=3, n_closed_today=0,
        )
        db.insert_equity_scan(
            scan_dt="2026-05-08T09:30:00", scan_kind="open", mode="paper",
            n_signals=0, n_trades=1, n_open_positions=2, n_closed_today=1,
        )
        r = client.get("/api/equity/scans")
        body = r.json()
        assert r.status_code == 200
        assert len(body["scans"]) == 2
        # ORDER BY id DESC — most recent inserted first
        assert body["scans"][0]["scan_kind"] == "open"
        assert body["scans"][0]["n_closed_today"] == 1


class TestFiiDii:
    def test_missing_cache_returns_503(self, client):
        # FII_CACHE_DIR was redirected to a path that doesn't exist.
        r = client.get("/api/equity/fii-dii")
        assert r.status_code == 503
        assert "fetch_fii_dii" in r.json()["detail"]

    def test_cache_present_returns_panel(self, client, tmp_path):
        cache = tmp_path / "fii_dii"
        cache.mkdir(parents=True, exist_ok=True)
        for d, fii_net, dii_net in [
            ("2026-05-05", -1200.0, 800.0),
            ("2026-05-06",   300.0, 200.0),
            ("2026-05-07",   500.0, 100.0),
            ("2026-05-08",   700.0, 400.0),
        ]:
            (cache / f"{d}.json").write_text(json.dumps([
                {"category": "FII/FPI", "buyValue": 1000.0,
                 "sellValue": 1000.0 - fii_net, "netValue": fii_net},
                {"category": "DII", "buyValue": 500.0,
                 "sellValue": 500.0 - dii_net, "netValue": dii_net},
            ]))
        r = client.get("/api/equity/fii-dii")
        assert r.status_code == 200
        body = r.json()
        assert body["generated_at"] is not None
        assert len(body["rows"]) == 4
        last = body["rows"][-1]
        assert last["date"] == "2026-05-08"
        assert last["fii_net"] == pytest.approx(700.0)
        assert last["dii_net"] == pytest.approx(400.0)
        # 5d cumulative sum (across the 4 days we wrote)
        assert last["fii_net_5d"] == pytest.approx(-1200.0 + 300.0 + 500.0 + 700.0)
        # Last day's fii_net_5d is +300; boost flag must agree
        assert last["fii_boost"] == 1
