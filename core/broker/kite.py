"""Zerodha Kite adapter — wraps the existing auth paths, no behaviour change.

Headless runners keep using `KiteAuthManager` (TOTP screen-scrape).
The dashboard keeps using `backend.kite_oauth` (browser redirect).
Both still share `.kite_session.json`. This adapter is the factory's
Zerodha leaf so `broker.name = zerodha` is the default, not a special case.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .base import BrokerAdapter

logger = logging.getLogger(__name__)


class ZerodhaKiteAdapter(BrokerAdapter):
    name = "zerodha"
    display_name = "Zerodha Kite"
    login_style = "oauth"

    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self._auth = None
        self._client = None

    def login(self) -> Any:
        from core.kite_auth import KiteAuthManager

        self._auth = KiteAuthManager(self.config_path)
        self._client = self._auth.get_kite()
        return self._client

    def refresh(self) -> Any:
        # Re-run get_kite so a mid-session TokenException (H8) rebuilds
        # from cache or a fresh TOTP login. Recreating KiteAuthManager
        # is intentional: the cached token file is the source of truth.
        return self.login()

    def logout(self) -> None:
        # Headless cache. Dashboard OAuth logout is backend.kite_oauth
        # (core must not import backend).
        path = Path(".kite_session.json")
        if path.exists():
            path.unlink()
        self._client = None
        self._auth = None

    def status_profile(self) -> Optional[dict]:
        # Dashboard OAuth status is dispatched in backend/routers/auth.py
        # via kite_oauth so we don't need user_id/password here. Headless
        # callers that want a profile should login() and call profile().
        return None

    def get_login_url(self) -> Optional[str]:
        return None
