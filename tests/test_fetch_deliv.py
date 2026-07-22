"""Tests for market_data/fetch_deliv.py — sec_bhavdata_full parsing quirks.

The sec_bhavdata_full CSV is messier than the UDiFF bhavcopy: whitespace-padded
headers/values, and ``" -"`` placeholders in the delivery columns for non-EQ
series. Each quirk gets a test because a silent mis-parse here poisons the
own-history percentile rank for a symbol (the whole signal), not just one row.
"""
from __future__ import annotations

import io
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from market_data import fetch_deliv
from market_data.fetch_deliv import _download_one, _parse_deliv_day, write_per_symbol

# Realistic fixture: padded header + padded values, an EQ row, a BE-series row
# with " -" delivery fields, a non-universe symbol, and a corrupt row where
# DELIV_QTY > TTL_TRD_QNTY.
SAMPLE_CSV = (
    "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,"
    " LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,"
    " NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
    "IRCTC, EQ, 21-Jul-2026, 700.0, 702.0, 710.0, 698.0, 705.0, 706.0, 704.0,"
    " 1000000, 7040.00, 50000, 630000, 63.00\n"
    "RELIANCE, EQ, 21-Jul-2026, 1300.0, 1301.0, 1310.0, 1295.0, 1305.0, 1306.0,"
    " 1304.0, 5000000, 65200.00, 250000, 2100000, 42.00\n"
    "SUZLON, BE, 21-Jul-2026, 50.0, 50.5, 51.0, 49.5, 50.2, 50.3, 50.1,"
    " 2000000, 1002.00, 90000, -, -\n"
    "OBSCURECO, EQ, 21-Jul-2026, 10.0, 10.1, 10.5, 9.9, 10.2, 10.3, 10.2,"
    " 300000, 30.60, 4000, 120000, 40.00\n"
    "BROKENROW, EQ, 21-Jul-2026, 20.0, 20.1, 20.5, 19.9, 20.2, 20.3, 20.2,"
    " 100000, 20.20, 2000, 150000, 150.00\n"
)
DAY = datetime(2026, 7, 21)
UNIVERSE = {"IRCTC", "RELIANCE", "SUZLON", "BROKENROW"}


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Point the raw and per-symbol dirs at tmp so tests never touch data_cache."""
    raw = tmp_path / "deliv_raw"
    out = tmp_path / "equity_delivery"
    monkeypatch.setattr(fetch_deliv, "RAW_DELIV_DIR", raw)
    monkeypatch.setattr(fetch_deliv, "DELIV_OUT_DIR", out)
    return raw, out


def _raw_df():
    df = pd.read_csv(io.StringIO(SAMPLE_CSV), dtype=str, skipinitialspace=True)
    df.columns = df.columns.str.strip()
    return df


def _stub_session(status=200, content=SAMPLE_CSV.encode()):
    s = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    s.get.return_value = resp
    return s


class TestParseDelivDay:

    def test_series_filter_and_padding(self):
        """Padded ' EQ' / ' BE' values must strip cleanly and only EQ survive:
        a BE row leaking through would inject ' -' strings into a numeric
        column and crash (or worse, silently NaN) the percentile build."""
        out = _parse_deliv_day(_raw_df(), DAY, UNIVERSE)
        assert set(out["symbol"]) == {"IRCTC", "RELIANCE"}
        assert "SUZLON" not in set(out["symbol"])

    def test_non_universe_symbol_excluded(self):
        out = _parse_deliv_day(_raw_df(), DAY, UNIVERSE)
        assert "OBSCURECO" not in set(out["symbol"])

    def test_numeric_coercion_and_schema(self):
        """String-typed raw columns must come out numeric with the canonical
        schema — consumers rank deliv_per numerically; a str column would
        rank lexicographically ('9.0' > '63.0') without erroring."""
        out = _parse_deliv_day(_raw_df(), DAY, UNIVERSE)
        assert list(out.columns) == ["date", "symbol", "traded_qty", "deliv_qty", "deliv_per"]
        irctc = out[out["symbol"] == "IRCTC"].iloc[0]
        assert irctc["deliv_per"] == pytest.approx(63.0)
        assert irctc["deliv_qty"] == pytest.approx(630000)
        assert pd.api.types.is_numeric_dtype(out["deliv_per"])
        assert (out["date"] == pd.Timestamp("2026-07-21")).all()

    def test_corrupt_row_dropped_loudly(self, caplog):
        """DELIV_QTY > TTL_TRD_QNTY (or pct > 100) is exchange-side corruption;
        it must be dropped AND logged (Rule 12), never clamped or kept."""
        with caplog.at_level("WARNING"):
            out = _parse_deliv_day(_raw_df(), DAY, UNIVERSE)
        assert "BROKENROW" not in set(out["symbol"])
        assert any("sanity" in r.message for r in caplog.records)

    def test_missing_columns_raise(self):
        """Schema drift on NSE's side must fail loud, not produce empties."""
        df = _raw_df().drop(columns=["DELIV_PER"])
        with pytest.raises(ValueError, match="missing columns"):
            _parse_deliv_day(df, DAY, UNIVERSE)


