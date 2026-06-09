"""Tests for fetch_bhavcopy.py — focused on the 2026-05-19 Kite-historical
fallback for today's missing F&O bhavcopy.

NSE publishes the F&O bhavcopy ~18:00–20:00 IST (sometimes later). The
weekday screen-pairs.timer fires at 19:00 IST, so on a fast night it tries
to fetch today's file before NSE has published it (404). The fallback
synthesises a UDiFF-shaped CSV from kite.historical_data so the pair
screener sees today's STF closes anyway. These tests pin the contract:

  - the synthesised CSV roundtrips through screen_pairs.load_front_month_panel
  - the synthesised CSV has the columns _parse_udiff_day's required-cols check needs
  - a Kite-fallback cache is sentinel-marked, so a later run upgrades it
    once NSE finally publishes
  - auth/instrument/historical_data failures all degrade to "return None"
    (no crash, same as a genuinely missing bhavcopy)
"""
from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import fetch_bhavcopy
from fetch_bhavcopy import _build_today_stfs_via_kite, _download_bhavcopy


# ──────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────

@pytest.fixture
def isolated_raw_dir(tmp_path, monkeypatch):
    """Point RAW_DIR at a tmp dir so tests don't touch the real cache."""
    monkeypatch.setattr(fetch_bhavcopy, "RAW_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def today():
    """Today's wall-clock date. MUST stay real-now: _download_bhavcopy only
    fires the Kite fallback when `date == datetime.now().date()` (the fallback
    is a today-only path), so a pinned past date would silently skip it. Fixture
    contract dates are therefore expressed relative to this (see `expiries`)."""
    return datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)


@pytest.fixture
def expiries(today):
    """Front / mid / far expiry ISO strings, relative to `today`.

    _build_today_stfs_via_kite keeps only `expiry >= today`, so fixture data
    must use real-future dates — hardcoded calendar dates made these tests a
    time bomb (issue #41): once the clock passed them, every contract read as
    expired and was dropped, yielding None / empty CSVs."""
    from datetime import timedelta
    return (
        (today + timedelta(days=14)).strftime("%Y-%m-%d"),
        (today + timedelta(days=44)).strftime("%Y-%m-%d"),
        (today + timedelta(days=74)).strftime("%Y-%m-%d"),
    )


def _mk_kite(instruments_list, candle_close_by_token):
    """Build a Mock kite whose instruments('NFO') returns the given list and
    whose historical_data returns a single day-bar with the matching close."""
    kite = MagicMock()
    kite.instruments.return_value = instruments_list
    def _historical(token, frm, to, interval):
        close = candle_close_by_token.get(int(token))
        if close is None:
            return []
        return [{"date": frm, "open": close, "high": close,
                 "low": close, "close": close, "volume": 0}]
    kite.historical_data.side_effect = _historical
    return kite


def _instruments_for(symbols_with_expiry):
    """Build a fake instruments('NFO') return value.
    `symbols_with_expiry` is a list of (name, expiry_iso, token, lot_size)."""
    return [
        {"name": s, "tradingsymbol": f"{s}26MAYFUT", "instrument_token": tok,
         "instrument_type": "FUT", "expiry": exp, "lot_size": lot}
        for (s, exp, tok, lot) in symbols_with_expiry
    ]


# ──────────────────────────────────────────────────────────
# _build_today_stfs_via_kite
# ──────────────────────────────────────────────────────────

