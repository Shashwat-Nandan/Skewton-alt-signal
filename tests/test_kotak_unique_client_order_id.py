"""Client order ids: unique per ORDER, identical across that order's retries.

2026-10-07 and 2026-10-09 (live): every order of a pair carried the same tag,
`pair-EICHE-BAJFI`. Kotak treats it as a client order id (`ig`, GuiOrdId),
filled the first leg and refused the second leg, the reversal and every
unwind retry with stCode 32 "Client Order Id Error Client OrderID already
exists", leaving a one-legged futures position both days.

The PR #15 review then showed that making the id unique per HTTP attempt is
worse: a retry after a lost response becomes a second live order. So the id
is made once per order and reused, and an attempt that landed is adopted from
the order book rather than re-sent. A failure here means either the second
leg is refused again, or a retry can double a position.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import strategies.order_executor as oe
from core.broker.errors import BrokerNetworkError, BrokerOrderError
from core.broker.kotak import KotakNeoClient, clean_client_order_id
from core.trade_proposer import TradeProposal


class _Broker:
    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY, TRANSACTION_TYPE_SELL = "BUY", "SELL"
    PRODUCT_NRML, ORDER_TYPE_LIMIT, VALIDITY_DAY = "NRML", "LIMIT", "DAY"

    def __init__(self, script=()):
        self.script = list(script)      # per place_order: "ok", "lost", "net", "dup"
        self.book = {}                  # cid -> order id, as Kotak's order book
        self.placed = []                # every place_order call's tag

    def place_order(self, **kw):
        cid = kw["tag"]
        self.placed.append(cid)
        step = self.script.pop(0) if self.script else "ok"
        if step == "dup" or (cid in self.book and step != "net"):
            raise BrokerOrderError("error from core [HTTP 400] body={\"stat\": "
                                   "\"Client Order Id Error Client OrderID already exists\"}")
        if step == "net":
            raise BrokerNetworkError("Kotak 502 on POST place")
        oid = f"OID{len(self.book) + 1}"
        self.book[cid] = oid
        if step == "lost":              # accepted, response lost
            raise BrokerNetworkError("Kotak POST place failed: read timeout")
        return oid

    def find_order_by_tag(self, cid):
        return self.book.get(cid)

    def order_history(self, oid):
        return [{"status": "COMPLETE", "filled_quantity": 100, "average_price": 7000.0}]

    def quote(self, keys):
        return {k: {"last_price": 7000.0} for k in keys}


def _executor(broker):
    ex = oe.OrderExecutor(broker, order_tag="pair-EICHE-BAJFI", poll_timeout_s=1.0,
                          poll_interval_s=0.0)
    return ex


def _prop(ts="EICHERMOT26OCTFUT", side="BUY"):
    return TradeProposal(tradingsymbol=ts, instrument_token=0, strike=0.0,
                         expiry="2026-10-27", option_type="FUT", lot_size=100,
                         quantity=1, price=7000.0, transaction_type=side, iv=0.0,
                         bid_ask_spread_pct=0.0, margin_required=0.0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(oe.time, "sleep", lambda s: None)


def test_each_leg_and_the_reversal_get_their_own_id():
    """The 10-07/10-09 failure: leg 2 and the reversal reused leg 1's id."""
    broker = _Broker()
    ex = _executor(broker)
    for ts, side in (("EICHERMOT26OCTFUT", "BUY"), ("BAJFINANCE26OCTFUT", "SELL"),
                     ("EICHERMOT26OCTFUT", "SELL")):
        assert ex.execute(_prop(ts, side))["status"] == "COMPLETE"
    assert len(set(broker.placed)) == 3
    assert all(c.startswith("p-EICHE-BAJFI-") and len(c) <= 20 for c in broker.placed)


def test_a_lost_response_is_adopted_not_re_sent():
    """Review finding 1: a new id per retry turns a lost response into a
    second live order. The landed order must be found and tracked."""
    broker = _Broker(["lost"])
    res = _executor(broker).execute(_prop())
    assert len(broker.placed) == 1, "no second order may be sent"
    assert res["status"] == "COMPLETE" and res["order_id"] == "OID1"


def test_a_real_retry_reuses_the_same_id():
    broker = _Broker(["net", "ok"])
    res = _executor(broker).execute(_prop())
    assert res["status"] == "COMPLETE"
    assert len(broker.placed) == 2 and broker.placed[0] == broker.placed[1]


def test_a_failed_retry_still_finds_an_order_that_landed():
    """Review finding 3: retry fails, but an attempt reached the broker — it
    must be tracked, not left as an orphan."""
    broker = _Broker(["net", "lost"])
    res = _executor(broker).execute(_prop())
    assert res["status"] == "COMPLETE" and res["order_id"] == "OID1"


def test_duplicate_id_refusal_means_already_placed(monkeypatch):
    broker = _Broker()
    cid = "p-EICHE-BAJFI-0abc12"
    broker.book[cid] = "OID9"                             # landed earlier
    monkeypatch.setattr(oe, "make_client_order_id", lambda tag: cid)
    res = _executor(broker).execute(_prop())
    assert res["order_id"] == "OID9" and res["status"] == "COMPLETE"
    assert broker.placed == [cid], "refused once, then adopted — not re-sent"


def test_ids_are_deterministic_bounded_and_safe():
    ids = [oe.make_client_order_id("pair-M&M-BAJFINANCE") for _ in range(2000)]
    assert len(set(ids)) == 2000                          # never repeats in-process
    assert all(len(i) <= 20 and "&" not in i for i in ids)
    assert ids[0].startswith("p-MM-BAJFINA")
    assert oe.make_client_order_id("taleb-BANKNIFTY").startswith("taleb-BANKNIF")
    assert oe.make_client_order_id("").startswith("algo-")


def _kotak():
    c = KotakNeoClient.__new__(KotakNeoClient)
    c.base_url = c.login_base = "https://example.invalid"
    c.server_id = None
    c.trade_token = c.sid = c.consumer_key = c.neo_fin_key = "x"
    sent = []

    def request(method, url, headers=None, data=None, json=None, timeout=None):
        sent.append((url, data))
        body = ({"stat": "Ok", "data": [
                    {"GuiOrdId": "p-EICHE-BAJFI-0abc12", "nOrdNo": "111", "ordDtTm": "09-Oct-2026 09:15:17"},
                    {"GuiOrdId": "other", "nOrdNo": "222", "ordDtTm": "09-Oct-2026 09:16:00"}]}
                if "orders" in url else {"stat": "Ok", "nOrdNo": "333"})
        return SimpleNamespace(status_code=200, text="", json=lambda: body)

    c.session = SimpleNamespace(request=request)
    return c, sent


def test_kotak_sends_the_id_it_is_given_made_safe():
    c, sent = _kotak()
    c.place_order(exchange="NFO", tradingsymbol="M&M26OCTFUT", transaction_type="BUY",
                  quantity=200, product="NRML", order_type="LIMIT", price=3600.0,
                  tag="p-M&M-BAJFI-0abc12")
    ig = json.loads(sent[-1][1]["jData"])["ig"]
    assert ig == "p-MM-BAJFI-0abc12" == clean_client_order_id("p-M&M-BAJFI-0abc12")


def test_kotak_finds_an_order_by_its_client_id():
    c, _ = _kotak()
    assert c.find_order_by_tag("p-EICHE-BAJFI-0abc12") == "111"
    assert c.find_order_by_tag("p-NOPE-0abc12") is None
