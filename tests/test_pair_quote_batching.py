"""Pair runners must not run the Kotak account into quote 429s.

2026-10-07: the live persistent runner and the baseline paper runner share
one Kotak login, entered their tick loops at 09:15:00.000 and .001, and each
quoted every leg in its own request — ~14 and ~18 requests in the same
second, every minute, under per-process 8 req/s buckets. Kotak answered with
429s (26 that day; 79-99 on busier days), each blinding a pair for a minute.

These pin the fix: one batched request per tick per runner, a fallback that
is exactly the old behaviour, a snapshot that never outlives its tick, and
two runners whose ticks can never share a second.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import runners.run_paper_pairs as rpp
from tests.test_pair_trading import _make_strategy

LOG = logging.getLogger("test_pair_quote_batching")


class _Kotak:
    """Counts quote requests the way the 429 budget sees them."""

    def __init__(self, prices, fail=False):
        self.prices, self.fail, self.requests = prices, fail, []

    def quote(self, keys):
        self.requests.append(list(keys))
        if self.fail:
            raise RuntimeError("Kotak rate-limited (429)")
        return {k: {"last_price": self.prices[k]} for k in keys if k in self.prices}


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
    snap = rpp.prefetch_tick_quotes(pairs, kotak, LOG)
    assert len(kotak.requests) == 1
    assert sorted(kotak.requests[0]) == sorted(PRICES)
    for s in pairs:
        spread, prices = s._observe_spread()
        assert spread is not None
    assert len(kotak.requests) == 1, "every leg must come from the snapshot"
    assert snap["NFO:AAA26OCTFUT"] == 100.0


def test_a_failed_batch_falls_back_to_the_old_per_leg_quotes():
    kotak = _Kotak(PRICES, fail=True)
    s = _pair("AAA", "BBB", kotak)
    assert rpp.prefetch_tick_quotes([s], kotak, LOG) is None
    kotak.fail = False
    spread, _ = s._observe_spread()
    assert spread is not None
    assert len(kotak.requests) == 3          # the batch + one per leg, as before


def test_the_snapshot_never_outlives_its_tick():
    """Session-end flatten and order pricing must not trade off a stale tick."""
    kotak = _Kotak(PRICES)
    s = _pair("AAA", "BBB", kotak)
    rpp.prefetch_tick_quotes([s], kotak, LOG)
    s.set_tick_quotes(None)
    kotak.prices["NFO:AAA26OCTFUT"] = 101.0
    _, prices = s._observe_spread()
    assert prices["AAA"] == 101.0
    assert len(kotak.requests) == 3


def test_held_leg_contracts_are_in_the_batch():
    """A leg held over a roll is quoted on its own contract, not the front."""
    kotak = _Kotak(PRICES)
    s = _pair("AAA", "BBB", kotak)
    from strategies.pair_trading import PairLeg
    s.state.legs = [PairLeg(symbol="AAA", tradingsymbol="AAA26SEPFUT", lot_size=1,
                            quantity=1, entry_price=99.0, current_price=99.0,
                            expiry="2026-09-29")]
    assert "NFO:AAA26SEPFUT" in s.quote_keys()


def test_two_pair_runners_never_tick_in_the_same_second():
    live, paper = rpp.default_tick_offset("persistent"), rpp.default_tick_offset("baseline")
    assert live == 0 and paper == 30
    # Over a whole session of wall-clock slots, whatever each tick's work took.
    for start in (1_790_000_000.0, 1_790_000_013.7):
        t_live = start + rpp.seconds_to_next_slot(live, start)
        t_paper = start + rpp.seconds_to_next_slot(paper, start)
        for _ in range(375):                  # 09:15-15:30 at one per minute
            assert int(t_live) % 60 == 0 and int(t_paper) % 60 == 30
            t_live += rpp.seconds_to_next_slot(live, t_live + 4.2)   # 4.2 s of work
            t_live += 4.2
            t_paper += rpp.seconds_to_next_slot(paper, t_paper + 0.3) + 0.3


def test_slot_wait_is_never_a_busy_zero():
    assert 1.0 <= rpp.seconds_to_next_slot(0, 1_790_000_000.0) <= 60.0
    assert 1.0 <= rpp.seconds_to_next_slot(30, 1_790_000_029.5) <= 60.0
