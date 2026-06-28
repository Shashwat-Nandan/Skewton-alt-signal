"""Tests for loop_engine.checker — the independent deterministic verifier (Phase 2).

Rule 9: the checker IS the edge (paper §III-C), so these encode WHY each gate
matters, not just that numbers come back. The statistics are checked against hand
calculations (a checker that computed Sharpe wrong would pass noise); the gates
are checked with a KNOWN-good series that must pass and a KNOWN-bad series that
must be rejected with the failing gate named; and the kalman adapter is checked to
(a) wire the OOS returns into the gates and (b) honestly REJECT the NO-GO
candidate (a low/empty rejection rate is the failure mode, §VI-A).
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loop_engine import checker as ck


# ──────────────────────────────────────────────────────────────────────────
# Deterministic statistics
# ──────────────────────────────────────────────────────────────────────────
def test_sharpe_matches_hand_calc():
    r = np.array([0.01, -0.005, 0.02, 0.0, 0.015])
    expected = r.mean() / r.std() * math.sqrt(252)
    assert ck.annualized_sharpe(r) == pytest.approx(expected)


def test_sharpe_is_nan_when_nothing_varies():
    """A flat (zero-variance) curve has an undefined Sharpe — must be NaN, not a
    flattering 0.0 that could sneak past a >= gate."""
    assert math.isnan(ck.annualized_sharpe(np.zeros(10)))


def test_max_drawdown_known_path():
    # equity: 1.5, 0.75 → peak 1.5 → trough 0.75 → 50% drawdown.
    assert ck.max_drawdown(np.array([0.5, -0.5])) == pytest.approx(0.5)


def test_max_drawdown_zero_for_monotonic_up():
    assert ck.max_drawdown(np.array([0.1, 0.1, 0.1])) == pytest.approx(0.0)


def test_newey_west_with_zero_lags_is_the_plain_tstat():
    r = np.array([0.02, 0.01, 0.03, -0.01, 0.02])
    se = r.std() / math.sqrt(r.size)            # pop std / sqrt(n) at 0 lags
    assert ck.newey_west_tstat(r, lags=0) == pytest.approx(r.mean() / se)


# ──────────────────────────────────────────────────────────────────────────
# Gates
# ──────────────────────────────────────────────────────────────────────────
def _good_returns(n=600, seed=0):
    """A long series with a strong, steady positive edge: high Sharpe, shallow
    drawdown, significant t-stat, and >24 months of daily obs."""
    rng = np.random.default_rng(seed)
    return 0.001 + rng.normal(0, 0.0008, n)


def test_known_good_candidate_passes_all_gates():
    res = ck.apply_gates(_good_returns(), ck.GateThresholds())
    assert res.passed, res.report()
    assert res.failures() == []


def test_known_bad_candidate_is_rejected_naming_the_gate():
    """A no-edge noise series must be killed, and the report must name WHY —
    silent rejection would hide verification debt (§IV-C)."""
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, 600)            # zero mean → Sharpe/t near 0
    res = ck.apply_gates(noise, ck.GateThresholds())
    assert not res.passed
    assert any(f.startswith("sharpe") for f in res.failures())


def test_short_history_fails_the_oos_months_gate():
    """Even a great-looking short sample fails OOS-span — the paper requires a
    real out-of-sample period, not a lucky month."""
    res = ck.apply_gates(_good_returns(n=30), ck.GateThresholds())
    assert not res.passed
    assert any(f.startswith("oos_months") for f in res.failures())


def test_empty_returns_is_killed_not_crashed():
    res = ck.apply_gates(np.array([]), ck.GateThresholds())
    assert not res.passed
    assert "never traded" in res.note


def test_nan_statistic_fails_closed():
    """A flat curve yields NaN Sharpe; the gate must FAIL (fail-closed, Rule 12),
    not pass on a NaN comparison."""
    res = ck.apply_gates(np.zeros(600), ck.GateThresholds())
    assert not res.passed
    assert any(f.startswith("sharpe") for f in res.failures())


def test_thresholds_actually_parsed_from_skill_not_default(tmp_path):
    """Rule 9: use NON-default values so the test fails if from_skill ever stops
    reading SKILL.md (the committed values equal the dataclass defaults, which
    would make a committed-file assertion tautological)."""
    (tmp_path / "kt").mkdir()
    (tmp_path / "kt" / "SKILL.md").write_text(
        "## Rules\n- sharpe_min: 0.7\n- max_dd_max: 0.25\n"
        "- nw_tstat_min: 1.0\n- oos_months_min: 6\n", encoding="utf-8")
    t = ck.GateThresholds.from_skill("kt", root=tmp_path)
    assert (t.sharpe_min, t.max_dd_max, t.nw_tstat_min, t.oos_months_min) == (0.7, 0.25, 1.0, 6.0)


def test_committed_skill_thresholds_parse():
    t = ck.GateThresholds.from_skill("kalman_trend")
    assert (t.sharpe_min, t.max_dd_max, t.nw_tstat_min, t.oos_months_min) == (1.5, 0.10, 2.0, 24.0)


def test_malformed_threshold_keeps_default_and_warns(tmp_path, caplog):
    """A fat-fingered value must keep the default LOUDLY (Rule 12), not silently."""
    import logging
    (tmp_path / "kt").mkdir()
    (tmp_path / "kt" / "SKILL.md").write_text(
        "## Rules\n- sharpe_min: 1.5x\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        t = ck.GateThresholds.from_skill("kt", root=tmp_path)
    assert t.sharpe_min == 1.5                       # the dataclass default
    assert any("unparseable" in r.message for r in caplog.records)


def test_annotated_threshold_value_is_tolerated(tmp_path):
    """`0.08 (was 0.10)` / `20000  # tighten` should bind to the leading number."""
    (tmp_path / "kt").mkdir()
    (tmp_path / "kt" / "SKILL.md").write_text(
        "## Rules\n- max_dd_max: 0.08 (was 0.10)\n", encoding="utf-8")
    assert ck.GateThresholds.from_skill("kt", root=tmp_path).max_dd_max == 0.08


# ──────────────────────────────────────────────────────────────────────────
# kalman_trend adapter (wiring; heavy fit monkeypatched for speed/determinism)
# ──────────────────────────────────────────────────────────────────────────
def test_adapter_pools_oos_returns_and_feeds_gates(monkeypatch):
    """The adapter must turn each fold's points-PnL into fractional returns
    (pnl/close) and pool them — verify with a canned fold so cmaes isn't run."""
    import backtest_kalman_trend as bt
    import optimize_kalman_trend as o

    closes = np.full(260, 100.0)                 # flat price; 5 folds @100/30/30

    class _FakeSim:
        def __init__(self, daily):
            self.daily_pnl = np.asarray(daily, float)
            self.n_trades = 3

    monkeypatch.setattr(o, "fit_kalman_reduced", lambda *a, **k: {"x": 1})
    # each test slice is 30 long; PnL of +1 point on a 100 close → +0.01 return
    monkeypatch.setattr(bt, "_fold_oos", lambda *a, **k: _FakeSim(np.ones(30)))

    returns = ck.kalman_trend_oos_returns(closes, train_len=100, test_len=30, step=30)
    assert returns.size == 150                    # 5 folds × 30
    assert np.allclose(returns, 0.01)             # 1 point / 100 close


