"""
Test-suite-wide setup.

The dashboard now refuses to boot without DASHBOARD_PASSWORD and
DASHBOARD_SESSION_SECRET (security review CRITICAL #1, 2026-05-05). Tests
need both set before any backend module imports `get_settings()`. Set them
here at collection time and clear the lru_cache so a stale instance from a
prior test session can't leak across.
"""
from __future__ import annotations

import os

# Anything reasonable; only TestClient sees these.
os.environ["DASHBOARD_PASSWORD"] = "test-password"
os.environ["DASHBOARD_SESSION_SECRET"] = (
    "test-session-secret-must-be-at-least-some-bytes-for-itsdangerous"
)
# TestClient's base URL is http://testserver. Force a non-https dashboard
# URL so SessionMiddleware does NOT mark the cookie Secure — otherwise
# httpx refuses to replay it on the next test request and the session
# silently disappears between requests.
os.environ["DASHBOARD_URL"] = "http://testserver"
# Live mode is OFF by default for tests (the dashboard is signals+paper only,
# CRITICAL Q1). Pin it here so the suite stays deterministic even when the host
# .env has ALLOW_LIVE_MODE=true armed for a live runner — an env var overrides
# the .env file, so tests never read the ambient value. Tests that exercise the
# armed path flip this explicitly (see the live_mode_client fixture).
os.environ["ALLOW_LIVE_MODE"] = "false"

# Clear the cache so the first get_settings() in the test process sees the
# env vars above, not whatever Pydantic baked at import time elsewhere.
try:
    from backend.settings import get_settings
    get_settings.cache_clear()
except Exception:
    # Backend not on the import path yet — that's fine, tests/test_backend.py
    # adjusts sys.path before importing.
    pass


# Login helper now lives in tests/_helpers.py so sibling test modules can
# import it directly (conftest is autoloaded by pytest but not importable
# as a regular module).
