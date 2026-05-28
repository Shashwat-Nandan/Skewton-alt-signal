"""M-O1 / M-O2 / M-O4 — holidays.csv lint, disk-space pre-flight, TZ assert."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch
from collections import namedtuple

import pytest


# ──────────────────────────────────────────────────────────
# M-O1 — holidays.csv lint (precise error on bad line)
# ──────────────────────────────────────────────────────────

def test_load_holidays_raises_with_line_number_on_typo(tmp_path):
    from run_paper_pairs import load_holidays
    bad = tmp_path / "holidays.csv"
    bad.write_text("\n".join([
        "# header comment",
        "2026-01-26,Republic Day",
        "2026-13-05,Bogus Holiday",  # invalid month
        "2026-10-21,Diwali",
    ]))
    with pytest.raises(ValueError, match=r"holidays\.csv:3:.*2026-13-05"):
        load_holidays(bad)


def test_load_holidays_tolerates_header_row(tmp_path):
    from run_paper_pairs import load_holidays
    f = tmp_path / "holidays.csv"
    f.write_text("date,description\n2026-01-26,Republic Day\n")
    days = load_holidays(f)
    assert len(days) == 1


def test_load_holidays_ignores_comments_and_blanks(tmp_path):
    from run_paper_pairs import load_holidays
    f = tmp_path / "holidays.csv"
    f.write_text("\n".join([
        "",
        "# top-of-file note",
        "2026-01-26",
        "",
        "# mid-file",
        "2026-10-21,Diwali",
    ]))
    assert load_holidays(f) == {
        __import__("datetime").date(2026, 1, 26),
        __import__("datetime").date(2026, 10, 21),
    }


# ──────────────────────────────────────────────────────────
# M-O2 — disk-space pre-flight
# ──────────────────────────────────────────────────────────

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def test_disk_space_ok_when_above_thresholds(tmp_path):
    from run_paper_pairs import assert_disk_space_ok
    fake = DiskUsage(total=100 * 1024**3, used=10 * 1024**3, free=90 * 1024**3)
    with patch("shutil.disk_usage", return_value=fake):
        # Must not raise
        assert_disk_space_ok([tmp_path], logging.getLogger("test"))


def test_disk_space_refuses_below_mb_threshold(tmp_path):
    from run_paper_pairs import assert_disk_space_ok
    # 100MB free against a 500MB minimum — breach
    fake = DiskUsage(total=100 * 1024**3, used=99 * 1024**3 + 900 * 1024**2,
                     free=100 * 1024**2)
    with patch("shutil.disk_usage", return_value=fake):
        with pytest.raises(RuntimeError, match="M-O2"):
            assert_disk_space_ok([tmp_path], logging.getLogger("test"))


def test_disk_space_refuses_below_pct_threshold(tmp_path):
    from run_paper_pairs import assert_disk_space_ok
    # 600MB free but only 0.6% — breach even though MB threshold passes
    fake = DiskUsage(total=100 * 1024**3, used=99 * 1024**3 + 400 * 1024**2,
                     free=600 * 1024**2)
    with patch("shutil.disk_usage", return_value=fake):
        with pytest.raises(RuntimeError, match="M-O2"):
            assert_disk_space_ok([tmp_path], logging.getLogger("test"))


def test_disk_space_dedupes_same_mountpoint(tmp_path, caplog):
    from run_paper_pairs import assert_disk_space_ok
    caplog.set_level(logging.INFO)
    fake = DiskUsage(total=100 * 1024**3, used=10 * 1024**3, free=90 * 1024**3)
    with patch("shutil.disk_usage", return_value=fake):
        # Two distinct paths on the same (mocked) volume — exactly one
        # success log message expected.
        assert_disk_space_ok([tmp_path / "a", tmp_path / "b"],
                              logging.getLogger("test"))
    success = [r for r in caplog.records if "Disk OK" in r.message]
    assert len(success) == 1


# ──────────────────────────────────────────────────────────
# M-O4 — TZ assertion
# ──────────────────────────────────────────────────────────

def test_tz_assert_accepts_ist():
    from run_paper_pairs import assert_timezone_ist
    with patch("time.tzname", ("IST", "IST")), \
         patch("time.daylight", 0):
        assert_timezone_ist(logging.getLogger("test"))  # must not raise


def test_tz_assert_accepts_offset_form():
    # Some glibc builds report "+0530" instead of "IST" when the zone
    # file isn't installed — accept both forms.
    from run_paper_pairs import assert_timezone_ist
    with patch("time.tzname", ("+0530", "+0530")), \
         patch("time.daylight", 0):
        assert_timezone_ist(logging.getLogger("test"))


def test_tz_assert_refuses_utc():
    from run_paper_pairs import assert_timezone_ist
    with patch("time.tzname", ("UTC", "UTC")), \
         patch("time.daylight", 0):
        with pytest.raises(RuntimeError, match="M-O4"):
            assert_timezone_ist(logging.getLogger("test"))
