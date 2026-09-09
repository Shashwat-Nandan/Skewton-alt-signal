"""Weekly universe drift report (issue #226).

WHY (Rule 9): this job exists because universe decay is invisible at runtime.
A drift report that reports "no drift" for the wrong reason is worse than no
report — it converts an unnoticed problem into an actively believed all-clear.
The Kite-fallback guard below is the whole point: those days carry ONLY the
NIFTY_50 names, so reconciling the list against one compares the list to
itself and finds nothing wrong, forever.
"""
import logging

import pandas as pd
import pytest

from scripts import reconcile_universe as R


def _write_day(tmp_path, day, symbols, fallback=False):
    rows = [{"FinInstrmTp": "STF", "TckrSymb": s, "XpryDt": "2026-10-27",
             "TradDt": f"{day[:4]}-{day[4:6]}-{day[6:]}", "ClsPric": 100.0}
            for s in symbols]
    # An index-future row that must never be counted as a stock underlying.
    rows.append({"FinInstrmTp": "IDF", "TckrSymb": "NIFTY", "XpryDt": "2026-10-27",
                 "TradDt": f"{day[:4]}-{day[4:6]}-{day[6:]}", "ClsPric": 100.0})
    p = tmp_path / f"bhavcopy_fo_{day}.parquet"
    pd.DataFrame(rows).to_parquet(p)
    if fallback:
        p.with_suffix(".kite-fallback").touch()
    return p


def _board(n, prefix="SYM"):
    return [f"{prefix}{i:03d}" for i in range(n)]


class TestFallbackBlindness:
    def test_a_fallback_day_is_skipped_for_the_day_before(self, tmp_path, caplog):
        # 09-08 is a real fallback day in this repo's archive: 48 underlyings
        # against 210 the session before.
        _write_day(tmp_path, "20260907", _board(150))
        _write_day(tmp_path, "20260908", _board(48), fallback=True)
        with caplog.at_level(logging.WARNING, logger="reconcile_universe"):
            board, day = R.latest_board(tmp_path)
        assert day == "20260907", "must not reconcile against a filtered day"
        assert len(board) == 150
        assert any("kite-fallback" in r.getMessage() for r in caplog.records)

    def test_a_short_day_is_skipped_even_without_the_marker(self, tmp_path):
        # A marker file is easy to lose; a silent all-clear is the failure this
        # script exists to prevent, so the size floor is a second line.
        _write_day(tmp_path, "20260907", _board(150))
        _write_day(tmp_path, "20260908", _board(48))       # no marker
        _, day = R.latest_board(tmp_path)
        assert day == "20260907"

    def test_all_days_unusable_raises_rather_than_reporting_all_clear(self, tmp_path):
        _write_day(tmp_path, "20260908", _board(48), fallback=True)
        with pytest.raises(RuntimeError, match="No usable bhavcopy day"):
            R.latest_board(tmp_path)

    def test_index_futures_are_not_stock_underlyings(self, tmp_path):
        _write_day(tmp_path, "20260907", _board(150))
        board, _ = R.latest_board(tmp_path)
        assert "NIFTY" not in board


class TestDriftDirections:
    def test_departure_is_reported(self, tmp_path):
        _write_day(tmp_path, "20260907", _board(150))
        rep = R.reconcile(universe=["SYM001", "DELISTED"], raw_dir=tmp_path)
        assert rep["departures"] == ["DELISTED"]

    def test_addition_is_reported(self, tmp_path):
        # Both directions, deliberately: decay was the bug we found, but a
        # board that grows without review is the same blindness pointing the
        # other way.
        _write_day(tmp_path, "20260907", ["AAA", "BBB"] + _board(150))
        rep = R.reconcile(universe=["AAA"], raw_dir=tmp_path)
        assert "BBB" in rep["additions"]
        assert rep["departures"] == []

    def test_a_renamed_symbol_is_not_a_departure(self, tmp_path):
        # The board says LTM, a stale list says LTIM: that is a list to tidy,
        # not a missing security, and it must not read as decay.
        _write_day(tmp_path, "20260907", ["LTM"] + _board(150))
        rep = R.reconcile(universe=["LTIM"], raw_dir=tmp_path)
        assert rep["departures"] == []
        assert rep["stale_aliases"] == ["LTIM"], \
            "but it IS worth flagging so the list gets the current ticker"

    # NOTE: there is deliberately no "the real NIFTY_50 has no departures"
    # test here. Checking the list against the live board needs the bhavcopy
    # archive, which CI does not have, so such a test can only skip there —
    # and it cost the suite its 8-skip budget when it was written that way
    # (CI run 34337696152). Synthesising the board from the list under test
    # instead makes it tautological, which is what the review of PR #227
    # caught. The right shape for that check is the weekly reconcile JOB,
    # which runs on the host where the archive lives; what belongs in the
    # suite is the archive-free invariant, and that is
    # tests/test_universe.py::TestTheListItself.


class TestStaleBoard:
    """WHY (Rule 9): a report that says 'no departures' about a board from
    three weeks ago is a believed all-clear, the failure mode this whole job
    exists to prevent. This repo has already lost 8 sessions to a host outage
    (2026-08-20→28)."""

    def test_a_stale_board_is_flagged(self, tmp_path, caplog):
        _write_day(tmp_path, "20250101", _board(150))       # long past
        with caplog.at_level(logging.WARNING, logger="reconcile_universe"):
            rep = R.reconcile(universe=["SYM001"], raw_dir=tmp_path)
        assert rep["board_is_stale"] is True
        assert rep["board_age_days"] > R.MAX_BOARD_AGE_DAYS
        assert any("STALE BOARD" in r.getMessage() for r in caplog.records)

    def test_a_fresh_board_is_not_flagged(self, tmp_path, caplog):
        from datetime import date as _date
        _write_day(tmp_path, _date.today().strftime("%Y%m%d"), _board(150))
        with caplog.at_level(logging.WARNING, logger="reconcile_universe"):
            rep = R.reconcile(universe=["SYM001"], raw_dir=tmp_path)
        assert rep["board_is_stale"] is False
        assert not any("STALE BOARD" in r.getMessage() for r in caplog.records)


class TestUnreadableFile:
    def test_an_unreadable_newest_file_costs_one_day_not_the_run(self, tmp_path):
        # A truncated parquet used to raise past main()'s RuntimeError handler,
        # killing the unit with no report at all (review of PR #227).
        _write_day(tmp_path, "20260907", _board(150))
        (tmp_path / "bhavcopy_fo_20260908.parquet").write_bytes(b"not a parquet")
        board, day = R.latest_board(tmp_path)
        assert day == "20260907" and len(board) == 150
