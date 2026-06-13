"""
Audit 2026-06-10 task 2.5: tests for the autoresearch parameter generator.

These pin the three decisions that, if silently flipped, would let the
weekly loop "optimize" in the wrong direction without any error:
  * _mutate_one — range clamp, per-param rounding, cross-param invariants.
  * _evaluate_experiment — keep-if-strictly-better (a `>`→`<` flip here is
    the canonical "sign-flip in fitness" the audit calls out).
  * _run_experiment — variance penalty, zero-trade penalty, drawdown veto;
    the actual scalar the accept/reject decision consumes.

The heavy constructor needs a live hedger + config; we bypass it with
__new__ and set only the attributes each method reads.
"""
import configparser
import copy
from types import SimpleNamespace

import numpy as np
import pytest

from autoresearch_loop import ZERO_TRADE_PENALTY, HedgeResearchLoop


def _loop(**attrs):
    loop = HedgeResearchLoop.__new__(HedgeResearchLoop)
    for k, v in attrs.items():
        setattr(loop, k, v)
    return loop


# ── _mutate_one ────────────────────────────────────────────

class TestMutateOne:
    def test_clamps_to_upper_bound(self, monkeypatch):
        # A huge positive Gaussian step must clamp to `high`, never exceed it.
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 1e9)
        loop = _loop(mutation_step=0.1)
        low, high = loop.TUNABLE_RANGES["gamma_scalp_band_pct"]
        params = {"gamma_scalp_band_pct": 1.0}
        old, new = loop._mutate_one(params, "gamma_scalp_band_pct")
        assert old == 1.0
        assert new == high
        assert params["gamma_scalp_band_pct"] == high   # mutated in place

    def test_clamps_to_lower_bound(self, monkeypatch):
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: -1e9)
        loop = _loop(mutation_step=0.1)
        low, _ = loop.TUNABLE_RANGES["gamma_scalp_band_pct"]
        params = {"gamma_scalp_band_pct": 2.0}
        _, new = loop._mutate_one(params, "gamma_scalp_band_pct")
        assert new == low

    def test_vega_limit_rounds_to_integer(self, monkeypatch):
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 13.37)
        loop = _loop(mutation_step=0.0001)
        params = {"vega_limit": 4000.0}
        _, new = loop._mutate_one(params, "vega_limit")
        assert new == round(new)            # integer-valued

    def test_default_param_rounds_to_4dp(self, monkeypatch):
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 0.0001234567)
        loop = _loop(mutation_step=0.001)
        params = {"cost_hurdle_factor": 2.0}
        _, new = loop._mutate_one(params, "cost_hurdle_factor")
        assert new == round(new, 4)

    def test_iv_min_invariant_stays_below_max(self, monkeypatch):
        # entry_iv_percentile_min must remain <= max - 5 even if the step
        # would push it above. Force a big upward step.
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 1e9)
        loop = _loop(mutation_step=0.5)
        params = {"entry_iv_percentile_min": 10.0, "entry_iv_percentile_max": 40.0}
        _, new = loop._mutate_one(params, "entry_iv_percentile_min")
        assert new <= 40.0 - 5


# ── _evaluate_experiment (the accept/reject sign) ──────────

class TestEvaluateExperiment:
    def test_none_baseline_accepts(self):
        assert _loop(baseline_metric=None)._evaluate_experiment(-5.0) is True

    def test_strictly_better_accepts(self):
        assert _loop(baseline_metric=1.0)._evaluate_experiment(1.0001) is True

    def test_equal_is_rejected(self):
        # strictly-better, not better-or-equal — guards against drift churn
        assert _loop(baseline_metric=1.0)._evaluate_experiment(1.0) is False

    def test_worse_is_rejected(self):
        # the canonical sign-flip guard: if `>` became `<`, this passes worse
        assert _loop(baseline_metric=1.0)._evaluate_experiment(0.5) is False


# ── _run_experiment (the fitness scalar) ───────────────────

