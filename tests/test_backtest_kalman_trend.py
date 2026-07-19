"""Tests for backtest_kalman_trend — the walk-forward harness (Option B).

Rule 9: pin the two things that make the walk-forward honest. (1) The pooled
OOS Sharpe must be computed on the CONCATENATED test-slice P&L (a per-20-bar-
window Sharpe is dominated by the no-trade penalty — the bug this harness fixes).
(2) The OOS evaluation must be causal: test bars are scored with params fit only
on that fold's train slice, and the pooled OOS day-count must equal the sum of
the (non-overlapping) test windows — no train bars leaking into the score.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research import backtest_kalman_trend as b


def test_pooled_sharpe_matches_manual():
    pnl = np.array([1.0, -0.5, 2.0, 0.0, 1.5, -1.0])
    expect = pnl.mean() / pnl.std() * np.sqrt(252)
    assert b._pooled_sharpe(pnl) == expect
    # zero-variance (e.g. nothing traded) is UNDEFINED → NaN, not 0.0. Returning
    # 0.0 made a no-trade run tie 0>=0 and falsely PASS the gate; NaN can't.
    assert np.isnan(b._pooled_sharpe(np.zeros(5)))


def test_no_trade_walkforward_does_not_falsely_pass():
    """A flat series where nothing trades must NOT report PASS. Previously both
    books pooled all-zero P&L → 0>=0 and 100% fold-win → false GO (Rule 12)."""
    flat = np.full(900, 100.0)        # zero variation → no trades, no signal
    r = b.walk_forward("FLAT", flat, train_len=200, test_len=60, step=60,
                       seeds=[0], n_gen=10, cost=1.0)
    assert r["kal_trades"] == 0
    assert r["passed"] is False


def test_walk_forward_scores_only_nonoverlapping_test_windows():
    """oos_days must equal n_folds × test_len — the pooled series is exactly the
    concatenated, non-overlapping OOS windows (no train leakage, no double
    counting)."""
    rng = np.random.default_rng(0)
    prices = 100 + np.cumsum(rng.normal(0.2, 1.0, 260))
    r = b.walk_forward("SYN", prices, train_len=100, test_len=20, step=20,
                       seeds=[0], n_gen=15, cost=1.0)
    assert r["oos_days"] == r["n_folds"] * 20
    assert {"kal_pooled_sharpe", "ma_pooled_sharpe", "fold_win_rate",
            "passed"} <= r.keys()
    assert 0.0 <= r["fold_win_rate"] <= 1.0
