"""Tests for optimize_kalman_trend — the fixed-tick trend simulation, the
Kalman/MA signals, the CMA-ES driver and the train-Sharpe fits.

Rule 9: these encode WHY each piece matters. The execution engine must book a
fixed-tick stop and target at the right PRICE (not the close); the CMA-ES + L1
machinery must actually shrink irrelevant parameters to zero (the paper's
sparsity claim); and a fit on a genuinely trending series must find a
profitable, actively-trading strategy — a fit that returned junk params would
pass a 'runs without error' test but fail these.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import optimize_kalman_trend as o


# ──────────────────────────────────────────────────────────────────────────
# Execution engine books stop/target at the level, not the close
# ──────────────────────────────────────────────────────────────────────────
def test_long_target_books_at_target_price():
    """A long that gaps through its target must realize the TARGET distance, not
    the (larger) close move — otherwise stop/target sizing is meaningless."""
    prices = [100.0, 100.0, 130.0]
    res = o.simulate(prices, [1, 0, 0], stop_ticks=50, target_ticks=20, tick_size=1.0)
    assert res.n_trades == 1
    assert res.realized_pnl == pytest.approx(20.0)   # target = 100 + 20


def test_long_stop_books_at_stop_price():
    prices = [100.0, 100.0, 70.0]
    res = o.simulate(prices, [1, 0, 0], stop_ticks=20, target_ticks=80, tick_size=1.0)
    assert res.n_trades == 1
    assert res.realized_pnl == pytest.approx(-20.0)  # stop = 100 - 20


def test_short_target_books_at_target_price():
    prices = [100.0, 100.0, 70.0]
    res = o.simulate(prices, [-1, 0, 0], stop_ticks=50, target_ticks=20, tick_size=1.0)
    assert res.realized_pnl == pytest.approx(20.0)    # short target = 100 - 20


def test_costs_reduce_realized():
    prices = [100.0, 100.0, 130.0]
    base = o.simulate(prices, [1, 0, 0], stop_ticks=50, target_ticks=20).realized_pnl
    costed = o.simulate(prices, [1, 0, 0], stop_ticks=50, target_ticks=20,
                        cost_per_unit=1.5).realized_pnl
    assert costed == pytest.approx(base - 2 * 1.5)


# ──────────────────────────────────────────────────────────────────────────
# Signals
# ──────────────────────────────────────────────────────────────────────────
def test_kalman_direction_is_directional_on_a_trend():
    """On a clean uptrend the Kalman signal must be net long (it forecasts the
    next close above the current one); on a downtrend, net short."""
    up = 100 + 0.8 * np.arange(120)
    d_up = o.kalman_direction(up, [0.5, 0.0, 0.05, 1.0, 50.0], model=1, mu=0.0)
    assert d_up.sum() > 50   # overwhelmingly long
    down = 200 - 0.8 * np.arange(120)
    d_dn = o.kalman_direction(down, [0.5, 0.0, 0.05, 1.0, 50.0], model=1, mu=0.0)
    assert d_dn.sum() < -50


def test_ma_crossover_flips_on_reversal():
    """SMA crossover must be long in the up leg and short in the down leg."""
    up = 100 + 0.5 * np.arange(120)
    down = up[-1] - 0.5 * np.arange(1, 121)
    prices = np.concatenate([up, down])
    d = o.ma_direction(prices, short=5, long=30, offset=0.0)
    assert d[100] > 0 and d[-5] < 0


def test_ma_direction_rejects_bad_windows():
    with pytest.raises(ValueError):
        o.ma_direction(np.arange(50.0), short=0, long=10, offset=0.0)


# ──────────────────────────────────────────────────────────────────────────
# CMA-ES + L1 sparse recovery (the paper's penalty)
# ──────────────────────────────────────────────────────────────────────────
def test_cmaes_l1_shrinks_irrelevant_params_to_zero():
    """The L1 penalty must drive components that don't help the objective to
    ~0 while keeping the ones that do — this is exactly the mechanism the paper
    relies on to zero out meaningless filter params (Table 2)."""
    target = np.array([3.0, 0.0, 0.0, 5.0])

    def obj(x):
        return float(np.sum((x - target) ** 2) + 0.5 * np.sum(np.abs(x)))

    bounds = np.array([[-10.0, 10.0]] * 4)
    bx, _ = o.run_cmaes(obj, np.zeros(4), bounds, sigma=0.3, n_gen=150, seed=1)
    assert abs(bx[1]) < 0.25 and abs(bx[2]) < 0.25   # irrelevant → shrunk to ~0
    assert bx[0] > 1.5 and bx[3] > 3.5               # relevant → kept (L1-shrunk)


# ──────────────────────────────────────────────────────────────────────────
# Fits find a profitable, actively-trading strategy on a trending series
# ──────────────────────────────────────────────────────────────────────────
def test_fit_kalman_finds_profitable_strategy_on_trends():
    """On a clearly regime-switching series (up, down, up) the fit must produce
    a strategy that trades several times AND has a positive in-sample Sharpe.
    A fit that returned degenerate params would trade 0-1 times or score -10."""
    rng = np.random.default_rng(3)
    segs = [100 + 0.6 * np.arange(80),
            148 - 0.6 * np.arange(80),
            100 + 0.6 * np.arange(80)]
    prices = np.concatenate(segs) + rng.normal(0, 1.0, 240)
    fit = o.fit_kalman_trend(prices, model=1, tick_size=1.0, l1_lambda=0.01,
                             n_gen=60, seed=2)
    assert fit["n_trades"] >= 3
    assert fit["train_sharpe"] > 0.0


def test_fit_kalman_rejects_unstable_models():
    with pytest.raises(NotImplementedError):
        o.fit_kalman_trend(100 + np.arange(50.0), model=3)


def test_kalman_direction_warmup_matches_live():
    """kalman_direction must suppress signals for the first `warmup` bars so the
    backtest/fit trade the SAME rule as the live IntradayTrendStrategy (which
    skips warmup_bars entries) — otherwise the fit books phantom early entries
    the runner never takes."""
    prices = 100 + 0.8 * np.arange(60)
    d = o.kalman_direction(prices, [0.5, 0.0, 0.05, 1.0, 50.0], model=1, mu=0.0,
                           warmup=10)
    assert (d[:10] == 0).all()
    assert d[10:].sum() > 0
    # default warmup is the shared WARMUP_BARS constant, not 0
    d0 = o.kalman_direction(prices, [0.5, 0.0, 0.05, 1.0, 50.0], model=1, mu=0.0)
    assert (d0[:o.WARMUP_BARS] == 0).all()


def test_sharpe_is_nan_not_sentinel_when_undefined():
    """No-trade / no-variation Sharpe is NaN (undefined), not a magic -10.0 that
    a genuinely-bad -9 strategy could collide with."""
    assert np.isnan(o._sharpe(np.zeros(10), n_trades=0))
    assert np.isnan(o._sharpe(np.zeros(10), n_trades=5))   # traded but flat


def test_simulate_too_short_is_nan_not_sentinel():
    """The n<2 early-return must honour the NaN contract, not the old -10.0."""
    res = o.simulate([100.0], [1], stop_ticks=10, target_ticks=10)
    assert np.isnan(res.sharpe)


def test_beats_requires_kalman_traded_and_strict():
    """`beats` is the shared win rule. A no-trade Kalman (NaN) must NOT beat even
    a LOSING MA — that loophole (0 > negative) let an inert Kalman score wins."""
    assert o.beats(1.0, 0.5) is True
    assert o.beats(0.5, 1.0) is False
    assert o.beats(0.5, 0.5) is False                 # strict, not >=
    assert o.beats(float("nan"), -5.0) is False       # ← the #1 core: inert kal
    assert o.beats(1.0, float("nan")) is True         # kal traded, MA didn't
    assert o.beats(float("nan"), float("nan")) is False


def test_verdict_passed_policy():
    assert o.verdict_passed(1.0, 0.5, kal_trades=5, win_rate=0.6) is True
    assert o.verdict_passed(1.0, 0.5, kal_trades=1, win_rate=0.6) is False  # trades
    assert o.verdict_passed(1.0, 0.5, kal_trades=5, win_rate=0.5) is False  # win>0.5
    assert o.verdict_passed(0.5, 0.5, kal_trades=5, win_rate=0.9) is False  # tie
    assert o.verdict_passed(float("nan"), -5.0, 5, 0.9) is False            # inert


def test_reduced_fit_is_smaller_and_model2_and_evaluable():
    """The reduced fit (Option B) must expose ONE filter knob on model 2 and
    round-trip through evaluate() — fewer params is the whole anti-overfit point,
    and evaluate must honor the model tag (not assume model 1)."""
    rng = np.random.default_rng(5)
    segs = [100 + 0.6 * np.arange(80), 148 - 0.6 * np.arange(80),
            100 + 0.6 * np.arange(80)]
    prices = np.concatenate(segs) + rng.normal(0, 1.0, 240)
    fit = o.fit_kalman_reduced(prices, n_gen=60, seed=1)
    assert fit["model"] == 2
    assert "s_vel" in fit and fit["n_trades"] >= 3
    # evaluate must reconstruct a model-2 filter (a model-1 assumption would
    # mis-shape the param vector); reproduces the in-sample Sharpe.
    res = o.evaluate(prices, kind="kalman", params=fit)
    assert res.sharpe == pytest.approx(fit["train_sharpe"], rel=1e-6)


# ──────────────────────────────────────────────────────────────────────────
# session_ends: the fit must model the runner's 15:25 flatten (issue #121)
# ──────────────────────────────────────────────────────────────────────────
def test_session_ends_flattens_open_position_at_the_close():
    """Without session_ends a position rides to its stop/target across bars; with
    it, the bar marked True force-closes at that close. This is the whole point
    of #121: the runner flattens daily, so the fit must too — otherwise it fits
    targets that can never be reached inside a session."""
    prices = [100.0, 101.0, 130.0]          # would reach a +25 target on bar 2
    direction = [1, 0, 0]
    held = o.simulate(prices, direction, stop_ticks=50, target_ticks=25)
    flat = o.simulate(prices, direction, stop_ticks=50, target_ticks=25,
                      session_ends=[False, True, False])
    assert held.realized_pnl == pytest.approx(25.0)    # target hit on bar 2
    assert flat.realized_pnl == pytest.approx(1.0)     # flattened at 101 on bar 1
    assert flat.n_trades == 1


def test_session_ends_charges_round_trip_on_a_closing_bar_entry():
    """A signal on the session's last bar opens and is flattened at the same
    price — the runner's on_bar()-then-eod_close() order. It must cost the round
    trip, not be silently dropped (that would understate churn in the fit)."""
    res = o.simulate([100.0, 100.0], [0, 1], stop_ticks=50, target_ticks=50,
                     cost_per_unit=2.5, session_ends=[False, True])
    assert res.n_trades == 1
    assert res.realized_pnl == pytest.approx(-5.0)     # 2 x 2.5, zero price move


def test_session_ends_length_must_match_prices():
    with pytest.raises(ValueError, match="session_ends length"):
        o.simulate([100.0, 101.0], [1, 0], stop_ticks=10, target_ticks=10,
                   session_ends=[True])


def test_daily_path_unchanged_when_session_ends_is_none():
    """Regression guard: the daily gates pass session_ends=None and must behave
    exactly as before the #121 fix (one bar == one day; holding across bars IS
    the daily strategy)."""
    prices = [100.0, 101.0, 130.0]
    a = o.simulate(prices, [1, 0, 0], stop_ticks=50, target_ticks=25)
    b = o.simulate(prices, [1, 0, 0], stop_ticks=50, target_ticks=25, session_ends=None)
    assert a.realized_pnl == b.realized_pnl == pytest.approx(25.0)


def test_session_ends_from_timestamps_marks_last_bar_of_each_day():
    from datetime import datetime
    ts = [datetime(2026, 5, 1, 9, 15), datetime(2026, 5, 1, 15, 25),
          datetime(2026, 5, 4, 9, 15), datetime(2026, 5, 4, 15, 25)]
    assert list(o.session_ends_from_timestamps(ts)) == [False, True, False, True]


# ──────────────────────────────────────────────────────────────────────────
# PARITY GATE — simulate(session_ends) must equal the live intraday book
# ──────────────────────────────────────────────────────────────────────────
def _synthetic_intraday_tape(n_days=40, bars_per_day=75, seed=0):
    """Deterministic multi-session 5-min-shaped tape: (prices, timestamps).

    HERMETIC BY DESIGN. The parity property below is what guards #121, so it must
    run in CI — and the real 5-min tape is gitignored, so a data-dependent test
    would skip there and protect nothing exactly where regressions land. The
    volatility is scaled so a 250/670 stop/target and the session flatten all
    actually fire (a tape too quiet to trigger them would pass vacuously).
    The real-tape reproduction (₹74,708 / 996.1 pts / 119 trades) is recorded as
    evidence in tasks/kalman-trend-findings.md; it is not a regression guard.
    """
    from datetime import datetime, timedelta
    rng = np.random.default_rng(seed)
    prices, ts = [], []
    px = 24000.0
    day = datetime(2026, 5, 4, 9, 15)
    for d in range(n_days):
        if day.weekday() >= 5:
            day += timedelta(days=2)
        drift = rng.normal(0, 6.0)          # per-day regime: trends and chop
        for b in range(bars_per_day):
            px += rng.normal(drift, 18.0)   # 5-min step; big enough to hit stops
            prices.append(px)
            ts.append(day + timedelta(minutes=5 * b))
        day += timedelta(days=1)
    return np.array(prices, float), ts


def test_simulate_with_session_ends_matches_live_intraday_book():
    """PARITY GATE: the FIT engine and the LIVE book must book identical trades.

    This is what stops fit and deploy silently diverging again — the exact
    failure #121 documents (the fit held multi-day while the runner flattened at
    15:25, so the fitted 670-pt target fired 0 times in 120 sessions). If this
    test ever fails, the two exit models have drifted apart: fix that, do not
    relax the assertion.
    """
    from strategies.kalman_trend_following import IntradayTrendStrategy

    prices, ts = _synthetic_intraday_tape()
    ends = o.session_ends_from_timestamps(ts)
    P = dict(short=13, long=52, offset=51.44557346247022,
             stop_ticks=250.0437334612003, target_ticks=669.8496641922126)
    COST = 2.5

    direction = o.ma_direction(prices, short=P["short"], long=P["long"],
                               offset=P["offset"])
    sim = o.simulate(prices, direction, stop_ticks=P["stop_ticks"],
                     target_ticks=P["target_ticks"], tick_size=1.0,
                     cost_per_unit=COST, session_ends=ends)

    # live book, stepped exactly as the runner does: on_bar per bar, then the
    # 15:25 eod_close, per session.
    live = IntradayTrendStrategy(
        signal_kind="ma", short=P["short"], long=P["long"], offset=P["offset"],
        stop_ticks=P["stop_ticks"], target_ticks=P["target_ticks"],
        tick_size=1.0, cost_per_unit=COST, lot_size=1)
    start = 0
    for i, is_end in enumerate(ends):
        if not is_end:
            continue
        live.on_session_start()
        for px in prices[start:i + 1]:
            live.on_bar(float(px))
        live.force_close(float(prices[i]))
        start = i + 1

    assert sim.n_trades == len(live.trades), "engines disagree on trade COUNT"
    assert sim.realized_pnl == pytest.approx(live.realized_points, abs=1e-6)
    # Guard against a vacuous pass: the tape must actually exercise the paths.
    assert sim.n_trades > 10, "tape too quiet to be a real parity test"


# ──────────────────────────────────────────────────────────────────────────
# trail_ticks: ratcheting trailing stop (issue #122 candidate)
# ──────────────────────────────────────────────────────────────────────────
def test_trailing_stop_ratchets_and_exits_on_retrace():
    """The trail must follow the peak UP and exit only on a retrace of the trail
    distance from that peak — not from entry. Entry 100, peak 120, trail 10 →
    exit at 110, banking +10, not the −0 a from-entry stop would give."""
    prices = [100.0, 120.0, 109.0]
    res = o.simulate(prices, [1, 0, 0], stop_ticks=999, target_ticks=999,
                     trail_ticks=10)
    assert res.n_trades == 1
    assert res.realized_pnl == pytest.approx(10.0)     # 110 - 100


def test_trailing_stop_does_not_exit_while_making_new_peaks():
    """A monotonically rising series must never trip the trail — otherwise the
    trail would cut winners, which is the opposite of its purpose."""
    res = o.simulate([100.0, 105.0, 110.0, 115.0], [1, 0, 0, 0],
                     stop_ticks=999, target_ticks=999, trail_ticks=10)
    assert res.n_trades == 1                            # only the end-of-series close
    assert res.realized_pnl == pytest.approx(15.0)      # rode to 115


def test_trailing_stop_short_side_ratchets_down():
    prices = [100.0, 80.0, 91.0]
    res = o.simulate(prices, [-1, 0, 0], stop_ticks=999, target_ticks=999,
                     trail_ticks=10)
    assert res.realized_pnl == pytest.approx(10.0)      # 100 - 90


def test_trail_ignores_fixed_stop_and_target():
    """When trailing, the fixed levels must not fire — the trail replaces both.
    A 5-pt target would otherwise cut this trade at 105 instead of trailing."""
    res = o.simulate([100.0, 120.0, 109.0], [1, 0, 0],
                     stop_ticks=1, target_ticks=5, trail_ticks=10)
    assert res.realized_pnl == pytest.approx(10.0)


def test_variant_d_trail_plus_session_flatten():
    """Variant D: the trail manages the trade but the 15:25 flatten still closes
    it — the combination that avoids overnight gap/margin exposure entirely."""
    res = o.simulate([100.0, 105.0, 108.0], [1, 0, 0], stop_ticks=999,
                     target_ticks=999, trail_ticks=50,
                     session_ends=[False, True, False])
    assert res.n_trades == 1
    assert res.realized_pnl == pytest.approx(5.0)      # flattened at 105, trail never hit


# ──────────────────────────────────────────────────────────────────────────
# Code-review fixes (2026-07-14)
# ──────────────────────────────────────────────────────────────────────────
def test_session_ends_from_timestamps_rejects_unsorted_input():
    """The mask is built from days[i+1] != days[i], so an order inversion invents
    a session boundary and force-closes there — a silently wrong fit. Fail loud
    (Rule 12) rather than return a plausible-looking wrong mask."""
    from datetime import datetime
    ts = [datetime(2026, 5, 4, 9, 15), datetime(2026, 5, 1, 9, 15)]   # inverted
    with pytest.raises(ValueError, match="chronological"):
        o.session_ends_from_timestamps(ts)


def test_end_of_series_force_close_charges_its_exit_cost_to_daily():
    """The final force-close increments n_trades and realized, so its exit cost
    must also hit `daily` — _sharpe divides daily by a trade count that includes
    this trade. Without it the daily series understates cost for any fold ending
    with an open position (the DAILY gates' common case)."""
    prices = [100.0, 101.0]
    res = o.simulate(prices, [1, 0], stop_ticks=999, target_ticks=999,
                     cost_per_unit=2.5)
    assert res.n_trades == 1
    # realized: +1 price move - 2x2.5 cost = -4.0
    assert res.realized_pnl == pytest.approx(-4.0)
    # daily must agree with realized once the trade is closed at the last bar
    assert float(res.daily_pnl.sum()) == pytest.approx(res.realized_pnl)


# ──────────────────────────────────────────────────────────────────────────
# OHLC fills (#122): touched-vs-closed-beyond, and gap-aware fill prices
# ──────────────────────────────────────────────────────────────────────────
def test_ohlc_stop_fires_when_TOUCHED_intrabar_even_if_the_close_recovers():
    """The close-only engine misses a stop the bar traded through and recovered
    from — it holds a position that was really stopped out. With high/low the
    touch is seen. Entry 100, stop 90; the bar dips to 89 and closes at 99."""
    close_only = o.simulate([100.0, 99.0], [1, 0], stop_ticks=10, target_ticks=999)
    with_ohlc = o.simulate([100.0, 99.0], [1, 0], stop_ticks=10, target_ticks=999,
                           highs=[100.0, 101.0], lows=[100.0, 89.0],
                           opens=[100.0, 99.5])
    assert close_only.n_trades == 1                    # only the end-of-series close
    assert close_only.realized_pnl == pytest.approx(-1.0)   # rode to 99, never "stopped"
    assert with_ohlc.realized_pnl == pytest.approx(-10.0)   # stopped at 90
    assert with_ohlc.n_trades == 1


def test_ohlc_gap_through_the_stop_fills_at_the_OPEN_not_the_level():
    """The whole point: you do not get your stop price when the bar gaps past it.
    Entry 100, stop 90, next bar OPENS at 80 → fill 80, not 90. Booking at the
    level here is the bias that manufactured ₹1.2M on one tape."""
    res = o.simulate([100.0, 82.0], [1, 0], stop_ticks=10, target_ticks=999,
                     highs=[100.0, 83.0], lows=[100.0, 79.0], opens=[100.0, 80.0])
    assert res.realized_pnl == pytest.approx(-20.0)    # 80 - 100, NOT -10
    assert res.n_trades == 1


def test_ohlc_normal_trade_through_fills_at_the_LEVEL():
    """When the bar did NOT gap, price genuinely traded at the stop, so the level
    IS the honest fill — OHLC makes level-booking correct rather than optimistic."""
    res = o.simulate([100.0, 88.0], [1, 0], stop_ticks=10, target_ticks=999,
                     highs=[100.0, 99.0], lows=[100.0, 87.0], opens=[100.0, 98.0])
    assert res.realized_pnl == pytest.approx(-10.0)    # filled at 90


def test_ohlc_same_bar_stop_and_target_assumes_the_STOP_first():
    """Without tick data the order is unknowable; the conservative convention is
    documented and pinned so it can never silently flip to the favourable one."""
    res = o.simulate([100.0, 100.0], [1, 0], stop_ticks=10, target_ticks=10,
                     highs=[100.0, 115.0], lows=[100.0, 85.0], opens=[100.0, 100.0])
    assert res.realized_pnl == pytest.approx(-10.0)    # stop, not the +10 target


def test_ohlc_short_side_mirrors():
    # short from 100, stop 110: bar touches 112 without gapping -> fill at 110
    res = o.simulate([100.0, 105.0], [-1, 0], stop_ticks=10, target_ticks=999,
                     highs=[100.0, 112.0], lows=[100.0, 99.0], opens=[100.0, 101.0])
    assert res.realized_pnl == pytest.approx(-10.0)


def test_ohlc_trail_ratchets_on_the_bar_EXTREME_not_the_close():
    """A trail follows the peak the market actually REACHED, not the peak close.

    Asserts the CONTRAST, not just the OHLC branch: close-only sees peak 108 →
    stop 98 → never exits (rides to the end); with highs the peak is 120 → stop
    110 → the retrace to 109 exits there. Without both assertions a regression to
    close-ratcheting could pass unnoticed.
    """
    prices = [100.0, 108.0, 109.0]
    highs = [100.0, 120.0, 111.0]
    lows = [100.0, 100.0, 109.0]
    with_ohlc = o.simulate(prices, [1, 0, 0], stop_ticks=999, target_ticks=999,
                           trail_ticks=10, highs=highs, lows=lows,
                           opens=[100.0, 101.0, 110.0])
    close_only = o.simulate(prices, [1, 0, 0], stop_ticks=999, target_ticks=999,
                            trail_ticks=10)
    assert with_ohlc.realized_pnl == pytest.approx(10.0)    # trailed to 110
    # close-only never trails above 99 (peak=109 at the last bar), so it never
    # stops out and rides to the final close: +9, NOT the honest +10.
    assert close_only.realized_pnl == pytest.approx(9.0)
    assert with_ohlc.realized_pnl != close_only.realized_pnl


def test_partial_ohlc_is_refused_not_silently_downgraded():
    """All-or-nothing (Rule 12). highs-without-lows silently fell back to the
    close-only path, and highs+lows without opens silently booked GAPS at the
    LEVEL — reinstating the exact bias OHLC exists to remove. The caller that
    wires this (#122's fit/harness step) is the one that would trip it."""
    for kw in ({"highs": [1.0, 2.0]},                       # lows+opens missing
               {"highs": [1.0, 2.0], "lows": [0.5, 1.5]}):  # opens missing
        with pytest.raises(ValueError, match="all-or-nothing"):
            o.simulate([1.0, 2.0], [1, 0], stop_ticks=10, target_ticks=10, **kw)


def test_ohlc_integrity_violation_fails_loud():
    """Only lengths were checked, so a misaligned/shifted OHLC slice — a live risk
    when threading highs[b:c] through walk-forward folds — silently mis-filled
    every trade. A close outside its own bar's [low, high] must raise."""
    with pytest.raises(ValueError, match="integrity"):
        o.simulate([100.0, 50.0], [1, 0], stop_ticks=10, target_ticks=999,
                   highs=[100.0, 101.0], lows=[100.0, 99.0], opens=[100.0, 100.0])
    with pytest.raises(ValueError, match="integrity"):        # low > high
        o.simulate([100.0, 100.0], [1, 0], stop_ticks=10, target_ticks=999,
                   highs=[100.0, 99.0], lows=[100.0, 101.0], opens=[100.0, 100.0])


def test_ohlc_length_mismatch_fails_loud():
    with pytest.raises(ValueError, match="highs length"):
        o.simulate([100.0, 101.0], [1, 0], stop_ticks=10, target_ticks=10,
                   highs=[100.0], lows=[99.0, 98.0])


def test_close_only_path_unchanged_when_ohlc_absent():
    """Regression: every existing caller passes closes only and must be
    byte-identical to before this change."""
    a = o.simulate([100.0, 101.0, 130.0], [1, 0, 0], stop_ticks=50, target_ticks=25)
    b = o.simulate([100.0, 101.0, 130.0], [1, 0, 0], stop_ticks=50, target_ticks=25,
                   highs=None, lows=None, opens=None)
    assert a.realized_pnl == b.realized_pnl == pytest.approx(25.0)


# ──────────────────────────────────────────────────────────────────────────
# OHLC bundle: the three arrays must travel and SLICE as one unit
# ──────────────────────────────────────────────────────────────────────────
def test_ohlc_bundle_slices_all_three_together():
    """Slicing is where misalignment is born (highs[b:c] vs closes[b-1:c-1] in a
    walk-forward fold). One slice() call moves all three, so a fold cannot
    half-slice them."""
    b = o.OHLC(opens=np.arange(10.0), highs=np.arange(10.0) + 1,
               lows=np.arange(10.0) - 1)
    s = b.slice(2, 5)
    assert len(s) == 3
    assert list(s.opens) == [2.0, 3.0, 4.0]
    assert list(s.highs) == [3.0, 4.0, 5.0]
    assert list(s.lows) == [1.0, 2.0, 3.0]


def test_ohlc_kwargs_splats_into_simulate_and_none_is_close_only():
    b = o.OHLC(opens=np.array([100.0, 99.5]), highs=np.array([100.0, 101.0]),
               lows=np.array([100.0, 89.0]))
    assert o._ohlc_kwargs(None) == {}                     # None -> close-only
    kw = o._ohlc_kwargs(b)
    assert set(kw) == {"opens", "highs", "lows"}          # all three, never partial
    honest = o.simulate([100.0, 99.0], [1, 0], stop_ticks=10, target_ticks=999, **kw)
    assert honest.realized_pnl == pytest.approx(-10.0)    # saw the touch at 90
    sliced = o._ohlc_kwargs(b, 0, 1)
    assert len(sliced["highs"]) == 1


def test_ohlc_from_frame_rejects_a_close_only_table():
    """A close-only tape must fail loud, not silently fall back to the biased
    level-booking path that made the first variant-D run fiction."""
    pd = pytest.importorskip("pandas")
    with pytest.raises(ValueError, match="close-only"):
        o.OHLC.from_frame(pd.DataFrame({"datetime": [1, 2], "close": [1.0, 2.0]}))
