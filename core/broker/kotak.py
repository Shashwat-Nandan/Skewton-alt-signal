"""Kotak Securities Neo adapter — headless TOTP + MPIN, Kite-shaped orders.

Talks to the Neo Trade API over REST (no official `kotakneoapi` dependency:
that SDK pulls httpx/pydantic/numpy pins we do not want to drag through
the lockfile for a first increment). Endpoints and form-field names match
the official SDK (`login/1.0/tradeApiLogin`, `quick/order/rule/ms/place`).

The client duck-types the KiteConnect methods the live order executor
and runners actually call: place_order, cancel_order, order_history,
quote, profile, positions, instruments. Vendor codes (nse_fo, B/S, L/MKT)
are translated in `core.broker.mapping`. F&O contract dumps come from the
Neo scrip-master CSV, mapped to Kite-shaped rows.
"""
from __future__ import annotations

import json
import logging
import os
from configparser import ConfigParser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote as urlquote

import pyotp
import requests

from .base import BrokerAdapter
from .credentials import reject_placeholders, resolve_credential
from .errors import (
    BrokerAuthError,
    BrokerConfigError,
    BrokerNetworkError,
    BrokerOrderError,
    BrokerTokenError,
)
from .kotak_instruments import match_scrip_url, parse_scrip_csv
from .mapping import (
    kite_to_kotak_tradingsymbol,
    kotak_order_type,
    kotak_segment,
    kotak_side,
    kotak_status,
)

logger = logging.getLogger(__name__)

LOGIN_BASE = "https://gw-napi.kotaksecurities.com"
DEFAULT_NEO_FIN_KEY = "neotradeapi"
TOKEN_CACHE_FILE = ".kotak_session.json"

# Official SDK PROD_URL map (kotak-neo-python settings.py).
_PATHS = {
    "totp_login": "login/1.0/tradeApiLogin",
    "totp_validate": "login/1.0/tradeApiValidate",
    "logout": "apim/login/2.0/logout",
    "place_order": "quick/order/rule/ms/place",
    "cancel_order": "quick/order/cancel",
    "order_history": "quick/order/history",
    "order_book": "quick/user/orders",
    "positions": "quick/user/positions",
    "holdings": "portfolio/v1/holdings",
    "limits": "quick/user/limits",
    "quotes": "script-details/1.0/quotes/neosymbol/{neo_symbols}/{quote_type}",
    "scrip_master": "script-details/1.0/masterscrip/file-paths",
}

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCRIP_CACHE = _REPO_ROOT / "data_cache" / "kotak_scrip"


def _write_json_0600(path: Path, payload: dict) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)


