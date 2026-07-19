"""Tests for fetch_index_daily — the host-only index daily-close fetcher.

Rule 9: pin the bits that silently break and corrupt the gate's input. The
BANKNIFTY → "NIFTY BANK" alias is the whole reason this script exists (the gate
had no BANKNIFTY data); an unknown symbol must fail loud rather than fetch the
wrong instrument; and the candles must become a sorted, de-duplicated
(date, close) frame in exactly the shape `validate_kalman_trend` reads.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from market_data import fetch_index_daily as f


class _FakeKite:
    def instruments(self, exchange):
        assert exchange == "NSE"
        return [
            {"tradingsymbol": "NIFTY 50", "instrument_token": 256265},
            {"tradingsymbol": "NIFTY BANK", "instrument_token": 260105},
            {"tradingsymbol": "RELIANCE", "instrument_token": 111},
        ]

    def historical_data(self, token, frm, to, interval):
        assert interval == "day"
        # two overlapping candle dates to exercise de-dup, returned out of order.
        # Shaped like a real Kite candle (date/open/high/low/close/volume) — the
        # o/h/l are what a backtest needs to know whether a stop was TOUCHED
        # intrabar rather than merely closed beyond (#122).
        return [
            {"date": date(2026, 1, 2), "open": 100.5, "high": 102.0,
             "low": 100.0, "close": 101.0, "volume": 10},
            {"date": date(2026, 1, 1), "open": 99.0, "high": 100.5,
             "low": 98.0, "close": 100.0, "volume": 11},
            {"date": date(2026, 1, 2), "open": 100.5, "high": 102.0,
             "low": 100.0, "close": 101.0, "volume": 10},   # duplicate
        ]


def test_banknifty_resolves_to_nse_index_name():
    """The alias map must turn the F&O symbol BANKNIFTY into the NSE spot name
    'NIFTY BANK' — the gate's missing instrument."""
    assert f.resolve_index_token(_FakeKite(), "BANKNIFTY", None) == 260105


def test_explicit_nse_symbol_override_wins():
    assert f.resolve_index_token(_FakeKite(), "WHATEVER", "NIFTY 50") == 256265


def test_unknown_symbol_fails_loud():
    """An unrecognized index must raise (listing candidates), not silently grab
    the wrong token."""
    with pytest.raises(ValueError, match="could not resolve"):
        f.resolve_index_token(_FakeKite(), "NOTANINDEX", None)


def test_fetch_daily_candles_is_sorted_deduped_and_shaped():
    df = f.fetch_candles(_FakeKite(), 260105, date(2026, 1, 1), date(2026, 1, 2))
    # OHLC is persisted, not discarded (#122): without high/low a backtest cannot
    # tell a TOUCHED stop from a close beyond it. `close` keeps its name/position
    # so every existing reader (which selects by name) is unaffected.
    assert list(df.columns) == ["date", "open", "high", "low", "close"]
    assert len(df) == 2                                # duplicate dropped
    assert list(df["date"]) == [date(2026, 1, 1), date(2026, 1, 2)]  # sorted
    assert df["close"].tolist() == [100.0, 101.0]
    assert df["high"].tolist() == [100.5, 102.0]
    assert df["low"].tolist() == [98.0, 100.0]
    assert df["open"].tolist() == [99.0, 100.5]


def test_fetch_empty_range_fails_loud():
    class _Empty(_FakeKite):
        def historical_data(self, *a, **k):
            return []
    with pytest.raises(ValueError, match="no candles"):
        f.fetch_candles(_Empty(), 260105, date(2026, 1, 1), date(2026, 1, 3))