class TestDownloadOne:

    def test_200_parses_and_caches(self, isolated_dirs):
        raw_dir, _ = isolated_dirs
        out = _download_one(DAY, _stub_session())
        assert out is not None
        assert "SYMBOL" in out.columns  # padding stripped from header
        cached = raw_dir / "deliv_20260721.parquet"
        assert cached.exists()

    def test_cache_hit_short_circuits(self, isolated_dirs):
        """Second run must not re-hit NSE — backfill idempotency."""
        _download_one(DAY, _stub_session())
        s2 = _stub_session()
        out = _download_one(DAY, s2)
        assert out is not None
        s2.get.assert_not_called()

    def test_404_returns_none(self, isolated_dirs):
        assert _download_one(DAY, _stub_session(status=404)) is None

    def test_garbage_body_returns_none(self, isolated_dirs):
        """A 200 with undecodable bytes must not be cached as data."""
        raw_dir, _ = isolated_dirs
        out = _download_one(DAY, _stub_session(content=b"\xff\xfe garbage \x00"))
        assert out is None
        assert not (raw_dir / "deliv_20260721.parquet").exists()

    def test_html_block_page_not_cached(self, isolated_dirs):
        """An Akamai bot-challenge page is HTTP 200 UTF-8 HTML that pd.read_csv
        parses without error — pre-fix it was cached, and the table_exists
        short-circuit then pinned a permanently missing day for the whole
        universe (code-review 2026-07-22). Must return None and cache nothing."""
        raw_dir, _ = isolated_dirs
        html = b"<html><head><title>Access Denied</title></head>\n<body>denied</body></html>\n"
        out = _download_one(DAY, _stub_session(content=html))
        assert out is None
        assert not (raw_dir / "deliv_20260721.parquet").exists()
        # and a later run with a good body must succeed (day not poisoned)
        out2 = _download_one(DAY, _stub_session())
        assert out2 is not None
        assert (raw_dir / "deliv_20260721.parquet").exists()


class TestWritePerSymbol:

    def test_merge_dedupes_by_date(self, isolated_dirs):
        """Re-running a backfill over an overlapping range must not duplicate
        rows — duplicated dates would double-count days in the rolling
        percentile window."""
        _, out_dir = isolated_dirs
        day1 = _parse_deliv_day(_raw_df(), DAY, UNIVERSE)
        write_per_symbol(day1, out_dir)
        # same day again, plus nothing new
        write_per_symbol(day1, out_dir)
        back = pd.read_parquet(out_dir / "IRCTC.parquet")
        assert len(back) == 1
        assert list(back.columns) == ["date", "traded_qty", "deliv_qty", "deliv_per"]

    def test_new_day_appends(self, isolated_dirs):
        _, out_dir = isolated_dirs
        day1 = _parse_deliv_day(_raw_df(), DAY, UNIVERSE)
        write_per_symbol(day1, out_dir)
        day2 = day1.copy()
        day2["date"] = pd.Timestamp("2026-07-22")
        write_per_symbol(day2, out_dir)
        back = pd.read_parquet(out_dir / "IRCTC.parquet")
        assert len(back) == 2
        assert back["date"].is_monotonic_increasing
