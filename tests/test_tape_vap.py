"""Tests for research/tape_vap.py — volume-at-price tape reader (reversal A1).

These encode WHY the reader is correct (Rule 9): cumulative volume must be
DIFFERENCED before it becomes volume-at-price (attributing the running total to
the first observed bin would pile a whole session onto one price); a stable tick
order must not manufacture volume decreases; the index spot legitimately carries
no volume. The parquet round-trip test exercises the depth-passthrough and the
epoch-zero out-of-session drop end to end.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research import tape_vap
from research.tape_vap import (
    _bars_from_ticks,
    _volume_at_price,
    _volume_deltas,
    build_tape_profile,
    read_tape_columns,
    resolve_profile_tokens,
)

SESSION = "2026-07-13"
_T0 = datetime(2026, 7, 13, 9, 15)


def _tick_frame(rows, token=111, symbol="NIFTY26JULFUT"):
    """rows = list of (minute_offset, last_price, volume_traded_cumulative)."""
    return pd.DataFrame({
        "instrument_token": token,
        "exchange_timestamp": [pd.Timestamp(_T0) + pd.Timedelta(minutes=m)
                               for m, _, _ in rows],
        "last_price": [p for _, p, _ in rows],
        "last_traded_quantity": [1 for _ in rows],
        "volume_traded": [v for _, _, v in rows],
        "tradingsymbol": symbol,
    })


# ──────────────────────────────────────────────────────────
# volume deltas
# ──────────────────────────────────────────────────────────

class TestVolumeDeltas:
    def test_first_tick_delta_is_zero(self):
        # The first snapshot's cumulative total is trades we never observed
        # printing — attributing it to the first price bin would be a lie.
        delta, neg = _volume_deltas(np.array([500.0, 510.0, 525.0]))
        assert list(delta) == [0.0, 10.0, 15.0]
        assert neg == 0

    def test_negative_step_clipped_and_counted(self):
        # A non-monotonic cumulative series is a feed glitch: clip to 0 so it
        # adds no phantom volume, but COUNT it so the caller can fail loud.
        delta, neg = _volume_deltas(np.array([10.0, 25.0, 20.0, 40.0]))
        assert list(delta) == [0.0, 15.0, 0.0, 20.0]
        assert neg == 1

    def test_empty(self):
        delta, neg = _volume_deltas(np.array([]))
        assert delta.size == 0 and neg == 0


# ──────────────────────────────────────────────────────────
# volume-at-price binning
# ──────────────────────────────────────────────────────────

class TestVolumeAtPrice:
    def test_attributes_volume_to_last_price_bin(self):
        price = np.array([100.0, 101.0, 102.0])
        volume = np.array([0.0, 15.0, 35.0])
        mids, vols = _volume_at_price(price, volume, tick_size=1.0)
        # bins [100,101),[101,102),[102,103) → mids 100.5/101.5/102.5
        assert mids == [100.5, 101.5, 102.5]
        assert vols == [0.0, 15.0, 35.0]

    def test_zero_range_yields_one_bin(self):
        mids, vols = _volume_at_price(
            np.array([100.0, 100.0]), np.array([0.0, 5.0]), tick_size=1.0)
        assert len(mids) == 1
        assert vols == [5.0]

    def test_empty(self):
        assert _volume_at_price(np.array([]), np.array([]), 1.0) == ([], [])


# ──────────────────────────────────────────────────────────
# bar building
# ──────────────────────────────────────────────────────────

class TestBars:
    def test_ohlc_and_volume_per_bucket(self):
        ts = pd.Series([pd.Timestamp(_T0) + pd.Timedelta(minutes=m)
                        for m in (0, 1, 2, 6)])
        price = pd.Series([100.0, 103.0, 101.0, 105.0])
        volume = np.array([0.0, 10.0, 5.0, 8.0])
        bars = _bars_from_ticks(ts, price, volume, "5min")
        assert len(bars) == 2
        first = bars[0]
        # First 5-min bucket: three ticks 100/103/101 → O100 H103 L100 C101.
        assert (first.open, first.high, first.low, first.close) == (100.0, 103.0, 100.0, 101.0)
        assert first.volume == 15   # 0 + 10 + 5
        assert bars[1].open == 105.0

    def test_empty(self):
        assert _bars_from_ticks(pd.Series([], dtype="datetime64[ns]"),
                                pd.Series([], dtype=float), np.array([]), "5min") == []


# ──────────────────────────────────────────────────────────
# token resolution
# ──────────────────────────────────────────────────────────

class TestResolveTokens:
    def test_spot_and_front_month_future(self):
        df = pd.DataFrame({
            "instrument_token": [1, 1, 2, 2, 2, 3],
            "tradingsymbol": ["NIFTY 50", "NIFTY 50",
                              "NIFTY26JULFUT", "NIFTY26JULFUT", "NIFTY26JULFUT",
                              "NIFTY26AUGFUT"],
        })
        got = resolve_profile_tokens(df, "NIFTY")
        assert got["spot"] == 1
        # Front month = most-traded FUT (token 2, three ticks) not the far
        # month (token 3, one tick).
        assert got["future"] == 2

    def test_missing_roles_are_none(self):
        df = pd.DataFrame({"instrument_token": [9],
                           "tradingsymbol": ["NIFTY24800CE"]})
        got = resolve_profile_tokens(df, "NIFTY")
        assert got == {"spot": None, "future": None}


# ──────────────────────────────────────────────────────────
# build_tape_profile — end to end from a frame
# ──────────────────────────────────────────────────────────

class TestBuildTapeProfile:
    def test_future_profile_end_to_end(self):
        df = _tick_frame([
            (0, 100.0, 500), (1, 101.0, 515), (2, 100.0, 515), (6, 102.0, 550),
        ])
        prof = build_tape_profile(df, 111, SESSION, resolution="5min", tick_size=1.0)
        assert prof is not None
        assert prof.tradingsymbol == "NIFTY26JULFUT"
        assert prof.n_ticks == 4
        # deltas: [0, 15, 0, 35] → total 50, attributed 101→15, 102→35.
        assert prof.total_volume == 50.0
        assert prof.vpoc() == 102.5
        assert dict(zip(prof.bin_mids, prof.bin_volumes))[101.5] == 15.0
        # 5-min buckets: {09:15-09:20}=3 ticks, {09:21-09:26}=1 tick.
        assert len(prof.bars) == 2

    def test_index_spot_null_volume_is_empty_not_nan(self):
        # The index has no traded volume; its histogram must be empty and its
        # total 0, never NaN (which would poison downstream comparisons).
        df = _tick_frame([(0, 100.0, None), (1, 101.0, None)],
                         token=1, symbol="NIFTY 50")
        prof = build_tape_profile(df, 1, SESSION, resolution="5min", tick_size=1.0)
        assert prof is not None
        assert prof.total_volume == 0.0
        assert prof.vpoc() is None
        assert not any(prof.bin_volumes)

    def test_leading_null_volume_does_not_dump_prior_total_into_one_bin(self):
        # The token is subscribed before its first trade prints: the first two
        # snapshots carry NULL volume_traded, then tick 3 reports a cumulative
        # 1000. That 1000 is volume we never observed printing — it must NOT be
        # attributed to tick 3's price bin (regression for the 2026-07-24 review;
        # ffill can't fill leading NaN, bfill must). Only the genuinely-observed
        # delta (1200-1000=200 at 102) may land in a bin.
        df = _tick_frame([
            (0, 100.0, None), (1, 101.0, None), (2, 102.0, 1000), (3, 102.0, 1200),
        ])
        prof = build_tape_profile(df, 111, SESSION, resolution="5min", tick_size=1.0)
        assert prof is not None
        assert prof.total_volume == 200.0   # the 1000 baseline is not counted
        assert dict(zip(prof.bin_mids, prof.bin_volumes))[102.5] == 200.0

    def test_missing_token_returns_none(self):
        df = _tick_frame([(0, 100.0, 500)])
        assert build_tape_profile(df, 999, SESSION) is None


# ──────────────────────────────────────────────────────────
# read_tape_columns — parquet round-trip (depth + in-session drop)
# ──────────────────────────────────────────────────────────

def _write_parquet(path, df):
    import duckdb
    con = duckdb.connect()
    con.register("t", df)
    con.execute(
        f"COPY t TO '{path}' (FORMAT parquet)")
    con.close()


class TestReadTapeColumns:
    def test_parquet_passes_depth_and_drops_epoch_zero(self, tmp_path, monkeypatch):
        parquet = tmp_path / f"ticks-{SESSION}.parquet"
        df = pd.DataFrame({
            "instrument_token": [111, 111, 111],
            # middle row is a pre-first-trade epoch-zero snapshot → dropped.
            "exchange_timestamp": [
                pd.Timestamp("2026-07-13 09:15:01"),
                pd.Timestamp("1970-01-01 05:30:00"),
                pd.Timestamp("2026-07-13 09:20:00"),
            ],
            "last_price": [100.0, 100.0, 101.0],
            "last_traded_quantity": [1, 1, 2],
            "volume_traded": [500, 500, 515],
            "tradingsymbol": ["NIFTY26JULFUT"] * 3,
            "bid1_price": [99.9, None, 100.9],
            "bid1_quantity": [50, None, 75],
            "ask1_price": [100.1, None, 101.1],
            "ask1_quantity": [40, None, 60],
        })
        _write_parquet(parquet, df)
        monkeypatch.setattr(tape_vap, "_tape_path", lambda d: parquet)

        out = read_tape_columns(SESSION)
        # epoch-zero row dropped; two in-session ticks remain.
        assert len(out) == 2
        assert list(out["last_price"]) == [100.0, 101.0]
        # depth columns passed through when present.
        assert "bid1_price" in out.columns
        assert list(out["ask1_quantity"]) == [40, 60]

    def test_parquet_without_depth_still_reads(self, tmp_path, monkeypatch):
        parquet = tmp_path / f"ticks-{SESSION}.parquet"
        df = pd.DataFrame({
            "instrument_token": [111, 111],
            "exchange_timestamp": [
                pd.Timestamp("2026-07-13 09:15:01"),
                pd.Timestamp("2026-07-13 09:20:00"),
            ],
            "last_price": [100.0, 101.0],
            "last_traded_quantity": [1, 2],
            "volume_traded": [500, 515],
            "tradingsymbol": ["NIFTY26JULFUT"] * 2,
        })
        _write_parquet(parquet, df)
        monkeypatch.setattr(tape_vap, "_tape_path", lambda d: parquet)

        out = read_tape_columns(SESSION)
        assert len(out) == 2
        assert "bid1_price" not in out.columns   # absent, not fabricated

    def test_token_filter(self, tmp_path, monkeypatch):
        parquet = tmp_path / f"ticks-{SESSION}.parquet"
        df = pd.DataFrame({
            "instrument_token": [111, 222],
            "exchange_timestamp": [pd.Timestamp("2026-07-13 09:15:01")] * 2,
            "last_price": [100.0, 200.0],
            "last_traded_quantity": [1, 1],
            "volume_traded": [500, 900],
            "tradingsymbol": ["NIFTY26JULFUT", "NIFTY 50"],
        })
        _write_parquet(parquet, df)
        monkeypatch.setattr(tape_vap, "_tape_path", lambda d: parquet)

        out = read_tape_columns(SESSION, tokens=[222])
        assert list(out["instrument_token"]) == [222]