class TestBuildTodayStfsViaKite:

    def test_returns_csv_with_required_columns(self, today, expiries):
        """Synth CSV must carry every column both consumers read:
        screen_pairs.load_front_month_panel reads TradDt/FinInstrmTp/TckrSymb/XpryDt/ClsPric;
        fetch_bhavcopy._parse_udiff_day's required-cols guard checks
        TckrSymb/FinInstrmTp/XpryDt/StrkPric/OptnTp/ClsPric/UndrlygPric/NewBrdLotQty.
        """
        front = expiries[0]
        instruments = _instruments_for([
            ("RELIANCE", front, 1001, 250),
            ("INFY", front, 1002, 400),
        ])
        kite = _mk_kite(instruments, {1001: 1327.00, 1002: 1450.50})
        auth_mock = MagicMock()
        auth_mock.get_kite.return_value = kite

        with patch("kite_auth.KiteAuthManager", return_value=auth_mock), \
             patch("screen_pairs.NIFTY_50", ["RELIANCE", "INFY"]):
            csv_bytes = _build_today_stfs_via_kite(today)

        assert csv_bytes is not None
        df = pd.read_csv(io.BytesIO(csv_bytes))
        required = {"TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "ClsPric",
                    "StrkPric", "OptnTp", "UndrlygPric", "NewBrdLotQty"}
        assert required.issubset(set(df.columns)), f"missing: {required - set(df.columns)}"
        assert set(df["FinInstrmTp"].unique()) == {"STF"}
        assert set(df["TckrSymb"]) == {"RELIANCE", "INFY"}
        rel = df[df["TckrSymb"] == "RELIANCE"].iloc[0]
        assert rel["ClsPric"] == pytest.approx(1327.00)
        assert rel["XpryDt"] == front

    def test_picks_front_month_when_multiple_expiries(self, today, expiries):
        """If an instrument has multiple expiries listed, only the
        nearest-future one should appear in the synth CSV."""
        front, mid, far = expiries
        instruments = _instruments_for([
            ("RELIANCE", front, 1001, 250),
            ("RELIANCE", mid, 1002, 250),
            ("RELIANCE", far, 1003, 250),
        ])
        kite = _mk_kite(instruments, {1001: 1327.00, 1002: 1335.00, 1003: 1340.00})
        auth_mock = MagicMock(); auth_mock.get_kite.return_value = kite

        with patch("kite_auth.KiteAuthManager", return_value=auth_mock), \
             patch("screen_pairs.NIFTY_50", ["RELIANCE"]):
            csv_bytes = _build_today_stfs_via_kite(today)

        df = pd.read_csv(io.BytesIO(csv_bytes))
        assert len(df) == 1
        assert df.iloc[0]["XpryDt"] == front
        assert df.iloc[0]["ClsPric"] == pytest.approx(1327.00)

    def test_skips_already_expired_contracts(self, today):
        """Contracts whose expiry < today's date should not appear."""
        from datetime import timedelta
        past = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        future = (today + timedelta(days=10)).strftime("%Y-%m-%d")
        instruments = _instruments_for([
            ("RELIANCE", past, 1001, 250),
            ("RELIANCE", future, 1002, 250),
        ])
        kite = _mk_kite(instruments, {1001: 1327.00, 1002: 1335.00})
        auth_mock = MagicMock(); auth_mock.get_kite.return_value = kite

        with patch("kite_auth.KiteAuthManager", return_value=auth_mock), \
             patch("screen_pairs.NIFTY_50", ["RELIANCE"]):
            csv_bytes = _build_today_stfs_via_kite(today)

        df = pd.read_csv(io.BytesIO(csv_bytes))
        assert len(df) == 1
        assert df.iloc[0]["XpryDt"] == future

    def test_auth_failure_returns_none(self, today):
        """Auth failure must not crash — just degrade to current
        no-bhavcopy behaviour."""
        with patch("kite_auth.KiteAuthManager",
                   side_effect=RuntimeError("totp expired")):
            assert _build_today_stfs_via_kite(today) is None

    def test_instruments_failure_returns_none(self, today):
        kite = MagicMock()
        kite.instruments.side_effect = RuntimeError("kite api down")
        auth_mock = MagicMock(); auth_mock.get_kite.return_value = kite
        with patch("kite_auth.KiteAuthManager", return_value=auth_mock):
            assert _build_today_stfs_via_kite(today) is None

    def test_no_nifty50_futures_returns_none(self, today):
        """Defensive: if NFO dump has no NIFTY-50 futures (shouldn't happen
        in production but possible during a market structure change), return
        None rather than synthesise an empty CSV that downstream consumers
        would misinterpret as "no data today"."""
        kite = _mk_kite(instruments_list=[], candle_close_by_token={})
        auth_mock = MagicMock(); auth_mock.get_kite.return_value = kite
        with patch("kite_auth.KiteAuthManager", return_value=auth_mock):
            assert _build_today_stfs_via_kite(today) is None

    def test_individual_historical_data_failure_is_skipped_not_fatal(self, today, expiries):
        """If one symbol's historical_data 5xx's, the rest should still be
        fetched — one flaky symbol can't sink the whole synthesis."""
        front = expiries[0]
        instruments = _instruments_for([
            ("RELIANCE", front, 1001, 250),
            ("INFY", front, 1002, 400),
        ])
        kite = MagicMock()
        kite.instruments.return_value = instruments
        def _hist(token, *a, **k):
            if int(token) == 1001:
                raise RuntimeError("kite hiccup")
            return [{"date": today, "open": 1450, "high": 1450,
                     "low": 1450, "close": 1450, "volume": 0}]
        kite.historical_data.side_effect = _hist
        auth_mock = MagicMock(); auth_mock.get_kite.return_value = kite

        with patch("kite_auth.KiteAuthManager", return_value=auth_mock), \
             patch("screen_pairs.NIFTY_50", ["RELIANCE", "INFY"]):
            csv_bytes = _build_today_stfs_via_kite(today)

        df = pd.read_csv(io.BytesIO(csv_bytes))
        assert len(df) == 1
        assert df.iloc[0]["TckrSymb"] == "INFY"


