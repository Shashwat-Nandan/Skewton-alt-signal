"""Dhan adapter — registered, not live-wired.

DhanHQ v2 is a real product (client_id + access token, or PIN+TOTP).
Same gate as Groww: the factory accepts `broker.name = dhan` so the
toggle is real, then login() fails loud. Do not "just call the REST
API" from a runner until this adapter has a paper soak.
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any, Optional

from .base import BrokerAdapter
from .credentials import reject_placeholders, resolve_credential
from .errors import BrokerConfigError, BrokerNotImplementedError


class DhanAdapter(BrokerAdapter):
    name = "dhan"
    display_name = "Dhan"
    login_style = "headless"

    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        config = configparser.ConfigParser()
        path = Path(config_path)
        if path.exists():
            config.read(path)
        self.client_id = resolve_credential(
            "DHAN_CLIENT_ID", config, "dhan", "client_id"
        )
        self.access_token = resolve_credential(
            "DHAN_ACCESS_TOKEN", config, "dhan", "access_token"
        )
        self.pin = resolve_credential("DHAN_PIN", config, "dhan", "pin")
        self.totp_key = resolve_credential(
            "DHAN_TOTP_KEY", config, "dhan", "totp_key"
        )
        if not path.exists() and not any(
            (self.client_id, self.access_token, self.pin, self.totp_key)
        ):
            raise BrokerConfigError(
                f"Config file not found: {config_path} (needed for [dhan])."
            )

    def login(self) -> Any:
        # Access token *or* PIN+TOTP is enough to prove the operator filled
        # the section; we still refuse to trade.
        if self.access_token:
            reject_placeholders({
                "client_id": self.client_id,
                "access_token": self.access_token,
            })
        else:
            reject_placeholders({
                "client_id": self.client_id,
                "pin": self.pin,
                "totp_key": self.totp_key,
            })
        raise BrokerNotImplementedError(
            "Dhan is registered in the broker factory but is not live-wired. "
            "Set broker.name=zerodha or kotak. Dhan orders require a paper "
            "soak of this adapter before anyone considers live (AGENTS.md "
            "paper → live gate). Config keys: [dhan] client_id, access_token "
            "(or pin + totp_key)."
        )

    def refresh(self) -> Any:
        return self.login()

    def logout(self) -> None:
        return None

    def status_profile(self) -> Optional[dict]:
        return None
