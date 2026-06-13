"""
Kite OAuth — browser redirect flow for the dashboard.

Distinct from kite_auth.KiteAuthManager (the headless TOTP screen-scrape used
by the unattended VPS daemon). This module is the user-driven OAuth flow:

  1. Frontend hits GET /api/auth/login → 302 to https://kite.zerodha.com/connect/login
  2. User authenticates on Kite's site
  3. Kite redirects to GET /api/auth/callback?request_token=...
  4. Backend exchanges request_token + api_secret → access_token via SDK
  5. Token cached to .kite_session.json (compatible with the daemon path)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

from kiteconnect import KiteConnect

from .settings import get_settings

logger = logging.getLogger(__name__)


def get_login_url() -> str:
    """URL the user is redirected to in order to start the Kite login flow."""
    settings = get_settings()
    if not settings.kite_api_key:
        raise RuntimeError(
            "KITE_API_KEY is not set. Register an app at "
            "https://developers.kite.trade/ and add KITE_API_KEY/KITE_API_SECRET to .env"
        )
    kite = KiteConnect(api_key=settings.kite_api_key)
    return kite.login_url()


def exchange_request_token(request_token: str) -> dict:
    """
    Trade the one-shot request_token (from the OAuth callback) for an
    access_token, then persist the token to the cache file.

    Returns the full session dict from kiteconnect (includes user_id,
    user_name, email, broker, exchanges, products, access_token).
    """
    settings = get_settings()
    if not (settings.kite_api_key and settings.kite_api_secret):
        raise RuntimeError("KITE_API_KEY / KITE_API_SECRET must be configured")

    kite = KiteConnect(api_key=settings.kite_api_key)
    session = kite.generate_session(request_token, api_secret=settings.kite_api_secret)
    _save_token(session)
    logger.info("Authenticated %s (%s) via OAuth", session.get("user_name"), session.get("user_id"))
    return session


def _save_token(session: dict) -> None:
    payload = {
        "access_token": session["access_token"],
        "timestamp": datetime.now().isoformat(),
        "user_id": session.get("user_id"),
        "user_name": session.get("user_name"),
        "email": session.get("email"),
    }
    path = get_settings().token_cache_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open with 0600 explicitly — default umask leaves the cache 0644,
    # which exposes a live access_token to any local UID.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)


def load_cached_session() -> Optional[dict]:
    """Return the cached session dict (or None if absent / unparseable)."""
    path = get_settings().token_cache_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_cached_session() -> None:
    path = get_settings().token_cache_path
    if path.exists():
        path.unlink()


def get_authenticated_kite() -> Optional[KiteConnect]:
    """
    Build a KiteConnect client from the cached access_token.

    Returns None if no token is cached or the token has been rejected by Kite.
    Callers should treat None as "user must re-authenticate".
    """
    session = load_cached_session()
    if not session or not session.get("access_token"):
        return None
    settings = get_settings()
    if not settings.kite_api_key:
        return None
    kite = KiteConnect(api_key=settings.kite_api_key)
    kite.set_access_token(session["access_token"])
    return kite


def verify_token(kite: KiteConnect) -> Optional[dict]:
    """Light health-check — fetches profile to confirm the token is still good."""
    try:
        return kite.profile()
    except Exception as e:
        logger.info("Cached token rejected: %s", e)
        return None
