"""The 5-minute NIFTY fetcher feeds a pre-registered backtest whose holdout
is scored once. A silently missing week, a re-fetch that hammers the
runners' shared Kotak quota, or an unflagged short session would all
change the verdict without anyone seeing why. These pin the loud paths."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from core.broker.errors import BrokerNetworkError, BrokerOrderError
from market_data import fetch_index_5min as f

IST = f.IST


def _session(d: date, n: int = 76) -> list:
    start = datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)
    return [{"date": start + timedelta(minutes=5 * i), "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5} for i in range(n)]


def test_chunks_cover_the_span_without_overlap_or_exceeding_the_cap():
    chunks = f.chunk_ranges(date(2025, 1, 1), date(2025, 3, 15), 29)
    assert chunks[0][0] == date(2025, 1, 1) and chunks[-1][1] == date(2025, 3, 15)
    for (a, b), (c, _) in zip(chunks, chunks[1:]):
        assert c == b + timedelta(days=1)
    assert all((b - a).days + 1 <= 29 for a, b in chunks)


def test_missing_and_short_sessions_are_reported_inside_the_span():
    mon, tue, wed, thu = (date(2026, 9, 14) + timedelta(days=i) for i in range(4))
    candles = _session(mon) + _session(wed, n=40) + _session(thu)   # Tuesday absent
    missing, short = f.session_report(f.candles_to_frame(candles), holidays=set())
    assert missing == [tue]
    assert short == {wed: 40}
    # A holiday is not missing.
    missing, _ = f.session_report(f.candles_to_frame(candles), holidays={tue})
    assert missing == []


def test_inconsistent_candles_are_refused():
    bad = _session(date(2026, 9, 14), n=2)
    bad[1]["high"] = 90.0
    with pytest.raises(ValueError, match="inconsistent"):
        f.candles_to_frame(bad)


def test_a_complete_month_is_not_fetched_again(tmp_path):
    """Re-runs must not spend the shared Kotak quota on stored months."""
    sessions = f.sessions_between(date(2025, 6, 1), date(2025, 6, 30), set())
    frame = f.candles_to_frame([c for d in sessions for c in _session(d)])
    f.write_months(frame, tmp_path)
    assert f.month_complete(tmp_path, 2025, 6, set(), today=date(2026, 10, 16))
    gap = frame[frame["ts"].dt.date != sessions[3]]
    f.month_path(tmp_path, 2025, 6).unlink()
    f.write_months(gap, tmp_path)
    assert not f.month_complete(tmp_path, 2025, 6, set(), today=date(2026, 10, 16))


class _FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def historical_data(self, token, frm, to, interval):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_429_backs_off_and_retries_and_no_candles_is_none(monkeypatch):
    sleeps = []
    monkeypatch.setattr(f._time, "sleep", sleeps.append)
    ok = _session(date(2026, 9, 14), n=3)
    client = _FakeClient([BrokerNetworkError("Kotak historical_data HTTP 429: busy"), ok])
    assert f.fetch_chunk(client, -1, date(2026, 9, 14), date(2026, 9, 14), delay=15) == ok
    assert 60 in sleeps and sleeps.count(15) == 2      # back-off, then paced

    empty = _FakeClient([BrokerOrderError("returned no candles")])
    assert f.fetch_chunk(empty, -1, date(2020, 1, 1), date(2020, 1, 2), delay=0) is None

    other = _FakeClient([BrokerNetworkError("HTTP 500")])
    with pytest.raises(BrokerNetworkError):
        f.fetch_chunk(other, -1, date(2026, 9, 14), date(2026, 9, 14), delay=0)


def test_refuses_during_market_hours_without_force(monkeypatch):
    """The runners share this Kotak quota; a daytime backfill 429s them."""
    assert f.in_quiet_window(datetime(2026, 10, 16, 10, 0, tzinfo=IST))
    assert not f.in_quiet_window(datetime(2026, 10, 16, 16, 0, tzinfo=IST))
    assert not f.in_quiet_window(datetime(2026, 10, 17, 10, 0, tzinfo=IST))   # Saturday

    class _Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, 10, 0, tzinfo=IST)

    monkeypatch.setattr(f, "datetime", _Morning)
    monkeypatch.setattr("market_data.fetch_5min_stf.get_cached_client",
                        lambda *_: pytest.fail("must not touch the broker"))
    assert f.main(["--start", "2026-01-01"]) == 2


def test_loaded_series_is_sorted_and_deduplicated(tmp_path):
    d = date(2026, 9, 14)
    frame = f.candles_to_frame(_session(d, n=5))
    f.write_months(frame, tmp_path)
    f.write_months(frame, tmp_path)      # same month again
    out = f.load_all(tmp_path)
    assert len(out) == 5 and out["ts"].is_monotonic_increasing
    assert isinstance(out["ts"].dtype, pd.DatetimeTZDtype)
