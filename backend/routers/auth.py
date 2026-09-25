"""Broker auth endpoints (Zerodha OAuth + headless TOTP/MPIN)."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.broker import get_broker, read_broker_name
from core.broker.errors import (
    BrokerAuthError,
    BrokerConfigError,
    BrokerNetworkError,
    BrokerNotImplementedError,
)

from .. import kite_oauth
from ..settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_DISPLAY = {
    "zerodha": "Zerodha Kite",
    "kotak": "Kotak Securities Neo",
    "groww": "Groww",
    "dhan": "Dhan",
}


def _broker_meta() -> tuple[str, str, str]:
    name = read_broker_name(str(get_settings().config_path))
    display = _DISPLAY.get(name, name)
    style = "oauth" if name == "zerodha" else "headless"
    return name, display, style


class AuthStatus(BaseModel):
    authenticated: bool
    broker: str
    display_name: str
    login_style: str
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    email: Optional[str] = None


class LoginUrlResponse(BaseModel):
    broker: str
    display_name: str
    login_style: str
    login_url: Optional[str] = None


@router.get("/status", response_model=AuthStatus)
def status():
    """Whether we have a valid broker session, plus the logged-in profile."""
    name, display, style = _broker_meta()
    empty = AuthStatus(
        authenticated=False, broker=name, display_name=display, login_style=style,
    )
    if name == "zerodha":
        kite = kite_oauth.get_authenticated_kite()
        if kite is None:
            return empty
        profile = kite_oauth.verify_token(kite)
        if not profile:
            return empty
        return AuthStatus(
            authenticated=True,
            broker=name,
            display_name=display,
            login_style=style,
            user_id=profile.get("user_id"),
            user_name=profile.get("user_name"),
            email=profile.get("email"),
        )
    try:
        adapter = get_broker(str(get_settings().config_path))
    except BrokerConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    profile = adapter.status_profile()
    if not profile:
        return empty
    return AuthStatus(
        authenticated=True,
        broker=name,
        display_name=display,
        login_style=style,
        user_id=profile.get("user_id"),
        user_name=profile.get("user_name"),
        email=profile.get("email"),
    )


@router.get("/login", response_model=LoginUrlResponse)
def login_url():
    """
    Zerodha: returns the Kite OAuth URL the SPA should navigate to.
    Headless brokers: login_url is null; the SPA POSTs /auth/login instead.
    """
    name, display, style = _broker_meta()
    if style != "oauth":
        return LoginUrlResponse(
            broker=name, display_name=display, login_style=style, login_url=None,
        )
    try:
        url = kite_oauth.get_login_url()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return LoginUrlResponse(
        broker=name, display_name=display, login_style=style, login_url=url,
    )


@router.post("/login")
def headless_login():
    """Server-side TOTP/MPIN login using config.ini / env. Never accepts
    secrets from the browser — MPIN stays on the host."""
    name, display, style = _broker_meta()
    if style != "headless":
        raise HTTPException(
            status_code=400,
            detail="This broker uses OAuth. GET /auth/login for the redirect URL.",
        )
    try:
        adapter = get_broker(str(get_settings().config_path))
        adapter.login()
    except BrokerNotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except (BrokerConfigError, BrokerAuthError, BrokerNetworkError) as e:
        # Timeout and 429 stay 502. login() raises BrokerNetworkError
        # for those and leaves the shared Trade token in place.
        logger.exception("Headless broker login failed")
        raise HTTPException(status_code=502, detail=str(e))
    return {"status": "ok", "broker": name, "display_name": display}


@router.get("/callback")
def callback(
    request_token: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
):
    """
    OAuth redirect target. Kite hits us with ?request_token=... after the
    user logs in, then we exchange it for an access_token and bounce the
    user back to the dashboard.
    """
    if status == "error" or not request_token:
        raise HTTPException(status_code=400, detail="Kite login was cancelled or failed")
    try:
        kite_oauth.exchange_request_token(request_token)
    except Exception:
        # KiteConnect TokenException messages can contain URL fragments
        # and API-key prefixes — log full details server-side, return a
        # generic message to the client.
        logger.exception("Failed to exchange request_token")
        raise HTTPException(status_code=502, detail="Kite token exchange failed")

    # Bounce the browser back to the SPA root. The ?login=success query
    # tells the SPA to invalidate its cached auth state and re-fetch the
    # profile (which is now valid). The SPA strips the query from the URL
    # after handling so the user lands on a clean "/".
    target = f"{get_settings().dashboard_url.rstrip('/')}/?login=success"
    return RedirectResponse(url=target, status_code=302)


@router.post("/logout")
def logout():
    name, _, style = _broker_meta()
    if name == "zerodha" or style == "oauth":
        kite_oauth.clear_cached_session()
    else:
        try:
            get_broker(str(get_settings().config_path)).logout()
        except BrokerConfigError:
            pass
    return {"status": "ok"}