# ──────────────────────────────────────────────────────────
# _download_bhavcopy — fallback wiring + sentinel upgrade
# ──────────────────────────────────────────────────────────

class TestDownloadBhavcopyFallback:

    def _stub_404_session(self):
        s = MagicMock()
        resp = MagicMock(); resp.status_code = 404
        s.get.return_value = resp
        return s

    def _stub_zip_session(self, csv_bytes_inside_zip):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("BhavCopy.csv", csv_bytes_inside_zip)
        s = MagicMock()
        resp = MagicMock(); resp.status_code = 200; resp.content = buf.getvalue()
        s.get.return_value = resp
        return s

    def test_authoritative_cache_hit_short_circuits(self, isolated_raw_dir, today):
        """If a cached CSV exists and there's no sentinel, return it without
        hitting NSE or Kite."""
        yyyymmdd = today.strftime("%Y%m%d")
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        cache_file.write_text("AUTHORITATIVE_NSE_CONTENT")
        s = self._stub_404_session()
        out = _download_bhavcopy(today, s)
        assert out == b"AUTHORITATIVE_NSE_CONTENT"
        s.get.assert_not_called()

    def test_nse_404_today_triggers_kite_fallback(self, isolated_raw_dir, today, expiries):
        """On NSE 404 for today, the Kite fallback should fire and the
        cache should be written with a sentinel marker."""
        instruments = _instruments_for([("RELIANCE", expiries[0], 1001, 250)])
        kite = _mk_kite(instruments, {1001: 1327.00})
        auth_mock = MagicMock(); auth_mock.get_kite.return_value = kite

        s = self._stub_404_session()
        yyyymmdd = today.strftime("%Y%m%d")
        with patch("kite_auth.KiteAuthManager", return_value=auth_mock), \
             patch("screen_pairs.NIFTY_50", ["RELIANCE"]):
            out = _download_bhavcopy(today, s)

        assert out is not None
        assert b"RELIANCE" in out
        assert b"STF" in out
        # Both cache file AND sentinel must exist
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        sentinel = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.kite-fallback"
        assert cache_file.exists()
        assert sentinel.exists()

    def test_nse_404_for_past_date_does_not_call_kite(self, isolated_raw_dir):
        """Backfill of an older missing day must not silently consume the
        Kite quota — Kite historical for 'last Wednesday' is rarely what we
        want; bhavcopy is the canonical source for past days."""
        from datetime import timedelta
        past_date = datetime.now() - timedelta(days=7)
        s = self._stub_404_session()
        with patch("fetch_bhavcopy._build_today_stfs_via_kite") as mock_fallback:
            out = _download_bhavcopy(past_date, s)
        assert out is None
        mock_fallback.assert_not_called()

    def test_sentinel_cache_is_upgraded_when_nse_finally_publishes(
        self, isolated_raw_dir, today,
    ):
        """The sentinel forces a fresh NSE attempt on the next run; if NSE
        now returns 200, the cache is overwritten with the authoritative
        bhavcopy and the sentinel is removed."""
        yyyymmdd = today.strftime("%Y%m%d")
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        sentinel = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.kite-fallback"
        cache_file.write_text("STALE_KITE_FALLBACK_CONTENT")
        sentinel.write_text("synthesised_via_kite_historical_data\n")

        s = self._stub_zip_session(b"AUTHORITATIVE_NSE_CONTENT")
        out = _download_bhavcopy(today, s)
        assert out == b"AUTHORITATIVE_NSE_CONTENT"
        assert cache_file.read_bytes() == b"AUTHORITATIVE_NSE_CONTENT"
        assert not sentinel.exists()

    def test_sentinel_cache_kept_when_nse_still_404(
        self, isolated_raw_dir, today,
    ):
        """If NSE still 404s, a sentinel-marked cache from an earlier run
        is returned as-is rather than re-spending Kite quota on the same
        day's data."""
        yyyymmdd = today.strftime("%Y%m%d")
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        sentinel = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.kite-fallback"
        cache_file.write_text("EXISTING_KITE_FALLBACK_CONTENT")
        sentinel.write_text("synthesised_via_kite_historical_data\n")

        s = self._stub_404_session()
        with patch("fetch_bhavcopy._build_today_stfs_via_kite") as mock_fallback:
            out = _download_bhavcopy(today, s)
        assert out == b"EXISTING_KITE_FALLBACK_CONTENT"
        mock_fallback.assert_not_called()