def test_adapter_rejects_a_no_trade_candidate(monkeypatch):
    """A candidate that never trades (zero PnL → NaN Sharpe) must be REJECTED, not
    pass by default — the honest outcome for the NO-GO strategy (§VI-A)."""
    import backtest_kalman_trend as bt
    import optimize_kalman_trend as o

    closes = np.full(260, 100.0)

    class _FakeSim:
        daily_pnl = np.zeros(30)
        n_trades = 0

    monkeypatch.setattr(o, "fit_kalman_reduced", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(bt, "_fold_oos", lambda *a, **k: _FakeSim())

    res = ck.check_kalman_trend(closes, ck.GateThresholds(),
                                train_len=100, test_len=30, step=30)
    assert not res.passed


def test_default_checker_examines_every_symbol(monkeypatch):
    """The production checker must gate EVERY traded symbol (not a single-index
    proxy) and combine — wired without Kite, gating the real (NO-GO) edge."""
    import backtest_kalman_trend as bt
    import loop_engine.checker as ckmod
    import optimize_kalman_trend as o
    import validate_kalman_trend as vt

    seen = []

    def _fake_load(symbol):
        seen.append(symbol)
        return [], np.full(160, 100.0)            # flat → no edge → REJECT

    monkeypatch.setattr(vt, "load_daily_closes", _fake_load)

    class _Flat:
        daily_pnl = np.zeros(30)
        n_trades = 0

    monkeypatch.setattr(o, "fit_kalman_reduced", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(bt, "_fold_oos", lambda *a, **k: _Flat())

    check = ckmod.default_kalman_trend_checker(symbols=("NIFTY", "BANKNIFTY"))
    res = check(outcome=None)
    assert seen == ["NIFTY", "BANKNIFTY"]         # both examined
    assert not res.passed
    assert any(g.name.startswith("NIFTY.") for g in res.gates)
    assert any(g.name.startswith("BANKNIFTY.") for g in res.gates)


def test_default_checker_fails_closed_on_missing_symbol_data(monkeypatch):
    """A symbol whose daily data is missing must FAIL the verdict, not be silently
    dropped (§VI-A: a low rejection rate is a warning sign, not a pass)."""
    import backtest_kalman_trend as bt
    import loop_engine.checker as ckmod
    import optimize_kalman_trend as o
    import validate_kalman_trend as vt

    def _load(symbol):
        if symbol == "BANKNIFTY":
            raise FileNotFoundError("no BANKNIFTY_daily.csv")
        return [], np.full(700, 100.0)

    monkeypatch.setattr(vt, "load_daily_closes", _load)

    class _Win:
        daily_pnl = np.full(30, 1.0)
        n_trades = 5

    monkeypatch.setattr(o, "fit_kalman_reduced", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(bt, "_fold_oos", lambda *a, **k: _Win())

    res = ckmod.default_kalman_trend_checker(symbols=("NIFTY", "BANKNIFTY"))(outcome=None)
    assert not res.passed                          # BANKNIFTY missing → fail closed
    assert "BANKNIFTY" in res.note