class KotakNeoClient:
    """KiteConnect-shaped client backed by Kotak Neo REST."""

    VARIETY_REGULAR = "regular"
    PRODUCT_NRML = "NRML"
    PRODUCT_CNC = "CNC"
    PRODUCT_MIS = "MIS"
    ORDER_TYPE_LIMIT = "LIMIT"
    ORDER_TYPE_MARKET = "MARKET"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(
        self,
        consumer_key: str,
        *,
        neo_fin_key: str = DEFAULT_NEO_FIN_KEY,
        session: Optional[requests.Session] = None,
        login_base: str = LOGIN_BASE,
        scrip_cache_dir: Optional[Path] = None,
    ):
        self.consumer_key = consumer_key
        self.neo_fin_key = neo_fin_key or DEFAULT_NEO_FIN_KEY
        self.session = session or requests.Session()
        self.login_base = login_base.rstrip("/")
        self.scrip_cache_dir = Path(scrip_cache_dir) if scrip_cache_dir else DEFAULT_SCRIP_CACHE
        self.view_token: Optional[str] = None
        self.trade_token: Optional[str] = None
        self.sid: Optional[str] = None
        self.server_id: Optional[str] = None
        self.base_url: Optional[str] = None
        self.ucc: Optional[str] = None
        self.greeting_name: Optional[str] = None
        self._instruments_by_exchange: Dict[str, List[dict]] = {}

    # ── auth ────────────────────────────────────────────────────

    def totp_login(self, mobile_number: str, ucc: str, totp: str) -> dict:
        url = f"{self.login_base}/{_PATHS['totp_login']}"
        headers = {
            "Authorization": self.consumer_key,
            "neo-fin-key": self.neo_fin_key,
            "Content-Type": "application/json",
        }
        body = {"mobileNumber": mobile_number, "ucc": ucc, "totp": totp}
        data = self._request_json("POST", url, headers=headers, json_body=body)
        inner = _unwrap_data(data)
        self.view_token = inner.get("token")
        self.sid = inner.get("sid")
        self.ucc = inner.get("ucc") or ucc
        self.greeting_name = inner.get("greetingName")
        if not self.view_token or not self.sid:
            raise BrokerAuthError(
                f"Kotak totp_login did not return token/sid: {list(inner)}"
            )
        return data

    def totp_validate(self, mpin: str) -> dict:
        if not self.view_token or not self.sid:
            raise BrokerAuthError("totp_validate requires a successful totp_login")
        url = f"{self.login_base}/{_PATHS['totp_validate']}"
        headers = {
            "Authorization": self.consumer_key,
            "neo-fin-key": self.neo_fin_key,
            "sid": self.sid,
            "Auth": self.view_token,
            "Content-Type": "application/json",
        }
        data = self._request_json(
            "POST", url, headers=headers, json_body={"mpin": mpin}
        )
        inner = _unwrap_data(data)
        self.trade_token = inner.get("token")
        self.sid = inner.get("sid") or self.sid
        self.server_id = inner.get("hsServerId") or inner.get("dataCenter")
        base = inner.get("baseUrl") or inner.get("baseURL")
        if base:
            self.base_url = str(base).rstrip("/")
        self.ucc = inner.get("ucc") or self.ucc
        self.greeting_name = inner.get("greetingName") or self.greeting_name
        if inner.get("kType") and inner.get("kType") != "Trade":
            raise BrokerAuthError(
                f"Kotak totp_validate did not upgrade to a Trade token "
                f"(kType={inner.get('kType')!r}). Refusing to place orders."
            )
        if not self.trade_token:
            raise BrokerAuthError("Kotak totp_validate did not return a trade token")
        return data

    def restore_session(self, cached: dict) -> None:
        self.trade_token = cached.get("trade_token") or cached.get("access_token")
        self.sid = cached.get("sid")
        self.server_id = cached.get("server_id")
        self.base_url = (cached.get("base_url") or "").rstrip("/") or None
        self.ucc = cached.get("ucc")
        self.greeting_name = cached.get("greeting_name") or cached.get("user_name")

    def session_payload(self) -> dict:
        return {
            "trade_token": self.trade_token,
            "sid": self.sid,
            "server_id": self.server_id,
            "base_url": self.base_url,
            "ucc": self.ucc,
            "greeting_name": self.greeting_name,
            "timestamp": datetime.now().isoformat(),
        }

    # ── kite-shaped surface ─────────────────────────────────────

    def profile(self) -> dict:
        # limits() is the cheap authenticated call; we don't need a
        # dedicated profile endpoint to prove the session is live.
        self.limits()
        return {
            "user_id": self.ucc,
            "user_name": self.greeting_name,
            "email": None,
            "broker": "kotak",
            "exchanges": ["NSE", "NFO", "BSE", "BFO", "MCX"],
            "products": ["CNC", "NRML", "MIS", "MTF"],
        }

    def limits(self) -> dict:
        return self._trade_json("GET", _PATHS["limits"])

    def place_order(
        self,
        variety="regular",
        exchange="",
        tradingsymbol="",
        transaction_type="",
        quantity=0,
        product="NRML",
        order_type="LIMIT",
        price=0,
        validity="DAY",
        tag="",
        trigger_price=0,
        disclosed_quantity=0,
        **_ignored,
    ) -> str:
        # variety is a Kite concept (regular/amo/co/iceberg). Neo regular
        # orders are the `quick/order/rule/ms/place` path; amo="NO".
        del variety
        body = {
            "am": "NO",
            "dq": str(int(disclosed_quantity or 0)),
            "es": kotak_segment(exchange),
            "mp": "0",
            "pc": product,
            "pr": _fmt_price(price),
            "pt": kotak_order_type(order_type),
            "qt": str(int(quantity)),
            "rt": validity or "DAY",
            "tp": _fmt_price(trigger_price),
            "ts": kite_to_kotak_tradingsymbol(exchange, tradingsymbol),
            "tt": kotak_side(transaction_type),
            "ig": str(tag or "")[:20],
            "os": "NEOTRADEAPI",
        }
        data = self._trade_json(
            "POST", _PATHS["place_order"], form=body, content_type="form"
        )
        order_id = _extract_order_id(data)
        if not order_id:
            raise BrokerOrderError(
                f"Kotak place_order returned no order id: {data}"
            )
        return str(order_id)

    def cancel_order(self, variety="regular", order_id="", **_ignored) -> dict:
        del variety
        return self._trade_json(
            "POST",
            _PATHS["cancel_order"],
            form={"on": str(order_id), "am": "NO"},
            content_type="form",
        )

    def order_history(self, order_id) -> List[dict]:
        data = self._trade_json(
            "POST",
            _PATHS["order_history"],
            form={"on": str(order_id)},
            content_type="form",
        )
        rows = _extract_list(data)
        # Kite's order_history is oldest-first; the executor reads history[-1]
        # as the latest state. Kotak's sample is latest-first. Reverse so
        # the executor's "last row wins" contract holds.
        kite_rows = [_kotak_history_row(r) for r in rows]
        kite_rows.reverse()
        return kite_rows

    def quote(self, keys) -> dict:
        out: Dict[str, dict] = {}
        for key in keys:
            exchange, symbol = _split_quote_key(key)
            kotak_sym = kite_to_kotak_tradingsymbol(exchange, symbol)
            seg = kotak_segment(exchange)
            neo_symbol = urlquote(f"{seg}|{kotak_sym}", safe="")
            path = _PATHS["quotes"].format(neo_symbols=neo_symbol, quote_type="ltp")
            data = self._trade_json("GET", path)
            ltp = _extract_ltp(data)
            if ltp is None:
                raise BrokerOrderError(f"Kotak quote for {key} had no LTP: {data}")
            out[key] = {"last_price": ltp}
        return out

    def ltp(self, keys) -> dict:
        quoted = self.quote(keys)
        return {k: {"last_price": v["last_price"]} for k, v in quoted.items()}

    def positions(self) -> dict:
        data = self._trade_json("GET", _PATHS["positions"])
        rows = _extract_list(data)
        net = [_kotak_position_row(r) for r in rows]
        return {"net": net, "day": []}

    def holdings(self) -> list:
        data = self._trade_json("GET", _PATHS["holdings"])
        return _extract_list(data)

    def instruments(self, exchange=None):
        """Kite-shaped instrument dump for `exchange` (NSE/NFO/BSE/BFO/MCX).

        Backed by the Neo scrip-master CSV (cached under data_cache/kotak_scrip
        for the calendar day). Only consumer_key is required — the file-paths
        API does not need a Trade token.
        """
        exch = (exchange or "NFO").strip().upper()
        cached = self._instruments_by_exchange.get(exch)
        if cached is not None:
            return cached
        segment = kotak_segment(exch)
        text = self._scrip_csv_text(segment)
        rows = parse_scrip_csv(text, exch)
        if not rows:
            raise BrokerOrderError(
                f"Kotak scrip-master for {exch} ({segment}) parsed to 0 rows. "
                "Refusing to hand strategies an empty dump — they would "
                "size/resolve against nothing."
            )
        if exch in ("NFO", "BFO") and not any(
            r.get("instrument_type") in ("FUT", "CE", "PE") for r in rows
        ):
            raise BrokerOrderError(
                f"Kotak scrip-master for {exch} had no FUT/CE/PE rows. "
                "The CSV is the wrong segment or the mapper dropped everything."
            )
        self._instruments_by_exchange[exch] = rows
        logger.info(
            "Kotak instruments(%s): %d rows from scrip-master %s",
            exch, len(rows), segment,
        )
        return rows

    def _scrip_csv_text(self, segment: str) -> str:
        today = datetime.now().date().isoformat()
        cache_path = self.scrip_cache_dir / f"{today}_{segment}.csv"
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="replace")
        url = self._scrip_file_url(segment)
        try:
            resp = self.session.get(
                url, headers=self._scrip_headers(), timeout=120,
            )
        except requests.RequestException as e:
            raise BrokerNetworkError(
                f"Kotak scrip-master CSV download failed ({segment}): {e}"
            ) from e
        if resp.status_code >= 400:
            raise BrokerNetworkError(
                f"Kotak scrip-master CSV HTTP {resp.status_code} for {segment}"
            )
        text = resp.content.decode("utf-8", errors="replace")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
        return text

    def _scrip_file_url(self, segment: str) -> str:
        url = f"{self.login_base}/{_PATHS['scrip_master']}"
        payload = self._request_json("GET", url, headers=self._scrip_headers())
        inner = _unwrap_data(payload)
        paths = inner.get("filesPaths") or inner.get("filePaths") or []
        if isinstance(payload.get("filesPaths"), list) and not paths:
            paths = payload["filesPaths"]
        if not isinstance(paths, list) or not paths:
            raise BrokerOrderError(
                f"Kotak scrip-master file-paths response had no filesPaths: "
                f"{list(inner)[:8]}"
            )
        return match_scrip_url(paths, segment)

    def _scrip_headers(self) -> dict:
        return {
            "Authorization": self.consumer_key,
            "neo-fin-key": self.neo_fin_key,
            "Accept": "application/json",
        }

    def logout_remote(self) -> None:
        try:
            self._trade_json("POST", _PATHS["logout"], json_body={})
        except Exception as e:
            logger.warning("Kotak remote logout failed: %s", e)

    # ── HTTP ────────────────────────────────────────────────────

    def _trade_headers(self, content_type: Optional[str] = None) -> dict:
        if not self.trade_token or not self.sid:
            raise BrokerTokenError("Kotak session has no trade token; login first")
        headers = {
            "Authorization": self.consumer_key,
            "Auth": self.trade_token,
            "sid": self.sid,
            "Sid": self.sid,
            "neo-fin-key": self.neo_fin_key,
        }
        if content_type == "form":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif content_type == "json":
            headers["Content-Type"] = "application/json"
        return headers

    def _trade_url(self, path: str) -> str:
        base = (self.base_url or self.login_base).rstrip("/")
        path = path.lstrip("/")
        url = f"{base}/{path}"
        if self.server_id:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}sId={urlquote(str(self.server_id))}"
        return url

    def _trade_json(
        self,
        method: str,
        path: str,
        *,
        form: Optional[dict] = None,
        json_body: Optional[dict] = None,
        content_type: Optional[str] = None,
    ) -> dict:
        if form is not None:
            content_type = "form"
        elif json_body is not None:
            content_type = content_type or "json"
        return self._request_json(
            method,
            self._trade_url(path),
            headers=self._trade_headers(content_type),
            form=form,
            json_body=json_body,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        form: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> dict:
        try:
            resp = self.session.request(
                method,
                url,
                headers=headers,
                data=form,
                json=json_body,
                timeout=30,
            )
        except requests.RequestException as e:
            raise BrokerNetworkError(f"Kotak {method} {url} failed: {e}") from e
        if resp.status_code == 403:
            raise BrokerTokenError(
                f"Kotak session rejected (403) on {method} {url}"
            )
        if resp.status_code == 429:
            raise BrokerNetworkError(
                f"Kotak rate-limited (429) on {method} {url}"
            )
        if resp.status_code >= 500:
            raise BrokerNetworkError(
                f"Kotak {resp.status_code} on {method} {url}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise BrokerNetworkError(
                f"Kotak {method} {url} returned non-JSON ({resp.status_code})"
            ) from e
        if resp.status_code >= 400:
            raise BrokerOrderError(
                _error_message(payload) or f"Kotak HTTP {resp.status_code}"
            )
        if _is_not_ok(payload):
            raise BrokerOrderError(_error_message(payload) or str(payload))
        return payload


class KotakNeoAdapter(BrokerAdapter):
    name = "kotak"
    display_name = "Kotak Securities Neo"
    login_style = "headless"

    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        config = ConfigParser()
        path = Path(config_path)
        if not path.exists():
            raise BrokerConfigError(
                f"Config file not found: {config_path}\n"
                "Copy config_template.ini to config.ini and fill [kotak]."
            )
        config.read(path)
        self.consumer_key = resolve_credential(
            "KOTAK_CONSUMER_KEY", config, "kotak", "consumer_key"
        )
        self.mobile_number = resolve_credential(
            "KOTAK_MOBILE_NUMBER", config, "kotak", "mobile_number"
        )
        self.ucc = resolve_credential("KOTAK_UCC", config, "kotak", "ucc")
        self.mpin = resolve_credential("KOTAK_MPIN", config, "kotak", "mpin")
        self.totp_key = resolve_credential(
            "KOTAK_TOTP_KEY", config, "kotak", "totp_key"
        )
        self.neo_fin_key = (
            resolve_credential(
                "KOTAK_NEO_FIN_KEY", config, "kotak", "neo_fin_key",
                default=DEFAULT_NEO_FIN_KEY,
            )
            or DEFAULT_NEO_FIN_KEY
        )
        self.environment = (
            resolve_credential(
                "KOTAK_ENVIRONMENT", config, "kotak", "environment",
                default="prod",
            )
            or "prod"
        ).strip().lower()
        reject_placeholders({
            "consumer_key": self.consumer_key,
            "mobile_number": self.mobile_number,
            "ucc": self.ucc,
            "mpin": self.mpin,
            "totp_key": self.totp_key,
        })
        self._client: Optional[KotakNeoClient] = None
        self._cache_path = Path(TOKEN_CACHE_FILE)

    def login(self) -> KotakNeoClient:
        client = KotakNeoClient(
            self.consumer_key, neo_fin_key=self.neo_fin_key,
        )
        if self._load_cached(client):
            try:
                client.profile()
                logger.info("Using cached Kotak Neo session for %s", client.ucc)
                self._client = client
                return client
            except (BrokerTokenError, BrokerAuthError, BrokerNetworkError) as e:
                logger.warning("Cached Kotak session invalid (%s); re-login", e)
        self._perform_login(client)
        self._client = client
        return client

    def refresh(self) -> KotakNeoClient:
        # Drop the cache so login() cannot succeed on a token the broker
        # just rejected — otherwise H8 refresh-once would loop on the
        # same 403.
        if self._cache_path.exists():
            self._cache_path.unlink()
        return self.login()

    def logout(self) -> None:
        if self._client is not None:
            self._client.logout_remote()
        if self._cache_path.exists():
            self._cache_path.unlink()
        self._client = None

    def cached_client(self) -> Optional[KotakNeoClient]:
        cached = self._read_cache()
        if not cached or not cached.get("trade_token") or not cached.get("sid"):
            return None
        client = KotakNeoClient(
            self.consumer_key, neo_fin_key=self.neo_fin_key,
        )
        client.restore_session(cached)
        self._client = client
        return client

    def status_profile(self) -> Optional[dict]:
        client = self.cached_client()
        if client is None:
            return None
        try:
            return client.profile()
        except Exception as e:
            logger.info("Cached Kotak token rejected: %s", e)
            return None

    def _perform_login(self, client: KotakNeoClient) -> None:
        logger.info("Starting Kotak Neo TOTP login for UCC %s...", self.ucc)
        totp = pyotp.TOTP(self.totp_key).now()
        client.totp_login(
            mobile_number=self.mobile_number, ucc=self.ucc, totp=totp,
        )
        logger.info("Kotak TOTP accepted; validating MPIN")
        client.totp_validate(self.mpin)
        self._save_cache(client)
        logger.info(
            "Authenticated with Kotak Neo as %s (%s)",
            client.greeting_name, client.ucc,
        )

    def _load_cached(self, client: KotakNeoClient) -> bool:
        cached = self._read_cache()
        if not cached:
            return False
        ts = cached.get("timestamp")
        if ts:
            try:
                stamped = datetime.fromisoformat(ts)
            except ValueError:
                return False
            if not _token_still_valid(stamped):
                return False
        if not cached.get("trade_token") or not cached.get("sid"):
            return False
        client.restore_session(cached)
        return True

    def _read_cache(self) -> Optional[dict]:
        if not self._cache_path.exists():
            return None
        try:
            return json.loads(self._cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _save_cache(self, client: KotakNeoClient) -> None:
        _write_json_0600(self._cache_path, client.session_payload())
        logger.info("Kotak session cached to %s", self._cache_path)


def _token_still_valid(stamped: datetime) -> bool:
    # Same 06:00 IST next-day rule as Kite. Neo JWTs are session-scoped;
    # the limits() call in login() is the real validity check. This just
    # skips a doomed profile hit on a yesterday file.
    expiry = datetime.combine(
        stamped.date() + timedelta(days=1),
        datetime.strptime("06:00", "%H:%M").time(),
    )
    return datetime.now() < expiry


def _unwrap_data(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        return data
    if isinstance(payload, dict):
        return payload
    return {}


def _extract_list(payload: dict) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nested = data.get("data")
        if isinstance(nested, list):
            return nested
        if nested is None:
            return [data]
    if isinstance(payload, list):
        return payload
    return []


def _extract_order_id(payload: dict) -> Optional[str]:
    inner = _unwrap_data(payload)
    for key in ("nOrdNo", "nOrdNum", "orderId", "order_id"):
        if inner.get(key):
            return str(inner[key])
    data = payload.get("data")
    if isinstance(data, list) and data:
        row = data[0]
        if isinstance(row, dict) and row.get("nOrdNo"):
            return str(row["nOrdNo"])
    return None


def _extract_ltp(payload: dict) -> Optional[float]:
    inner = _unwrap_data(payload)
    for key in ("ltp", "last_traded_price", "lastPrice", "iv"):
        if inner.get(key) not in (None, ""):
            try:
                return float(inner[key])
            except (TypeError, ValueError):
                continue
    # Some quote payloads nest per-symbol.
    if isinstance(inner, dict):
        for v in inner.values():
            if isinstance(v, dict) and v.get("ltp") not in (None, ""):
                try:
                    return float(v["ltp"])
                except (TypeError, ValueError):
                    continue
    return None


def _kotak_history_row(row: dict) -> dict:
    filled = row.get("fldQty") if row.get("fldQty") is not None else row.get("filled_quantity", 0)
    avg = row.get("avgPrc") if row.get("avgPrc") is not None else row.get("average_price", 0)
    reason = row.get("rejRsn") or row.get("ordUsrMsg") or row.get("status_message") or ""
    if reason in ("--", "NA"):
        reason = ""
    return {
        "status": kotak_status(row.get("ordSt") or row.get("status") or ""),
        "filled_quantity": int(float(filled or 0)),
        "average_price": float(avg or 0),
        "status_message": reason,
        "order_id": str(row.get("nOrdNo") or row.get("order_id") or ""),
    }


def _kotak_position_row(row: dict) -> dict:
    qty = row.get("flBuyQty") or row.get("quantity") or row.get("netQty") or 0
    try:
        quantity = int(float(qty))
    except (TypeError, ValueError):
        quantity = 0
    return {
        "tradingsymbol": row.get("trdSym") or row.get("tradingsymbol") or "",
        "exchange": row.get("exch") or "",
        "quantity": quantity,
        "average_price": float(row.get("avgPrc") or row.get("average_price") or 0),
        "last_price": float(row.get("ltp") or row.get("last_price") or 0),
        "pnl": float(row.get("unrealisedPnl") or row.get("pnl") or 0),
        "product": row.get("prod") or row.get("product") or "",
    }


def _split_quote_key(key: str) -> tuple[str, str]:
    if ":" not in key:
        raise BrokerOrderError(
            f"Quote key {key!r} is not EXCHANGE:SYMBOL (Kite shape)."
        )
    exchange, symbol = key.split(":", 1)
    return exchange, symbol


def _fmt_price(price) -> str:
    try:
        return f"{float(price):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _is_not_ok(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    stat = str(payload.get("stat") or payload.get("status") or "").lower()
    if stat in ("not_ok", "not ok", "failed", "error"):
        return True
    if payload.get("error"):
        return True
    return False


def _error_message(payload: dict) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    err = payload.get("error")
    if isinstance(err, list) and err:
        first = err[0]
        if isinstance(first, dict):
            return str(first.get("message") or first)
        return str(first)
    if isinstance(err, dict):
        return str(err.get("message") or err)
    if isinstance(err, str):
        return err
    return str(payload.get("errMsg") or payload.get("message") or "")
