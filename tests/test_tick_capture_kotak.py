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


def test_a_dead_strike_re_requests_only_its_own_batch():
    """Kotak quotes 50 keys a request and fails the whole call on one
    missing LTP. Batching here means the batches that came back are not
    fetched again."""
    pairs = [(i, f"NFO:K{i}") for i in range(120)]
    client = _Quotes(missing={"NFO:K110"})
    rows = tc._poll_quotes(client, pairs, set())
    assert len(rows) == 119
    assert client.calls == 3 + 1


def test_setting_many_keys_aside_is_progress_not_a_failed_pass():
    """At the open many far strikes can lack an LTP. Each pass spends at
    most MAX_NEW_DEAD_PER_PASS extra calls setting them aside and returns
    what it has; it must not raise, or the outage counter would end the
    day."""
    healthy = [(100 + i, f"NFO:OK{i}") for i in range(60)]
    client = _Quotes(missing={key for _, key in PAIRS})
    dead = set()
    rows = tc._poll_quotes(client, PAIRS + healthy, dead)   # batches of 50 and 18
    assert len(rows) == 18        # the first batch waits for the next pass
    assert len(dead) == tc.MAX_NEW_DEAD_PER_PASS
    calls = client.calls
    rows = tc._poll_quotes(client, PAIRS + healthy, dead)
    assert len(rows) == 60
    assert dead == {key for _, key in PAIRS}
    assert client.calls - calls == 2 + (8 - tc.MAX_NEW_DEAD_PER_PASS)


def test_a_re_check_is_bounded_and_walks_the_whole_set():
    dead = {key for _, key in PAIRS}
    client = _Quotes(missing=set(dead))
    _, nxt = tc._reprobe(client, PAIRS, dead, 0)
    assert client.calls <= tc.MAX_NEW_DEAD_PER_PASS + 1
    client.missing = set()
    rows, _ = tc._reprobe(client, PAIRS, dead, nxt)
    assert len(rows) == tc.MAX_NEW_DEAD_PER_PASS
    assert len(dead) == len(PAIRS) - tc.MAX_NEW_DEAD_PER_PASS
    rows, _ = tc._reprobe(client, PAIRS, dead, 0)
    assert not dead


def test_a_set_aside_key_rejoins_when_its_ltp_returns():
    client = _Quotes(missing={"NFO:NIFTY5CE"})
    dead = {"NFO:NIFTY3CE", "NFO:NIFTY5CE"}
    rows, _ = tc._reprobe(client, PAIRS, dead)
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


def test_a_long_outage_keeps_polling_but_fails_the_day(monkeypatch):
    """A 7-minute Kotak outage must not cost the rest of the day's tape,
    and must still exit 1 so the failure alert fires."""
    _loop(monkeypatch, [BrokerNetworkError("429")] * tc.MAX_CONSECUTIVE_FAILURES
          + ["row"] * 3, 1e6)
    monkeypatch.setattr(tc, "_STOP_EPOCH", 30.0)
    assert tc._run_quote_poll(object(), [], {}, poll_seconds=1.0) is False
    assert tc._TICK_COUNT == 3        # recorded after the outage


def test_a_short_outage_is_a_good_day(monkeypatch):
    _loop(monkeypatch, [BrokerNetworkError("429")] * 3 + ["row"], 10.0)
    assert tc._run_quote_poll(object(), [], {}, poll_seconds=1.0) is True


def test_an_empty_tape_is_not_a_success(monkeypatch):
    _loop(monkeypatch, [None] * 5, 8.0)
    assert tc._run_quote_poll(object(), [], {}, poll_seconds=1.0) is False


def test_a_failed_re_check_does_not_cost_the_live_pass(monkeypatch):
    _loop(monkeypatch, [], 1e6)
    monkeypatch.setattr(tc, "REPROBE_S", 0.0)
    passes = []

    def poll(client, pairs, dead):
        dead.add("NFO:DEAD")
        passes.append(1)
        if len(passes) == 3:
            monkeypatch.setattr(tc, "_STOP_EPOCH", 0.0)
        return [{"instrument_token": 1}]

    def reprobe(*a):
        raise BrokerNetworkError("429")

    monkeypatch.setattr(tc, "_poll_quotes", poll)
    monkeypatch.setattr(tc, "_reprobe", reprobe)
    assert tc._run_quote_poll(object(), [], {}, poll_seconds=1.0) is True
    assert len(passes) == 3


def _main(monkeypatch, tmp_path, argv, profile_error=None):
    import core.broker as broker

    class _Client:
        def profile(self):
            if profile_error:
                raise profile_error
            return {"user_name": "u", "user_id": "x"}

    seen = {}

    def market(path, cached_only=False):
        seen["cached_only"] = cached_only
        return _Client()

    monkeypatch.setattr(broker, "get_market_client", market)
    monkeypatch.setattr(broker, "get_trading_client", lambda *_: (
        _Client() if profile_error else pytest.fail("must not log in")))
    monkeypatch.setattr(broker, "read_broker_name", lambda *_: "kotak")
    monkeypatch.setattr(tc, "resolve_instruments", lambda *a, **k: ([1], {1: "NIFTY 50"}))
    monkeypatch.setattr(tc, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["tick_capture", "--validate", *argv])
    return tc.main(), seen


def test_a_stale_cached_trade_token_does_not_cost_the_day(monkeypatch, tmp_path):
    """Kotak quotes use the consumer key only; profile() uses the trade
    token. A stale cached session must not stop a quote-only capture."""
    rc, _ = _main(monkeypatch, tmp_path, ["--cached-session"], BrokerTokenError("403"))
    assert rc == 0
    # What Kotak actually sent for a day-old token on 2026-10-20.
    rc, _ = _main(monkeypatch, tmp_path, ["--cached-session"],
                  BrokerOrderError("invalid session token"))
    assert rc == 0
    with pytest.raises(BrokerTokenError):
        _main(monkeypatch, tmp_path, [], BrokerTokenError("403"))


def test_cached_session_never_starts_a_login(monkeypatch, tmp_path):
    rc, seen = _main(monkeypatch, tmp_path, ["--cached-session"])
    assert rc == 0
    assert seen == {"cached_only": True}
