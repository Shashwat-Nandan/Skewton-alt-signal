"""
Backend API tests via FastAPI TestClient.

Strategy instantiation is mocked so tests don't need a real Kite session,
real bhavcopy, or real options chain. Auth is also stubbed at the
kite_oauth boundary.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db, run_manager as rm
from backend.settings import get_settings


@pytest.fixture
def client(tmp_path):
    # Reset the run manager singleton so each test sees an empty registry.
    # Bind the SQLite singleton to a tmp path so we never touch the real
    # data_cache/dashboard.db. Every auth-touching test mocks
    # kite_oauth.get_authenticated_kite at the boundary so the real
    # .kite_session.json is never read or written either.
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    app = create_app()
    with TestClient(app) as c:
        # Acquire a dashboard session for the rest of the test. The cookie
        # is held by TestClient and replayed on every subsequent request.
        # Tests that exercise the unauthenticated surface should use a
        # fresh TestClient (see TestDashboardSession below).
        r = c.post("/api/session/login", json={"password": "test-password"})
        assert r.status_code == 204, r.text
        yield c
    db.reset_for_tests(None)


@pytest.fixture
def unauthed_client(tmp_path):
    """Same setup as `client` but without the login step."""
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    app = create_app()
    with TestClient(app) as c:
        yield c
    db.reset_for_tests(None)


@pytest.fixture
def live_mode_client(tmp_path, monkeypatch):
    """`client` but with live mode ARMED (ALLOW_LIVE_MODE=true), to cover the
    inverse of the default signals+paper build. The env override + cache_clear
    make the running app's get_settings() return allow_live_mode=True; the cache
    is cleared again on teardown so later tests see the conftest default."""
    monkeypatch.setenv("ALLOW_LIVE_MODE", "true")
    get_settings.cache_clear()
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/api/session/login", json={"password": "test-password"})
        assert r.status_code == 204, r.text
        yield c
    db.reset_for_tests(None)
    get_settings.cache_clear()


# ──────────────────────────────────────────────────────────
# Meta + strategies
# ──────────────────────────────────────────────────────────

class TestMeta:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["version"] == "0.1.0"

    def test_root_does_not_leak_live_mode_posture(self, live_mode_client):
        # Audit 3.5: the unauthenticated root must NOT reveal whether live mode
        # is armed — even when ALLOW_LIVE_MODE is set. (Live is unconditionally
        # refused at the dashboard anyway; the flag was a pure info-leak.)
        r = live_mode_client.get("/")
        assert r.status_code == 200
        assert "live_mode_enabled" not in r.json()

    def test_strategies_list(self, client):
        r = client.get("/api/strategies")
        assert r.status_code == 200
        names = [s["name"] for s in r.json()]
        assert "taleb_karpathy" in names
        assert "pair_trading" in names

    def test_strategies_params_known(self, client):
        r = client.get("/api/strategies/pair_trading/params")
        assert r.status_code == 200
        param_names = {p["name"] for p in r.json()}
        assert {"entry_z", "exit_z", "stop_z"}.issubset(param_names)

    def test_strategies_params_unknown(self, client):
        r = client.get("/api/strategies/bogus/params")
        assert r.status_code == 404


# ──────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────

class TestAuth:
    def test_status_unauthenticated(self, client):
        # No token cached → not authed
        with patch("backend.kite_oauth.get_authenticated_kite", return_value=None):
            r = client.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json()["authenticated"] is False

    def test_status_authenticated(self, client):
        fake_kite = MagicMock()
        with patch("backend.kite_oauth.get_authenticated_kite", return_value=fake_kite), \
             patch("backend.kite_oauth.verify_token", return_value={
                 "user_id": "AB1234", "user_name": "Test User", "email": "test@example.com",
             }):
            r = client.get("/api/auth/status")
        assert r.status_code == 200
        body = r.json()
        assert body["authenticated"] is True
        assert body["user_id"] == "AB1234"
        assert body["user_name"] == "Test User"

    def test_login_url_missing_creds(self, client):
        # Settings has no api_key → 500 with helpful message
        with patch("backend.kite_oauth.get_login_url",
                   side_effect=RuntimeError("KITE_API_KEY is not set...")):
            r = client.get("/api/auth/login")
        assert r.status_code == 500
        assert "KITE_API_KEY" in r.json()["detail"]

    def test_login_url_ok(self, client):
        with patch("backend.kite_oauth.get_login_url",
                   return_value="https://kite.zerodha.com/connect/login?api_key=XYZ&v=3"):
            r = client.get("/api/auth/login")
        assert r.status_code == 200
        assert r.json()["login_url"].startswith("https://kite.zerodha.com")

    def test_callback_missing_token(self, client):
        r = client.get("/api/auth/callback?status=error")
        assert r.status_code == 400

    def test_logout_clears_session(self, client):
        with patch("backend.kite_oauth.clear_cached_session") as mock_clear:
            r = client.post("/api/auth/logout")
        assert r.status_code == 200
        mock_clear.assert_called_once()


# ──────────────────────────────────────────────────────────
# Runs lifecycle
# ──────────────────────────────────────────────────────────

class TestRuns:
    def test_list_runs_empty(self, client):
        r = client.get("/api/runs")
        assert r.status_code == 200
        assert r.json() == []

    def test_create_run_unknown_strategy(self, client):
        r = client.post("/api/runs", json={
            "strategy": "bogus", "mode": "paper", "params": {},
        })
        assert r.status_code == 400

    def test_create_run_live_rejected(self, client):
        r = client.post("/api/runs", json={
            "strategy": "pair_trading", "mode": "live", "params": {},
        })
        assert r.status_code == 403
        assert "not available from the dashboard" in r.json()["detail"]

    def test_create_run_live_rejected_even_when_flag_set(self, live_mode_client):
        # Audit 2026-06-10 task 1.3: the dashboard 403 is UNCONDITIONAL.
        # ALLOW_LIVE_MODE arms the headless runners' quad-lock; on a host
        # that sets it (production does), the dashboard must still refuse —
        # RunManager has none of the runner-side risk controls (H-2). This
        # test is the regression guard for the exact production hole the
        # audit found: flag set in .env → dashboard silently armed.
        r = live_mode_client.post("/api/runs", json={
            "strategy": "pair_trading", "mode": "live", "params": {},
        })
        assert r.status_code == 403
        assert "not available from the dashboard" in r.json()["detail"]

    def test_create_run_unauthenticated(self, client):
        with patch("backend.kite_oauth.get_authenticated_kite", return_value=None):
            r = client.post("/api/runs", json={
                "strategy": "pair_trading", "mode": "paper", "params": {},
            })
        assert r.status_code == 401

    def test_create_run_lifecycle(self, client):
        # Stub the strategy class so create_run doesn't hit Kite or bhavcopy
        fake_strategy = MagicMock()
        fake_strategy.scan_and_propose.return_value = []
        fake_strategy.check_and_rehedge.return_value = []
        fake_strategy.generate_eod_report.return_value = {"strategy": "fake"}

        with patch("backend.kite_oauth.get_authenticated_kite", return_value=MagicMock()), \
             patch("backend.run_manager.get_strategy",
                   return_value=lambda **kw: fake_strategy):
            # Create
            r = client.post("/api/runs", json={
                "strategy": "pair_trading", "mode": "paper", "params": {},
            })
            assert r.status_code == 201
            run_id = r.json()["id"]
            assert r.json()["status"] == "RUNNING"

            # List shows it
            r = client.get("/api/runs")
            assert any(run["id"] == run_id for run in r.json())

            # Detail
            r = client.get(f"/api/runs/{run_id}")
            assert r.status_code == 200
            body = r.json()
            assert body["id"] == run_id
            assert body["strategy_name"] == "pair_trading"
            assert "signals" in body
            assert "trades" in body
            assert "pnl_history" in body

            # Stop
            r = client.post(f"/api/runs/{run_id}/stop")
            assert r.status_code == 200
            assert r.json()["status"] in ("STOPPING", "STOPPED")

    def test_create_run_builds_strategy_off_event_loop(self, client):
        # Audit 2026-06-10 task 2.7 (M-9): pair __init__ seeds spread
        # history from bhavcopy and fetches the NFO dump — minutes, not
        # ms. If construction runs on the event loop, every dashboard
        # request freezes for the duration. Pin that RunManager routes it
        # through asyncio.to_thread.
        import asyncio as _asyncio

        fake_strategy = MagicMock()
        fake_strategy.scan_and_propose.return_value = []
        fake_strategy.check_and_rehedge.return_value = []
        fake_strategy.generate_eod_report.return_value = {"strategy": "fake"}

        with patch("backend.kite_oauth.get_authenticated_kite", return_value=MagicMock()), \
             patch("backend.run_manager.get_strategy",
                   return_value=lambda **kw: fake_strategy), \
             patch("backend.run_manager.asyncio.to_thread",
                   side_effect=_asyncio.to_thread) as to_thread:
            r = client.post("/api/runs", json={
                "strategy": "pair_trading", "mode": "paper", "params": {},
            })
            assert r.status_code == 201
            assert to_thread.called, (
                "strategy construction must go through asyncio.to_thread"
            )
            client.post(f"/api/runs/{r.json()['id']}/stop")

    def test_get_run_not_found(self, client):
        r = client.get("/api/runs/does-not-exist")
        assert r.status_code == 404

    def test_stop_run_not_found(self, client):
        r = client.post("/api/runs/does-not-exist/stop")
        assert r.status_code == 404
