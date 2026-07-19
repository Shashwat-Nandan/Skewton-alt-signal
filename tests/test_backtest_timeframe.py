"""Tests for the coarse-timeframe fail-loud warning (issue #63).

Encode the INTENT (Rule 9): a backtest running BELOW the 5-min standard must
announce it loudly so a reader never mistakes a daily result for a 5-min-grade
go/no-go, AND a backtest running at 5-min-or-finer must stay SILENT (a warning
that always fires is noise nobody reads). A test that merely called the function
would miss both failure modes: a helper that never warns, or one that warns on
compliant runs.
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.backtest_timeframe import STANDARD_TIMEFRAME, warn_coarse_timeframe


def test_daily_warns_loudly(caplog):
    with caplog.at_level(logging.WARNING):
        warned = warn_coarse_timeframe(
            "daily", backtest="bt_x", reason="no 5-min data exists")
    assert warned is True
    # The banner must name the standard, the offending resolution, and the reason
    # so the warning is self-explanatory in a log with no other context.
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(msgs) == 1
    text = msgs[0]
    assert "issue #63" in text
    assert STANDARD_TIMEFRAME in text
    assert "daily" in text
    assert "no 5-min data exists" in text


def test_eod_alias_warns(caplog):
    with caplog.at_level(logging.WARNING):
        warned = warn_coarse_timeframe("EOD", backtest="bt_x", reason="r")
    assert warned is True


def test_five_min_is_silent(caplog):
    with caplog.at_level(logging.WARNING):
        warned = warn_coarse_timeframe("5min", backtest="bt_x", reason="r")
    assert warned is False
    assert not caplog.records


def test_finer_than_5min_is_silent(caplog):
    with caplog.at_level(logging.WARNING):
        for tf in ("1min", "tick", "5minute"):
            assert warn_coarse_timeframe(tf, backtest="bt_x", reason="r") is False
    assert not caplog.records
