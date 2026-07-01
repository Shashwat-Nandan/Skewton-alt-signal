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

from autoresearch_loop import PNL_METRICS, ZERO_TRADE_PENALTY, HedgeResearchLoop


def _loop(**attrs):
    loop = HedgeResearchLoop.__new__(HedgeResearchLoop)
    for k, v in attrs.items():
        setattr(loop, k, v)
    return loop


# ── JOINT_PAIRS ↔ TUNABLE_RANGES consistency (2026-06-13 crash) ──

class TestJointPairsConsistency:
    """The 2026-06-13 weekly autoresearch died with KeyError
    'min_rv_iv_ratio': it was dropped from TUNABLE_RANGES on 2026-06-07 but
    left in JOINT_PAIRS (and still present in best_params.json), so the
    joint-availability check passed on params membership while _mutate_one
    KeyError'd on the missing range. Pin both the static invariant and the
    runtime guard so a future drop can't reintroduce the crash."""

    def test_every_joint_pair_key_is_a_tunable(self):
        for pair in HedgeResearchLoop.JOINT_PAIRS:
            for key in pair:
                assert key in HedgeResearchLoop.TUNABLE_RANGES, (
                    f"JOINT_PAIRS names {key!r}, absent from TUNABLE_RANGES "
                    f"— _mutate_one would KeyError when this pair is picked"
                )

    def test_propose_mutation_skips_stale_non_tunable_pair(self, monkeypatch):
        # Force the joint branch every time and inject a stale pair whose
        # key is in params but NOT in TUNABLE_RANGES — _propose_mutation
        # must NOT crash; it falls through to a valid single-param walk.
        import configparser

        cfg = configparser.ConfigParser()
        cfg.add_section("autoresearch")
        cfg.set("autoresearch", "joint_mutation_prob", "1.0")
        loop = _loop(
            config=cfg, mutation_step=0.1,
            baseline_params={"gamma_scalp_band_pct": 1.0,
                             "min_rv_iv_ratio": 1.2},   # stale, in params only
        )
        monkeypatch.setattr(HedgeResearchLoop, "JOINT_PAIRS",
                            [("min_rv_iv_ratio", "rv_window_days")])
        monkeypatch.setattr("autoresearch_loop.random.random", lambda: 0.0)
        monkeypatch.setattr("autoresearch_loop.random.choice", lambda seq: seq[0])
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 0.01)
        # Should not raise (no usable joint pair → single-param walk).
        params, name, old, new = loop._propose_mutation()
        assert name in loop.TUNABLE_RANGES   # a real single-param mutation


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

    def test_zero_trades_gets_penalty_for_ratio_metric(self, monkeypatch):
        _patch_backtest(monkeypatch, [
            {"gamma_theta_ratio": 9.9, "total_trades": 0, "max_drawdown": 0},
        ])
        loop = _run_loop(eval_cycles=1)
        assert loop._run_experiment({"gamma_scalp_band_pct": 1.2}) == ZERO_TRADE_PENALTY

    def test_zero_trades_scores_zero_for_pnl_metric(self, monkeypatch):
        # 2026-06-14 objective fix: with a net_pnl objective a no-trade
        # session is ₹0 (a real outcome), NOT the -1e6 ratio penalty —
        # otherwise the optimizer is pushed to overtrade instead of being
        # allowed to trade less when that's more profitable.
        _patch_backtest(monkeypatch, [
            {"net_pnl": 0.0, "total_trades": 0, "max_drawdown": 0},
        ])
        loop = _run_loop(eval_cycles=1, primary="net_pnl")
        assert loop._run_experiment({"gamma_scalp_band_pct": 1.2}) == 0.0

    def test_net_pnl_objective_prefers_more_profit(self, monkeypatch):
        # The accept scalar IS the rupee P&L (variance-penalised). A flat,
        # profitable pair must beat a flat, losing one.
        _patch_backtest(monkeypatch, [
            {"net_pnl": 5000.0, "total_trades": 3, "max_drawdown": 0},
            {"net_pnl": 5000.0, "total_trades": 3, "max_drawdown": 0},
        ])
        win = _run_loop(eval_cycles=2, primary="net_pnl")._run_experiment({"x": 1})
        _patch_backtest(monkeypatch, [
            {"net_pnl": -8000.0, "total_trades": 3, "max_drawdown": 0},
            {"net_pnl": -8000.0, "total_trades": 3, "max_drawdown": 0},
        ])
        lose = _run_loop(eval_cycles=2, primary="net_pnl")._run_experiment({"x": 1})
        assert win == pytest.approx(5000.0)   # zero variance → no penalty
        assert lose == pytest.approx(-8000.0)
        assert win > lose

    def test_net_pnl_is_a_recognized_pnl_metric(self):
        # Guard the objective the weekly cron uses against a future typo
        # that would silently score 0 every experiment (flat fitness).
        assert "net_pnl" in PNL_METRICS

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


