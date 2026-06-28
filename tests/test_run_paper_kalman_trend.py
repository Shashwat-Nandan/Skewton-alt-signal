"""Tests for the PURE core of run_paper_kalman_trend (the Kite-wired main() is
host-smoke-test-only). Rule 9: the A/B is only fair if both books step on the
SAME prices, intraday exits fire between bars, the 5-min aggregation rolls on the
right boundary, and the EOD sidecar reports Kalman-minus-MA correctly.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import run_paper_kalman_trend as r

KAL = {"filter_params": [0.0, 0.0, 1.0, 100.0, 10000.0, 100.0], "model": 2,
       "mu": 0.0, "stop_ticks": 20, "target_ticks": 40}
MA = {"short": 3, "long": 8, "offset": 0.0, "stop_ticks": 20, "target_ticks": 40}


def test_bar_aggregator_rolls_on_boundary():
    a = r.BarAggregator(width_seconds=300)
    assert a.add(0, 100.0) is None
    assert a.add(60, 101.0) is None
    assert a.add(299, 102.0) is None          # still bucket 0
    assert a.add(300, 103.0) == 102.0          # bucket 0→1: close of bar 0 = 102
    assert a.add(305, 104.0) is None
    assert a.add(600, 110.0) == 104.0          # bucket 1→2: close of bar 1 = 104


def test_build_books_sets_lot_size_and_engines():
    b = r.build_books("NIFTY", KAL, MA)
    assert b.kalman.signal_kind == "kalman" and b.ma.signal_kind == "ma"
    assert b.kalman.lot_size == 75 and b.ma.lot_size == 75   # NIFTY lot


def test_both_books_step_on_same_bars_and_intraday_exit():
    b = r.build_books("NIFTY", KAL, MA)
    for p in 100 + 0.5 * np.arange(40):       # uptrend → both books go long
        b.on_bar(float(p))
    assert b.kalman.pos == 1 and b.ma.pos == 1
    # an intraday tick below both stops closes both between bars
    low = min(b.kalman.stop_price, b.ma.stop_price) - 1
    b.on_price(float(low))
    assert b.kalman.pos == 0 and b.ma.pos == 0


def test_eod_close_flattens_both():
    b = r.build_books("NIFTY", KAL, MA)
    for p in 100 + 0.5 * np.arange(40):
        b.on_bar(float(p))
    b.eod_close(118.0)
    assert b.kalman.pos == 0 and b.ma.pos == 0


def test_eod_report_totals_and_delta():
    b = r.build_books("NIFTY", KAL, MA)
    # give the kalman book a +10pt trade and the MA book a -4pt trade
    b.kalman.pos, b.kalman.entry_price = 1, 100.0
    b.kalman.force_close(110.0)               # +10 pts × 75 = ₹750
    b.ma.pos, b.ma.entry_price = 1, 100.0
    b.ma.force_close(96.0)                     # -4 pts × 75 = -₹300
    rep = r.eod_report([b], date(2026, 6, 27))
    assert rep["total_kalman_rupees"] == 750.0
    assert rep["total_ma_rupees"] == -300.0
    assert rep["kalman_minus_ma_rupees"] == 1050.0
    assert rep["instruments"][0]["symbol"] == "NIFTY"


def test_restored_book_can_inflate_for_overnight_gap():
    """The runner calls on_session_start() after restore() so a book resumed the
    next morning absorbs the overnight gap. Verify the restored Kalman filter's
    covariance inflates (the daily-restart path is the only real day boundary)."""
    b = r.build_books("NIFTY", KAL, MA)
    for p in 100 + 0.5 * np.arange(30):
        b.on_bar(float(p))
    rb = r.InstrumentBooks.restore(b.serialize())
    P_before = rb.kalman._filter.P.max()
    rb.on_session_start()
    assert rb.kalman._filter.P.max() > P_before


def test_instrument_books_serialize_restore_identity():
    b = r.build_books("BANKNIFTY", KAL, MA)
    for p in 50000 + 5 * np.arange(40):
        b.on_bar(float(p))
    rb = r.InstrumentBooks.restore(b.serialize())
    for p in [50210.0, 50180.0, 50250.0, 50300.0]:
        assert b.kalman.on_bar(p)["pos"] == rb.kalman.on_bar(p)["pos"]
        assert b.ma.on_bar(p)["pos"] == rb.ma.on_bar(p)["pos"]
    assert rb.kalman.realized_points == b.kalman.realized_points
