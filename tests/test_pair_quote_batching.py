"""Pair runners must not run the Kotak account into quote 429s — and the
batching that prevents it must never price a decision on an old quote.

2026-10-07: the live persistent runner and the baseline paper runner share
one Kotak login, entered their tick loops at 09:15:00.000 and .001, and each
quoted every leg in its own request — ~14 and ~18 requests in the same
second, every minute. Kotak answered with 429s (26 that day; 79-99 on busier
days), each blinding a pair for a minute.

The PR #12 review then showed the first fix could price trades on 40-60 s old
snapshots, burst 2N requests into a 429, fail every tick on one bad contract,
skip whole ticks, and still collide with the dispersion runner. Each test
pins one of those.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import runners.run_paper_dispersion as rpd
import runners.run_paper_pairs as rpp
from tests.test_pair_trading import _make_strategy

LOG = logging.getLogger("test_pair_quote_batching")


class _Kotak:
    """Counts quote requests the way the 429 budget sees them. Like the real
    adapter, a batch containing a contract with no LTP fails as a whole."""

    def __init__(self, prices, rate_limited=False, bad=()):
        self.prices, self.rate_limited, self.bad = dict(prices), rate_limited, set(bad)
        self.requests = []

    def quote(self, keys):
        self.requests.append(list(keys))
        if self.rate_limited:
            raise RuntimeError("Kotak rate-limited (429) on GET .../quotes")
        missing = [k for k in keys if k in self.bad or k not in self.prices]
        if missing:
            raise RuntimeError(f"Kotak quote for {missing[0]} had no LTP")
        return {k: {"last_price": self.prices[k]} for k in keys}


def _pair(a, b, client):
    s = _make_strategy(mode="live", spread_history=[(-1) ** i * 5.0 for i in range(30)])
    s.symbol_a, s.symbol_b = a, b
    s._cached_futures = {
        a: {"tradingsymbol": f"{a}26OCTFUT", "lot_size": 1, "expiry": "2026-10-27", "instrument_token": 1},
        b: {"tradingsymbol": f"{b}26OCTFUT", "lot_size": 1, "expiry": "2026-10-27", "instrument_token": 2},
    }
    s.client = client
    return s


PRICES = {f"NFO:{x}26OCTFUT": p for x, p in
          (("AAA", 100.0), ("BBB", 50.0), ("CCC", 30.0), ("DDD", 10.0), ("EEE", 7.0))}


def test_one_request_per_tick_for_every_pair():
    kotak = _Kotak(PRICES)
    # BBB is in two pairs: one key, one slot in the request.
    pairs = [_pair("AAA", "BBB", kotak), _pair("CCC", "BBB", kotak), _pair("DDD", "EEE", kotak)]
    snap, status = rpp.prefetch_tick_quotes(pairs, kotak, LOG)
    assert status == "ok" and len(kotak.requests) == 1
    assert sorted(kotak.requests[0]) == sorted(PRICES)
    for s in pairs:
        assert s._observe_spread()[0] is not None
    assert len(kotak.requests) == 1, "every leg must come from the snapshot"


def test_a_decision_never_reads_a_snapshot_older_than_its_limit():
    """Review: orders and fill polling take ~10 s a leg; a later pair in the
    same tick must not decide on a tick-start price."""
    kotak = _Kotak(PRICES)
    s = _pair("AAA", "BBB", kotak)
    rpp.prefetch_tick_quotes([s], kotak, LOG)
    s.set_tick_quotes(s._tick_quotes, time.monotonic() - (s.TICK_QUOTE_MAX_AGE_S + 1))
    kotak.prices["NFO:AAA26OCTFUT"] = 101.0
    _, prices = s._observe_spread()
    assert prices["AAA"] == 101.0
    assert len(kotak.requests) == 3          # batch + two fresh legs


def test_a_429_skips_the_tick_instead_of_bursting():
    """Review: falling back to 2N single requests into a throttling endpoint
    turns one 429 into many."""
    kotak = _Kotak(PRICES, rate_limited=True)
    pairs = [_pair("AAA", "BBB", kotak), _pair("CCC", "DDD", kotak)]
    _, status = rpp.prefetch_tick_quotes(pairs, kotak, LOG)
    assert status == "rate_limited"
    for s in pairs:
        assert s._observe_spread() == (None, {})
    assert len(kotak.requests) == 1, "no per-leg burst after a 429"


def test_one_bad_contract_is_isolated_not_fatal():
    """Review: one contract with no LTP used to fail the batch every tick."""
    kotak = _Kotak(PRICES, bad={"NFO:EEE26OCTFUT"})
    pairs = [_pair("AAA", "BBB", kotak), _pair("CCC", "DDD", kotak), _pair("EEE", "AAA", kotak)]
    snap, status = rpp.prefetch_tick_quotes(pairs, kotak, LOG)
    assert status == "ok"
    assert "NFO:EEE26OCTFUT" not in snap
    assert set(snap) == set(PRICES) - {"NFO:EEE26OCTFUT"}
    assert len(kotak.requests) <= 7          # 1 + 2·log2(5) bisection, not 2N+1
    assert pairs[0]._observe_spread()[0] is not None


def test_the_snapshot_never_outlives_its_tick():
    kotak = _Kotak(PRICES)
    s = _pair("AAA", "BBB", kotak)
    rpp.prefetch_tick_quotes([s], kotak, LOG)
    s.set_tick_quotes(None)
    kotak.prices["NFO:AAA26OCTFUT"] = 101.0
    assert s._observe_spread()[1]["AAA"] == 101.0


def test_only_front_month_contracts_are_batched():
    """Review: held-leg contracts were never read from the snapshot and an
    expired one could fail the whole batch."""
    from strategies.pair_trading import PairLeg
    s = _pair("AAA", "BBB", _Kotak(PRICES))
    s.state.legs = [PairLeg(symbol="AAA", tradingsymbol="AAA26SEPFUT", lot_size=1,
                            quantity=1, entry_price=99.0, current_price=99.0,
                            expiry="2026-09-29")]
    assert s.quote_keys() == ["NFO:AAA26OCTFUT", "NFO:BBB26OCTFUT"]


def test_three_runners_on_one_login_use_three_different_seconds():
    live = rpp.default_tick_offset("persistent")
    paper = rpp.default_tick_offset("baseline")
    assert {live, paper, rpd.TICK_SECOND} == {0, 30, 15}


def test_slots_hold_across_a_session_whatever_the_work_time():
    for offset in (0, 30):
        t = 1_790_000_013.7
        t += rpp.seconds_to_next_slot(offset, t)
        for work in [4.2, 0.3, 59.4] * 125:          # a session of ticks
            assert round(t) % 60 == offset
            end = t + work
            t = end + rpp.seconds_to_next_slot(offset, end)


def test_a_tick_ending_just_before_its_slot_does_not_skip_a_minute():
    """Review: a tick ending at :59.4 used to sleep 60.6 s, skipping a tick."""
    now = 1_790_000_039.4                    # epoch second ...39.4 → :59.4 on the minute grid
    wait = rpp.seconds_to_next_slot(0, now)
    assert round((now + wait)) % 60 == 0 and wait < 1.0


def test_dispersion_ticks_on_its_own_second():
    nxt = rpd.next_tick_slot(datetime(2026, 10, 28, 15, 0, 15, 200000))
    assert nxt == datetime(2026, 10, 28, 15, 1, 15)
    assert rpd.next_tick_slot(datetime(2026, 10, 28, 15, 0, 3)) == datetime(2026, 10, 28, 15, 0, 15)
