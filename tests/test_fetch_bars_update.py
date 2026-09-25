"""Token changes in `fetch_bars --update` must not orphan the stored series.

WHY: Market Profile and the equity swing read bars through
`bars_universe.instrument_token`. Kite tokens and Kotak pSymbols differ, so
the first update after the broker switch used to repoint the universe before
any bars existed under the new id. `latest_bar_ts` then missed the history,
the refill fell through to one 55-day chunk, and the old rows were never
read again — including when the Kotak fetch itself failed and left an empty
series.
"""
from datetime import datetime

import pytest

from backend import bars as bars_db
from market_data.fetch_bars import cmd_update
from market_data.history_limits import chunk_days


OLD_TOKEN = 111
NEW_TOKEN = 999
SYMBOL = "RELIANCE"
OLD_TS = "2026-01-05T09:15:00"
LATEST_TS = "2026-09-20T15:15:00"


@pytest.fixture
def fresh_db(tmp_path):
    from backend import db as backend_db
    backend_db.reset_for_tests(tmp_path / "bars.db")
    backend_db.init_schema()
    yield backend_db
    backend_db.reset_for_tests()


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr("market_data.fetch_bars.time.sleep", lambda *_a, **_k: None)


class _Kite:
    def __init__(self, token, candles=None, fail=False):
        self.token = token
        self.candles = candles if candles is not None else []
        self.fail = fail
        self.calls = []

    def instruments(self, exchange):
        assert exchange == "NSE"
        return [{
            "tradingsymbol": SYMBOL,
            "instrument_token": self.token,
            "name": "Reliance Industries",
        }]

    def historical_data(self, token, start, end, interval):
        self.calls.append((token, start, end, interval))
        if self.fail:
            raise RuntimeError("historical down")
        return list(self.candles)


def _seed_history():
    bars_db.upsert_universe(SYMBOL, OLD_TOKEN, "NSE", "Reliance Industries")
    bars_db.insert_bars(OLD_TOKEN, 30, [
        (OLD_TS, 10.0, 11.0, 9.0, 10.5, 100),
        (LATEST_TS, 12.0, 13.0, 11.0, 12.5, 110),
    ])
    # A different interval must not ride along: readers of 30-min bars
    # would otherwise see a foreign candle under the new token.
    bars_db.insert_bars(OLD_TOKEN, 5, [
        (OLD_TS, 1.0, 1.0, 1.0, 1.0, 1),
    ])
    bars_db.mark_updated(SYMBOL)


def _new_candle():
    return [{
        "date": datetime(2026, 9, 24, 15, 15),
        "open": 14.0, "high": 15.0, "low": 13.0, "close": 14.5, "volume": 50,
    }]


class TestTokenChangeKeepsHistory:
    def test_copies_existing_bars_before_repointing(self, fresh_db, no_sleep):
        """The new token must carry the January bar, and the incremental
        request must start at the copied latest bar — not a 55-day refill."""
        _seed_history()
        kite = _Kite(NEW_TOKEN, candles=_new_candle())
        cmd_update(kite)

        row = bars_db.get_universe_row(SYMBOL)
        assert row["instrument_token"] == NEW_TOKEN
        stored = bars_db.get_bars(NEW_TOKEN, 30)
        assert [b["ts"] for b in stored][0] == OLD_TS
        assert stored[-1]["ts"].startswith("2026-09-24T15:15:00")
        assert bars_db.count_bars(NEW_TOKEN, 5) == 0
        assert row["earliest_bar_ts"] == OLD_TS

        assert kite.calls, "update never asked the broker for the new bars"
        token, start, _end, interval = kite.calls[0]
        assert token == NEW_TOKEN
        assert interval == "30minute"
        assert start == "2026-09-20"

    def test_failed_fetch_after_copy_does_not_empty_the_series(self, fresh_db, no_sleep):
        """A Kotak error after the copy must leave the copied history in
        place. The old bug repointed first, then the failed fetch left the
        symbol with an empty series."""
        _seed_history()
        kite = _Kite(NEW_TOKEN, fail=True)
        cmd_update(kite)

        row = bars_db.get_universe_row(SYMBOL)
        assert row["instrument_token"] == NEW_TOKEN
        assert bars_db.count_bars(NEW_TOKEN, 30) == 2
        assert row["earliest_bar_ts"] == OLD_TS
        assert bars_db.count_bars(OLD_TOKEN, 30) == 2

    def test_same_token_fetches_forward_from_the_latest_bar(self, fresh_db, no_sleep):
        _seed_history()
        kite = _Kite(OLD_TOKEN, candles=_new_candle())
        cmd_update(kite)

        assert bars_db.get_universe_row(SYMBOL)["instrument_token"] == OLD_TOKEN
        assert kite.calls[0][0] == OLD_TOKEN
        assert kite.calls[0][1] == "2026-09-20"
        assert bars_db.count_bars(NEW_TOKEN, 30) == 0


class TestTokenChangeWithoutHistory:
    def test_failed_backfill_leaves_the_old_token(self, fresh_db, no_sleep):
        """Nothing to copy and the broker returns nothing: do not repoint.
        The next run must still be able to find this symbol under the id
        the operator backfilled."""
        bars_db.upsert_universe(SYMBOL, OLD_TOKEN, "NSE", "Reliance Industries")
        kite = _Kite(NEW_TOKEN, fail=True)
        cmd_update(kite)

        assert bars_db.get_universe_row(SYMBOL)["instrument_token"] == OLD_TOKEN
        assert bars_db.count_bars(NEW_TOKEN, 30) == 0
        assert kite.calls, "backfill was not attempted"
        assert kite.calls[0][0] == NEW_TOKEN

    def test_backfill_uses_the_30minute_cap_then_repoints(self, fresh_db, no_sleep):
        """With no stored series the window is the 30-minute cap (89 days,
        one under Kotak's 90), not the 55-day incremental chunk."""
        bars_db.upsert_universe(SYMBOL, OLD_TOKEN, "NSE", "Reliance Industries")
        kite = _Kite(NEW_TOKEN, candles=_new_candle())
        cmd_update(kite)

        assert bars_db.get_universe_row(SYMBOL)["instrument_token"] == NEW_TOKEN
        assert bars_db.count_bars(NEW_TOKEN, 30) == 1
        first = datetime.strptime(kite.calls[0][1], "%Y-%m-%d").date()
        span = (datetime.now().date() - first).days
        assert span == chunk_days("30minute")
        assert span > 55
