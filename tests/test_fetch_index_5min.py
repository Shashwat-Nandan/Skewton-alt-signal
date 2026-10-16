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


def _weekdays(start: date, end: date) -> list:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


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


def test_missing_and_short_sessions_are_judged_against_the_exchange_calendar():
    """The calendar is the index's own daily candles, not holidays.csv
    (which lists 2026 only and would call every 2024-25 holiday missing)."""
    mon, tue, wed, thu, fri = (date(2025, 9, 15) + timedelta(days=i) for i in range(5))
    candles = _session(mon) + _session(wed, n=40) + _session(thu)
    rep = f.session_report(f.candles_to_frame(candles), [mon, tue, wed, thu, fri], end=fri)
    assert rep["missing"] == [tue, fri]          # a gap and a lost trailing day
    assert rep["short"] == {wed: 40}
    # Tuesday a holiday: not in the calendar, so not missing.
    rep = f.session_report(f.candles_to_frame(candles), [mon, wed, thu], end=thu)
    assert rep["missing"] == [] and rep["short"] == {wed: 40}


def test_sessions_before_the_first_candle_are_reported_not_hidden():
    first, later = date(2025, 9, 15), date(2025, 9, 16)
    rep = f.session_report(f.candles_to_frame(_session(later)), [first, later], end=later)
    assert rep["before_history"] == [first] and rep["missing"] == []


def test_the_1530_print_does_not_complete_a_short_session():
    """75 rows with the 15:30 print but a missing 10:00 bar is not a full day."""
    d = date(2025, 9, 15)
    rows = [c for c in _session(d, n=76) if c["date"].strftime("%H:%M") != "10:00"]
    assert len(rows) == 75
    assert f.regular_counts(f.candles_to_frame(rows))[d] == 74


def test_an_evening_only_session_is_special_not_missing():
    d = date(2024, 11, 1)                                  # Muhurat
    evening = [{"date": datetime(2024, 11, 1, 18, 0, tzinfo=IST) + timedelta(minutes=5 * i),
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0} for i in range(12)]
    rep = f.session_report(f.candles_to_frame(evening), [d], end=d)
    assert rep["special"] == [d] and rep["missing"] == [] and rep["short"] == {}


def test_inconsistent_candles_are_refused():
    bad = _session(date(2026, 9, 14), n=2)
    bad[1]["high"] = 90.0
    with pytest.raises(ValueError, match="inconsistent"):
        f.candles_to_frame(bad)


def test_an_open_outside_the_bar_is_clamped_loudly_not_refused(caplog):
    """Kotak served 2026-07-03 09:15 with open 24375.65 above high 24338.75.
    Refusing it lost the whole fetch; keeping it would let the backtest fill a
    gap at a price the bar never printed. Clamp and warn; nothing else bends."""
    rows = _session(date(2026, 7, 3), n=2)
    rows[0].update(open=24375.65, high=24338.75, low=24295.45, close=24302.55)
    with caplog.at_level("WARNING"):
        out = f.candles_to_frame(rows)
    assert out.loc[0, "open"] == 24338.75 and out.loc[1, "open"] == 100.0
    assert "clamped" in caplog.text and "2026-07-03 09:15" in caplog.text
    rows[1]["close"] = 200.0                     # close outside: still refused
    with pytest.raises(ValueError, match="inconsistent"):
        f.candles_to_frame(rows)


def test_a_month_is_complete_only_with_every_session_full(tmp_path):
    """Re-runs must not spend the shared Kotak quota on stored months, and
    must not freeze a partial day as stored."""
    sessions = _weekdays(date(2025, 6, 1), date(2025, 6, 30))
    frame = f.candles_to_frame([c for d in sessions for c in _session(d)])
    f.write_months(frame, tmp_path)
    assert f.month_complete(tmp_path, 2025, 6, sessions, end=date(2026, 10, 15))
    # A short day: incomplete, so the next run fetches it again.
    short = f.candles_to_frame([c for d in sessions for c in _session(d, 76 if d != sessions[3] else 40)])
    f.month_path(tmp_path, 2025, 6).unlink()
    f.write_months(short, tmp_path)
    assert not f.month_complete(tmp_path, 2025, 6, sessions, end=date(2026, 10, 15))
    # A holiday absent from the calendar does not block completeness.
    assert f.month_complete(tmp_path, 2025, 6, [d for d in sessions if d != sessions[3]],
                            end=date(2026, 10, 15))


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


class _FakeKotak:
    """Daily calendar for every weekday; 5-minute data stops early."""

    def __init__(self, sessions, five_until):
        self.sessions, self.five_until = sessions, five_until
        self.calls = []

    def instruments(self, exchange):
        return [{"tradingsymbol": "NIFTY 50", "instrument_token": -900001},
                {"tradingsymbol": "NIFTY", "instrument_token": 26000}]

    def historical_data(self, token, frm, to, interval):
        self.calls.append(interval)
        days = [d for d in self.sessions if frm <= d <= to]
        if interval == "day":
            return [{"date": datetime(d.year, d.month, d.day, tzinfo=IST), "open": 1.0,
                     "high": 1.0, "low": 1.0, "close": 1.0} for d in days]
        days = [d for d in days if d <= self.five_until]
        if not days:
            raise BrokerOrderError("Kotak historical_data returned no candles")
        return [c for d in days for c in _session(d)]


def test_a_lost_trailing_window_fails_the_run(monkeypatch, tmp_path):
    """Before the fix, a span that came back short at the end shrank
    silently and the run exited 0 on a truncated history."""
    sessions = _weekdays(date(2025, 6, 2), date(2025, 7, 31))
    fake = _FakeKotak(sessions, five_until=date(2025, 7, 10))

    class _Evening(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 8, 1, 18, 0, tzinfo=IST)

    monkeypatch.setattr(f, "datetime", _Evening)
    monkeypatch.setattr(f._time, "sleep", lambda s: None)
    monkeypatch.setattr("market_data.fetch_5min_stf.get_cached_client", lambda *_: fake)
    args = ["--start", "2025-06-02", "--end", "2025-07-31", "--out-dir", str(tmp_path)]
    assert f.main(args) == 1
    # Complete the data: the same command now passes, and a re-run does
    # not fetch any 5-minute window again.
    fake.five_until = date(2025, 7, 31)
    assert f.main(args) == 0
    fake.calls.clear()
    assert f.main(args) == 0
    assert "5minute" not in fake.calls
