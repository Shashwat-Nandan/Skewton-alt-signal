"""
Dashboard session auth — gating + login/logout flow.

Companion to tests/test_backend.py: that file's `client` fixture logs in for
us, so most tests there exercise the *post-login* surface. This file uses
`unauthed_client` (also from test_backend.py via shared conftest) to verify
the gate itself — what happens before login, on logout, with a tampered or
missing cookie.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db, run_manager as rm


@pytest.fixture
def unauthed_client(tmp_path):
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    app = create_app()
    with TestClient(app) as c:
        yield c
    db.reset_for_tests(None)


# ──────────────────────────────────────────────────────────
# /session/* — the public auth surface
# ──────────────────────────────────────────────────────────

class TestSessionEndpoints:
    def test_me_returns_false_when_no_cookie(self, unauthed_client):
        r = unauthed_client.get("/api/session/me")
        assert r.status_code == 200
        assert r.json() == {"authenticated": False}

    def test_login_with_correct_password_sets_cookie(self, unauthed_client):
        r = unauthed_client.post("/api/session/login", json={"password": "test-password"})
        assert r.status_code == 204
        # SessionMiddleware writes Set-Cookie on the response. Attribute
        # case isn't guaranteed across versions — compare lowercased.
        cookie = r.headers.get("set-cookie", "").lower()
        assert "dashboard_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        # /session/me now reflects the new state.
        r2 = unauthed_client.get("/api/session/me")
        assert r2.json() == {"authenticated": True}

    def test_login_with_wrong_password_returns_401(self, unauthed_client):
        r = unauthed_client.post("/api/session/login", json={"password": "wrong"})
        assert r.status_code == 401
        # Still no session.
        assert unauthed_client.get("/api/session/me").json() == {"authenticated": False}

    def test_login_with_empty_password_rejected_by_validation(self, unauthed_client):
        r = unauthed_client.post("/api/session/login", json={"password": ""})
        assert r.status_code == 422  # Pydantic min_length=1

    def test_logout_clears_session(self, unauthed_client):
        unauthed_client.post("/api/session/login", json={"password": "test-password"})
        assert unauthed_client.get("/api/session/me").json() == {"authenticated": True}
        r = unauthed_client.post("/api/session/logout")
        assert r.status_code == 204
        assert unauthed_client.get("/api/session/me").json() == {"authenticated": False}


# ──────────────────────────────────────────────────────────
# Gating — every protected router must 401 without a session
# ──────────────────────────────────────────────────────────

class TestGating:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/strategies"),
            ("GET", "/api/strategies/pair_trading/params"),
            ("GET", "/api/runs"),
            ("POST", "/api/runs"),
            ("GET", "/api/runs/some-id"),
            ("POST", "/api/runs/some-id/stop"),
            ("GET", "/api/auth/status"),
            ("GET", "/api/auth/login"),
            ("POST", "/api/auth/logout"),
            ("GET", "/api/market-profile/symbols"),
            ("GET", "/api/market-profile/RELIANCE"),
            ("GET", "/api/pair-candidates"),
        ],
    )
    def test_protected_route_401_without_session(self, unauthed_client, method, path):
        r = unauthed_client.request(method, path)
        assert r.status_code == 401, f"{method} {path} returned {r.status_code}, expected 401"

    def test_meta_root_remains_public(self, unauthed_client):
        # Liveness probe: must work without auth so health checks / smoke
        # scripts can verify the backend is up.
        r = unauthed_client.get("/")
        assert r.status_code == 200

    def test_tampered_cookie_rejected(self, unauthed_client):
        unauthed_client.post("/api/session/login", json={"password": "test-password"})
        # Replace the signed cookie value with junk; SessionMiddleware should
        # refuse to deserialise and treat the request as unauthenticated.
        unauthed_client.cookies.set("dashboard_session", "tampered.garbage.value")
        r = unauthed_client.get("/api/strategies")
        assert r.status_code == 401

    def test_session_carries_through_to_protected_route(self, unauthed_client):
        # /strategies is parameter-listing only (no kite calls), so a
        # logged-in client can hit it directly with no extra mocks.
        unauthed_client.post("/api/session/login", json={"password": "test-password"})
        r = unauthed_client.get("/api/strategies")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