# ── _save_best_params sweep_quality stamping (2026-07-02) ──

class TestSaveBestParamsSweepQuality:
    """The 06-20/06-27 sweeps were uninformative (0–2 accepts, flat
    plateau) yet wrote candidate files indistinguishable from real
    optimization output. run_autoresearch now stamps a per-run
    `sweep_quality` verdict into the candidate; these pin that it (a) is
    written, (b) never leaks from the canonical file into a run that
    didn't produce one, and (c) doesn't break `_migrations` preservation."""

    def _saving_loop(self):
        return _loop(
            best_params={"gamma_scalp_band_pct": 1.5},
            primary_metric="net_pnl",
            best_metric_value=-42.0,
            experiment_number=3,
        )

    def test_sweep_quality_written_to_candidate(self, tmp_path, monkeypatch):
        import json
        monkeypatch.chdir(tmp_path)
        quality = {"informative": False, "accepted": 0,
                   "warnings": ["0 mutations accepted"]}
        self._saving_loop()._save_best_params(
            out_file="cand.json", sweep_quality=quality,
        )
        out = json.loads((tmp_path / "cand.json").read_text())
        assert out["sweep_quality"] == quality

    def test_stale_sweep_quality_not_preserved_from_canonical(
            self, tmp_path, monkeypatch):
        # A promoted candidate carries its sweep_quality into
        # best_params.json; the NEXT run must not inherit that verdict.
        import json
        monkeypatch.chdir(tmp_path)
        (tmp_path / "best_params.json").write_text(json.dumps({
            "best_params": {"old": 1},
            "sweep_quality": {"informative": True, "accepted": 9},
            "_migrations": [{"date": "2026-06-07", "note": "history"}],
        }))
        self._saving_loop()._save_best_params(out_file="cand.json")
        out = json.loads((tmp_path / "cand.json").read_text())
        assert "sweep_quality" not in out
        # _migrations preservation must survive the new exclusion.
        assert out["_migrations"] == [{"date": "2026-06-07", "note": "history"}]


# ── Tape cache across experiments (2026-07-02) ──

class TestTapeCache:
    """A 15-session replay window is only affordable because each
    session's multi-GB JSONL is parsed once per sweep, not once per
    experiment (~3 min/parse × 25 experiments would blow the unit's
    10 h timeout). Pin that _run_experiment reuses the cache across
    calls and hands each cycle a copy (a backtest mutating its frame
    must not poison later experiments)."""

    def _patch_tape(self, monkeypatch, sessions):
        import pandas as pd
        loads = {"n": 0}

        def fake_load(date_iso, underlying):
            loads["n"] += 1
            return pd.DataFrame({"session": [date_iso]})

        monkeypatch.setattr("backtest.list_captured_sessions",
                            lambda u: list(sessions))
        monkeypatch.setattr("backtest.load_captured_tape", fake_load)
        monkeypatch.setattr("backtest.load_iv_skew_seed",
                            lambda u, drop_recent=0: ([], []))
        monkeypatch.setattr(
            "backtest.run_backtest",
            lambda *a, **k: {"metrics": {
                "gamma_theta_ratio": 1.0, "total_trades": 3,
                "max_drawdown": 0,
            }},
        )
        return loads

    def test_sessions_parsed_once_across_experiments(self, monkeypatch):
        loads = self._patch_tape(
            monkeypatch, ["2026-01-01", "2026-01-02"],
        )
        loop = _run_loop(eval_cycles=2)
        loop._run_experiment({"gamma_scalp_band_pct": 1.1})
        loop._run_experiment({"gamma_scalp_band_pct": 1.2})
        assert loads["n"] == 2   # once per session, NOT once per cycle

    def test_cycles_get_independent_copies(self, monkeypatch):
        self._patch_tape(monkeypatch, ["2026-01-01"])
        seen = []
        monkeypatch.setattr(
            "backtest.run_backtest",
            lambda data, **k: (seen.append(data), {"metrics": {
                "gamma_theta_ratio": 1.0, "total_trades": 3,
                "max_drawdown": 0,
            }})[1],
        )
        loop = _run_loop(eval_cycles=1)
        loop._run_experiment({"gamma_scalp_band_pct": 1.1})
        loop._run_experiment({"gamma_scalp_band_pct": 1.2})
        assert seen[0] is not seen[1]
        assert seen[0] is not loop._tape_cache["2026-01-01"]
