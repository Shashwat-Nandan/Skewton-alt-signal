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


def test_build_books_applies_transaction_cost():
    """Issue #77: both books must carry the per-side cost, else the churny Kalman
    book is flattered vs MA by booking cost-free fills. A zero here means the EOD
    ₹ A/B is non-comparable to the backtest (which charges 2.5/side)."""
    b = r.build_books("NIFTY", KAL, MA)
    assert b.kalman.cost_per_unit == r.COST_PER_UNIT_POINTS
    assert b.ma.cost_per_unit == r.COST_PER_UNIT_POINTS
    assert r.COST_PER_UNIT_POINTS > 0


def test_set_cost_overrides_restored_zero_cost():
    """A book serialized before #77 carries cost_per_unit=0.0; restore() preserves
    it, so the runner must re-assert the current cost or a persisted book keeps
    booking cost-free fills forever. This is the subsequent-day path (fresh warmup
    only happens when state is absent)."""
    b = r.build_books("NIFTY", KAL, MA)
    blob = b.serialize()
    blob["kalman"]["cost_per_unit"] = 0.0        # simulate a pre-#77 persisted book
    blob["ma"]["cost_per_unit"] = 0.0
    rb = r.InstrumentBooks.restore(blob)
    assert rb.kalman.cost_per_unit == 0.0 and rb.ma.cost_per_unit == 0.0  # stale
    rb.set_cost(r.COST_PER_UNIT_POINTS)
    assert rb.kalman.cost_per_unit == r.COST_PER_UNIT_POINTS
    assert rb.ma.cost_per_unit == r.COST_PER_UNIT_POINTS
    # and it actually bites: a +10pt gross trade now nets (10 - 2*cost) points
    rb.kalman.pos, rb.kalman.entry_price = 1, 100.0
    rb.kalman.force_close(110.0)
    assert rb.kalman.realized_points == 10 - 2 * r.COST_PER_UNIT_POINTS


def test_fit_params_charges_book_cost(monkeypatch):
    """The warmup fit must optimize under the SAME per-side cost the book
    charges. optimize_kalman_trend defaults cost_per_unit=0.0, and a costless
    objective picks hyper-tight stops (deployed 6-pt stop vs 101-pt target =
    82% of the stop lost to costs per round trip); the 2026-07-07 walk-forward
    showed the zero-cost fit loses −769 pts/seed OOS on NIFTY where the costed
    fit makes +702 at half the trades. If this drifts back to a costless fit,
    the A/B tests parameters the live book can never afford."""
    seen = {}

    def fake_kal(prices, **kw):
        seen["kal"] = kw
        return dict(KAL)

    def fake_ma(prices, **kw):
        seen["ma"] = kw
        return dict(MA)

    monkeypatch.setattr(r.opt, "fit_kalman_reduced", fake_kal)
    monkeypatch.setattr(r.opt, "fit_ma_crossover", fake_ma)
    kal, ma = r.fit_params(np.linspace(100.0, 110.0, 50))
    assert seen["kal"]["cost_per_unit"] == r.COST_PER_UNIT_POINTS
    assert seen["ma"]["cost_per_unit"] == r.COST_PER_UNIT_POINTS
    assert kal == KAL and ma == MA


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
    # build_books charges COST_PER_UNIT_POINTS/side (round-trip 2×) per issue #77,
    # so each closed trade nets `gross - 2*cost` points before ×lot.
    rt = 2 * r.COST_PER_UNIT_POINTS
    # give the kalman book a +10pt gross trade and the MA book a -4pt gross trade
    b.kalman.pos, b.kalman.entry_price = 1, 100.0
    b.kalman.force_close(110.0)               # (10 - rt) pts × 75
    b.ma.pos, b.ma.entry_price = 1, 100.0
    b.ma.force_close(96.0)                     # (-4 - rt) pts × 75
    rep = r.eod_report([b], date(2026, 6, 27))
    assert rep["total_kalman_rupees"] == round((10 - rt) * 75, 2)
    assert rep["total_ma_rupees"] == round((-4 - rt) * 75, 2)
    assert rep["kalman_minus_ma_rupees"] == round((14) * 75, 2)  # cost cancels in the delta
    assert rep["instruments"][0]["symbol"] == "NIFTY"


