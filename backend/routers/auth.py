"""Kite OAuth endpoints."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .. import kite_oauth
from ..settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthStatus(BaseModel):
    authenticated: bool
    user_id: Optional[str] = None
    user_name: Optional[str] = None
    email: Optional[str] = None


class LoginUrlResponse(BaseModel):
    login_url: str


@router.get("/status", response_model=AuthStatus)
def status():
    """Whether we have a valid Kite session, plus the logged-in user's profile."""
    kite = kite_oauth.get_authenticated_kite()
    if kite is None:
        return AuthStatus(authenticated=False)
    profile = kite_oauth.verify_token(kite)
    if not profile:
        return AuthStatus(authenticated=False)
    return AuthStatus(
        authenticated=True,
        user_id=profile.get("user_id"),
        user_name=profile.get("user_name"),
        email=profile.get("email"),
    )


@router.get("/login", response_model=LoginUrlResponse)
def login_url():
    """
    Returns the URL the SPA should `window.location.assign()` to in order
    to start the Kite login flow.

    Returning JSON (instead of a 302) keeps the SPA in control of navigation
    and works cleanly with CORS.
    """
    try:
        url = kite_oauth.get_login_url()
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return LoginUrlResponse(login_url=url)


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
    kite_oauth.clear_cached_session()
    return {"status": "ok"}
