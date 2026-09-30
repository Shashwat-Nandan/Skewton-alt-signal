"""Tests for market_data/fetch_bhavcopy.py — the same-day Kotak historical
fallback for a missing F&O bhavcopy.

NSE publishes the F&O bhavcopy ~18:00–20:00 IST (sometimes later). The
weekday screen-pairs.timer fires at 19:00 IST, so on a fast night it tries
to fetch today's file before NSE has published it (404). The fallback
synthesises a UDiFF-shaped frame from Kotak historical_data so the pair
screener sees today's STF closes anyway. These tests pin the contract:

  - the synthesised day (cached as parquet) roundtrips through
    screen_pairs.load_front_month_panel
  - the synthesised frame has the columns _parse_udiff_day's required-cols check needs
  - a broker-fallback cache is sentinel-marked, so a later run upgrades it
    once NSE finally publishes
  - a leftover .kite-fallback marker is still non-authoritative
  - credential/instrument/historical_data failures all degrade to "return None"
    (no crash, same as a genuinely missing bhavcopy)
  - the client is built from the consumer key and does not call login()
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
from market_data.fetch_bhavcopy import (
    _build_today_stfs_via_kotak,
    _download_bhavcopy,
    _kotak_market_client,
)


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
    fires the Kotak fallback when `date == datetime.now().date()` (the fallback
    is a today-only path), so a pinned past date would silently skip it. Fixture
    contract dates are therefore expressed relative to this (see `expiries`)."""
    return datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)


@pytest.fixture
def expiries(today):
    """Front / mid / far expiry ISO strings, relative to `today`.

    _build_today_stfs_via_kotak keeps only `expiry >= today`, so fixture data
    must use real-future dates — hardcoded calendar dates made these tests a
    time bomb (issue #41): once the clock passed them, every contract read as
    expired and was dropped, yielding None / empty CSVs."""
    from datetime import timedelta
    return (
        (today + timedelta(days=14)).strftime("%Y-%m-%d"),
        (today + timedelta(days=44)).strftime("%Y-%m-%d"),
        (today + timedelta(days=74)).strftime("%Y-%m-%d"),
    )


def _mk_client(instruments_list, candle_close_by_token):
    """Build a Mock client whose instruments('NFO') returns the given list and
    whose historical_data returns a single day-bar with the matching close."""
    client = MagicMock()
    client.instruments.return_value = instruments_list
    def _historical(token, frm, to, interval):
        close = candle_close_by_token.get(int(token))
        if close is None:
            return []
        return [{"date": frm, "open": close, "high": close,
                 "low": close, "close": close, "volume": 0}]
    client.historical_data.side_effect = _historical
    return client


def _instruments_for(symbols_with_expiry):
    """Build a fake instruments('NFO') return value.
    `symbols_with_expiry` is a list of (name, expiry_iso, token, lot_size)."""
    return [
        {"name": s, "tradingsymbol": f"{s}26MAYFUT", "instrument_token": tok,
         "instrument_type": "FUT", "expiry": exp, "lot_size": lot}
        for (s, exp, tok, lot) in symbols_with_expiry
    ]


def _patch_kotak(client, symbols):
    """Consumer-key client, no pacing, and the NIFTY-50 filter under test."""
    return (
        patch("market_data.fetch_bhavcopy._kotak_market_client", return_value=client),
        patch("core.screen_pairs.NIFTY_50", symbols),
        patch("market_data.fetch_bhavcopy.time.sleep"),
    )


# ──────────────────────────────────────────────────────────
# _build_today_stfs_via_kotak
# ──────────────────────────────────────────────────────────

