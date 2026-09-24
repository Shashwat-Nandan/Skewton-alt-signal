"""Broker adapter factory, mapping, and Kotak REST client.

Why these tests exist: a wrong default would silently keep placing on
Zerodha after an operator set broker.name=kotak; a missing mapping would
route an F&O order onto nse_cm; a Groww/Dhan selection that fell through
to Kite would trade the wrong account. Fail loud is the contract.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.broker import (
    BrokerAuthError,
    BrokerConfigError,
    BrokerNotImplementedError,
    BrokerOrderError,
    BrokerTokenError,
    get_broker,
    get_trading_client,
    read_broker_name,
)
from core.broker.kotak import KotakNeoClient, _extract_ltp, normalize_kotak_mobile
from core.broker.kotak_instruments import match_scrip_url, parse_scrip_csv
from core.broker.mapping import (
    strategy_exchange_from_segment,
    strategy_to_kotak_tradingsymbol,
    kotak_segment,
    kotak_side,
    kotak_status,
    kotak_to_strategy_tradingsymbol,
    neo_index_quote_token,
)


def _write_ini(path: Path, body: str) -> str:
    path.write_text(body)
    return str(path)


class TestFactory:
    def test_missing_file_defaults_kotak(self, tmp_path):
        # Kotak Neo is the primary broker. A host that still wants Kite
        # sets [broker] name = zerodha; silence must not pick Zerodha.
        assert read_broker_name(str(tmp_path / "nope.ini")) == "kotak"

    def test_missing_section_defaults_kotak(self, tmp_path):
        cfg = _write_ini(tmp_path / "c.ini", "[kite]\napi_key = x\n")
        assert read_broker_name(cfg) == "kotak"
        with pytest.raises(BrokerConfigError, match="Credentials not configured"):
            get_broker(cfg)

    def test_explicit_zerodha(self, tmp_path):
        cfg = _write_ini(tmp_path / "c.ini", "[broker]\nname = zerodha\n")
        assert get_broker(cfg).name == "zerodha"

    def test_alias_kite(self, tmp_path):
        cfg = _write_ini(tmp_path / "c.ini", "[broker]\nname = kite\n")
        assert read_broker_name(cfg) == "zerodha"

    def test_unknown_name_does_not_fall_back_to_zerodha(self, tmp_path):
        cfg = _write_ini(tmp_path / "c.ini", "[broker]\nname = upstox\n")
        with pytest.raises(BrokerConfigError, match="Unknown broker"):
            get_broker(cfg)

    def test_groww_refuses_to_login_even_with_creds(self, tmp_path):
        cfg = _write_ini(
            tmp_path / "c.ini",
            "[broker]\nname = groww\n"
            "[groww]\napi_key = real-key\napi_secret = real-secret\n"
            "totp_key = JBSWY3DPEHPK3PXP\n",
        )
        adapter = get_broker(cfg)
        assert adapter.name == "groww"
        with pytest.raises(BrokerNotImplementedError, match="not live-wired"):
            adapter.login()

    def test_dhan_refuses_to_login_even_with_creds(self, tmp_path):
        cfg = _write_ini(
            tmp_path / "c.ini",
            "[broker]\nname = dhan\n"
            "[dhan]\nclient_id = 1234567890\naccess_token = real-token\n",
        )
        adapter = get_broker(cfg)
        assert adapter.name == "dhan"
        with pytest.raises(BrokerNotImplementedError, match="not live-wired"):
            adapter.login()

    def test_groww_placeholder_creds_fail_before_not_implemented(self, tmp_path):
        cfg = _write_ini(
            tmp_path / "c.ini",
            "[broker]\nname = groww\n"
            "[groww]\napi_key = YOUR_GROWW_API_KEY\n"
            "api_secret = YOUR_GROWW_API_SECRET\n",
        )
        with pytest.raises(BrokerConfigError, match="Credentials not configured"):
            get_broker(cfg).login()

    def test_kotak_placeholder_creds_fail_on_construct(self, tmp_path):
        cfg = _write_ini(
            tmp_path / "c.ini",
            "[broker]\nname = kotak\n"
            "[kotak]\nconsumer_key = YOUR_KOTAK_CONSUMER_KEY\n"
            "mobile_number = YOUR_KOTAK_MOBILE\n"
            "ucc = YOUR_KOTAK_UCC\n"
            "mpin = YOUR_KOTAK_MPIN\n"
            "totp_key = YOUR_KOTAK_TOTP_SECRET\n",
        )
        with pytest.raises(BrokerConfigError, match="Credentials not configured"):
            get_broker(cfg)

    def test_kotak_uat_environment_fails_loud(self, tmp_path):
        cfg = _write_ini(
            tmp_path / "c.ini",
            "[broker]\nname = kotak\n"
            "[kotak]\n"
            "consumer_key = real-consumer\n"
            "mobile_number = +919876543210\n"
            "ucc = ABC123\n"
            "mpin = 654321\n"
            "totp_key = JBSWY3DPEHPK3PXP\n"
            "environment = uat\n",
        )
        with pytest.raises(BrokerConfigError, match="not wired"):
            get_broker(cfg)

    def test_kotak_mobile_must_be_a_country_code_or_ten_digits(self):
        # A 10-digit number is prefixed. A truncated one must not be
        # posted — Kotak's error would otherwise look like a bad TOTP.
        with pytest.raises(BrokerConfigError, match="country code"):
            normalize_kotak_mobile("12345")


class TestMapping:
    def test_nfo_goes_to_nse_fo_not_cash(self):
        assert kotak_segment("NFO") == "nse_fo"
        assert kotak_segment("NSE") == "nse_cm"

    def test_unknown_exchange_refuses_to_guess(self):
        with pytest.raises(BrokerOrderError, match="No Kotak exchange_segment"):
            kotak_segment("NYSE")

    def test_cash_symbol_gets_eq_suffix(self):
        assert strategy_to_kotak_tradingsymbol("NSE", "RELIANCE") == "RELIANCE-EQ"
        assert strategy_to_kotak_tradingsymbol("NSE", "RELIANCE-EQ") == "RELIANCE-EQ"

    def test_option_symbol_is_sent_as_the_scrip_master_names_it(self):
        # Prod nse_fo.csv (2026-09-24) uses the Kite suffix. The older
        # C-before-strike form is not in that file; sending it would
        # place a symbol the master does not list.
        assert strategy_to_kotak_tradingsymbol("NFO", "NIFTY25SEP25000CE") == "NIFTY25SEP25000CE"
        assert strategy_to_kotak_tradingsymbol("NFO", "NIFTY26O1928100CE") == "NIFTY26O1928100CE"
        assert strategy_to_kotak_tradingsymbol("NFO", "BANKNIFTY25SEP52000PE") == (
            "BANKNIFTY25SEP52000PE"
        )

    def test_option_round_trip(self):
        kite = "NIFTY25SEP25000CE"
        kotak = strategy_to_kotak_tradingsymbol("NFO", kite)
        assert kotak_to_strategy_tradingsymbol("nse_fo", kotak) == kite

    def test_futures_symbol_passes_through(self):
        assert strategy_to_kotak_tradingsymbol("NFO", "TCS26JULFUT") == "TCS26JULFUT"
        assert kotak_to_strategy_tradingsymbol("nse_fo", "TCS26JULFUT") == "TCS26JULFUT"

    def test_side_and_status(self):
        assert kotak_side("BUY") == "B"
        assert kotak_side("SELL") == "S"
        assert kotak_status("complete") == "COMPLETE"
        assert kotak_status("open pending") == "PENDING"
        assert kotak_status("rejected") == "REJECTED"

    def test_segment_round_trip(self):
        assert strategy_exchange_from_segment("nse_fo") == "NFO"
        assert strategy_exchange_from_segment("nse_cm") == "NSE"

    def test_index_spot_is_not_eq_suffix(self):
        assert neo_index_quote_token("NSE", "NIFTY 50") == ("nse_cm", "Nifty 50")
        assert neo_index_quote_token("NSE", "NIFTY BANK") == ("nse_cm", "Nifty Bank")
        assert neo_index_quote_token("NSE", "RELIANCE") is None


class TestKotakClient:
    def _client(self):
        c = KotakNeoClient("consumer-key", session=MagicMock())
        c.trade_token = "trade-jwt"
        c.sid = "sid-1"
        c.server_id = "E43"
        c.base_url = "https://e43.kotaksecurities.com"
        c.ucc = "ABC123"
        c.greeting_name = "Test User"
        return c

    def test_place_order_maps_kite_kwargs_and_returns_id(self):
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"stat": "Ok", "nOrdNo": "2501010001"}
        client.session.request.return_value = resp

        oid = client.place_order(
            variety="regular",
            exchange="NFO",
            tradingsymbol="NIFTY25SEP25000CE",
            transaction_type="BUY",
            quantity=75,
            product="NRML",
            order_type="LIMIT",
            price=120.5,
            validity="DAY",
            tag="taleb-entry",
        )
        assert oid == "2501010001"
        method, url = client.session.request.call_args[0][:2]
        assert method == "POST"
        assert "quick/order/rule/ms/place" in url
        form = client.session.request.call_args.kwargs["data"]
        body = json.loads(form["jData"])
        assert set(form) == {"jData"}
        assert body["es"] == "nse_fo"
        assert body["ts"] == "NIFTY25SEP25000CE"
        assert body["tt"] == "B"
        assert body["pt"] == "L"
        assert body["pc"] == "NRML"
        assert body["qt"] == "75"
        assert body["pr"] == "120.50"

    def test_order_history_normalizes_and_is_oldest_first(self):
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        # Kotak sample is latest-first (complete, then open, …).
        resp.json.return_value = {
            "data": {
                "data": [
                    {"ordSt": "complete", "fldQty": 75, "avgPrc": "120.4",
                     "nOrdNo": "1", "rejRsn": "--"},
                    {"ordSt": "open", "fldQty": 0, "avgPrc": "0.00",
                     "nOrdNo": "1", "rejRsn": "--"},
                ]
            }
        }
        client.session.request.return_value = resp
        history = client.order_history("1")
        sent = json.loads(client.session.request.call_args.kwargs["data"]["jData"])
        assert sent == {"nOrdNo": "1"}
        assert history[0]["status"] == "PENDING"
        assert history[-1]["status"] == "COMPLETE"
        assert history[-1]["filled_quantity"] == 75
        assert history[-1]["average_price"] == 120.4

    def test_403_is_token_error(self):
        client = self._client()
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {"error": [{"message": "invalid session"}]}
        client.session.request.return_value = resp
        with pytest.raises(BrokerTokenError):
            client.limits()

    def test_totp_login_stores_view_token(self):
        client = KotakNeoClient("consumer-key", session=MagicMock())
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {"token": "view-jwt", "sid": "sid-1", "ucc": "ABC123",
                     "greetingName": "Test", "kType": "View"}
        }
        client.session.request.return_value = resp
        client.totp_login("+919999999999", "ABC123", "123456")
        assert client.view_token == "view-jwt"
        assert client.sid == "sid-1"
        body = client.session.request.call_args.kwargs["json"]
        assert body["mobileNumber"] == "+919999999999"
        assert body["totp"] == "123456"
        url = client.session.request.call_args[0][1]
        assert url.startswith("https://mis.kotaksecurities.com/login/1.0/tradeApiLogin")

    def test_totp_login_prefixes_bare_ten_digit_mobile(self):
        # Kotak rejected a 10-digit mobile and accepted the same digits
        # with +91. The adapter has to send the form the broker accepts.
        client = KotakNeoClient("consumer-key", session=MagicMock())
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {"token": "view-jwt", "sid": "sid-1", "ucc": "ABC123"}
        }
        client.session.request.return_value = resp
        client.totp_login("9999999999", "ABC123", "123456")
        body = client.session.request.call_args.kwargs["json"]
        assert body["mobileNumber"] == "+919999999999"

    def test_limits_posts_jdata_and_omits_server_query(self):
        # GET /quick/user/limits on the trade host is 404. The call that
        # returns Net is POST jData={seg,exch,prod}=ALL with no sId.
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"stat": "Ok", "Net": "1"}
        client.session.request.return_value = resp
        client.limits()
        method, url = client.session.request.call_args[0][:2]
        assert method == "POST"
        assert url == "https://e43.kotaksecurities.com/quick/user/limits"
        assert "sId=" not in url
        form = client.session.request.call_args.kwargs["data"]
        assert json.loads(form["jData"]) == {
            "seg": "ALL", "exch": "ALL", "prod": "ALL",
        }

    def test_quote_uses_gateway_token_path_not_tradingsymbol(self):
        client = self._client()
        client._instruments_by_exchange["NFO"] = [{
            "instrument_token": 999,
            "tradingsymbol": "NIFTY25SEP25000CE",
            "name": "NIFTY",
            "instrument_type": "CE",
            "lot_size": 75,
            "exchange": "NFO",
        }]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [
            {"exchange_token": "Nifty 50", "exchange": "nse_cm", "ltp": "22500.5"},
            {"exchange_token": "999", "exchange": "nse_fo", "ltp": "120.4"},
        ]
        client.session.request.return_value = resp
        quoted = client.quote(["NSE:NIFTY 50", "NFO:NIFTY25SEP25000CE"])
        url = client.session.request.call_args[0][1]
        assert url.startswith("https://mis.kotaksecurities.com/")
        assert "script-details/1.0/quotes/neosymbol/" in url
        assert url.rstrip("/").endswith("/all")
        assert "nse_cm|Nifty" in url
        assert "nse_fo|999" in url
        assert "%7C" not in url
        assert "NIFTY 50-EQ" not in url
        assert "-EQ" not in url
        assert quoted["NSE:NIFTY 50"]["last_price"] == 22500.5
        assert quoted["NFO:NIFTY25SEP25000CE"]["last_price"] == 120.4
        headers = client.session.request.call_args.kwargs["headers"]
        assert headers["Authorization"] == "consumer-key"
        assert "Sid" not in headers
        assert "Auth" not in headers

    def test_extract_ltp_ignores_iv(self):
        assert _extract_ltp({"iv": 0.15, "ltp": "12.5"}) == 12.5
        assert _extract_ltp({"iv": 0.15}) is None
        assert _extract_ltp({"data": {"ltp": "9.1", "iv": "0.2"}}) == 9.1

    def test_positions_net_qty_exchange_and_kite_symbol(self):
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": [
                {
                    "exSeg": "nse_fo",
                    "trdSym": "BHARTIARTL26APRFUT",
                    "flBuyQty": "475",
                    "flSellQty": "0",
                    "cfBuyQty": "0",
                    "cfSellQty": "0",
                    "avgPrc": "1650.5",
                    "prod": "NRML",
                },
                {
                    "exSeg": "nse_fo",
                    "trdSym": "NIFTY25SEPC25000",
                    "flBuyQty": "0",
                    "flSellQty": "75",
                    "cfBuyQty": "0",
                    "cfSellQty": "0",
                    "avgPrc": "120.4",
                    "prod": "NRML",
                },
            ]
        }
        client.session.request.return_value = resp
        net = client.positions()["net"]
        long_ = next(r for r in net if r["tradingsymbol"] == "BHARTIARTL26APRFUT")
        assert long_["exchange"] == "NFO"
        assert long_["quantity"] == 475
        short = next(r for r in net if r["tradingsymbol"] == "NIFTY25SEP25000CE")
        assert short["exchange"] == "NFO"
        assert short["quantity"] == -75

    def test_positions_missing_qty_fails_loud(self):
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": [{"exSeg": "nse_fo", "trdSym": "TCS26JULFUT"}]
        }
        client.session.request.return_value = resp
        with pytest.raises(BrokerOrderError, match="no qty fields"):
            client.positions()

    def test_margins_maps_neo_net(self):
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "stat": "Ok",
            "Net": "464000.5",
            "CollateralValue": "38.19",
            "MarginUsed": "18.78",
            "SpanMarginPrsnt": "10",
            "ExposureMarginPrsnt": "8",
        }
        client.session.request.return_value = resp
        m = client.margins()
        assert m["equity"]["net"] == 464000.5
        assert m["equity"]["available"]["collateral"] == 38.19
        assert m["equity"]["available"]["live_balance"] == 464000.5

    def test_margins_missing_net_fails_loud(self):
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"stat": "Ok", "Category": "CLIENT_SPECIAL"}
        client.session.request.return_value = resp
        with pytest.raises(BrokerOrderError, match="no Net"):
            client.margins()

    def test_basket_order_margins_sums_check_margin(self):
        client = self._client()
        client._instruments_by_exchange["NFO"] = [{
            "instrument_token": 52175,
            "tradingsymbol": "NIFTY25SEP25000CE",
            "lot_size": 75,
            "exchange": "NFO",
        }]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": {"reqdMrgn": "15000", "ordMrgn": "15000", "stat": "Ok"}
        }
        client.session.request.return_value = resp
        basket = client.basket_order_margins([{
            "exchange": "NFO",
            "tradingsymbol": "NIFTY25SEP25000CE",
            "transaction_type": "BUY",
            "quantity": 75,
            "price": 120.5,
            "product": "NRML",
            "order_type": "LIMIT",
        }])
        assert basket["initial"]["total"] == 15000.0
        assert basket["final"]["total"] == 15000.0
        body = json.loads(client.session.request.call_args.kwargs["data"]["jData"])
        assert body["exSeg"] == "nse_fo"
        assert body["tok"] == "52175"
        assert body["trnsTp"] == "B"
        assert body["brkName"] == "KOTAK"
        assert "es" not in body
        assert "quick/user/check-margin" in client.session.request.call_args[0][1]

    def test_basket_margin_uses_order_margin_not_the_shortfall(self):
        # Live check-margin returns reqdMrgn 0 when the account can fund
        # the order, and ordMrgn as the order's own margin. Preferring
        # the shortfall would zero the gate.
        client = self._client()
        client._instruments_by_exchange["NFO"] = [{
            "instrument_token": 52175,
            "tradingsymbol": "NIFTY25SEP25000CE",
            "lot_size": 75,
            "exchange": "NFO",
        }]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "stat": "Ok", "ordMrgn": "15500.00", "reqdMrgn": "0.000000",
        }
        client.session.request.return_value = resp
        basket = client.basket_order_margins([{
            "exchange": "NFO",
            "tradingsymbol": "NIFTY25SEP25000CE",
            "transaction_type": "BUY",
            "quantity": 75,
            "price": 120.5,
            "product": "NRML",
            "order_type": "LIMIT",
        }])
        assert basket["final"]["total"] == 15500.0

    def test_positions_no_data_is_an_empty_book(self):
        # stCode 5203 is "no positions", HTTP 200. Raising here makes
        # the runners abort reconciliation on a flat account.
        client = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "stat": "Not_Ok", "stCode": 5203, "errMsg": "No Data",
        }
        client.session.request.return_value = resp
        assert client.positions() == {"net": [], "day": []}

    def test_quote_depth_is_kite_shaped_numbers(self):
        client = self._client()
        client._instruments_by_exchange["NSE"] = [{
            "instrument_token": 1333,
            "tradingsymbol": "HDFCBANK",
            "exchange": "NSE",
        }]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [{
            "exchange_token": "1333",
            "exchange": "nse_cm",
            "ltp": "801.60",
            "depth": {
                "buy": [{"price": "801.55", "quantity": "10", "orders": "2"}],
                "sell": [{"price": "801.65", "quantity": "4", "orders": "1"}],
            },
        }]
        client.session.request.return_value = resp
        quoted = client.quote(["NSE:HDFCBANK"])
        buy = quoted["NSE:HDFCBANK"]["depth"]["buy"][0]
        assert buy["price"] == 801.55
        assert buy["quantity"] == 10
        assert isinstance(buy["price"], float)

    def test_historical_data_maps_kite_interval_and_candles(self):
        client = self._client()
        client._instruments_by_exchange["NFO"] = [{
            "instrument_token": 12346,
            "tradingsymbol": "NIFTY25SEPFUT",
            "exchange": "NFO",
        }]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "status": "success",
            "data": {
                "candles": [
                    ["2026-08-20T09:15:00+0530", 100.0, 101.0, 99.0, 100.5, 10, 0],
                ]
            },
        }
        client.session.get.return_value = resp
        rows = client.historical_data(
            12346, date(2026, 8, 1), date(2026, 8, 20), "5minute",
        )
        assert rows[0]["close"] == 100.5
        assert rows[0]["open"] == 100.0
        params = client.session.get.call_args.kwargs["params"]
        assert params["interval"] == "5min"
        assert params["neosymbol"] == "nse_fo|12346"
        url = client.session.get.call_args[0][0]
        assert url.startswith("https://mis.kotaksecurities.com/")
        assert "market-data/1.0/historical/details" in url


class TestKotakAdapterLogin:
    def _kotak_ini(self, tmp_path: Path) -> str:
        return _write_ini(
            tmp_path / "c.ini",
            "[broker]\nname = kotak\n"
            "[kotak]\n"
            "consumer_key = real-consumer\n"
            "mobile_number = +919876543210\n"
            "ucc = ABC123\n"
            "mpin = 654321\n"
            "totp_key = JBSWY3DPEHPK3PXP\n",
        )

    def test_login_totp_then_mpin_and_caches_session(self, tmp_path, monkeypatch):
        cfg = self._kotak_ini(tmp_path)
        monkeypatch.chdir(tmp_path)

        def fake_request(method, url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "tradeApiLogin" in url:
                resp.json.return_value = {
                    "data": {
                        "token": "view-jwt", "sid": "sid-1",
                        "ucc": "ABC123", "greetingName": "Ada", "kType": "View",
                    }
                }
            elif "tradeApiValidate" in url:
                resp.json.return_value = {
                    "data": {
                        "token": "trade-jwt", "sid": "sid-2",
                        "hsServerId": "E43",
                        "baseUrl": "https://e43.kotaksecurities.com",
                        "ucc": "ABC123", "greetingName": "Ada", "kType": "Trade",
                    }
                }
            else:
                # limits() used by profile()
                resp.json.return_value = {"stat": "Ok", "data": {}}
            return resp

        with patch("core.broker.kotak.requests.Session") as sess_cls:
            sess_cls.return_value.request.side_effect = fake_request
            client = get_trading_client(cfg)
        assert client.trade_token == "trade-jwt"
        assert client.ucc == "ABC123"
        cache = json.loads((tmp_path / ".kotak_session.json").read_text())
        assert cache["trade_token"] == "trade-jwt"
        assert cache["sid"] == "sid-2"
        # 0600 — the same constraint kite_auth enforces on .kite_session.json
        assert (tmp_path / ".kotak_session.json").stat().st_mode & 0o777 == 0o600
        urls = [c[0][1] for c in sess_cls.return_value.request.call_args_list]
        assert any("quick/user/limits" in u for u in urls)

    def test_login_does_not_cache_a_session_that_cannot_read_limits(
        self, tmp_path, monkeypatch,
    ):
        # The trade host 404s the old GET. Caching the token anyway made
        # the next start look authenticated until the first margin check.
        cfg = self._kotak_ini(tmp_path)
        monkeypatch.chdir(tmp_path)

        def fake_request(method, url, **kwargs):
            resp = MagicMock()
            if "tradeApiLogin" in url or "tradeApiValidate" in url:
                resp.status_code = 200
                token = "view-jwt" if "tradeApiLogin" in url else "trade-jwt"
                resp.json.return_value = {
                    "data": {
                        "token": token, "sid": "sid-1", "ucc": "ABC123",
                        "kType": "View" if "tradeApiLogin" in url else "Trade",
                        "baseUrl": "https://e43.kotaksecurities.com",
                    }
                }
            else:
                resp.status_code = 404
                resp.json.return_value = {"error": [{"message": "not found"}]}
            return resp

        with patch("core.broker.kotak.requests.Session") as sess_cls:
            sess_cls.return_value.request.side_effect = fake_request
            with pytest.raises(BrokerAuthError, match="read limits"):
                get_trading_client(cfg)
        assert not (tmp_path / ".kotak_session.json").exists()

    def test_dotenv_beside_config_supplies_kotak_secrets(self, tmp_path, monkeypatch):
        # The dashboard never calls load_dotenv. Secrets live in the .env
        # next to config.ini; a .env in some other directory must not leak in.
        for key in (
            "KOTAK_CONSUMER_KEY", "KOTAK_MOBILE_NUMBER", "KOTAK_UCC",
            "KOTAK_MPIN", "KOTAK_TOTP_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        cfg = _write_ini(
            tmp_path / "config.ini",
            "[broker]\nname = kotak\n"
            "[kotak]\n"
            "consumer_key = YOUR_KOTAK_CONSUMER_KEY\n"
            "mobile_number = YOUR_KOTAK_MOBILE\n"
            "ucc = YOUR_KOTAK_UCC\n"
            "mpin = YOUR_KOTAK_MPIN\n"
            "totp_key = YOUR_KOTAK_TOTP_SECRET\n",
        )
        (tmp_path / ".env").write_text(
            "KOTAK_CONSUMER_KEY=real-consumer\n"
            "KOTAK_MOBILE_NUMBER=9876543210\n"
            "KOTAK_UCC=ABC123\n"
            "KOTAK_MPIN=654321\n"
            "KOTAK_TOTP_KEY=JBSWY3DPEHPK3PXP\n"
        )
        other = tmp_path / "other"
        other.mkdir()
        other_cfg = _write_ini(
            other / "config.ini",
            "[broker]\nname = kotak\n"
            "[kotak]\n"
            "consumer_key = YOUR_KOTAK_CONSUMER_KEY\n"
            "mobile_number = YOUR_KOTAK_MOBILE\n"
            "ucc = YOUR_KOTAK_UCC\n"
            "mpin = YOUR_KOTAK_MPIN\n"
            "totp_key = YOUR_KOTAK_TOTP_SECRET\n",
        )
        from core.broker.kotak import KotakNeoAdapter
        adapter = KotakNeoAdapter(cfg)
        assert adapter.mobile_number == "+919876543210"
        # The first construct published those keys into the process. Clear
        # them so the other directory cannot inherit the repo file via env.
        for key in (
            "KOTAK_CONSUMER_KEY", "KOTAK_MOBILE_NUMBER", "KOTAK_UCC",
            "KOTAK_MPIN", "KOTAK_TOTP_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(BrokerConfigError, match="Credentials not configured"):
            KotakNeoAdapter(other_cfg)

    def test_cached_session_skips_totp_when_limits_ok(self, tmp_path, monkeypatch):
        cfg = self._kotak_ini(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".kotak_session.json").write_text(json.dumps({
            "trade_token": "cached-jwt",
            "sid": "sid-cached",
            "server_id": "E43",
            "base_url": "https://e43.kotaksecurities.com",
            "ucc": "ABC123",
            "greeting_name": "Ada",
            "timestamp": "2099-01-01T00:00:00",
        }))

        def fake_request(method, url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"stat": "Ok", "data": {}}
            return resp

        with patch("core.broker.kotak.requests.Session") as sess_cls:
            sess_cls.return_value.request.side_effect = fake_request
            client = get_broker(cfg).login()
        assert client.trade_token == "cached-jwt"
        urls = [c[0][1] for c in sess_cls.return_value.request.call_args_list]
        assert not any("tradeApiLogin" in u for u in urls)

    def test_cached_client_does_not_hit_the_network(self, tmp_path, monkeypatch):
        cfg = self._kotak_ini(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".kotak_session.json").write_text(json.dumps({
            "trade_token": "cached-jwt",
            "sid": "sid-cached",
            "server_id": "E43",
            "base_url": "https://e43.kotaksecurities.com",
            "ucc": "ABC123",
            "timestamp": "2099-01-01T00:00:00",
        }))
        client = get_broker(cfg).cached_client()
        assert client is not None
        assert client.trade_token == "cached-jwt"
        assert get_broker(cfg).cached_client() is not None


class TestOrderExecutorAcceptsBrokerTokenError:
    """H8 refresh-once must fire for Kotak's BrokerTokenError, not just
    kiteconnect.TokenException — otherwise a Neo 403 fails the order
    without the retry the pair runner proved live."""

    def test_place_order_token_error_refreshes_once(self):
        from core.trade_proposer import TradeProposal
        from strategies.order_executor import OrderExecutor

        class Fake:
            VARIETY_REGULAR = "regular"
            TRANSACTION_TYPE_BUY = "BUY"
            TRANSACTION_TYPE_SELL = "SELL"
            PRODUCT_NRML = "NRML"
            ORDER_TYPE_LIMIT = "LIMIT"
            VALIDITY_DAY = "DAY"

            def __init__(self):
                self.placed = 0

            def quote(self, keys):
                return {keys[0]: {"last_price": 100.0}}

            def place_order(self, **kwargs):
                self.placed += 1
                if self.placed == 1:
                    raise BrokerTokenError("403")
                return "OID1"

            def order_history(self, order_id):
                return [{"status": "COMPLETE", "filled_quantity": 50,
                         "average_price": 100.0}]

        stale = Fake()
        fresh = Fake()
        fresh.placed = 1  # so the retry path does not raise
        prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=100.0,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )
        result = OrderExecutor(
            stale, order_tag="t", broker_refresh=lambda: fresh,
            poll_timeout_s=0.05, poll_interval_s=0.01,
        ).execute(prop)
        assert result["status"] == "COMPLETE"
        assert fresh.placed == 2


_NFO_CSV = """pSymbol,pExchSeg,pSymbolName,pTrdSymbol,pOptionType,pInstType,dTickSize,lLotSize,lExpiryDate,pExpiryDate,pScripRefKey,dStrikePrice
12345,nse_fo,NIFTY,NIFTY25SEP25000CE,CE,OPTIDX,5,75,1467297000,2016-06-30,NIFTY30JUN2625000.00CE,25000
12346,nse_fo,NIFTY,NIFTY25SEPFUT,XX,FUTIDX,5,75,1785196800,2026-07-28,NIFTY28JUL26,0
12347,nse_fo,INFY,INFY25JUN660PE,PE,OPTSTK,5,400,1467297000,2016-06-30,INFY30JUN26660.00PE,660
12348,nse_fo,TCS,TCS26JULFUT,XX,FUTSTK,5,225,1785196800,2026-07-28,TCS28JUL26,0
"""


class TestKotakScripMaster:
    """F&O contract resolution is how strategies size lots and pick the
    front-month future. A mapper that emits 2016 expiries (Kotak's known
    OPTSTK bug) or Kotak-native option symbols would make Taleb size on
    the wrong lot and place_order double-mangle the tradingsymbol."""

    def test_match_url_prefers_nse_fo_not_cash(self):
        url = match_scrip_url(
            [
                "https://lapi.example/transformed/nse_com.csv",
                "https://lapi.example/transformed/nse_fo.csv",
                "https://lapi.example/transformed-v1/nse_cm-v1.csv",
            ],
            "nse_fo",
        )
        assert url.endswith("nse_fo.csv")

    def test_match_url_nse_cm_skips_nse_com(self):
        url = match_scrip_url(
            [
                "https://lapi.example/transformed/nse_com.csv",
                "https://lapi.example/transformed-v1/nse_cm-v1.csv",
            ],
            "nse_cm",
        )
        assert "nse_cm-v1.csv" in url

    def test_match_url_missing_segment_fails_loud(self):
        with pytest.raises(BrokerOrderError, match="no CSV"):
            match_scrip_url(["https://lapi.example/transformed/mcx_fo.csv"], "nse_fo")

    def test_parse_maps_index_option_to_kite_shape(self):
        rows = parse_scrip_csv(_NFO_CSV, "NFO")
        by_ts = {r["tradingsymbol"]: r for r in rows}
        opt = by_ts["NIFTY25SEP25000CE"]
        assert opt["name"] == "NIFTY"
        assert opt["instrument_type"] == "CE"
        assert opt["lot_size"] == 75
        assert opt["tick_size"] == pytest.approx(0.05)
        assert opt["strike"] == 25000.0
        assert opt["expiry"] == date(2026, 6, 30)
        assert opt["exchange"] == "NFO"
        assert strategy_to_kotak_tradingsymbol("NFO", opt["tradingsymbol"]) == (
            "NIFTY25SEP25000CE"
        )

    def test_stock_option_expiry_uses_refkey_not_2016_field(self):
        rows = parse_scrip_csv(_NFO_CSV, "NFO")
        infy = next(r for r in rows if r["name"] == "INFY")
        assert infy["instrument_type"] == "PE"
        assert infy["expiry"] == date(2026, 6, 30)
        assert infy["strike"] == 660.0
        assert infy["tradingsymbol"] == "INFY25JUN660PE"

    def test_futures_keep_kite_like_symbol_and_type(self):
        rows = parse_scrip_csv(_NFO_CSV, "NFO")
        tcs = next(r for r in rows if r["name"] == "TCS")
        assert tcs["instrument_type"] == "FUT"
        assert tcs["tradingsymbol"] == "TCS26JULFUT"
        assert tcs["strike"] == 0.0
        assert tcs["lot_size"] == 225
        nifty_fut = next(
            r for r in rows
            if r["name"] == "NIFTY" and r["instrument_type"] == "FUT"
        )
        assert nifty_fut["tradingsymbol"] == "NIFTY25SEPFUT"
        assert nifty_fut["expiry"] == date(2026, 7, 28)

    def test_fo_missing_lot_is_skipped(self):
        csv = (
            "pSymbol,pExchSeg,pSymbolName,pTrdSymbol,pOptionType,pInstType,"
            "dTickSize,lLotSize,pScripRefKey,dStrikePrice\n"
            "1,nse_fo,NIFTY,NIFTY25SEPFUT,XX,FUTIDX,5,0,NIFTY28JUL26,0\n"
            "2,nse_fo,NIFTY,NIFTY25SEPFUT,XX,FUTIDX,5,75,NIFTY28JUL26,0\n"
        )
        rows = parse_scrip_csv(csv, "NFO")
        assert len(rows) == 1
        assert rows[0]["lot_size"] == 75

    def test_html_download_fails_loud(self):
        with pytest.raises(BrokerOrderError, match="HTML"):
            parse_scrip_csv("<!DOCTYPE html><html></html>", "NFO")

    def test_instruments_downloads_and_caches(self, tmp_path):
        client = KotakNeoClient("consumer-key", session=MagicMock(),
                                scrip_cache_dir=tmp_path)
        paths = MagicMock()
        paths.status_code = 200
        paths.json.return_value = {
            "data": {
                "filesPaths": [
                    "https://lapi.example/transformed/nse_fo.csv",
                    "https://lapi.example/transformed-v1/nse_cm-v1.csv",
                ]
            }
        }
        client.session.request.return_value = paths
        csv_resp = MagicMock()
        csv_resp.status_code = 200
        csv_resp.content = _NFO_CSV.encode()
        client.session.get.return_value = csv_resp

        rows = client.instruments("NFO")
        # lapi rejects an Authorization header on the CSV itself.
        csv_headers = client.session.get.call_args.kwargs.get("headers") or {}
        assert "Authorization" not in csv_headers
        assert any(r["instrument_type"] == "FUT" for r in rows)
        assert any(r["instrument_type"] == "CE" for r in rows)
        # In-process cache: second call does not hit the network again.
        client.session.get.reset_mock()
        client.session.request.reset_mock()
        again = client.instruments("NFO")
        assert again is rows
        client.session.get.assert_not_called()
        client.session.request.assert_not_called()
        # Day-cache file landed so a new client can skip the CSV download.
        cached = list(tmp_path.glob("*_nse_fo.csv"))
        assert len(cached) == 1

    def test_instruments_empty_dump_fails_loud(self, tmp_path):
        client = KotakNeoClient("consumer-key", session=MagicMock(),
                                scrip_cache_dir=tmp_path)
        paths = MagicMock()
        paths.status_code = 200
        paths.json.return_value = {
            "data": {"filesPaths": ["https://lapi.example/transformed/nse_fo.csv"]}
        }
        client.session.request.return_value = paths
        csv_resp = MagicMock()
        csv_resp.status_code = 200
        csv_resp.content = (
            b"pSymbol,pExchSeg,pSymbolName,pTrdSymbol,pOptionType,pInstType\n"
        )
        client.session.get.return_value = csv_resp
        with pytest.raises(BrokerOrderError, match="0 rows"):
            client.instruments("NFO")

