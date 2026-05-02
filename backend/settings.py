"""Backend configuration via env vars (with sensible defaults)."""
from __future__ import annotations

import os
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
    # the user is browsing from.
    kite_redirect_url: str = "http://127.0.0.1:8000/auth/callback"

    # Where the OAuth access token is cached. Reuses the path the existing
    # headless TOTP daemon uses so a single token file serves both paths.
    token_cache_path: Path = REPO_ROOT / ".kite_session.json"

    # Project config (consumed by strategies for risk rails, capital, etc.)
    config_path: Path = REPO_ROOT / "config.ini"

    # Tick cadence for the per-run async loop (seconds).
    tick_interval_seconds: int = 60

    # Where the dashboard SPA lives in dev — the backend allows CORS from here.
    dev_origin: str = "http://localhost:5173"

    # Per Q1: dashboard is signals + paper only. Live mode is rejected at the
    # run-create endpoint regardless of strategy. Set true only with eyes open.
    allow_live_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