def test_eod_report_carries_session_trades():
    """The dashboard's per-day view reads each book's THIS-session fills from the
    EOD sidecar, so eod_report must embed them. A fresh book's session == all its
    trades; a book with no fills carries an empty list (not a missing key)."""
    b = r.build_books("NIFTY", KAL, MA)
    b.kalman.pos, b.kalman.entry_price = 1, 100.0
    b.kalman.force_close(110.0)               # +10 gross, minus round-trip cost
    rep = r.eod_report([b], date(2026, 6, 27))
    kb = rep["instruments"][0]["kalman"]
    rt = 2 * r.COST_PER_UNIT_POINTS
    assert kb["session_n_trades"] == 1
    assert len(kb["session_trades"]) == 1
    t = kb["session_trades"][0]
    assert t["side"] == 1 and t["reason"] == "force_close"
    assert kb["session_realized_rupees"] == round((10 - rt) * 75, 2)
    assert t["pnl_rupees"] == round((10 - rt) * 75, 2)
    assert rep["instruments"][0]["ma"]["session_trades"] == []   # MA had no fills


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


# ── Experiment sunset (efficiency review 2026-07-05 §2.7) ─────────────────
# WHY: the A/B wraps a strategy whose backtest was NO-GO and whose checker
# REJECTs every session. The kill date is the pre-agreed end of its runway;
# if this gate stops binding, a rejected strategy trades forever.
def test_experiment_expired_boundaries():
    from datetime import timedelta
    kd = r.KILL_DATE
    assert not r.experiment_expired(kd - timedelta(days=1))
    assert r.experiment_expired(kd)            # on the date: expired
    assert r.experiment_expired(kd + timedelta(days=30))


def test_experiment_expired_honours_override():
    assert r.experiment_expired(date(2026, 1, 2), kill_date=date(2026, 1, 1))
    assert not r.experiment_expired(date(2026, 1, 1), kill_date=date(2027, 1, 1))


# ──────────────────────────────────────────────────────────────────────────
# #121 stale-fit detection + in-place re-fit (the fix must reach the live book)
# ──────────────────────────────────────────────────────────────────────────
def test_fit_is_stale_flags_a_pre_121_book():
    """restore() faithfully preserves whatever was serialized — including params
    fit WITHOUT the 15:25 flatten. Without this check a persisted book trades the
    mis-fit config forever and the #121 fix never reaches the live book."""
    assert r.fit_is_stale({"symbol": "NIFTY"}) is True           # pre-#121: no marker
    assert r.fit_is_stale({"fit_flatten_aware": False}) is True
    assert r.fit_is_stale({"fit_flatten_aware": True}) is False


def test_serialize_marks_the_fit_as_flatten_aware_and_roundtrips():
    b = r.build_books("NIFTY", KAL, MA)
    blob = b.serialize()
    assert blob["fit_flatten_aware"] is True
    assert r.fit_is_stale(blob) is False                          # fresh fits are current
    assert r.InstrumentBooks.restore(blob).refit_at is None


def test_reparam_swaps_params_but_PRESERVES_the_book():
    """The whole point of re-fitting in place: adopt the corrected params without
    destroying the accumulated A/B history (deleting sessions would be worse)."""
    b = r.build_books("NIFTY", KAL, MA)
    for px in (100.0, 101.0, 102.0, 103.0, 104.0, 130.0, 90.0, 95.0):
        b.on_bar(px)
    b.eod_close(95.0)
    trades_before = len(b.ma.trades)
    pnl_before = b.ma.realized_points
    assert trades_before > 0, "fixture must have traded or the test is vacuous"

    new_ma = {"short": 5, "long": 20, "offset": 1.5, "stop_ticks": 99, "target_ticks": 199}
    assert b.reparam(KAL, new_ma) is True
    assert b.ma.short == 5 and b.ma.long == 20 and b.ma.stop_ticks == 99
    # book preserved
    assert len(b.ma.trades) == trades_before
    assert b.ma.realized_points == pnl_before


def test_reparam_refuses_while_a_position_is_open():
    """Swapping params under a live position would manage it to stop/target levels
    it was never entered against. Defer to the next flat restart instead."""
    b = r.build_books("NIFTY", KAL, MA)
    for px in (100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0):
        b.on_bar(px)
    if b.ma.pos == 0 and b.kalman.pos == 0:
        b.ma.pos = 1                                   # force the guarded state
    stop_before = b.ma.stop_ticks
    assert b.reparam(KAL, {"short": 5, "long": 20, "offset": 1.5,
                           "stop_ticks": 99, "target_ticks": 199}) is False
    assert b.ma.stop_ticks == stop_before              # unchanged


def test_refit_seam_is_surfaced_in_the_eod_summary():
    """The re-fit preserves trades, so cumulative P&L spans two configs. The seam
    must be visible or the 2026-08-28 analysis would pool across it."""
    b = r.build_books("NIFTY", KAL, MA)
    b.refit_at = "2026-07-15T09:10:00"
    assert b.summary()["refit_at"] == "2026-07-15T09:10:00"
    assert b.serialize()["refit_at"] == "2026-07-15T09:10:00"