class TestBuildTodayStfsViaKotak:

    def test_market_client_does_not_login(self):
        """instruments() and historical_data() need the consumer key.
        login() rewrites .kotak_session.json and drops a runner that
        still holds the previous trade token."""
        with patch("core.broker.kotak.KotakNeoAdapter") as adapter_cls, \
             patch("core.broker.kotak.KotakNeoClient") as client_cls:
            adapter_cls.return_value.consumer_key = "ck"
            adapter_cls.return_value.neo_fin_key = "neotradeapi"
            got = _kotak_market_client("config.ini")
        adapter_cls.assert_called_once_with("config.ini")
        adapter_cls.return_value.login.assert_not_called()
        client_cls.assert_called_once_with("ck", neo_fin_key="neotradeapi")
        assert got is client_cls.return_value

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
        client = _mk_client(instruments, {1001: 1327.00, 1002: 1450.50})
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE", "INFY"])
        with p_client, p_names, p_sleep:
            df = _build_today_stfs_via_kotak(today)

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
        client = _mk_client(instruments, {1001: 1327.00, 1002: 1335.00, 1003: 1340.00})
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE"])
        with p_client, p_names, p_sleep:
            df = _build_today_stfs_via_kotak(today)

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
        client = _mk_client(instruments, {1001: 1327.00, 1002: 1335.00})
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE"])
        with p_client, p_names, p_sleep:
            df = _build_today_stfs_via_kotak(today)

        assert len(df) == 1
        assert df.iloc[0]["XpryDt"] == future

    def test_credential_failure_returns_none(self, today):
        """A missing Kotak credential must not crash — just degrade to
        current no-bhavcopy behaviour. login() is not the entry point."""
        with patch("market_data.fetch_bhavcopy._kotak_market_client",
                   side_effect=RuntimeError("consumer key missing")):
            assert _build_today_stfs_via_kotak(today) is None

    def test_instruments_failure_returns_none(self, today):
        client = MagicMock()
        client.instruments.side_effect = RuntimeError("scrip master down")
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE"])
        with p_client, p_names, p_sleep:
            assert _build_today_stfs_via_kotak(today) is None

    def test_no_nifty50_futures_returns_none(self, today):
        """Defensive: if NFO dump has no NIFTY-50 futures (shouldn't happen
        in production but possible during a market structure change), return
        None rather than synthesise an empty frame that downstream consumers
        would misinterpret as "no data today"."""
        client = _mk_client(instruments_list=[], candle_close_by_token={})
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE"])
        with p_client, p_names, p_sleep:
            assert _build_today_stfs_via_kotak(today) is None

    def test_individual_historical_data_failure_is_skipped_not_fatal(self, today, expiries):
        """If one symbol's historical_data fails, the rest should still be
        fetched — one flaky symbol can't sink the whole synthesis."""
        front = expiries[0]
        instruments = _instruments_for([
            ("RELIANCE", front, 1001, 250),
            ("INFY", front, 1002, 400),
        ])
        client = MagicMock()
        client.instruments.return_value = instruments
        def _hist(token, *a, **k):
            if int(token) == 1001:
                raise RuntimeError("historical hiccup")
            return [{"date": today, "open": 1450, "high": 1450,
                     "low": 1450, "close": 1450, "volume": 0}]
        client.historical_data.side_effect = _hist
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE", "INFY"])
        with p_client, p_names, p_sleep:
            df = _build_today_stfs_via_kotak(today)

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
        and there's no sentinel, return it without hitting NSE or Kotak."""
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

    def test_nse_404_today_triggers_kotak_fallback(self, isolated_raw_dir, today, expiries):
        """On NSE 404 for today, the Kotak fallback should fire and the
        cache should be written with a .broker-fallback marker, not the
        old .kite-fallback name."""
        instruments = _instruments_for([("RELIANCE", expiries[0], 1001, 250)])
        client = _mk_client(instruments, {1001: 1327.00})

        s = self._stub_404_session()
        yyyymmdd = today.strftime("%Y%m%d")
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE"])
        with p_client, p_names, p_sleep:
            out = _download_bhavcopy(today, s)

        assert out is not None
        assert "RELIANCE" in set(out["TckrSymb"])
        assert set(out["FinInstrmTp"]) == {"STF"}
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.parquet"
        sentinel = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.broker-fallback"
        old = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.kite-fallback"
        assert cache_file.exists()
        assert sentinel.read_text() == "synthesised_via_kotak_historical_data\n"
        assert not old.exists()

    def test_nse_404_for_past_date_does_not_call_kotak(self, isolated_raw_dir):
        """Backfill of an older missing day must not silently fill a partial
        NIFTY-50 board. Bhavcopy is the canonical source for past days."""
        from datetime import timedelta
        past_date = datetime.now() - timedelta(days=7)
        s = self._stub_404_session()
        with patch("market_data.fetch_bhavcopy._build_today_stfs_via_kotak") as mock_fallback:
            out = _download_bhavcopy(past_date, s)
        assert out is None
        mock_fallback.assert_not_called()

    def test_both_markers_are_removed_when_nse_finally_publishes(
        self, isolated_raw_dir, today,
    ):
        """Either marker forces a fresh NSE attempt. On HTTP 200 the cache
        is overwritten with the authoritative bhavcopy, both markers are
        removed, and the stale synthetic CSV cannot shadow the parquet."""
        yyyymmdd = today.strftime("%Y%m%d")
        legacy_csv = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        kite_marker = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.kite-fallback"
        broker_marker = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.broker-fallback"
        legacy_csv.write_text("TckrSymb,ClsPric\nSTALE,0.0\n")
        kite_marker.write_text("synthesised_via_kite_historical_data\n")
        broker_marker.write_text("synthesised_via_kotak_historical_data\n")

        s = self._stub_zip_session(b"TckrSymb,ClsPric\nAUTH,1.0\n")
        out = _download_bhavcopy(today, s)
        assert out.iloc[0]["TckrSymb"] == "AUTH"
        back = pd.read_parquet(isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.parquet")
        assert back.iloc[0]["TckrSymb"] == "AUTH"
        assert not kite_marker.exists()
        assert not broker_marker.exists()
        assert not legacy_csv.exists()

    @pytest.mark.parametrize("suffix", [".broker-fallback", ".kite-fallback"])
    def test_sentinel_cache_kept_when_nse_still_404(
        self, isolated_raw_dir, today, suffix,
    ):
        """If NSE still 404s, a sentinel-marked cache from an earlier run
        is returned as-is rather than calling Kotak again for the same day.
        The previous marker name must keep that property too."""
        yyyymmdd = today.strftime("%Y%m%d")
        cache_file = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}.csv"
        sentinel = isolated_raw_dir / f"bhavcopy_fo_{yyyymmdd}{suffix}"
        cache_file.write_text("TckrSymb,ClsPric\nKOTAK_FB,1.0\n")
        sentinel.write_text("synthesised\n")

        s = self._stub_404_session()
        with patch("market_data.fetch_bhavcopy._build_today_stfs_via_kotak") as mock_fallback:
            out = _download_bhavcopy(today, s)
        assert out.iloc[0]["TckrSymb"] == "KOTAK_FB"
        mock_fallback.assert_not_called()
        s.get.assert_called()


# ──────────────────────────────────────────────────────────
# End-to-end: synth CSV roundtrips through the screener loader
# ──────────────────────────────────────────────────────────

class TestSynthCsvIsScreenerCompatible:

    def test_load_front_month_panel_consumes_synth_day(self, tmp_path, today, expiries):
        """The whole point of the fallback is that core/screen_pairs.py reads
        the synth day cleanly. Verify end-to-end via the parquet cache the
        fallback now writes."""
        from core.screen_pairs import load_front_month_panel

        instruments = _instruments_for([
            ("RELIANCE", expiries[0], 1001, 250),
            ("INFY", expiries[0], 1002, 400),
        ])
        client = _mk_client(instruments, {1001: 1327.00, 1002: 1450.50})
        p_client, p_names, p_sleep = _patch_kotak(client, ["RELIANCE", "INFY"])
        with p_client, p_names, p_sleep:
            df = _build_today_stfs_via_kotak(today)
        assert df is not None

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