def _run_loop(eval_cycles=1, primary="gamma_theta_ratio",
              max_dd_threshold=20.0, variance_penalty=0.5):
    cfg = configparser.ConfigParser()
    cfg.add_section("autoresearch")
    cfg.set("autoresearch", "variance_penalty", str(variance_penalty))
    hedger = SimpleNamespace(
        tunable_params={"gamma_scalp_band_pct": 1.0},
        immutable_params={"total_capital": 500000},
        underlying="NIFTY",
    )
    return _loop(
        config=cfg, eval_cycles=eval_cycles, primary_metric=primary,
        max_dd_threshold=max_dd_threshold, hedger=hedger,
        _iv_seed=[], _skew_seed=[], _config_path="config.ini",
    )


def _patch_backtest(monkeypatch, metrics_seq):
    """Stub the (locally-imported) backtest helpers so _run_experiment
    takes the synthetic path and run_backtest yields `metrics_seq` in
    order, one dict per cycle."""
    seq = list(metrics_seq)
    calls = {"i": 0}

    def fake_run_backtest(*a, **k):
        m = seq[calls["i"] % len(seq)]
        calls["i"] += 1
        return {"metrics": m}

    monkeypatch.setattr("backtest.list_captured_sessions", lambda u: [])
    monkeypatch.setattr("backtest.load_iv_skew_seed",
                        lambda u, drop_recent=0: ([], []))
    monkeypatch.setattr("backtest.generate_synthetic_data",
                        lambda **k: object())
    monkeypatch.setattr("backtest.run_backtest", fake_run_backtest)


class TestRunExperiment:
    def test_drawdown_breach_is_vetoed(self, monkeypatch):
        # max_drawdown 50% of capital >> 20% threshold → hard reject scalar.
        _patch_backtest(monkeypatch, [
            {"gamma_theta_ratio": 5.0, "total_trades": 3, "max_drawdown": 250000},
        ])
        loop = _run_loop(eval_cycles=1, max_dd_threshold=20.0)
        assert loop._run_experiment({"gamma_scalp_band_pct": 1.2}) == -999999.0

    def test_zero_trades_gets_penalty(self, monkeypatch):
        _patch_backtest(monkeypatch, [
            {"gamma_theta_ratio": 9.9, "total_trades": 0, "max_drawdown": 0},
        ])
        loop = _run_loop(eval_cycles=1)
        assert loop._run_experiment({"gamma_scalp_band_pct": 1.2}) == ZERO_TRADE_PENALTY

    def test_variance_penalty_subtracts_half_a_stddev(self, monkeypatch):
        # cycles return 10 and 20 → mean 15, std 5, penalty 0.5*5 → 12.5
        _patch_backtest(monkeypatch, [
            {"gamma_theta_ratio": 10.0, "total_trades": 3, "max_drawdown": 0},
            {"gamma_theta_ratio": 20.0, "total_trades": 3, "max_drawdown": 0},
        ])
        loop = _run_loop(eval_cycles=2, variance_penalty=0.5)
        assert loop._run_experiment({"gamma_scalp_band_pct": 1.2}) == pytest.approx(12.5)

    def test_consistent_winner_beats_unstable_spike(self, monkeypatch):
        # Same mean (15), but the consistent pair has zero variance and so a
        # higher penalized fitness — the whole point of the penalty.
        _patch_backtest(monkeypatch, [
            {"gamma_theta_ratio": 15.0, "total_trades": 3, "max_drawdown": 0},
            {"gamma_theta_ratio": 15.0, "total_trades": 3, "max_drawdown": 0},
        ])
        steady = _run_loop(eval_cycles=2)._run_experiment({"gamma_scalp_band_pct": 1.2})
        _patch_backtest(monkeypatch, [
            {"gamma_theta_ratio": 0.0, "total_trades": 3, "max_drawdown": 0},
            {"gamma_theta_ratio": 30.0, "total_trades": 3, "max_drawdown": 0},
        ])
        spiky = _run_loop(eval_cycles=2)._run_experiment({"gamma_scalp_band_pct": 1.2})
        assert steady > spiky

    def test_params_restored_after_run(self, monkeypatch):
        _patch_backtest(monkeypatch, [
            {"gamma_theta_ratio": 5.0, "total_trades": 3, "max_drawdown": 0},
        ])
        loop = _run_loop(eval_cycles=1)
        before = copy.deepcopy(loop.hedger.tunable_params)
        loop._run_experiment({"gamma_scalp_band_pct": 9.9})
        assert loop.hedger.tunable_params == before   # mutation didn't leak
