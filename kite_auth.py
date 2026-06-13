"""
Kite Auth Manager — Automated Zerodha Kite Connect Authentication
=================================================================
Handles the full login flow:
  1. POST credentials to /api/login
  2. Generate TOTP and POST to /api/twofa
  3. Extract request_token from redirect
  4. Exchange for access_token via kiteconnect SDK
  5. Cache token and auto-refresh on expiry

Dependencies: kiteconnect, pyotp, requests
"""

import os
import json
import logging
import configparser
from datetime import datetime, timedelta
from pathlib import Path

import pyotp
import requests
from kiteconnect import KiteConnect

logger = logging.getLogger(__name__)


class KiteAuthManager:
    """
    Smart session manager for Zerodha Kite Connect API.

    Usage:
        auth = KiteAuthManager("config.ini")
        kite = auth.get_kite()  # Returns authenticated KiteConnect instance
    """

    TOKEN_CACHE_FILE = ".kite_session.json"
    KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
    KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"

    def __init__(self, config_path: str = "config.ini"):
        self.config = configparser.ConfigParser()
        if not Path(config_path).exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}\n"
                "Copy scripts/config_template.ini to config.ini and fill in your credentials."
            )
        self.config.read(config_path)

        # Credentials: prefer environment variables, fall back to config file
        self.api_key = self._resolve_credential("KITE_API_KEY", "api_key")
        self.api_secret = self._resolve_credential("KITE_API_SECRET", "api_secret")
        self.user_id = self._resolve_credential("KITE_USER_ID", "user_id")
        self.password = self._resolve_credential("KITE_PASSWORD", "password")
        self.totp_key = self._resolve_credential("KITE_TOTP_KEY", "totp_key")
        self.redirect_url = os.environ.get("KITE_REDIRECT_URL") or self.config["kite"].get("redirect_url", "http://127.0.0.1/")

        self._validate_no_placeholders()
        self.kite = KiteConnect(api_key=self.api_key)
        self._access_token = None
        self._token_timestamp = None

    def _resolve_credential(self, env_var: str, config_key: str) -> str:
        """Resolve a credential from env var or config, rejecting placeholders."""
        value = os.environ.get(env_var) or self.config["kite"][config_key]
        return value

    def _validate_no_placeholders(self):
        """Fail fast if any credential is empty or still a placeholder.

        Catches three failure modes: empty strings, the original ${...}
        env-var format, and the YOUR_* template values shipped in
        config_template.ini that operators sometimes forget to replace
        (and which would otherwise be sent to kite.zerodha.com as a
        password attempt — landing the placeholder in upstream logs).
        """
        import re
        placeholder_re = re.compile(r"^\$\{.+\}$")
        creds = {
            "api_key": self.api_key, "api_secret": self.api_secret,
            "user_id": self.user_id, "password": self.password,
            "totp_key": self.totp_key,
        }
        bad = []
        for name, val in creds.items():
            stripped = val.strip()
            if (
                not stripped
                or placeholder_re.match(stripped)
                or stripped.startswith("YOUR_")
            ):
                bad.append(name)
        if bad:
            raise AuthenticationError(
                f"Credentials not configured: {', '.join(bad)}. "
                "Set the corresponding KITE_* environment variables or update config.ini."
            )

    def get_kite(self) -> KiteConnect:
        """Return an authenticated KiteConnect instance, refreshing token if needed."""
        if self._is_token_valid():
            logger.info("Using cached access token.")
            self.kite.set_access_token(self._access_token)
            return self.kite

        # Try loading from cache file
        if self._load_cached_token():
            logger.info("Loaded access token from cache file.")
            self.kite.set_access_token(self._access_token)
            # Verify token is still valid with a lightweight API call
            try:
                self.kite.profile()
                return self.kite
            except Exception:
                logger.warning("Cached token is invalid. Re-authenticating...")

        # Full login flow
        self._perform_login()
        return self.kite

    def _is_token_valid(self) -> bool:
        """Check if current token is still valid (tokens expire ~6 AM IST daily)."""
        if not self._access_token or not self._token_timestamp:
            return False
        now = datetime.now()
        # Kite tokens expire at ~6:00 AM IST next day
        token_date = self._token_timestamp.date()
        expiry = datetime.combine(
            token_date + timedelta(days=1),
            datetime.strptime("06:00", "%H:%M").time()
        )
        return now < expiry

    def _load_cached_token(self) -> bool:
        """Load token from local cache file."""
        try:
            if not Path(self.TOKEN_CACHE_FILE).exists():
                return False
            with open(self.TOKEN_CACHE_FILE, "r") as f:
                data = json.load(f)
            self._access_token = data["access_token"]
            self._token_timestamp = datetime.fromisoformat(data["timestamp"])
            return self._is_token_valid()
        except (json.JSONDecodeError, KeyError):
            return False

    def _save_token_cache(self):
        """Persist token to local file."""
        data = {
            "access_token": self._access_token,
            "timestamp": self._token_timestamp.isoformat(),
            "user_id": self.user_id,
        }
        # Open with 0600 explicitly — default umask leaves the cache 0644,
        # which exposes a live access_token to any local UID.
        fd = os.open(
            self.TOKEN_CACHE_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        logger.info("Access token cached to %s", self.TOKEN_CACHE_FILE)

    def _perform_login(self):
        """
        Execute the full Kite Connect login flow:
        1. POST user_id + password → get request_id
        2. Generate TOTP → POST 2FA → get redirect with request_token
        3. Exchange request_token for access_token
        """
        logger.info("Starting Zerodha Kite login for user %s...", self.user_id)
        session = requests.Session()

        # ── Step 1: Initial login ──
        login_resp = session.post(
            self.KITE_LOGIN_URL,
            data={"user_id": self.user_id, "password": self.password}
        )
        login_data = login_resp.json()
        if login_data.get("status") != "success":
            raise AuthenticationError(
                f"Login failed: {login_data.get('message', 'Unknown error')}"
            )
        request_id = login_data["data"]["request_id"]
        logger.info("Step 1/3 complete: credentials accepted.")

        # ── Step 2: Two-Factor Authentication (TOTP) ──
        totp_code = pyotp.TOTP(self.totp_key).now()
        twofa_resp = session.post(
            self.KITE_TWOFA_URL,
            data={
                "user_id": self.user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp",
                "skip_session": False,
            }
        )
        twofa_data = twofa_resp.json()
        if twofa_data.get("status") != "success":
            raise AuthenticationError(
                f"2FA failed: {twofa_data.get('message', 'Unknown error')}"
            )
        logger.info("Step 2/3 complete: TOTP verified.")

        # ── Step 3: Get request_token via redirect ──
        # Navigate to login URL — this triggers the redirect with request_token
        kite_login_url = (
            f"https://kite.trade/connect/login?v=3&api_key={self.api_key}"
        )

        # Walk the redirect chain manually. If the callback URL is itself an
        # active endpoint, allow_redirects=True can invalidate request_token
        # before we redeem it. Stop at the first Location header carrying the
        # token, before issuing the request to that URL.
        from urllib.parse import urlparse, parse_qs, urljoin

        # Hosts the OAuth flow legitimately redirects through. Anything
        # off-list means our session cookies (and any token in the URL
        # query) would be sent to an attacker-controlled host — refuse.
        allowed_redirect_hosts = {"kite.zerodha.com", "kite.trade"}
        callback_host = urlparse(self.redirect_url).hostname
        if callback_host:
            allowed_redirect_hosts.add(callback_host)

        resp = session.get(kite_login_url, allow_redirects=False)
        chain_urls = [kite_login_url]
        request_token = None
        for _ in range(10):  # safety cap on redirect depth
            if not (resp.is_redirect or resp.is_permanent_redirect):
                break
            location_header = resp.headers.get("Location")
            if not location_header:
                break
            location = urljoin(resp.url, location_header)
            parsed = urlparse(location)
            if parsed.scheme not in {"http", "https"} or (
                parsed.hostname and parsed.hostname not in allowed_redirect_hosts
            ):
                raise AuthenticationError(
                    "Refusing to follow redirect to unexpected host "
                    f"{parsed.hostname!r} during Kite OAuth flow."
                )
            chain_urls.append(location)
            token = parse_qs(parsed.query).get("request_token", [None])[0]
            if token:
                request_token = token
                break
            resp = session.get(location, allow_redirects=False)

        if not request_token:
            # Strip query strings — the chain may contain auth-bearing tokens
            # we don't want landing in the system journal.
            scrubbed = [u.split("?", 1)[0] for u in chain_urls]
            raise AuthenticationError(
                "Could not extract request_token from redirect chain. "
                f"Visited URLs (query stripped): {scrubbed}"
            )

        # ── Step 4: Exchange for access_token ──
        session_data = self.kite.generate_session(
            request_token, api_secret=self.api_secret
        )
        self._access_token = session_data["access_token"]
        self._token_timestamp = datetime.now()
        self.kite.set_access_token(self._access_token)

        logger.info("Step 3/3 complete: access_token obtained.")
        self._save_token_cache()

        # Verify
        profile = self.kite.profile()
        logger.info(
            "Authenticated as: %s (%s)", profile["user_name"], profile["user_id"]
        )

    def get_access_token(self) -> str:
        """Return the raw access token string."""
        if not self._access_token:
            self.get_kite()
        return self._access_token


class AuthenticationError(Exception):
    """Raised when Kite authentication fails."""
    pass


# ── CLI entry point for testing ──
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.ini"
    auth = KiteAuthManager(config_path)
    kite = auth.get_kite()
    profile = kite.profile()
    print(f"\n✅ Successfully authenticated as {profile['user_name']} ({profile['user_id']})")
    print(f"   Exchanges: {', '.join(profile['exchanges'])}")
    print(f"   Products: {', '.join(profile['products'])}")
