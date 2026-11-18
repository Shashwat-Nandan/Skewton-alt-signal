"""The trading runners' start gate now lives in one helper. These pin the policy
each runner relied on inline: a stale or malformed calendar refuses to start,
a weekend or holiday is a logged no-op unless --force."""
from __future__ import annotations

import logging
from datetime import date

import pytest

import core.runner_common as rc

LOG = logging.getLogger("t")
FRESH = "\n".join(["2026-01-26", "2026-03-04", "2026-03-31", "2026-04-03", "2026-05-01",
                    "2026-08-15", "2026-10-02", "2026-11-10", "2026-12-25", "2027-01-26"]) + "\n"


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    def write(text):
        path = tmp_path / "holidays.csv"
        path.write_text(text)
        monkeypatch.setattr(rc, "HOLIDAYS_PATH", path)
    return write


def test_a_trading_day_runs(calendar):
    calendar(FRESH)
    assert rc.require_trading_day(date(2026, 11, 11), LOG) is True


def test_a_holiday_is_a_no_op_unless_forced(calendar, caplog):
    calendar(FRESH)
    with caplog.at_level("INFO"):
        assert rc.require_trading_day(date(2026, 11, 10), LOG) is False
    assert "No-op" in caplog.text
    assert rc.require_trading_day(date(2026, 11, 10), LOG, force=True) is True


def test_a_stale_calendar_refuses_to_start(calendar):
    calendar("\n".join(FRESH.split("\n")[:-2]) + "\n")
    with pytest.raises(RuntimeError, match="Refusing to start"):
        rc.require_trading_day(date(2026, 12, 1), LOG)


def test_a_malformed_calendar_refuses_to_start(calendar):
    """run_equity_swing used to skip a bad line silently; one rule for all now."""
    calendar(FRESH + "2026-13-05\n")
    with pytest.raises(ValueError, match="malformed"):
        rc.require_trading_day(date(2026, 11, 11), LOG)


def test_every_trading_runner_uses_the_shared_gate():
    """An inline copy is how the recorder lost the freshness check."""
    from pathlib import Path
    runners = Path(rc.__file__).resolve().parent.parent / "runners"
    inline = [p.name for p in runners.glob("*.py")
              if "assert_holiday_data_fresh(" in p.read_text()
              or "_is_trading_day(" in p.read_text()]
    assert inline == []
