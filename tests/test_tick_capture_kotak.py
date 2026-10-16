"""The Kotak quote poll runs beside the live pair runner and shares its
Kotak session and quote quota. These pin the two ways it could hurt that
runner: a fresh login that replaces the shared token, and a burst of
retries into a rate limit."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core.broker.errors import BrokerNetworkError, BrokerOrderError
from market_data import tick_capture as tc


class _Quotes:
    def __init__(self, fail_with=None, missing=()):
        self.calls = 0
        self.fail_with, self.missing = fail_with, set(missing)

    def quote(self, keys):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if self.missing & set(keys):
            raise BrokerOrderError("Kotak quote had no LTP")
        return {k: {"last_price": 100.0} for k in keys}


PAIRS = [(i, f"NFO:NIFTY{i}CE") for i in range(8)]


def test_a_rate_limited_pass_is_one_call_not_a_split_storm():
    client = _Quotes(fail_with=BrokerNetworkError("Kotak rate-limited (429)"))
    with pytest.raises(BrokerNetworkError):
        tc._poll_quotes(client, PAIRS)
    assert client.calls == 1


def test_a_missing_ltp_still_splits_so_one_strike_does_not_drop_the_book(monkeypatch):
    monkeypatch.setattr(tc, "_LOG", tc.logging.getLogger("test"))
    client = _Quotes(missing={"NFO:NIFTY3CE"})
    rows = tc._poll_quotes(client, PAIRS)
    assert sorted(r["instrument_token"] for r in rows) == [0, 1, 2, 4, 5, 6, 7]


def test_failed_passes_back_off_and_the_loop_continues(monkeypatch):
    sleeps = []
    clock = iter([0.0, 0.0, 1e12])
    # A stand-in for the module tick_capture sees, so logging's clock is untouched.
    monkeypatch.setattr(tc, "time", SimpleNamespace(time=lambda: next(clock),
                                                    sleep=sleeps.append))
    monkeypatch.setattr(tc, "_STOP_EPOCH", 1.0)
    monkeypatch.setattr(tc, "_LOG", tc.logging.getLogger("test"))
    calls = iter([BrokerNetworkError("429"), None])

    def poll(client, pairs):
        step = next(calls)
        if step is not None:
            raise step
        return []

    monkeypatch.setattr(tc, "_poll_quotes", poll)
    tc._run_quote_poll(object(), [], {}, poll_seconds=10.0)
    assert sleeps == [tc.FAILURE_BACKOFF_S, 10.0]


def test_cached_session_never_starts_a_login(monkeypatch, tmp_path):
    import core.broker as broker

    class _Client:
        def profile(self):
            return {"user_name": "u", "user_id": "x"}

    seen = {}

    def market(path, cached_only=False):
        seen["cached_only"] = cached_only
        return _Client()

    monkeypatch.setattr(broker, "get_market_client", market)
    monkeypatch.setattr(broker, "get_trading_client",
                        lambda *_: pytest.fail("must not log in"))
    monkeypatch.setattr(broker, "read_broker_name", lambda *_: "kotak")
    monkeypatch.setattr(tc, "resolve_instruments", lambda *a, **k: ([1], {1: "NIFTY 50"}))
    monkeypatch.setattr(tc, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["tick_capture", "--validate", "--cached-session"])
    assert tc.main() == 0
    assert seen == {"cached_only": True}