# ──────────────────────────────────────────────────────────
# End-to-end: synth CSV roundtrips through the screener loader
# ──────────────────────────────────────────────────────────

class TestSynthCsvIsScreenerCompatible:

    def test_load_front_month_panel_consumes_synth_csv(self, tmp_path, today, expiries):
        """The whole point of the fallback is that screen_pairs.py reads
        the synth CSV cleanly. Verify end-to-end."""
        from screen_pairs import load_front_month_panel

        # Synthesise today's CSV via the fallback
        instruments = _instruments_for([
            ("RELIANCE", expiries[0], 1001, 250),
            ("INFY", expiries[0], 1002, 400),
        ])
        kite = _mk_kite(instruments, {1001: 1327.00, 1002: 1450.50})
        auth_mock = MagicMock(); auth_mock.get_kite.return_value = kite
        with patch("kite_auth.KiteAuthManager", return_value=auth_mock), \
             patch("screen_pairs.NIFTY_50", ["RELIANCE", "INFY"]):
            csv_bytes = _build_today_stfs_via_kite(today)
        assert csv_bytes is not None

        # Write into a tmp raw_dir as bhavcopy_fo_YYYYMMDD.csv (today's date)
        yyyymmdd = today.strftime("%Y%m%d")
        (tmp_path / f"bhavcopy_fo_{yyyymmdd}.csv").write_bytes(csv_bytes)

        panel = load_front_month_panel(
            ["RELIANCE", "INFY"], raw_dir=tmp_path, min_coverage=0.0,
        )
        assert "RELIANCE" in panel.columns
        assert "INFY" in panel.columns
        assert panel.shape[0] == 1
        assert panel["RELIANCE"].iloc[0] == pytest.approx(1327.00)
        assert panel["INFY"].iloc[0] == pytest.approx(1450.50)
