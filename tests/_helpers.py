"""Shared test utilities. conftest.py can't be imported as a regular
module from sibling test files, so the helper lives here."""
from __future__ import annotations


def login_client(client) -> None:
    """Acquire a dashboard session on `client`. Tests that build their own
    TestClient (rather than using the shared `client` fixture) need to call
    this once after construction so subsequent gated routes don't 401."""
    r = client.post("/session/login", json={"password": "test-password"})
    assert r.status_code == 204, r.text
