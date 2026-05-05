"""
Dashboard session auth.

Wraps the entire FastAPI surface with a single shared-password login. The
session lives in a signed HttpOnly cookie (Starlette SessionMiddleware), so
state is stateless on the server and survives backend restarts. Three things
to keep in mind for the rest of the codebase:

- `require_session` is the only dependency callers should use. Add it to
  every router that handles operator state.
- `/session/login`, `/session/logout`, and `/session/me` are public —
  they CANNOT be gated behind themselves.
- Cookie attributes are HttpOnly + SameSite=Lax + Secure-on-HTTPS. Lax (not
  Strict) is required so the Kite OAuth callback (a top-level cross-site
  GET from kite.zerodha.com → us) carries the session. CSRF protection on
  state-changing routes is preserved because they're all POSTs.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any, Dict

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

SESSION_KEY = "dashboard_authed"


def password_matches(submitted: str, expected: str) -> bool:
    """Constant-time compare. Returns False on empty inputs to avoid
    accidentally accepting a request when configuration is half-applied."""
    if not submitted or not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def mark_authenticated(request: Request) -> None:
    """Stamp the session cookie. Caller still needs to return a Response;
    SessionMiddleware writes Set-Cookie on the way out."""
    request.session[SESSION_KEY] = True


def clear_session(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def require_session(request: Request) -> None:
    """FastAPI dependency. Raises 401 with no body when the session cookie
    is missing, expired (Starlette enforces max_age), or tampered with
    (Starlette signs and refuses bad sigs). The 401 is intentionally bare —
    no leak about which check failed."""
    if not is_authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )


def session_status(request: Request) -> Dict[str, Any]:
    """Used by the SPA to decide login-page vs. app on first paint."""
    return {"authenticated": is_authenticated(request)}
