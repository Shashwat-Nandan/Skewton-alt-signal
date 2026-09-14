"""Groww adapter — registered, not live-wired.

Groww's Trade API (growwapi) is a real product (API key + secret or TOTP
→ access token → place_order). It is *not* wired here: no paper soak, no
fill-semantics proof, and AGENTS.md forbids skipping paper → live. The
factory accepts `broker.name = groww` so the toggle exists, then login()
fails loud instead of silently placing on Zerodha.
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any, Optional

from .base import BrokerAdapter
from .credentials import reject_placeholders, resolve_credential
from .errors import BrokerConfigError, BrokerNotImplementedError


class GrowwAdapter(BrokerAdapter):
    name = "groww"
    display_name = "Groww"
    login_style = "headless"

    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        config = configparser.ConfigParser()
        path = Path(config_path)
        if path.exists():
            config.read(path)
        self.api_key = resolve_credential("GROWW_API_KEY", config, "groww", "api_key")
        self.api_secret = resolve_credential(
            "GROWW_API_SECRET", config, "groww", "api_secret"
        )
        self.totp_key = resolve_credential(
            "GROWW_TOTP_KEY", config, "groww", "totp_key"
        )
        if not Path(config_path).exists() and not any(
            (self.api_key, self.api_secret, self.totp_key)
        ):
            raise BrokerConfigError(
                f"Config file not found: {config_path} (needed for [groww])."
            )

    def login(self) -> Any:
        reject_placeholders({
            "api_key": self.api_key,
            "api_secret": self.api_secret,
        })
        raise BrokerNotImplementedError(
            "Groww is registered in the broker factory but is not live-wired. "
            "Set broker.name=zerodha or kotak. Groww orders require a paper "
            "soak of this adapter before anyone considers live (AGENTS.md "
            "paper → live gate). Config keys: [groww] api_key, api_secret, "
            "totp_key (or GROWW_* env vars)."
        )

    def refresh(self) -> Any:
        return self.login()

    def logout(self) -> None:
        return None

    def status_profile(self) -> Optional[dict]:
        return None
