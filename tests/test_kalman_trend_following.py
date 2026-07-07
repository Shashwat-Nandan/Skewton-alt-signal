"""Tests for strategies.kalman_trend_following.IntradayTrendStrategy.

Rule 9: encode the behaviours the paper A/B depends on. The strategy must enter
on the correct side of the signal, book stops/targets at the LEVEL (not the
trigger price), allow the runner to close intraday BETWEEN signal bars (the whole
point of going intraday), honour the long-only constraint, and survive a
restart byte-identically (filter covariance + MA window + open position) — a
naive restore that dropped the filter state would silently diverge.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.kalman_trend import WARMUP_BARS
from strategies.kalman_trend_following import IntradayTrendStrategy


def test_warmup_bars_default_is_the_shared_constant():
    """Fit/live parity must be ENFORCED, not just commented: the strategy's
    warmup default has to BE the shared WARMUP_BARS the backtest uses."""
    s = IntradayTrendStrategy(signal_kind="ma", short=2, long=4,
                              stop_ticks=1, target_ticks=1)
    assert s.warmup_bars == WARMUP_BARS

# benign model-2 filter (p1=0,p2=0,p3=vel_std, R, P0_lvl, P0_vel) for prices ~100s
KAL_P = [0.0, 0.0, 1.0, 100.0, 10000.0, 100.0]


def _kal(**kw):
    base = dict(signal_kind="kalman", filter_params=KAL_P, model=2, mu=0.0,
                stop_ticks=20, target_ticks=40, tick_size=1.0)
    base.update(kw)
    return IntradayTrendStrategy(**base)


def test_kalman_enters_long_on_uptrend():
    """A rising series must put the Kalman book LONG (it forecasts the next bar
    above the current one)."""
    s = _kal()
    for p in 100 + 0.5 * np.arange(40):
        s.on_bar(float(p))
    assert s.pos == 1


def test_kalman_enters_short_on_downtrend():
    s = _kal()
    for p in 200 - 0.5 * np.arange(40):
        s.on_bar(float(p))
    assert s.pos == -1


def test_long_only_skips_shorts():
    """allow_short=False: a downtrend must produce NO position and NO trade —
    cash equity can't be shorted overnight, so that book stays flat."""
    s = _kal(allow_short=False)
    for p in 200 - 0.5 * np.arange(40):
        s.on_bar(float(p))
    assert s.pos == 0
    assert s.trades == []


def test_target_books_at_target_level_not_trigger_price():
    """A long that gaps past target books the TARGET distance, not the larger
    trigger move — otherwise stop/target sizing is meaningless."""
    s = _kal(stop_ticks=20, target_ticks=10)
    for p in 100 + 0.5 * np.arange(30):
        s.on_bar(float(p))
    assert s.pos == 1
    tgt = s.target_price
    rec = s.check_exit(s.entry_price + 50)   # blow past target
    assert rec is not None and rec.reason == "target"
    assert rec.exit_price == pytest.approx(tgt)
    assert s.pos == 0
    assert rec.pnl_points == pytest.approx(10.0)   # target_ticks, cost 0


def test_stop_books_at_stop_level():
    s = _kal(stop_ticks=15, target_ticks=40)
    for p in 100 + 0.5 * np.arange(30):
        s.on_bar(float(p))
    assert s.pos == 1
    rec = s.check_exit(s.entry_price - 50)
    assert rec is not None and rec.reason == "stop"
    assert rec.pnl_points == pytest.approx(-15.0)


def test_intraday_exit_between_bars():
    """The runner polls live price between signal bars; check_exit must close
    the position THEN (intraday), not wait for the next bar — this is the whole
    reason for going intraday."""
    s = _kal(stop_ticks=10, target_ticks=10)
    for p in 100 + 0.5 * np.arange(30):
        s.on_bar(float(p))
    assert s.pos == 1
    # no new bar — just an intra-bar price tick that pierces the target
    rec = s.check_exit(s.target_price + 0.05)
    assert rec is not None and s.pos == 0


def test_costs_reduce_pnl():
    s = _kal(stop_ticks=20, target_ticks=10, cost_per_unit=1.5)
    for p in 100 + 0.5 * np.arange(30):
        s.on_bar(float(p))
    rec = s.check_exit(s.entry_price + 50)
    assert rec.pnl_points == pytest.approx(10.0 - 2 * 1.5)


def test_ma_crossover_enters_on_trend():
    s = IntradayTrendStrategy(signal_kind="ma", short=3, long=8, offset=0.0,
                              stop_ticks=20, target_ticks=40)
    for p in 100 + 0.4 * np.arange(40):
        s.on_bar(float(p))
    assert s.pos == 1


def test_ma_requires_valid_windows():
    with pytest.raises(ValueError):
        IntradayTrendStrategy(signal_kind="ma", short=10, long=5,
                              stop_ticks=20, target_ticks=40)


def test_kalman_requires_filter_params():
    with pytest.raises(ValueError, match="filter_params"):
        IntradayTrendStrategy(signal_kind="kalman", stop_ticks=20, target_ticks=40)


