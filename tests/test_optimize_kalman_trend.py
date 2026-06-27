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
