"""Broker factory — one config key selects the adapter.

    [broker]
    name = kotak   # zerodha | groww | dhan

Kotak Neo is the primary broker. Missing section / missing file → kotak.
Set name = zerodha to stay on Kite. An unknown name fails loud: it does
not fall through to another broker.
"""
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

from .base import BrokerAdapter
from .dhan import DhanAdapter
from .errors import BrokerConfigError
from .groww import GrowwAdapter
from .kite import ZerodhaKiteAdapter
from .kotak import KotakNeoAdapter

SUPPORTED = ("kotak", "zerodha", "groww", "dhan")
DEFAULT_BROKER = "kotak"

# Aliases operators actually type.
_ALIASES = {
    "kite": "zerodha",
    "zerodha kite": "zerodha",
    "kotak securities": "kotak",
    "kotak neo": "kotak",
    "neo": "kotak",
}


def read_broker_name(config_path: str = "config.ini") -> str:
    path = Path(config_path)
    if not path.exists():
        return DEFAULT_BROKER
    cfg = configparser.ConfigParser()
    cfg.read(path)
    raw = (
        cfg.get("broker", "name", fallback=DEFAULT_BROKER)
        if cfg.has_section("broker") else DEFAULT_BROKER
    )
    name = (raw or DEFAULT_BROKER).strip().lower()
    return _ALIASES.get(name, name)


def get_broker(config_path: str = "config.ini") -> BrokerAdapter:
    name = read_broker_name(config_path)
    if name not in SUPPORTED:
        raise BrokerConfigError(
            f"Unknown broker {name!r}. Supported: {', '.join(SUPPORTED)}. "
            "Refusing to fall back to Zerodha — that would place on the "
            "wrong account."
        )
    if name == "zerodha":
        return ZerodhaKiteAdapter(config_path)
    if name == "kotak":
        return KotakNeoAdapter(config_path)
    if name == "groww":
        return GrowwAdapter(config_path)
    return DhanAdapter(config_path)


def get_trading_client(config_path: str = "config.ini") -> Any:
    """Authenticate the configured broker; return a Kite-shaped client.

    Drop-in replacement for `KiteAuthManager(path).get_kite()` in runners.
    """
    return get_broker(config_path).login()