def test_force_close_books_at_price():
    s = _kal()
    for p in 100 + 0.5 * np.arange(30):
        s.on_bar(float(p))
    assert s.pos == 1
    px = 113.0
    rec = s.force_close(px)
    assert rec.reason == "force_close" and rec.exit_price == px
    assert s.pos == 0
    assert s.force_close(px) is None      # idempotent when flat


def test_serialize_restore_is_identity_through_subsequent_bars():
    """Restart fidelity: a restored Kalman book must produce byte-identical
    events on the next bars — proving the FULL filter state (not just the level)
    and the open position round-tripped."""
    s = _kal(stop_ticks=30, target_ticks=60)
    for p in 100 + 0.5 * np.arange(40) + np.sin(np.arange(40)):
        s.on_bar(float(p))
    restored = IntradayTrendStrategy.restore(s.serialize())
    nxt = [120.0, 121.5, 119.0, 122.0, 123.5]
    for p in nxt:
        a = s.on_bar(p)
        b = restored.on_bar(p)
        assert a["signal"] == b["signal"] and a["pos"] == b["pos"]
    assert restored.realized_points == pytest.approx(s.realized_points)


def test_allow_entry_false_blocks_new_entry_but_still_exits():
    """HALT_NEW_ENTRIES path: allow_entry=False must open no new position but
    still manage an existing one to its stop/target."""
    s = _kal(stop_ticks=10, target_ticks=40)
    for p in 100 + 0.5 * np.arange(30):       # would normally go long
        s.on_bar(float(p), allow_entry=False)
    assert s.pos == 0 and s.trades == []      # entries blocked
    # open with entries allowed, then verify a blocked bar still exits
    s2 = _kal(stop_ticks=10, target_ticks=40)
    for p in 100 + 0.5 * np.arange(30):
        s2.on_bar(float(p))
    assert s2.pos == 1
    ev = s2.on_bar(float(s2.stop_price - 1.0), allow_entry=False)
    assert ev["exit"] is not None and s2.pos == 0


def test_on_session_start_inflates_filter_uncertainty():
    """Overnight-gap mitigation: a new session inflates the filter covariance so
    the gap is absorbed via a high gain, not read as one bar of velocity."""
    s = _kal()
    for p in 100 + 0.5 * np.arange(20):
        s.on_bar(float(p))
    P_before = s._filter.P.copy()
    s.on_session_start()
    assert s._filter.P.max() > P_before.max()


def test_on_session_start_noop_for_ma():
    s = IntradayTrendStrategy(signal_kind="ma", short=3, long=8, offset=0.0,
                              stop_ticks=20, target_ticks=40)
    for p in 100 + 0.4 * np.arange(20):
        s.on_bar(float(p))
    s.on_session_start()                       # must not raise (no filter)


def test_session_trades_isolate_the_current_session_across_restore():
    """The per-day dashboard view must show only TODAY's fills. The book carries
    prior sessions' trades across the daily restore, so session_trades() must
    exclude them: two trades 'yesterday', restore + on_session_start (the day
    boundary), then one trade 'today' → session has just the one, cumulative
    still counts all three, and session ₹ excludes yesterday's."""
    s = _kal(lot_size=75)
    for entry, exit_ in [(100.0, 110.0), (110.0, 105.0)]:   # two fills yesterday
        s.pos, s.entry_price = 1, entry
        s.force_close(exit_)
    assert len(s.trades) == 2

    r = IntradayTrendStrategy.restore(s.serialize())         # carry to next day
    r.on_session_start()                                     # marks the boundary
    assert r.session_trades() == []                          # nothing today yet

    r.pos, r.entry_price = -1, 120.0
    r.force_close(118.0)                                     # today's only fill: +2 pts
    sess = r.session_trades()
    assert len(sess) == 1 and sess[0].side == -1

    summ = r.book_summary()
    assert summ["session_n_trades"] == 1
    assert summ["n_trades"] == 3                             # cumulative unchanged
    assert summ["session_realized_rupees"] == round(sess[0].pnl_points * 75, 2)
    assert summ["session_realized_rupees"] == round(2.0 * 75, 2)   # only today's
    assert [t["side"] for t in summ["session_trades"]] == [-1]
    assert summ["session_trades"][0]["pnl_rupees"] == round(2.0 * 75, 2)


def test_fresh_book_treats_all_trades_as_this_session():
    """A fresh warmup book (no prior state; on_session_start never called) has
    _session_start_n=0, so every trade is this session's — session equals
    cumulative on day one."""
    s = _kal(lot_size=15)
    s.pos, s.entry_price = 1, 100.0
    s.force_close(105.0)                                     # +5 pts gross
    summ = s.book_summary()
    assert summ["session_n_trades"] == summ["n_trades"] == 1
    assert summ["session_realized_rupees"] == round(5.0 * 15, 2)


def test_ma_serialize_restore_identity():
    s = IntradayTrendStrategy(signal_kind="ma", short=3, long=8, offset=0.0,
                              stop_ticks=20, target_ticks=40)
    for p in 100 + 0.4 * np.arange(30):
        s.on_bar(float(p))
    restored = IntradayTrendStrategy.restore(s.serialize())
    for p in [112.0, 113.0, 111.0, 114.0]:
        assert s.on_bar(p)["signal"] == restored.on_bar(p)["signal"]
