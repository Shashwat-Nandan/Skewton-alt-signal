"""Backend configuration via env vars (with sensible defaults)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Kite OAuth credentials — register an app at https://developers.kite.trade/
    kite_api_key: str = ""
    kite_api_secret: str = ""
    # Must match the redirect URL set on the Kite developer console exactly.
    # Default works for `uvicorn backend.main:app --port 8000` on the same host
    # the user is browsing from. NOTE: when bumping the /api/* prefix here,
    # update the redirect URL on https://developers.kite.trade/ in the same
    # change — Kite will reject mismatched callbacks at OAuth time.
    kite_redirect_url: str = "http://127.0.0.1:8000/api/auth/callback"

    # Where the OAuth access token is cached. Reuses the path the existing
    # headless TOTP daemon uses so a single token file serves both paths.
    token_cache_path: Path = REPO_ROOT / ".kite_session.json"

    # Project config (consumed by strategies for risk rails, capital, etc.)
    config_path: Path = REPO_ROOT / "config.ini"

    # SQLite store for runs/proposals/pnl history. Survives restarts; orphan
    # RUNNING rows are marked STOPPED on backend boot since their in-memory
    # tick loops are gone.
    db_path: Path = REPO_ROOT / "data_cache" / "dashboard.db"

    # Tick cadence for the per-run async loop (seconds).
    tick_interval_seconds: int = 60

    # Public URL the user opens in their browser to use the dashboard.
    # Used both for the OAuth post-callback redirect AND for CORS. Override
    # in prod to your real hostname (e.g. https://dashboard.example.com).
    dashboard_url: str = "http://localhost:5173"

    # Per Q1: dashboard is signals + paper only. Live mode is rejected at the
    # run-create endpoint regardless of strategy. Set true only with eyes open.
    allow_live_mode: bool = False

    # ── Dashboard session auth ────────────────────────────────────
    # The dashboard is single-operator. Any caller who reaches the API surface
    # can drive runs against the operator's cached broker session — so we gate
    # every route behind a password-cookie session (Starlette SessionMiddleware,
    # signed by `dashboard_session_secret`). Both values MUST be set; the
    # backend refuses to boot with either missing.
    dashboard_password: str = ""
    dashboard_session_secret: str = ""
    # Cookie max-age. 7 days is operator-friendly for a personal dashboard.
    dashboard_session_max_age_days: int = 7


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.dashboard_password or not s.dashboard_session_secret:
        # Fail loud at first access — the alternative is shipping without a
        # gate and silently exposing the API. There is no degraded mode.
        raise RuntimeError(
            "DASHBOARD_PASSWORD and DASHBOARD_SESSION_SECRET must be set in "
            ".env. Generate the secret with:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(64))'\n"
            "Then choose a password and add both to .env (mode 0600)."
        )
    return s
