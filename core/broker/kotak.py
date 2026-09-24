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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote as urlquote

import pyotp
import requests
from dotenv import load_dotenv

from .base import BrokerAdapter
from .credentials import reject_placeholders, resolve_credential
from .errors import (
    BrokerAuthError,
    BrokerConfigError,
    BrokerNetworkError,
    BrokerOrderError,
    BrokerTokenError,
)
from .kotak_instruments import ensure_index_rows, match_scrip_url, parse_scrip_csv
from .mapping import (
    strategy_exchange_from_segment,
    strategy_to_kotak_tradingsymbol,
    kotak_order_type,
    kotak_segment,
    kotak_side,
    kotak_status,
    kotak_to_strategy_tradingsymbol,
    neo_index_quote_token,
)

logger = logging.getLogger(__name__)

# Prod session host (Kotak Neo SDK SESSION_PROD_BASE_URL). The old
# gw-napi name is NXDOMAIN; login, quotes, and the scrip master live here.
# Order/limits calls use the baseUrl returned by totp_validate (e.g. e41).
LOGIN_BASE = "https://mis.kotaksecurities.com"
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
    "margin": "quick/user/check-margin",
    "historical_data": "market-data/1.0/historical/details",
}

_KITE_INTERVAL_TO_NEO = {
    "minute": "1min",
    "3minute": "3min",
    "5minute": "5min",
    "10minute": "10min",
    "15minute": "15min",
    "30minute": "30min",
    "60minute": "60min",
    "day": "D",
    "week": "W",
}

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SCRIP_CACHE = _REPO_ROOT / "data_cache" / "kotak_scrip"


