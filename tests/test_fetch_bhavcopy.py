"""Tests for market_data/fetch_bhavcopy.py — focused on the 2026-05-19 Kite-historical
fallback for today's missing F&O bhavcopy.

NSE publishes the F&O bhavcopy ~18:00–20:00 IST (sometimes later). The
weekday screen-pairs.timer fires at 19:00 IST, so on a fast night it tries
to fetch today's file before NSE has published it (404). The fallback
synthesises a UDiFF-shaped frame from kite.historical_data so the pair
screener sees today's STF closes anyway. These tests pin the contract:

  - the synthesised day (cached as parquet) roundtrips through
    screen_pairs.load_front_month_panel
  - the synthesised frame has the columns _parse_udiff_day's required-cols check needs
  - a Kite-fallback cache is sentinel-marked, so a later run upgrades it
    once NSE finally publishes
  - auth/instrument/historical_data failures all degrade to "return None"
    (no crash, same as a genuinely missing bhavcopy)
"""
from __future__ import annotations

import io
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from market_data import fetch_bhavcopy
from market_data.fetch_bhavcopy import _build_today_stfs_via_kite, _download_bhavcopy


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
        """Synth frame must carry every column both consumers read:
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
        with patch("core.broker.get_trading_client", return_value=kite), \
             patch("core.screen_pairs.NIFTY_50", ["RELIANCE", "INFY"]):
            df = _build_today_stfs_via_kite(today)

        assert df is not None
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
        nearest-future one should appear in the synth frame."""
        front, mid, far = expiries
        instruments = _instruments_for([
            ("RELIANCE", front, 1001, 250),
            ("RELIANCE", mid, 1002, 250),
            ("RELIANCE", far, 1003, 250),
        ])
        kite = _mk_kite(instruments, {1001: 1327.00, 1002: 1335.00, 1003: 1340.00})
        with patch("core.broker.get_trading_client", return_value=kite), \
             patch("core.screen_pairs.NIFTY_50", ["RELIANCE"]):
            df = _build_today_stfs_via_kite(today)

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
        with patch("core.broker.get_trading_client", return_value=kite), \
             patch("core.screen_pairs.NIFTY_50", ["RELIANCE"]):
            df = _build_today_stfs_via_kite(today)

        assert len(df) == 1
        assert df.iloc[0]["XpryDt"] == future

    def test_auth_failure_returns_none(self, today):
        """Auth failure must not crash — just degrade to current
        no-bhavcopy behaviour."""
        with patch("core.broker.get_trading_client",
                   side_effect=RuntimeError("totp expired")):
            assert _build_today_stfs_via_kite(today) is None

    def test_instruments_failure_returns_none(self, today):
        kite = MagicMock()
        kite.instruments.side_effect = RuntimeError("kite api down")
        with patch("core.broker.get_trading_client", return_value=kite):
            assert _build_today_stfs_via_kite(today) is None

    def test_no_nifty50_futures_returns_none(self, today):
        """Defensive: if NFO dump has no NIFTY-50 futures (shouldn't happen
        in production but possible during a market structure change), return
        None rather than synthesise an empty frame that downstream consumers
        would misinterpret as "no data today"."""
        kite = _mk_kite(instruments_list=[], candle_close_by_token={})
        with patch("core.broker.get_trading_client", return_value=kite):
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
        with patch("core.broker.get_trading_client", return_value=kite), \
             patch("core.screen_pairs.NIFTY_50", ["RELIANCE", "INFY"]):
            df = _build_today_stfs_via_kite(today)

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
        """If a cached day exists (parquet, or a legacy pre-migration CSV)
        and there's no sentinel, return it without hitting NSE or Kite."""
        yyyymmdd = today.strftime("%Y%m%d")
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        cache_file.write_text("TckrSymb,ClsPric\nAUTH,1.0\n")
        s = self._stub_404_session()
        out = _download_bhavcopy(today, s)
        assert out is not None and out.iloc[0]["TckrSymb"] == "AUTH"
        s.get.assert_not_called()

    def test_authoritative_parquet_cache_hit_short_circuits(self, isolated_raw_dir, today):
        """Same short-circuit for the parquet cache new downloads write."""
        yyyymmdd = today.strftime("%Y%m%d")
        pd.DataFrame({"TckrSymb": ["AUTH"], "ClsPric": [1.0]}).to_parquet(
            isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.parquet", index=False)
        s = self._stub_404_session()
        out = _download_bhavcopy(today, s)
        assert out is not None and out.iloc[0]["TckrSymb"] == "AUTH"
        s.get.assert_not_called()

    def test_raw_cache_round_trips_full_frame(self, isolated_raw_dir, today):
        """The parquet raw cache replaced a byte-preserving CSV cache; this
        pins its successor invariant: EVERY column (str-forced or inferred,
        including numeric-looking strings and untouched extras) comes back
        from the cache exactly as parsing NSE's bytes produced it — not just
        one spot-checked cell."""
        import io as _io
        csv_bytes = (
            b"TckrSymb,FinInstrmTp,FinInstrmNm,XpryDt,ClsPric,OpnIntrst,ISIN\n"
            b"360ONE,STF,360ONE26JULFUT,2026-07-30,1050.5,1200,INE466L01038\n"
            b"123,STF,123FUT,2026-07-30,10.0,0,INE000A01010\n"  # numeric-looking symbol
        )
        s = self._stub_zip_session(csv_bytes)
        out = _download_bhavcopy(today, s)

        expected = pd.read_csv(_io.BytesIO(csv_bytes), dtype=fetch_bhavcopy.RAW_STR_COLS)
        yyyymmdd = today.strftime("%Y%m%d")
        cached = pd.read_parquet(isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.parquet")
        pd.testing.assert_frame_equal(out, expected)
        pd.testing.assert_frame_equal(cached, expected)
        # the str-forcing must hold even for the all-numeric symbol
        assert cached["TckrSymb"].tolist() == ["360ONE", "123"]

    def test_nse_404_today_triggers_kite_fallback(self, isolated_raw_dir, today, expiries):
        """On NSE 404 for today, the Kite fallback should fire and the
        cache should be written with a sentinel marker."""
        instruments = _instruments_for([("RELIANCE", expiries[0], 1001, 250)])
        kite = _mk_kite(instruments, {1001: 1327.00})

        s = self._stub_404_session()
        yyyymmdd = today.strftime("%Y%m%d")
        with patch("core.broker.get_trading_client", return_value=kite), \
             patch("core.screen_pairs.NIFTY_50", ["RELIANCE"]):
            out = _download_bhavcopy(today, s)

        assert out is not None
        assert "RELIANCE" in set(out["TckrSymb"])
        assert set(out["FinInstrmTp"]) == {"STF"}
        # Both cache file AND sentinel must exist
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.parquet"
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
        with patch("market_data.fetch_bhavcopy._build_today_stfs_via_kite") as mock_fallback:
            out = _download_bhavcopy(past_date, s)
        assert out is None
        mock_fallback.assert_not_called()

    def test_sentinel_cache_is_upgraded_when_nse_finally_publishes(
        self, isolated_raw_dir, today,
    ):
        """The sentinel forces a fresh NSE attempt on the next run; if NSE
        now returns 200, the cache is overwritten with the authoritative
        bhavcopy, the sentinel is removed, and the stale synthetic CSV is
        removed so it can't shadow the authoritative parquet."""
        yyyymmdd = today.strftime("%Y%m%d")
        legacy_csv = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        sentinel = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.kite-fallback"
        legacy_csv.write_text("TckrSymb,ClsPric\nSTALE,0.0\n")
        sentinel.write_text("synthesised_via_kite_historical_data\n")

        s = self._stub_zip_session(b"TckrSymb,ClsPric\nAUTH,1.0\n")
        out = _download_bhavcopy(today, s)
        assert out.iloc[0]["TckrSymb"] == "AUTH"
        back = pd.read_parquet(isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.parquet")
        assert back.iloc[0]["TckrSymb"] == "AUTH"
        assert not sentinel.exists()
        assert not legacy_csv.exists()

    def test_sentinel_cache_kept_when_nse_still_404(
        self, isolated_raw_dir, today,
    ):
        """If NSE still 404s, a sentinel-marked cache from an earlier run
        is returned as-is rather than re-spending Kite quota on the same
        day's data."""
        yyyymmdd = today.strftime("%Y%m%d")
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        sentinel = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.kite-fallback"
        cache_file.write_text("TckrSymb,ClsPric\nKITE_FB,1.0\n")
        sentinel.write_text("synthesised_via_kite_historical_data\n")

        s = self._stub_404_session()
        with patch("market_data.fetch_bhavcopy._build_today_stfs_via_kite") as mock_fallback:
            out = _download_bhavcopy(today, s)
        assert out.iloc[0]["TckrSymb"] == "KITE_FB"
        mock_fallback.assert_not_called()


# ──────────────────────────────────────────────────────────
# End-to-end: synth CSV roundtrips through the screener loader
# ──────────────────────────────────────────────────────────

class TestSynthCsvIsScreenerCompatible:

    def test_load_front_month_panel_consumes_synth_day(self, tmp_path, today, expiries):
        """The whole point of the fallback is that core/screen_pairs.py reads
        the synth day cleanly. Verify end-to-end via the parquet cache the
        fallback now writes."""
        from core.screen_pairs import load_front_month_panel

        # Synthesise today's frame via the fallback
        instruments = _instruments_for([
            ("RELIANCE", expiries[0], 1001, 250),
            ("INFY", expiries[0], 1002, 400),
        ])
        kite = _mk_kite(instruments, {1001: 1327.00, 1002: 1450.50})
        with patch("core.broker.get_trading_client", return_value=kite), \
             patch("core.screen_pairs.NIFTY_50", ["RELIANCE", "INFY"]):
            df = _build_today_stfs_via_kite(today)
        assert df is not None

        # Write into a tmp raw_dir as bhavcopy_fo_YYYYMMDD.parquet (today's date)
        yyyymmdd = today.strftime("%Y%m%d")
        df.to_parquet(tmp_path / f"bhavcopy_fo_{yyyymmdd}.parquet", index=False)

        panel = load_front_month_panel(
            ["RELIANCE", "INFY"], raw_dir=tmp_path, min_coverage=0.0,
        )
        assert "RELIANCE" in panel.columns
        assert "INFY" in panel.columns
        assert panel.shape[0] == 1
        assert panel["RELIANCE"].iloc[0] == pytest.approx(1327.00)
        assert panel["INFY"].iloc[0] == pytest.approx(1450.50)
