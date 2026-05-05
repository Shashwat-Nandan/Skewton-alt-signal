"""Dashboard session endpoints — the only routes outside `require_session`."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .. import dashboard_auth
from ..settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/session", tags=["session"])


class LoginRequest(BaseModel):
    password: Annotated[str, Field(min_length=1, max_length=512)]


class SessionStatus(BaseModel):
    authenticated: bool


@router.get("/me", response_model=SessionStatus)
def me(request: Request) -> SessionStatus:
    """SPA calls this on first paint to choose login-page vs. app."""
    return SessionStatus(**dashboard_auth.session_status(request))


@router.post("/login", status_code=status.HTTP_204_NO_CONTENT)
def login(body: LoginRequest, request: Request, response: Response) -> Response:
    settings = get_settings()
    if not dashboard_auth.password_matches(body.password, settings.dashboard_password):
        # Log the attempt without echoing the submitted password.
        client = request.client.host if request.client else "unknown"
        logger.warning("Dashboard login rejected from %s", client)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password",
        )
    dashboard_auth.mark_authenticated(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    dashboard_auth.clear_session(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