def normalize_kotak_mobile(mobile_number: str) -> str:
    """Kotak rejects a bare 10-digit mobile. +91 plus those digits is accepted.

    A number that already starts with + is left alone. 12 digits beginning
    with 91 get the plus. Anything else fails here, before a login POST.
    """
    raw = "".join((mobile_number or "").split())
    if raw.startswith("+"):
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    raise BrokerConfigError(
        "Kotak mobile_number must be +91 followed by 10 digits. "
        "A bare 10-digit number is accepted and prefixed; "
        "Kotak rejects the number without the country code."
    )


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
        body = {
            "mobileNumber": normalize_kotak_mobile(mobile_number),
            "ucc": ucc,
            "totp": totp,
        }
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
        # The trade host 404s a GET of this path, and a POST of the raw
        # fields does not return Net. LimitsAPI.limit_init posts jData
        # and does not append sId (the data center is the baseUrl host).
        return self._trade_json(
            "POST",
            _PATHS["limits"],
            form={
                "jData": json.dumps({"seg": "ALL", "exch": "ALL", "prod": "ALL"}),
            },
            include_server_id=False,
        )

    def margins(self) -> dict:
        """Kite-shaped RMS snapshot from Neo `limits()`.

        Pair H15 gates on `equity.net` (Zerodha free-margin). Neo's `Net`
        is the equivalent; missing it must not look like ₹0 of cash.
        """
        raw = self.limits()
        inner = _unwrap_data(raw)
        if "Net" not in inner and "net" not in inner:
            raise BrokerOrderError(
                f"Kotak limits() had no Net field: {list(inner)[:12]}"
            )
        net = float(inner.get("Net") if inner.get("Net") is not None else inner.get("net") or 0)
        collateral = float(
            inner.get("CollateralValue") or inner.get("Collateral") or 0
        )
        used = float(inner.get("MarginUsed") or inner.get("MarginUsedPrsnt") or 0)
        return {
            "equity": {
                "enabled": True,
                "net": net,
                "available": {
                    "live_balance": net,
                    "collateral": collateral,
                    "cash": net,
                },
                "utilised": {
                    "debits": used,
                    "span": float(inner.get("SpanMarginPrsnt") or 0),
                    "exposure": float(inner.get("ExposureMarginPrsnt") or 0),
                },
            }
        }

    def basket_order_margins(self, params, consider_positions=True) -> dict:
        """Sum of Neo per-order `check-margin`. No native basket endpoint.

        Returns the Kite `{initial,final}.total` shape pair H15 reads.
        `consider_positions` is ignored: Neo RMS already includes the book.
        """
        del consider_positions
        if not params:
            return {"initial": {"total": 0.0}, "final": {"total": 0.0}}
        total = 0.0
        for p in params:
            token = self._token_for(p["exchange"], p["tradingsymbol"])
            # MarginAPI field names, not the place-order ones. A live
            # check-margin rejected es/pr/tk ("please provide valid symbol")
            # and accepted exSeg/prc/tok. reqdMrgn is the extra cash still
            # required — it is 0 when the account can fund the order — so
            # the gate must read ordMrgn, the order's own margin.
            data = self._trade_json(
                "POST",
                _PATHS["margin"],
                form=_jdata_form({
                    "exSeg": kotak_segment(p["exchange"]),
                    "prc": _fmt_price(p.get("price") or 0),
                    "prcTp": kotak_order_type(p.get("order_type") or "LIMIT"),
                    "prod": p.get("product") or "NRML",
                    "qty": str(int(p["quantity"])),
                    "tok": str(token),
                    "trnsTp": kotak_side(p["transaction_type"]),
                    "brkName": "KOTAK",
                    "brnchId": "ONLINE",
                }),
            )
            inner = _unwrap_data(data)
            mrgn = _first_float(inner, "ordMrgn", "totMrgnUsd", "mrgnUsd")
            if mrgn is None:
                raise BrokerOrderError(
                    f"Kotak check-margin had no margin figure: {list(inner)[:12]}"
                )
            total += mrgn
        return {"initial": {"total": total}, "final": {"total": total}}

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
            "ts": strategy_to_kotak_tradingsymbol(exchange, tradingsymbol),
            "tt": kotak_side(transaction_type),
            "ig": str(tag or "")[:20],
            "os": "NEOTRADEAPI",
        }
        # Every Neo form POST is jData=JSON. A raw form body 500s; the
        # same body under jData is the call the trade host accepts.
        data = self._trade_json(
            "POST", _PATHS["place_order"], form=_jdata_form(body)
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
            form=_jdata_form({"on": str(order_id), "am": "NO"}),
        )

    def order_history(self, order_id) -> List[dict]:
        # `on` is the cancel field. History requires nOrdNo; `on` is
        # rejected as a missing NestOrderNo before the id is looked up.
        data = self._trade_json(
            "POST",
            _PATHS["order_history"],
            form=_jdata_form({"nOrdNo": str(order_id)}),
        )
        rows = _extract_list(data)
        # Kite's order_history is oldest-first; the executor reads history[-1]
        # as the latest state. Kotak's sample is latest-first. Reverse so
        # the executor's "last row wins" contract holds.
        kite_rows = [_kotak_history_row(r) for r in rows]
        kite_rows.reverse()
        return kite_rows

    def quote(self, keys) -> dict:
        """Official Quotes API: gateway + `segment|instrument_token`.

        Index spots (`NSE:NIFTY 50`) use the index name as the token.
        Everything else looks up `pSymbol` from the scrip master. Hits
        `LOGIN_BASE` with consumer_key only — not the order `base_url`.
        """
        if isinstance(keys, str):
            keys = [keys]
        identities = []
        for key in keys:
            exchange, symbol = _split_quote_key(key)
            seg, token = self._quote_identity(exchange, symbol)
            identities.append((key, seg, token))
        out: Dict[str, dict] = {}
        for i in range(0, len(identities), 50):
            batch = identities[i:i + 50]
            joined = ",".join(f"{seg}|{tok}" for _, seg, tok in batch)
            path = _PATHS["quotes"].format(
                neo_symbols=urlquote(joined, safe="|,"),
                # `ltp` omits the book. Arbitrage prices off depth-1.
                quote_type="all",
            )
            url = f"{self.login_base}/{path}"
            data = self._request_json(
                "GET", url, headers=self._scrip_headers(),
            )
            rows = _quote_rows(data)
            by_token = {}
            for row in rows:
                tok = str(row.get("exchange_token") or row.get("instrument_token") or "")
                seg = str(row.get("exchange") or row.get("exchange_segment") or "").lower()
                by_token[(seg, tok)] = row
            for key, seg, tok in batch:
                row = by_token.get((seg.lower(), str(tok))) or by_token.get(("", str(tok)))
                if row is None and len(rows) == 1:
                    row = rows[0]
                ltp = _extract_ltp(row) if row is not None else _extract_ltp(data)
                if ltp is None:
                    raise BrokerOrderError(
                        f"Kotak quote for {key} ({seg}|{tok}) had no LTP"
                    )
                out[key] = {"last_price": ltp}
                if isinstance(row, dict) and row.get("ohlc"):
                    out[key]["ohlc"] = row["ohlc"]
                depth = _order_book_depth(row.get("depth")) if isinstance(row, dict) else None
                if depth:
                    out[key]["depth"] = depth
        return out

    def ltp(self, keys) -> dict:
        quoted = self.quote(keys)
        return {k: {"last_price": v["last_price"]} for k, v in quoted.items()}

    def positions(self) -> dict:
        # An account with no book returns HTTP 200, stat Not_Ok, stCode
        # 5203, errMsg "No Data". That is an empty book, not a failed
        # reconciliation — runners halt if positions() raises.
        data = self._trade_json(
            "GET", _PATHS["positions"], allow_not_ok=True,
        )
        if _empty_kotak_book(data):
            return {"net": [], "day": []}
        if _is_not_ok(data):
            raise BrokerOrderError(_error_message(data) or "Kotak positions failed")
        rows = _extract_list(data)
        net = [_kotak_position_row(r) for r in rows]
        return {"net": net, "day": []}

    def historical_data(
        self, instrument_token, from_date, to_date, interval,
        continuous=False, oi=False,
    ):
        """Kite-shaped candles from Neo `market-data/1.0/historical/details`.

        Consumer-key only (no Trade token). `interval` is Kite's
        (`5minute`); mapped to Neo's (`5min`).
        """
        del continuous, oi
        row, exch = self._row_for_token(int(instrument_token))
        # Index rows carry quote_token ("Nifty 50"). A numeric pSymbol
        # is the token for everything else.
        token = str(row.get("quote_token") or int(instrument_token))
        seg = kotak_segment(exch)
        neo_iv = _KITE_INTERVAL_TO_NEO.get((interval or "").strip())
        if not neo_iv:
            raise BrokerOrderError(
                f"No Kotak interval for Kite interval {interval!r}."
            )
        # The live query names are fromdate/todate. from_date is rejected
        # as a missing parameter.
        params = {
            "neosymbol": f"{seg}|{token}",
            "interval": neo_iv,
            "fromdate": _fmt_day(from_date),
            "todate": _fmt_day(to_date),
        }
        url = f"{self.login_base}/{_PATHS['historical_data']}"
        try:
            resp = self.session.get(
                url, headers=self._scrip_headers(), params=params, timeout=60,
            )
        except requests.RequestException as e:
            raise BrokerNetworkError(f"Kotak historical_data failed: {e}") from e
        if resp.status_code == 403:
            raise BrokerTokenError("Kotak historical_data rejected (403)")
        if resp.status_code >= 400:
            raise BrokerNetworkError(
                f"Kotak historical_data HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise BrokerNetworkError("Kotak historical_data returned non-JSON") from e
        candles = _historical_candles(payload)
        if not candles:
            raise BrokerOrderError(
                f"Kotak historical_data for {seg}|{token} returned no candles"
            )
        return candles

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
        rows = ensure_index_rows(parse_scrip_csv(text, exch), exch)
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
            # The CSV lives on lapi (object storage). Sending the consumer
            # key as Authorization makes that host return 400 InvalidArgument.
            # The file-paths call above already authenticated; the CSV is a
            # plain GET of the URL it returned.
            resp = self.session.get(url, timeout=120)
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

    def _quote_identity(self, exchange: str, symbol: str) -> tuple[str, str]:
        indexed = neo_index_quote_token(exchange, symbol)
        if indexed is not None:
            return indexed
        token = self._token_for(exchange, symbol)
        return kotak_segment(exchange), str(token)

    def _token_for(self, exchange: str, tradingsymbol: str) -> str:
        rows = self.instruments(exchange)
        for r in rows:
            if r.get("tradingsymbol") == tradingsymbol:
                tok = r.get("instrument_token")
                if tok:
                    return str(tok)
        raise BrokerOrderError(
            f"No Kotak scrip token for {exchange}:{tradingsymbol}. "
            "Scrip master loaded but the contract is missing."
        )

    def _row_for_token(self, token: int) -> tuple[dict, str]:
        for exch in ("NFO", "NSE", "BFO", "BSE"):
            for r in self.instruments(exch):
                try:
                    if int(r.get("instrument_token") or 0) == int(token):
                        return r, exch
                except (TypeError, ValueError):
                    continue
        raise BrokerOrderError(
            f"No Kotak scrip for instrument_token={token}"
        )

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

    def _trade_url(self, path: str, *, include_server_id: bool = True) -> str:
        base = (self.base_url or self.login_base).rstrip("/")
        path = path.lstrip("/")
        url = f"{base}/{path}"
        if include_server_id and self.server_id:
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
        include_server_id: bool = True,
        allow_not_ok: bool = False,
    ) -> dict:
        if form is not None:
            content_type = "form"
        elif json_body is not None:
            content_type = content_type or "json"
        return self._request_json(
            method,
            self._trade_url(path, include_server_id=include_server_id),
            headers=self._trade_headers(content_type),
            form=form,
            json_body=json_body,
            allow_not_ok=allow_not_ok,
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict,
        form: Optional[dict] = None,
        json_body: Optional[dict] = None,
        allow_not_ok: bool = False,
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
        if _is_not_ok(payload) and not allow_not_ok:
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
        # Runners call load_dotenv themselves. The dashboard does not, and
        # pydantic only maps declared settings, so KOTAK_* in the .env beside
        # this config.ini would otherwise be invisible and the YOUR_*
        # placeholders would fail login. A config in another directory does
        # not pick up the repo .env (tests, and a second book).
        load_dotenv(path.parent / ".env", override=False)
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
        if self.environment != "prod":
            raise BrokerConfigError(
                f"Kotak environment={self.environment!r} is not wired. "
                "Only prod is supported (UAT uses different login hosts "
                "and paths). Set [kotak] environment = prod or omit it."
            )
        reject_placeholders({
            "consumer_key": self.consumer_key,
            "mobile_number": self.mobile_number,
            "ucc": self.ucc,
            "mpin": self.mpin,
            "totp_key": self.totp_key,
        })
        self.mobile_number = normalize_kotak_mobile(self.mobile_number)
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
        try:
            # A Trade token that cannot read limits is not a session.
            # Caching it would make the next start look logged in and
            # then fail on the first margin check.
            client.profile()
        except (BrokerTokenError, BrokerOrderError, BrokerNetworkError) as e:
            raise BrokerAuthError(
                f"Kotak login did not yield a session that can read limits: {e}"
            ) from e
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


def _extract_ltp(payload) -> Optional[float]:
    if payload is None:
        return None
    if isinstance(payload, list):
        for item in payload:
            ltp = _extract_ltp(item)
            if ltp is not None:
                return ltp
        return None
    inner = _unwrap_data(payload) if isinstance(payload, dict) else {}
    if not isinstance(inner, dict):
        inner = payload if isinstance(payload, dict) else {}
    for key in ("ltp", "last_traded_price", "lastPrice"):
        if inner.get(key) not in (None, ""):
            try:
                return float(inner[key])
            except (TypeError, ValueError):
                continue
    for v in inner.values() if isinstance(inner, dict) else []:
        if isinstance(v, dict) and v.get("ltp") not in (None, ""):
            try:
                return float(v["ltp"])
            except (TypeError, ValueError):
                continue
    return None


def _quote_rows(payload) -> list:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            return [data]
        if payload.get("ltp") is not None:
            return [payload]
    return []


def _first_float(row: dict, *keys) -> Optional[float]:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            try:
                return float(row[k])
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
    """Kite-shaped net position. Qty is shares: (cf+fl buy) − (cf+fl sell)."""
    buy_cf = _first_float(row, "cfBuyQty")
    buy_fl = _first_float(row, "flBuyQty")
    sell_cf = _first_float(row, "cfSellQty")
    sell_fl = _first_float(row, "flSellQty")
    net_direct = _first_float(row, "netQty", "quantity")
    if all(v is None for v in (buy_cf, buy_fl, sell_cf, sell_fl, net_direct)):
        raise BrokerOrderError(
            f"Kotak position row has no qty fields: {list(row)[:12]}"
        )
    if any(v is not None for v in (buy_cf, buy_fl, sell_cf, sell_fl)):
        quantity = int(
            (buy_cf or 0) + (buy_fl or 0) - (sell_cf or 0) - (sell_fl or 0)
        )
    else:
        quantity = int(net_direct or 0)
    seg = row.get("exSeg") or row.get("exch") or ""
    trd = row.get("trdSym") or row.get("tradingsymbol") or ""
    exchange = strategy_exchange_from_segment(seg) if seg else ""
    tradingsymbol = (
        kotak_to_strategy_tradingsymbol(seg, trd) if trd else ""
    )
    return {
        "tradingsymbol": tradingsymbol,
        "exchange": exchange,
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


def _jdata_form(body: dict) -> dict:
    """Neo form POST body. The trade host 500s a raw field form."""
    cleaned = {k: v for k, v in body.items() if v is not None}
    return {"jData": json.dumps(cleaned)}


def _empty_kotak_book(payload: dict) -> bool:
    """True for the no-positions payload (stCode 5203 / errMsg No Data)."""
    if not isinstance(payload, dict) or not _is_not_ok(payload):
        return False
    msg = str(payload.get("errMsg") or payload.get("message") or "").strip().lower()
    try:
        code = int(payload.get("stCode"))
    except (TypeError, ValueError):
        code = None
    return code == 5203 or msg == "no data"


def _order_book_depth(depth) -> Optional[dict]:
    """Kotak depth → Kite `{buy,sell}[{price,quantity,orders}]` numbers."""
    if not isinstance(depth, dict):
        return None
    out = {}
    for side in ("buy", "sell"):
        levels = []
        for lvl in depth.get(side) or []:
            if not isinstance(lvl, dict):
                continue
            try:
                price = float(lvl.get("price") or 0)
                qty = int(float(lvl.get("quantity") or 0))
                orders = int(float(lvl.get("orders") or 0))
            except (TypeError, ValueError):
                continue
            levels.append({"price": price, "quantity": qty, "orders": orders})
        out[side] = levels
    if not out.get("buy") and not out.get("sell"):
        return None
    return out


def _fmt_price(price) -> str:
    try:
        return f"{float(price):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _fmt_day(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _historical_candles(payload) -> list:
    inner = payload.get("data") if isinstance(payload, dict) else payload
    rows = []
    if isinstance(inner, dict):
        rows = inner.get("candles") or inner.get("data") or []
    elif isinstance(inner, list):
        rows = inner
    out = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            ts, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
            vol = row[5] if len(row) > 5 else 0
            oi = row[6] if len(row) > 6 else 0
        elif isinstance(row, dict):
            ts = row.get("timestamp") or row.get("date") or row.get("time")
            o, h, l, c = row.get("open"), row.get("high"), row.get("low"), row.get("close")
            vol = row.get("volume") or 0
            oi = row.get("oi") or 0
        else:
            continue
        out.append({
            "date": _parse_candle_ts(ts),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": int(float(vol or 0)),
            "oi": int(float(oi or 0)),
        })
    return out


def _parse_candle_ts(raw):
    text = str(raw).strip()
    if text.endswith("+0530"):
        text = text[:-5] + "+05:30"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return text


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
