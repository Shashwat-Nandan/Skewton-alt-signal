"""The Kotak quote poll runs beside the live pair runner and shares its
Kotak session and quote quota. These pin the ways it could hurt that
runner (a fresh login that replaces the shared token, a burst of calls
into a rate limit) and the ways it could fail quietly (a day of retries
that ends in an empty tape and exit 0)."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from core.broker.errors import (
    BrokerNetworkError,
    BrokerOrderError,
    BrokerQuoteMissingError,
    BrokerTokenError,
)
from market_data import tick_capture as tc


class _Quotes:
    """Raises like Kotak: the first key without an LTP fails the call."""

    def __init__(self, fail_with=None, missing=()):
        self.calls = 0
        self.fail_with, self.missing = fail_with, set(missing)

    def quote(self, keys):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        for k in keys:
            if k in self.missing:
                raise BrokerQuoteMissingError(f"Kotak quote for {k} had no LTP", k)
        return {k: {"last_price": 100.0} for k in keys}


PAIRS = [(i, f"NFO:NIFTY{i}CE") for i in range(8)]


@pytest.fixture(autouse=True)
def _quiet_log(monkeypatch):
    monkeypatch.setattr(tc, "_LOG", tc.logging.getLogger("test"))


def test_a_rate_limited_pass_is_one_call_not_a_split_storm():
    client = _Quotes(fail_with=BrokerNetworkError("Kotak rate-limited (429)"))
    with pytest.raises(BrokerNetworkError):
        tc._poll_quotes(client, PAIRS, set())
    assert client.calls == 1


def test_any_other_reject_is_one_call_too():
    """Kotak raises BrokerOrderError for a 4xx or a Not_Ok body, which is
    how a throttle can arrive. Only a named missing LTP is retried."""
    client = _Quotes(fail_with=BrokerOrderError("Kotak 400: too many requests"))
    with pytest.raises(BrokerOrderError):
        tc._poll_quotes(client, PAIRS, set())
    assert client.calls == 1


def test_a_dead_strike_costs_one_extra_call_once_not_every_pass():
    client = _Quotes(missing={"NFO:NIFTY3CE"})
    dead = set()
    rows = tc._poll_quotes(client, PAIRS, dead)
    assert sorted(r["instrument_token"] for r in rows) == [0, 1, 2, 4, 5, 6, 7]
    assert dead == {"NFO:NIFTY3CE"} and client.calls == 2
    tc._poll_quotes(client, PAIRS, dead)
    assert client.calls == 3


def test_many_dead_keys_fail_the_pass_instead_of_a_call_each():
    client = _Quotes(missing={key for _, key in PAIRS})
    with pytest.raises(BrokerQuoteMissingError):
        tc._poll_quotes(client, PAIRS, set())
    assert client.calls == tc.MAX_NEW_DEAD_PER_PASS + 1


def test_a_set_aside_key_rejoins_when_its_ltp_returns():
    client = _Quotes(missing={"NFO:NIFTY5CE"})
    dead = {"NFO:NIFTY3CE", "NFO:NIFTY5CE"}
    rows = tc._reprobe(client, PAIRS, dead)
    assert [r["instrument_token"] for r in rows] == [3]
    assert dead == {"NFO:NIFTY5CE"}


def _loop(monkeypatch, steps, stop_after):
    """Fake clock and file for _run_quote_poll: each time() call moves 1 s
    and the cutoff is at `stop_after`. Returns the list of sleeps."""
    sleeps, now = [], [0.0]

    def tick():
        now[0] += 1.0
        return now[0]

    monkeypatch.setattr(tc, "time", SimpleNamespace(time=tick, time_ns=lambda: 0,
                                                    sleep=sleeps.append))
    monkeypatch.setattr(tc, "_STOP_EPOCH", stop_after)
    monkeypatch.setattr(tc, "_TICK_COUNT", 0)
    monkeypatch.setattr(tc, "_OUT_FILE", SimpleNamespace(write=lambda _: None,
                                                         flush=lambda: None))
    calls = iter(steps)

    def poll(client, pairs, dead):
        step = next(calls, None)
        if isinstance(step, Exception):
            raise step
        return [{"instrument_token": 1}] if step == "row" else []

    monkeypatch.setattr(tc, "_poll_quotes", poll)
    return sleeps


def test_failed_passes_back_off_doubling_and_the_loop_continues(monkeypatch):
    sleeps = _loop(monkeypatch, [BrokerNetworkError("429")] * 3 + ["row"], 1e6)
    assert tc._run_quote_poll(object(), [], {}, poll_seconds=10.0)
    assert sleeps[:4] == [10.0, 20.0, 40.0, 10.0]


def test_backoff_stops_at_the_cutoff(monkeypatch):
    sleeps = _loop(monkeypatch, ["row", BrokerNetworkError("429")], 4.0)
    tc._run_quote_poll(object(), [], {}, poll_seconds=10.0)
    assert sleeps and all(s <= 2.0 for s in sleeps)


def test_a_day_of_failures_gives_up_and_is_not_a_success(monkeypatch):
    _loop(monkeypatch, [BrokerNetworkError("429")] * 50 + ["row"], 200.0)
    assert tc._run_quote_poll(object(), [], {}, poll_seconds=10.0) is False


def test_an_empty_tape_is_not_a_success(monkeypatch):
    _loop(monkeypatch, [None] * 5, 8.0)
    assert tc._run_quote_poll(object(), [], {}, poll_seconds=1.0) is False


def test_a_rejected_token_rereads_the_cache_not_a_login(monkeypatch):
    _loop(monkeypatch, [], 1e6)
    seen = []

    def poll(client, pairs, dead):
        seen.append(client)
        if len(seen) == 1:
            raise BrokerTokenError("403")
        monkeypatch.setattr(tc, "_STOP_EPOCH", 0.0)
        return [{"instrument_token": 1}]

    monkeypatch.setattr(tc, "_poll_quotes", poll)
    stale, fresh = object(), object()
    assert tc._run_quote_poll(stale, [], {}, 10.0, reload_client=lambda: fresh)
    assert seen == [stale, fresh]


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
