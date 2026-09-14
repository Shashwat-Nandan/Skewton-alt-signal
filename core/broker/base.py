"""BrokerAdapter — the seam between this repo and a retail broker.

Callers (runners, dashboard auth, the live order executor) talk to this
ABC. Concrete adapters wrap one vendor SDK/REST surface and, for the
order path, expose a *Kite-shaped* client so existing
`kite.place_order` / `kite.quote` / `kite.order_history` call sites keep
working. Vendor quirks (Kotak `nse_fo` vs Kite `NFO`, `B` vs `BUY`) stay
inside the adapter.

Market-data CLIs (`market_data/fetch_*`, tick capture) are *not* on this
seam yet — they still use `KiteAuthManager` directly. Switching those is
a later increment; this one covers login + ordering, which is what a
broker change actually moves.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class BrokerAdapter(ABC):
    """One instance per process. Holds session state, not positions."""

    name: str
    display_name: str
    # "oauth" — dashboard redirects the operator to the broker (Zerodha).
    # "headless" — server-side TOTP/MPIN/token using config.ini / env.
    login_style: str

    @abstractmethod
    def login(self) -> Any:
        """Authenticate and return a Kite-shaped trading client.

        Runners pass the return value into strategies as `kite`.
        """

    @abstractmethod
    def refresh(self) -> Any:
        """Re-bind a fresh client after a mid-session token reject (H8)."""

    @abstractmethod
    def logout(self) -> None:
        """Drop the local session cache. Best-effort remote logout."""

    @abstractmethod
    def status_profile(self) -> Optional[dict]:
        """Cached-session health check for the dashboard.

        Returns a kite-like profile dict, or None if the operator must
        re-authenticate. Must not require password/TOTP credentials for
        Zerodha — the dashboard OAuth path has no user_id/password.
        """

    def get_login_url(self) -> Optional[str]:
        """OAuth redirect URL, or None for headless brokers."""
        return None

    def cached_client(self) -> Optional[Any]:
        """Kite-shaped client from the session cache, no login.

        None if there is no cache. Dashboard portfolio/runs poll this
        every few seconds — it must not fire TOTP/MPIN.
        """
        return None
