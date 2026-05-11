"""
Dashboard backend entrypoint.

Run locally:
    uvicorn backend.main:app --reload --port 8000

Then open the SPA at http://localhost:5173 (Phase 4) — it will hit this
backend at http://localhost:8000.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .dashboard_auth import require_session
from .routers import (
    auth,
    dashboard_session,
    equity_swing,
    market_profile,
    pair_candidates,
    runs,
    strategies,
)
from .run_manager import get_run_manager
from .settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)
logger = logging.getLogger("backend")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.init_schema()
    get_run_manager().hydrate_from_db()
    yield
    await get_run_manager().shutdown()
    logger.info("All runs cancelled.")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Taleb-Karpathy + Pair Trading Dashboard",
        description=(
            "REST API for the strategy dashboard. Signals + paper modes only "
            "(live execution is intentionally disabled in this build)."
        ),
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.dashboard_url],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # SameSite=Lax (not Strict) so the Kite OAuth callback — a top-level
    # cross-site GET from kite.zerodha.com → us — still carries the cookie.
    # CSRF on state-changing routes is preserved because they're all POSTs,
    # which Lax blocks cross-site.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.dashboard_session_secret,
        session_cookie="dashboard_session",
        max_age=settings.dashboard_session_max_age_days * 86400,
        same_site="lax",
        https_only=settings.dashboard_url.startswith("https://"),
    )

    # The /session/* router stays public — it's how callers acquire a session
    # in the first place. Every other router is gated.
    app.include_router(dashboard_session.router)

    gated = [Depends(require_session)]
    app.include_router(auth.router, dependencies=gated)
    app.include_router(strategies.router, dependencies=gated)
    app.include_router(runs.router, dependencies=gated)
    app.include_router(market_profile.router, dependencies=gated)
    app.include_router(pair_candidates.router, dependencies=gated)
    app.include_router(equity_swing.router, dependencies=gated)

    @app.get("/", tags=["meta"])
    def root():
        return {
            "name": app.title,
            "version": app.version,
            "live_mode_enabled": settings.allow_live_mode,
        }

    return app


app = create_app()
