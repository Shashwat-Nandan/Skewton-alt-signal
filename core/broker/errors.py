"""Broker-agnostic errors for the adapter layer.

Strategies and the order executor catch these *in addition to* kiteconnect's
TokenException / NetworkException / OrderException so a Kotak (or later
Groww/Dhan) client can fail with the same recovery policy the pair runner
proved live, without importing kiteconnect in a non-Zerodha process.
"""
from __future__ import annotations


class BrokerError(Exception):
    """Base for every adapter failure. Never swallowed."""


class BrokerConfigError(BrokerError):
    """Missing / placeholder credentials or an unknown broker name."""


class BrokerAuthError(BrokerError):
    """Login failed (bad TOTP, MPIN, OAuth exchange, etc.)."""


class BrokerTokenError(BrokerError):
    """Session token rejected (403 / expired). Callers may refresh-once."""


class BrokerNetworkError(BrokerError):
    """Transient transport failure. Callers may retry-once."""


class BrokerOrderError(BrokerError):
    """Broker-side reject (margin, validation, exchange). Do not retry."""


class BrokerNotImplementedError(BrokerError):
    """Broker is registered in the factory but not live-wired.

    Selecting groww/dhan today must fail here — never fall through to
    Zerodha. The paper → live gate (AGENTS.md safety rule 3) is why.
    """
