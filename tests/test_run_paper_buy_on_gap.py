"""Tests for run_paper_buy_on_gap pure helpers (no auth / no market).

Two behaviours that would silently corrupt a session if they regressed:
  - the intraday book is NEVER carried overnight: a restored position dated
    before today must be dropped (not resurrected and then flattened at a
    stale mark);
  - fetch_today_quotes maps kite.quote's NSE:<sym> / ohlc shape into the
    {open, ltp, low} the strategy expects, and degrades to {} (not a crash)
    on a total quote failure so the heartbeat can catch a dead token.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import run_paper_buy_on_gap as r
from strategies.buy_on_gap import BuyOnGapStrategy, GapPosition

LOG = logging.getLogger("test")


class _NullKite:
    pass


def _strategy():
    s = BuyOnGapStrategy(kite=_NullKite(), config_path="/dev/null", mode="paper")
    return s


def test_restore_drops_overnight_positions():
    today = date(2026, 4, 17)
    s = _strategy()
    # Prior blob: one position from today (intraday restart), one from a prior
    # session (must be dropped — never held overnight).
    s.positions["TODAY"] = GapPosition(
        symbol="TODAY", entry_dt=pd.Timestamp("2026-04-17"), entry_px=100.0,
        qty=10, stop_px=95.0, gap_ret=-0.03, gap_z=-1.5, rationale="x")
    s.positions["STALE"] = GapPosition(
        symbol="STALE", entry_dt=pd.Timestamp("2026-04-16"), entry_px=100.0,
        qty=10, stop_px=95.0, gap_ret=-0.03, gap_z=-1.5, rationale="x")
    blob = s.serialize_state()

    fresh = _strategy()
    r.restore_strategy(fresh, blob, today, LOG)
    assert "TODAY" in fresh.positions
    assert "STALE" not in fresh.positions


def test_fetch_today_quotes_maps_kite_shape():
    class FakeKite:
        def quote(self, keys):
            return {
                "NSE:INFY": {"last_price": 1492.0,
                             "ohlc": {"open": 1500.0, "high": 1505.0,
                                      "low": 1480.0, "close": 1510.0}},
                # missing ohlc/last_price for one symbol → dropped, not crashed
                "NSE:TCS": {},
            }
    out = r.fetch_today_quotes(FakeKite(), ["INFY", "TCS", "WIPRO"], LOG)
    assert out["INFY"] == {"open": 1500.0, "ltp": 1492.0, "low": 1480.0}
    # TCS had no fields → present but Nones; WIPRO absent entirely.
    assert "WIPRO" not in out


def test_fetch_today_quotes_total_failure_returns_empty():
    class DeadKite:
        def quote(self, keys):
            raise RuntimeError("token expired")
    assert r.fetch_today_quotes(DeadKite(), ["INFY"], LOG) == {}
