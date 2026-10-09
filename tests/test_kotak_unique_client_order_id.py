"""Kotak's order tag is a client order ID: it must be unique per order.

2026-10-07 and 2026-10-09 (live): every order of a pair carried the same tag,
`pair-EICHE-BAJFI`. Kotak filled the first leg and refused the second leg, the
reversal and every unwind retry with stCode 32 "Client Order Id Error Client
OrderID already exists" (logged as "error from core"), leaving a one-legged
futures position both days. Taleb (`taleb-<underlying>`) and arbitrage reuse
tags the same way. A failure here means the second order of any multi-leg
trade is refused again.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.broker.kotak import KotakNeoClient, unique_client_order_id


def _client():
    c = KotakNeoClient.__new__(KotakNeoClient)
    c.base_url = c.login_base = "https://example.invalid"
    c.server_id = None
    c.trade_token = c.sid = c.consumer_key = c.neo_fin_key = "x"
    sent = []

    def request(method, url, headers=None, data=None, json=None, timeout=None):
        sent.append(data)
        return SimpleNamespace(status_code=200, text="",
                               json=lambda: {"stat": "Ok", "nOrdNo": str(len(sent))})

    c.session = SimpleNamespace(request=request)
    return c, sent


def _ig(form):
    return json.loads(form["jData"])["ig"]


def test_every_order_of_a_pair_gets_its_own_client_order_id():
    c, sent = _client()
    for ts, side in (("EICHERMOT26OCTFUT", "BUY"), ("BAJFINANCE26OCTFUT", "SELL"),
                     ("EICHERMOT26OCTFUT", "SELL")):          # leg, leg, reversal
        c.place_order(exchange="NFO", tradingsymbol=ts, transaction_type=side,
                      quantity=100, product="NRML", order_type="LIMIT", price=7000.0,
                      tag="pair-EICHE-BAJFI")
    ids = [_ig(f) for f in sent]
    assert len(set(ids)) == 3, f"duplicate client order ids: {ids}"
    assert all(i.startswith("EICHE-BAJFI-") and len(i) <= 20 for i in ids)


def test_unique_ids_stay_readable_and_bounded():
    many = {unique_client_order_id("taleb-NIFTY") for _ in range(1000)}
    assert len(many) == 1000
    assert all(i.startswith("taleb-NIFTY") and len(i) <= 20 for i in many)
    assert unique_client_order_id("").startswith("algo-")
