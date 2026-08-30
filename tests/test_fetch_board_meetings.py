"""
Tests for the NSE board-meeting (earnings calendar) cache.

This cache is the source of the 1,236-event study in
docs/research/pre-earnings-iv-crush-2026-08-29.md AND the live input to the
short-call runner's anti-look-ahead gate, so the properties pinned here are
about not corrupting history and not silently disarming that gate.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from market_data import fetch_board_meetings as fbm


def _row(sym, bm_date, ts, purpose="Financial Results"):
    return {"bm_symbol": sym, "bm_date": bm_date, "bm_timestamp": ts,
            "bm_purpose": purpose,
            "bm_desc": f"{sym} informed the Exchange about {purpose}"}


def test_write_cached_merges_instead_of_truncating(tmp_path, monkeypatch):
    """Finding 3. `sync` clips its fetch to the requested range, and the runner
    calls sync(today, today+45d) every session — so the current month is always
    fetched as a tail. An overwrite would delete every earlier meeting in that
    month on the first daily run, progressively shredding the record the
    research depends on."""
    monkeypatch.setattr(fbm, "CACHE_DIR", tmp_path)
    early = [_row("AAA", "05-Aug-2026", "01-Aug-2026 10:00:00"),
             _row("BBB", "07-Aug-2026", "02-Aug-2026 10:00:00")]
    fbm.write_cached(early, date(2026, 8, 1))
    # a later partial fetch covering only the tail of the month
    fbm.write_cached([_row("CCC", "29-Aug-2026", "26-Aug-2026 10:00:00")],
                     date(2026, 8, 1))
    kept = json.loads((tmp_path / "2026-08.json").read_text())
    assert {r["bm_symbol"] for r in kept} == {"AAA", "BBB", "CCC"}


def test_write_cached_refreshes_a_repeated_intimation(tmp_path, monkeypatch):
    monkeypatch.setattr(fbm, "CACHE_DIR", tmp_path)
    fbm.write_cached([_row("AAA", "05-Aug-2026", "01-Aug-2026 10:00:00")],
                     date(2026, 8, 1))
    fbm.write_cached([_row("AAA", "05-Aug-2026", "01-Aug-2026 10:00:00")],
                     date(2026, 8, 1))
    assert len(json.loads((tmp_path / "2026-08.json").read_text())) == 1


def test_dedupe_keeps_whole_rows_not_spliced_columns(tmp_path, monkeypatch):
    """Finding 4. groupby(...).last() takes the last NON-NULL value per column
    independently. A revised meeting whose timestamp failed to parse would
    contribute event_date while announced_at was inherited from an OLDER
    intimation — a stale-but-valid timestamp that waves the event straight
    through the strategy's 'was this public yet?' check."""
    monkeypatch.setattr(fbm, "CACHE_DIR", tmp_path)
    fbm.write_cached([
        _row("AAA", "10-Jun-2026", "01-Jun-2026 10:00:00"),
        _row("AAA", "12-Jun-2026", "bogus-timestamp"),          # revision, unparseable
    ], date(2026, 6, 1))
    cal = fbm.load_results_calendar(tmp_path)
    assert len(cal) == 1
    row = cal.iloc[0]
    # the operative (revised) meeting date wins ...
    assert row.event_date == pd.Timestamp("2026-06-12")
    # ... and it must NOT inherit the older row's timestamp, which would make a
    # stale-but-valid announced_at wave the event through the publicity check
    assert pd.isna(row.announced_at)


def test_empty_calendar_is_datetime_typed(tmp_path):
    """Finding 2. Callers do `cal.event_date.dt.normalize()`. An object-dtype
    empty frame raises AttributeError there, turning 'no earnings known' — the
    documented degraded mode — into a crash on any host without the cache."""
    cal = fbm.load_results_calendar(tmp_path / "does-not-exist")
    assert cal.empty
    cal.event_date.dt.normalize()          # must not raise
    (tmp_path / "empty").mkdir()
    fbm.load_results_calendar(tmp_path / "empty").event_date.dt.normalize()


def test_non_results_meetings_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(fbm, "CACHE_DIR", tmp_path)
    fbm.write_cached([
        _row("AAA", "10-Jun-2026", "01-Jun-2026 10:00:00"),
        _row("BBB", "11-Jun-2026", "01-Jun-2026 10:00:00", purpose="Fund Raising"),
    ], date(2026, 6, 1))
    cal = fbm.load_results_calendar(tmp_path)
    assert list(cal.symbol) == ["AAA"]


def test_unparseable_timestamps_are_surfaced(tmp_path, monkeypatch, caplog):
    """If NSE changes the timestamp format every announced_at becomes NaT and
    the anti-look-ahead gate becomes a silent no-op — exactly the contamination
    the research doc calls out as a t=6.25 phantom edge. It must be loud."""
    monkeypatch.setattr(fbm, "CACHE_DIR", tmp_path)
    fbm.write_cached([_row(s, "10-Jun-2026", "not-a-date") for s in ("A", "B", "C")],
                     date(2026, 6, 1))
    with caplog.at_level("ERROR"):
        fbm.load_results_calendar(tmp_path)
    assert any("anti-look-ahead" in r.message for r in caplog.records)
