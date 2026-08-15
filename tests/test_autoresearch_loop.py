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

from runners.autoresearch_loop import PNL_METRICS, ZERO_TRADE_PENALTY, HedgeResearchLoop


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
        monkeypatch.setattr("runners.autoresearch_loop.random.random", lambda: 0.0)
        monkeypatch.setattr("runners.autoresearch_loop.random.choice", lambda seq: seq[0])
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
        #
        # The band left TUNABLE_RANGES on 2026-08-09 (demoted to a feature
        # under regime dispatch), so the range is injected here: _mutate_one
        # still carries the invariant, and this pins that it survives — the
        # band stays config-settable on the legacy path, and if the range is
        # ever restored the guard must still hold.
        monkeypatch.setattr(HedgeResearchLoop, "TUNABLE_RANGES",
                            {**HedgeResearchLoop.TUNABLE_RANGES,
                             "entry_iv_percentile_min": (5.0, 30.0),
                             "entry_iv_percentile_max": (20.0, 90.0)})
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 1e9)
        loop = _loop(mutation_step=0.5)
        params = {"entry_iv_percentile_min": 10.0, "entry_iv_percentile_max": 40.0}
        _, new = loop._mutate_one(params, "entry_iv_percentile_min")
        assert new <= 40.0 - 5

    def test_iv_band_is_not_swept_under_regime_dispatch(self):
        # WHY (2026-08-09): while the band was BOTH a hard entry gate and a
        # swept tunable, the sweep spent experiments pulling
        # entry_iv_percentile_max down — which is how it reached 43, blocking
        # 100% of out-of-band ticks by the upper bound and making
        # CALENDAR_SHORT_FRONT (regime_calendar_iv_pct_min = 70) unreachable.
        # Now that the gate is a feature under dispatch, sweeping it moves
        # nothing; re-adding it would resurrect the wasted experiments the
        # 2026-06-07 min_rv_iv_ratio/skew_pct_max drop already ruled out.
        for key in ("entry_iv_percentile_min", "entry_iv_percentile_max"):
            assert key not in HedgeResearchLoop.TUNABLE_RANGES
            assert not any(key in pair for pair in HedgeResearchLoop.JOINT_PAIRS)


# ── _propose_mutation no-op guard (2026-08-08 sweep, experiment 20) ──

class TestProposeMutationNoOp:
    """The 2026-08-08 weekly sweep logged
    `[20/25] rejected ... (mutated entry_iv_percentile_min: 5.0000 -> 5.0000)`.
    `entry_iv_percentile_min` was already sitting on its TUNABLE_RANGES low
    (5.0), so the outward Gaussian step clamped straight back to it. That
    burned one of 25 replays (~6 min) re-measuring a config already scored,
    and counted toward the plateau_share that sweep_quality reads as
    'landscape is flat'. WHY it matters: the budget is the whole search — a
    wasted experiment is a config never explored, and a fake plateau makes
    an uninformative sweep look informative."""

    @staticmethod
    def _pinned_loop(**over):
        cfg = configparser.ConfigParser()
        cfg.add_section("autoresearch")
        cfg.set("autoresearch", "joint_mutation_prob", "0.0")
        attrs = dict(
            config=cfg, mutation_step=0.1,
            baseline_params={"entry_iv_percentile_min": 5.0,
                             "entry_iv_percentile_max": 21.0},
            experiment_number=20,
        )
        attrs.update(over)
        return _loop(**attrs)

    def test_pinned_param_does_not_produce_a_noop(self, monkeypatch):
        # Only the two IV-band params are reachable; `min` is pinned at its
        # low bound, so a downward step is a guaranteed no-op. The proposer
        # must re-draw until something actually moves.
        monkeypatch.setattr(HedgeResearchLoop, "TUNABLE_RANGES",
                            {"entry_iv_percentile_min": (5.0, 30.0),
                             "entry_iv_percentile_max": (20.0, 90.0)})
        loop = self._pinned_loop()
        # Always step downward — pins `min`, but `max` can still move down.
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: -8.0)
        params, name, old, new = loop._propose_mutation()
        assert params != loop.baseline_params, (
            f"proposer returned an unchanged param set ({name}: {old} -> {new})"
        )

    def test_fully_pinned_space_warns_and_still_returns(self, monkeypatch, caplog):
        # Degenerate range (low == high == current) — nothing can ever move.
        # Rule 12: return a well-formed experiment but say so loudly rather
        # than spin or crash.
        monkeypatch.setattr(HedgeResearchLoop, "TUNABLE_RANGES",
                            {"entry_iv_percentile_min": (5.0, 5.0)})
        loop = self._pinned_loop(
            baseline_params={"entry_iv_percentile_min": 5.0})
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 3.0)
        with caplog.at_level("WARNING"):
            params, name, old, new = loop._propose_mutation()
        assert old == new == 5.0
        assert params == loop.baseline_params
        assert "no-op" in caplog.text

    def test_redraw_is_bounded(self, monkeypatch):
        # The retry must not be unbounded: a fully-pinned space has to exit
        # after _MUTATION_ATTEMPTS draws, not loop forever.
        monkeypatch.setattr(HedgeResearchLoop, "TUNABLE_RANGES",
                            {"entry_iv_percentile_min": (5.0, 5.0)})
        loop = self._pinned_loop(
            baseline_params={"entry_iv_percentile_min": 5.0})
        monkeypatch.setattr(np.random, "normal", lambda *a, **k: 3.0)
        calls = []
        orig = HedgeResearchLoop._propose_mutation_once
        monkeypatch.setattr(
            HedgeResearchLoop, "_propose_mutation_once",
            lambda self: (calls.append(1), orig(self))[1])
        loop._propose_mutation()
        assert len(calls) == HedgeResearchLoop._MUTATION_ATTEMPTS


# ── the report's "Baseline:" line (2026-08-08 sweep) ──

class TestReportBaselineIsTheSeed:
    """The 2026-08-08 sweep printed `Baseline: -188.767916 / Best:
    -188.767916` for a run whose seed actually scored -2739.60 — the
    console report hid a 14x improvement and read as 'the sweep found
    nothing'. Cause: `loop.baseline_metric` is the hill-climber's CURRENT
    anchor, overwritten on every acceptance, not the seed's score. The
    candidate JSON was always right (sweep_quality.seed_baseline); only
    the human-facing summary was wrong, and the summary is what the
    operator reads before deciding whether to look further."""

    def test_baseline_metric_drifts_on_acceptance(self):
        # The mechanism: pin that baseline_metric is NOT a stable seed
        # score, so anything reporting "the baseline" must not read it.
        loop = _loop(
            baseline_metric=-2739.6, best_metric_value=-2739.6,
            baseline_params={"vega_limit": 4000.0},
            best_params={"vega_limit": 4000.0},
            hedger=SimpleNamespace(tunable_params={}),
            experiment_number=0, _experiment_records=[],
            vetoed_baseline_abs_floor=0.0,
            primary_metric="convexity_edge",
        )
        loop._propose_mutation = lambda: (
            {"vega_limit": 1974.0}, "vega_limit", 4000.0, 1974.0)
        loop._run_experiment = lambda params: -188.77
        loop._log_experiment = lambda *a, **k: None

        loop.run_single_experiment()

        assert loop.baseline_metric == -188.77, "acceptance must move the anchor"
        assert loop.baseline_metric != -2739.6, (
            "baseline_metric no longer holds the seed score — a report that "
            "prints it as 'Baseline' shows Baseline == Best on every sweep"
        )

    def test_report_prints_seed_baseline_not_the_drifting_anchor(self):
        # Guard the call site itself: the "Baseline:" line in the report
        # must read the captured seed, not loop.baseline_metric. A source
        # check (cf. the AST sweep in test_arbitrage) because the report
        # lives inline in main() and has no seam to assert on.
        import ast
        import inspect
        from runners import run_autoresearch as _mod

        tree = ast.parse(inspect.getsource(_mod))
        offenders = [
            ast.dump(n) for n in ast.walk(tree)
            if isinstance(n, ast.JoinedStr)
            and any(isinstance(v, ast.Constant)
                    and isinstance(v.value, str) and "Baseline:" in v.value
                    for v in n.values)
            and any(isinstance(a, ast.Attribute) and a.attr == "baseline_metric"
                    for v in n.values for a in ast.walk(v))
        ]
        assert not offenders, (
            "the report's 'Baseline:' f-string reads loop.baseline_metric, "
            "which drifts on every acceptance — use the captured "
            "seed_baseline instead"
        )


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

    # ── vetoed baseline → absolute floor (issue #159) ──

    def test_vetoed_baseline_rejects_nonvetoed_negative(self):
        # WHY: the 2026-07-19 re-score degeneracy — with a vetoed seed, a
        # −3,600 candidate "beat" −999999 and would have anchored the sweep.
        loop = _loop(baseline_metric=-999999.0, vetoed_baseline_abs_floor=0.0)
        assert loop._evaluate_experiment(-3600.0) is False

    def test_vetoed_baseline_accepts_only_above_floor(self):
        loop = _loop(baseline_metric=-999999.0, vetoed_baseline_abs_floor=0.0)
        assert loop._evaluate_experiment(1.0) is True
        # strict: 0.0 fitness (e.g. an all-no-trade config) is not edge
        assert loop._evaluate_experiment(0.0) is False

    def test_vetoed_baseline_configured_floor_respected(self):
        loop = _loop(baseline_metric=-999999.0,
                     vetoed_baseline_abs_floor=500.0)
        assert loop._evaluate_experiment(499.0) is False
        assert loop._evaluate_experiment(501.0) is True

    def test_zero_trade_penalty_baseline_counts_as_vetoed(self):
        # ZERO_TRADE_PENALTY (−1e6) sits below VETO_FITNESS by design: a
        # baseline averaging the zero-trade penalty is equally no baseline.
        loop = _loop(baseline_metric=ZERO_TRADE_PENALTY,
                     vetoed_baseline_abs_floor=0.0)
        assert loop._evaluate_experiment(-1.0) is False

    def test_negative_floor_rejected_at_construction(self, tmp_path):
        # WHY (2026-07-19 review): a negative floor means "accept losing
        # configs" — semantically contrary to the bar's purpose. Fail loud
        # at __init__, before hours of sweep compute, not mid-sweep.
        cfg = tmp_path / "config.ini"
        cfg.write_text(
            "[autoresearch]\n"
            "eval_cycles_per_experiment = 5\n"
            "metric = net_pnl\n"
            "max_drawdown_threshold = 5.0\n"
            "results_file = results.tsv\n"
            "log_file = autoresearch.log\n"
            "mutation_step_size = 0.1\n"
            "vetoed_baseline_abs_floor = -500\n")
        hedger = SimpleNamespace(tunable_params={})
        with pytest.raises(ValueError, match="vetoed_baseline_abs_floor"):
            HedgeResearchLoop(hedger, config_path=str(cfg))


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
        # normally set by __init__ (bypassed by _loop's __new__)
        _experiment_records=[], _tape_cache={}, _replay_sessions=None,
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

    monkeypatch.setattr("research.backtest.list_captured_sessions", lambda u: [])
    monkeypatch.setattr("research.backtest.load_iv_skew_seed",
                        lambda u, drop_recent=0: ([], []))
    monkeypatch.setattr("research.backtest.generate_synthetic_data",
                        lambda **k: object())
    monkeypatch.setattr("research.backtest.run_backtest", fake_run_backtest)


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

        monkeypatch.setattr("research.backtest.list_captured_sessions",
                            lambda u: list(sessions))
        monkeypatch.setattr("research.backtest.load_captured_tape", fake_load)
        monkeypatch.setattr("research.backtest.load_iv_skew_seed",
                            lambda u, drop_recent=0: ([], []))
        monkeypatch.setattr(
            "research.backtest.run_backtest",
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
            "research.backtest.run_backtest",
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
        # Identity alone would still pass under a shallow copy (distinct
        # objects, shared buffers) — mutate what the backtest received and
        # pin that the CACHED frame is untouched, which is the actual
        # poisoning-protection contract.
        seen[0].loc[0, "session"] = "poisoned"
        assert loop._tape_cache["2026-01-01"].loc[0, "session"] == "2026-01-01"

    def test_load_failure_propagates_and_restores_params(self, monkeypatch):
        # A session that fails to LOAD (corrupt archive, missing zstd
        # binary) is an infrastructure failure shared by every experiment.
        # Scoring it -999999 flattened entire sweeps (baseline included)
        # with the root cause buried in per-cycle warnings — it must raise.
        # The loader's real failure shapes since the DuckDB reader:
        # duckdb.InvalidInputException on a corrupt file, RuntimeError
        # from the zstd -t archive gate — use the former so this exercises
        # a genuine (non-fabricated) error type.
        self._patch_tape(monkeypatch, ["2026-01-01"])
        import duckdb

        def broken_load(date_iso, underlying):
            raise duckdb.InvalidInputException(
                'Malformed JSON in file "ticks-2026-01-01.jsonl.zst"')

        monkeypatch.setattr("research.backtest.load_captured_tape", broken_load)
        loop = _run_loop(eval_cycles=1)
        before = copy.deepcopy(loop.hedger.tunable_params)
        with pytest.raises(duckdb.InvalidInputException, match="Malformed"):
            loop._run_experiment({"gamma_scalp_band_pct": 9.9})
        # ...and the propagating error must not leak the mutation into the
        # hedger (the try/finally restore).
        assert loop.hedger.tunable_params == before

    def test_backtest_failure_restores_params(self, monkeypatch):
        # The -999999 early return used to skip the post-loop restore,
        # leaking the rejected mutation into the hedger permanently.
        self._patch_tape(monkeypatch, ["2026-01-01"])

        def broken_backtest(*a, **k):
            raise ValueError("bad params blew up the backtest")

        monkeypatch.setattr("research.backtest.run_backtest", broken_backtest)
        loop = _run_loop(eval_cycles=1)
        before = copy.deepcopy(loop.hedger.tunable_params)
        assert loop._run_experiment({"gamma_scalp_band_pct": 9.9}) == -999999.0
        assert loop.hedger.tunable_params == before

    def test_thin_host_replays_each_session_once_not_wrapped(self, monkeypatch):
        # eval_cycles=5 but only 2 sessions: the old `cycle % len` wrap
        # replayed sessions 2-3x each, biasing the mean toward duplicated
        # days and shrinking the variance penalty's std. Each session must
        # score exactly once.
        loads = self._patch_tape(monkeypatch, ["2026-01-01", "2026-01-02"])
        runs = {"n": 0}

        def counting_backtest(*a, **k):
            runs["n"] += 1
            return {"metrics": {"gamma_theta_ratio": 1.0, "total_trades": 3,
                                "max_drawdown": 0}}

        monkeypatch.setattr("research.backtest.run_backtest", counting_backtest)
        loop = _run_loop(eval_cycles=5)
        loop._run_experiment({"gamma_scalp_band_pct": 1.1})
        assert runs["n"] == 2      # one backtest per session, no wrap
        assert loads["n"] == 2

    def test_replay_window_pinned_across_experiments(self, monkeypatch):
        # The window is snapshotted on first use: sessions appearing
        # mid-sweep (e.g. a live capture file on a Persistent=true
        # catch-up run) must NOT shift the window, which is what keeps
        # fitness comparable across experiments.
        current = ["2026-01-01", "2026-01-02"]
        loads = self._patch_tape(monkeypatch, current)
        monkeypatch.setattr("research.backtest.list_captured_sessions",
                            lambda u: list(current))
        loop = _run_loop(eval_cycles=2)
        loop._run_experiment({"gamma_scalp_band_pct": 1.1})
        current.append("2026-01-03")   # new session lands mid-sweep
        loop._run_experiment({"gamma_scalp_band_pct": 1.2})
        assert loop._replay_sessions == ["2026-01-01", "2026-01-02"]
        assert loads["n"] == 2         # the new session was never parsed


# ── sweep_quality() — the shared verdict (2026-07-02 review fix) ──

class TestSweepQualityMethod:
    """One definition for both entrypoints: runners/run_autoresearch.py's weekly
    sweep and runners/run.py's LOOP-FOREVER mode (whose Ctrl+C save used to write
    best_params.json permanently verdict-less)."""

    def _loop_with(self, records, best, seed=-100.0):
        return _loop(_experiment_records=records, best_metric_value=best)

    def test_zero_accepts_flags_uninformative(self):
        q = self._loop_with(
            [{"accepted": False, "metric_value": -100.0}] * 3, best=-100.0,
        ).sweep_quality(seed_baseline=-100.0)
        assert q["informative"] is False
        assert q["accepted"] == 0
        # 0-accepts implies best==seed; that must be reported as ONE
        # warning, not two co-firing restatements of the same fact.
        assert sum("accepted" in w or "seed baseline" in w
                   for w in q["warnings"]) == 1

    def test_flat_plateau_flagged(self):
        records = [{"accepted": False, "metric_value": -2137.055408}] * 8 \
            + [{"accepted": True, "metric_value": -2000.0}] * 2
        q = self._loop_with(records, best=-2000.0).sweep_quality(-2137.055408)
        assert q["informative"] is False
        assert any("identical fitness" in w for w in q["warnings"])
        assert q["plateau_share"] == 0.8

    def test_healthy_sweep_is_informative(self):
        records = [
            {"accepted": True, "metric_value": -90.0},
            {"accepted": False, "metric_value": -120.0},
            {"accepted": True, "metric_value": -50.0},
        ]
        q = self._loop_with(records, best=-50.0).sweep_quality(-100.0)
        assert q["informative"] is True
        assert q["warnings"] == []
        assert q["accepted"] == 2
        assert q["best"] == -50.0
        assert q["seed_baseline"] == -100.0

    def test_empty_records_do_not_crash(self):
        q = self._loop_with([], best=float("-inf")).sweep_quality(0.0)
        assert q["experiments"] == 0
        assert q["informative"] is False

    def test_vetoed_seed_is_a_headline_warning(self):
        # WHY (issue #159): with a vetoed seed, every "beats seed" comparison
        # in the run is meaningless — the candidate file must carry that as a
        # finding, not leave the reader to check the seed row.
        records = [{"accepted": False, "metric_value": -3600.0}] * 3
        q = _loop(_experiment_records=records, best_metric_value=-999999.0,
                  vetoed_baseline_abs_floor=0.0).sweep_quality(-999999.0)
        assert q["seed_vetoed"] is True
        assert q["informative"] is False
        assert any("SEED VETOED" in w for w in q["warnings"])

    def test_healthy_seed_not_flagged_vetoed(self):
        q = self._loop_with([{"accepted": True, "metric_value": -90.0}],
                            best=-90.0).sweep_quality(-100.0)
        assert q["seed_vetoed"] is False


class TestReplayWindowPreflight:
    """2026-07-12: a stillborn tape (parses to 0 rows) inside the replay
    window raised in EVERY experiment's cycle loop, was converted to the
    -999999 hard-failure sentinel by the per-cycle except, and flattened
    the entire 25-experiment sweep. The pre-flight must exclude such
    sessions and back-fill with older ones so infrastructure defects
    cannot masquerade as fitness."""

    def _patch_tapes(self, monkeypatch, sessions, empty=()):
        import pandas as pd

        def fake_list(u):
            return list(sessions)

        def fake_load(session, underlying):
            if session in empty:
                return pd.DataFrame()
            return pd.DataFrame({"timestamp": pd.to_datetime(["2026-07-01 09:15:00"]),
                                 "last_price": [100.0]})

        monkeypatch.setattr("research.backtest.list_captured_sessions", fake_list)
        monkeypatch.setattr("research.backtest.load_captured_tape", fake_load)
        monkeypatch.setattr("research.backtest.load_iv_skew_seed",
                            lambda u, drop_recent=0: ([], []))
        monkeypatch.setattr(
            "research.backtest.run_backtest",
            lambda *a, **k: {"metrics": {"gamma_theta_ratio": 1.0,
                                         "total_trades": 3,
                                         "max_drawdown": 0.0}})

    def test_stillborn_session_excluded_and_backfilled(self, monkeypatch, caplog):
        import logging
        loop = _run_loop(eval_cycles=3)
        self._patch_tapes(monkeypatch,
                          sessions=["s1", "s2", "s3", "s4", "s5"],
                          empty=("s4",))
        with caplog.at_level(logging.WARNING, logger="autoresearch_loop"):
            fitness = loop._run_experiment({"gamma_scalp_band_pct": 1.2})
        # newest-first walk: s5 ok, s4 EMPTY (skip), s3 ok, s2 back-fills.
        assert loop._replay_sessions == ["s2", "s3", "s5"]
        assert "s4" not in loop._tape_cache
        assert any("stillborn" in r.message for r in caplog.records)
        assert fitness != -999999.0, \
            "a stillborn session must not flatten the sweep to the sentinel"

    def test_all_sessions_stillborn_falls_back_loudly(self, monkeypatch):
        # Degenerate case: every tape empty → no replay sessions → the
        # synthetic-GBM fallback path, not a crash and not the sentinel.
        loop = _run_loop(eval_cycles=2)
        self._patch_tapes(monkeypatch, sessions=["s1", "s2"], empty=("s1", "s2"))
        monkeypatch.setattr("research.backtest.generate_synthetic_data", lambda **k: object())
        fitness = loop._run_experiment({"gamma_scalp_band_pct": 1.2})
        assert loop._replay_sessions == []
        assert fitness != -999999.0


# ──────────────────────────────────────────────────────────
# convexity_edge component fitness (2026-07-18 redesign).
# Each test encodes a Phase-0 failure the objective exists to reject —
# see tasks/todo.md (Taleb fitness redesign) and memory of 07-08/07-10.
# ──────────────────────────────────────────────────────────


def _edge_loop(capital=1_000_000.0, **cfg_overrides):
    cfg = configparser.ConfigParser()
    cfg.add_section("autoresearch")
    for k, v in cfg_overrides.items():
        cfg.set("autoresearch", k, str(v))
    return _loop(
        config=cfg,
        hedger=SimpleNamespace(immutable_params={"total_capital": capital}),
    )


def _cycle(pnl=0.0, theo=0.0, theta=0.0, middle=0.0):
    return {"net_pnl": pnl, "theoretical_scalp_pnl": theo,
            "theta_decay_paid": theta, "middle_band_worst_pnl": middle}


class TestConvexityEdgeFitness:
    def test_middle_short_penalized_despite_crash_day_win(self):
        # WHY (Phase-0 F2 / success criterion 2): the 07-08 crash win was a
        # short-ATM leg getting lucky; the same shape lost ₹11.7k on 07-10's
        # +1.02%. Two configs with IDENTICAL session P&Ls must rank by their
        # structure: deep negative middle bands must score strictly worse.
        loop = _edge_loop()
        pnls = [22_000.0, -11_663.0, -2_000.0, 1_500.0]
        short_middle = [_cycle(pnl=p, middle=-14_000.0) for p in pnls]
        clean = [_cycle(pnl=p, middle=0.0) for p in pnls]
        f_short = loop._convexity_edge_fitness(short_middle)
        f_clean = loop._convexity_edge_fitness(clean)
        assert f_short < f_clean
        # Default w_middle=1.0 → the gap is the mean middle magnitude.
        assert abs((f_clean - f_short) - 14_000.0) < 1e-6

    def test_unmanaged_bleed_is_a_veto_not_a_tiebreak(self):
        # WHY: a −17k/−15k quiet-day bleed (observed in the forward record)
        # on a ₹1M book breaches the 1.5% live daily-loss guard. A config
        # that does this must be disqualified outright — averaging would let
        # a lucky week buy it back.
        loop = _edge_loop()
        cycles = [_cycle(pnl=5_000.0), _cycle(pnl=-17_000.0)]
        assert loop._convexity_edge_fitness(cycles) == -999999.0

    def test_squandered_edge_is_a_veto(self):
        # WHY: a session where realized variance was worth ₹20k against the
        # theta rent (spread ≥ 0.5% of capital) and the config still lost
        # money means the structure/exits threw the tail away — the exact
        # failure "net P&L on quiet windows" could never see.
        loop = _edge_loop()
        cycles = [_cycle(pnl=-1_000.0, theo=22_000.0, theta=2_000.0)]
        assert loop._convexity_edge_fitness(cycles) == -999999.0

    def test_cheap_convexity_outranks_expensive_convexity(self):
        # WHY (component C): identical realized P&L, but config A bought its
        # variance at half the rent (theo 2× theta) while B paid double
        # (theo 0.5× theta). The objective must prefer A — that spread IS the
        # Taleb edge, measurable on every session without a tail.
        loop = _edge_loop()
        pnls = [1_000.0, -500.0]
        a = [_cycle(pnl=p, theo=4_000.0, theta=2_000.0) for p in pnls]
        b = [_cycle(pnl=p, theo=1_000.0, theta=2_000.0) for p in pnls]
        assert loop._convexity_edge_fitness(a) > loop._convexity_edge_fitness(b)

    def test_short_premium_carry_earns_no_spread_credit(self):
        # WHY (2026-07-18 review): theta_decay_paid is NEGATIVE for a short-
        # premium book (rent EARNED). With raw `theo − theta` the collected
        # rent leaks into "spread" and rewards short-vol carry — the exact
        # short-the-middle structure this objective must reject. Rent is
        # clamped at 0, so a short-premium session's carry adds nothing.
        loop = _edge_loop()
        # Two calm sessions, IDENTICAL except one collected ₹3k of theta.
        collected = [_cycle(pnl=2_000.0, theo=-200.0, theta=-3_000.0)]
        neutral = [_cycle(pnl=2_000.0, theo=-200.0, theta=0.0)]
        # The collected rent must NOT buy a higher score.
        assert (loop._convexity_edge_fitness(collected)
                <= loop._convexity_edge_fitness(neutral) + 1e-9)

    def test_long_convexity_outscores_short_vol_carry_at_equal_pnl(self):
        # WHY: same net_pnl, but A genuinely captured realized variance against
        # rent paid (long convexity) while B just harvested theta (short vol,
        # blows up on the next out-of-window tail). A must win outright.
        loop = _edge_loop()
        a = [_cycle(pnl=2_000.0, theo=5_000.0, theta=3_000.0, middle=0.0)]
        b = [_cycle(pnl=2_000.0, theo=-200.0, theta=-3_000.0, middle=-500.0)]
        assert loop._convexity_edge_fitness(a) > loop._convexity_edge_fitness(b)

    def test_zero_trade_config_scores_zero_and_beats_bleeder(self):
        # WHY: choosing not to trade edgeless tape is a legitimate ₹0 outcome
        # (no-promote gates handle "never trades" at promotion time); it must
        # not be vetoed, and it must outrank a config that bleeds within cap.
        loop = _edge_loop()
        flat = [_cycle() for _ in range(5)]
        bleeder = [_cycle(pnl=-8_000.0, theta=3_000.0) for _ in range(5)]
        f_flat = loop._convexity_edge_fitness(flat)
        assert f_flat == 0.0
        assert f_flat > loop._convexity_edge_fitness(bleeder)

    def test_convexity_edge_has_pnl_zero_trade_semantics(self):
        # WHY: if convexity_edge ever leaves PNL_METRICS, zero-trade sessions
        # get ZERO_TRADE_PENALTY injected and the optimizer is pushed to
        # overtrade — the exact pathology the rupee-metric split prevents.
        assert "convexity_edge" in PNL_METRICS


# ──────────────────────────────────────────────────────────
# Phase-3 validation/promotion helpers (2026-07-18 redesign).
# The hold-out must be able to FALSIFY a convexity candidate: include a
# tail session, refuse zero-trade "improvements", bound bleed.
# ──────────────────────────────────────────────────────────

from runners.autoresearch_loop import (  # noqa: E402
    build_validation_verdict,
    load_daily_moves,
    pick_holdout_sessions,
)


class TestPickHoldoutSessions:
    def test_includes_recent_and_tail_session(self):
        # WHY: validating only the most recent (usually quiet) session can
        # never falsify a convexity candidate — the biggest-|move| session
        # outside the window must join the hold-out.
        captured = [f"2026-07-{d:02d}" for d in range(1, 11)]
        window = set(captured[-5:])                    # 06..10 in-window
        moves = {"2026-07-02": -2.1, "2026-07-03": 0.3, "2026-07-04": 0.8,
                 "2026-07-05": -0.2}
        picks = pick_holdout_sessions(captured, window, moves)
        assert picks[0] == "2026-07-05"                # most recent outside
        assert "2026-07-02" in picks                   # the tail session
        assert len(picks) == 2

    def test_tail_equals_recent_collapses_to_one(self):
        captured = ["2026-07-01", "2026-07-02", "2026-07-03"]
        window = {"2026-07-03"}
        moves = {"2026-07-01": 0.1, "2026-07-02": -1.9}
        assert pick_holdout_sessions(captured, window, moves) == ["2026-07-02"]

    def test_no_outside_sessions_yields_empty(self):
        captured = ["2026-07-01"]
        assert pick_holdout_sessions(captured, {"2026-07-01"}, {}) == []

    def test_no_moves_degrades_to_recency_only(self):
        # WHY: stale/missing daily data must degrade loudly-but-safely to the
        # pre-Phase-3 behaviour, not crash the weekly sweep's validation.
        captured = ["2026-07-01", "2026-07-02", "2026-07-03"]
        window = {"2026-07-03"}
        assert pick_holdout_sessions(captured, window, {}) == ["2026-07-02"]


class TestBuildValidationVerdict:
    CAP = 1_000_000.0

    def _r(self, date, pnl, trades=2, dd=1000.0):
        return {"date": date, "net_pnl": pnl, "total_trades": trades,
                "max_drawdown": dd}

    def test_zero_trade_holdout_blocks_promotion(self):
        # WHY: the standing no-promote rule — narrowing gates until nothing
        # trades is "improvement" by abstention, not edge (2026-07-04 lesson).
        v = build_validation_verdict(
            [self._r("2026-07-05", 0.0, trades=0)], self.CAP, {})
        assert v["checks"]["holdout_trades_nonzero"] is False
        assert v["promote_ok"] is False

    def test_tail_day_loss_blocks_promotion(self):
        # WHY: tails are the product. A candidate that lost the −2.1% session
        # must not be promotable no matter how good the quiet days looked.
        moves = {"2026-07-02": -2.1}
        v = build_validation_verdict(
            [self._r("2026-07-02", -7_000.0), self._r("2026-07-05", 3_000.0)],
            self.CAP, moves)
        assert v["checks"]["tail_day_nonnegative"] is False
        assert v["promote_ok"] is False
        assert any("tail" in w for w in v["warnings"])

    def test_bleed_breach_blocks_promotion(self):
        # WHY: mirrors the convexity_edge veto and the live daily-loss guard —
        # a hold-out session losing >1.5% of capital is disqualifying.
        v = build_validation_verdict(
            [self._r("2026-07-05", -16_000.0)], self.CAP, {})
        assert v["checks"]["bleed_bounded"] is False
        assert v["promote_ok"] is False

    def test_healthy_candidate_passes_with_tail_tested(self):
        moves = {"2026-07-02": 1.4}
        v = build_validation_verdict(
            [self._r("2026-07-02", 9_000.0), self._r("2026-07-05", -2_000.0)],
            self.CAP, moves)
        assert v["promote_ok"] is True
        assert v["checks"]["tail_day_nonnegative"] is True

    def test_untested_thesis_warns_but_does_not_block(self):
        # WHY: no tail session available is a data limitation, not a candidate
        # failure — but the verdict must SAY the thesis went untested rather
        # than let silence read as validation (Rule 12).
        v = build_validation_verdict(
            [self._r("2026-07-05", 1_000.0)], self.CAP, {})
        assert v["checks"]["tail_day_nonnegative"] is None
        assert v["promote_ok"] is True
        assert any("NOT tested" in w for w in v["warnings"])

    def test_bootstrap_is_deterministic_and_bounded(self):
        # WHY: success criterion 1 (determinism) — re-running the same window
        # must produce the same verdict, or the verdict is itself noise.
        sessions = [self._r("2026-07-05", 2_000.0)]
        ins = [1_000.0, -500.0, 2_000.0, -1_500.0, 3_000.0]
        p1 = build_validation_verdict(sessions, self.CAP, {}, insample_pnls=ins)
        p2 = build_validation_verdict(sessions, self.CAP, {}, insample_pnls=ins)
        pn = p1["checks"]["bootstrap_p_negative"]
        assert pn == p2["checks"]["bootstrap_p_negative"]
        assert 0.0 <= pn <= 1.0

    def test_too_few_points_reports_none_not_fake_p(self):
        v = build_validation_verdict(
            [self._r("2026-07-05", 2_000.0)], self.CAP, {}, insample_pnls=[1.0])
        assert v["checks"]["bootstrap_p_negative"] is None

    # ── absolute gates: shuffle null + walk-forward (issue #159) ──

    def test_all_negative_sessions_fail_both_absolute_gates(self):
        # WHY (issue #159): the −1.7k..−3.6k re-score candidates — negative
        # everywhere, yet "better than the vetoed seed". The absolute checks
        # must reject them WITHOUT referencing any seed.
        ins = [-1000.0, -2000.0, -1500.0, -3000.0, -500.0, -2500.0,
               -1800.0, -900.0, -2200.0]
        v = build_validation_verdict(
            [self._r("2026-07-05", -1_200.0)], self.CAP, {}, insample_pnls=ins)
        assert v["checks"]["edge_beats_shuffle_null"] is False
        assert v["checks"]["walkforward_any_window_positive"] is False
        assert v["promote_ok"] is False

    def test_consistent_positive_edge_passes_both_absolute_gates(self):
        ins = [1500.0, 2000.0, 1800.0, 2200.0, 1600.0, 1900.0, 2100.0,
               1700.0, 2400.0]
        v = build_validation_verdict(
            [self._r("2026-07-05", 2_000.0)], self.CAP, {}, insample_pnls=ins)
        assert v["checks"]["edge_beats_shuffle_null"] is True
        assert v["checks"]["shuffle_null_p"] < 0.10
        assert v["checks"]["walkforward_any_window_positive"] is True
        assert v["promote_ok"] is True

    def test_convexity_shape_not_blocked_by_walkforward(self):
        # WHY: a tail-harvester bleeds small in quiet windows and earns its
        # P&L in the window holding the tail. "Most windows profitable" would
        # structurally reject a HEALTHY convexity config — the walk-forward
        # gate must fire only when NO window is positive.
        ins = [-800.0, -600.0, -700.0,        # quiet window: managed bleed
               -900.0, 25_000.0, -750.0,      # tail window: the payoff
               -650.0, -800.0, -700.0]        # quiet window: managed bleed
        v = build_validation_verdict(
            [self._r("2026-07-05", -500.0)], self.CAP, {}, insample_pnls=ins)
        assert v["walkforward"]["n_windows"] == 3
        assert v["checks"]["walkforward_any_window_positive"] is True
        assert v["walkforward"]["consistency_rate"] == 0.333  # rounded 1/3
        # A SINGLE tail win is statistically indistinguishable from luck
        # (Phase-0: "won crash day +21,994 by luck") — the shuffle null sits
        # near p=0.5 and must NOT pass on one tail. Deliberate: promotion
        # needs repeated evidence, and the operator sees the p to judge.
        assert v["checks"]["edge_beats_shuffle_null"] is False
        assert 0.3 < v["checks"]["shuffle_null_p"] < 0.7

    def test_multi_tail_positive_mean_fails_with_underpowered_warning(self):
        # WHY (2026-07-19 review): a book whose positive mean is concentrated
        # in k large sessions bottoms out near p≈2^-k — the gate CANNOT pass
        # it at alpha 0.10 until ~4+ tails accumulate. That is insufficient
        # evidence, not a bleeding config, and the warning must say which so
        # the operator reads "wait for more tape", not "reject the shape".
        ins = [-500.0] * 13 + [15_000.0, 12_000.0]
        v = build_validation_verdict(
            [self._r("2026-07-05", 2_000.0)], self.CAP, {}, insample_pnls=ins)
        assert v["checks"]["edge_beats_shuffle_null"] is False
        assert 0.10 < v["checks"]["shuffle_null_p"] < 0.5
        assert any("concentrated in too few sessions" in w
                   for w in v["warnings"])
        # And the in-sample dominance of the series is disclosed, not silent.
        assert v["shuffle_sessions"] == {"insample": 15, "holdout": 1}

    def test_shuffle_p_is_deterministic(self):
        ins = [1000.0, -500.0, 2000.0, -1500.0, 3000.0, 800.0]
        v1 = build_validation_verdict(
            [self._r("2026-07-05", 2_000.0)], self.CAP, {}, insample_pnls=ins)
        v2 = build_validation_verdict(
            [self._r("2026-07-05", 2_000.0)], self.CAP, {}, insample_pnls=ins)
        assert v1["checks"]["shuffle_null_p"] == v2["checks"]["shuffle_null_p"]
        assert 0.0 < v1["checks"]["shuffle_null_p"] <= 1.0

    def test_absolute_gates_none_when_untestable_do_not_block(self):
        # WHY: mirrors the tail-day None semantics — an untestable check is a
        # warned data limitation, not a candidate failure (Rule 12).
        v = build_validation_verdict(
            [self._r("2026-07-05", 1_000.0)], self.CAP, {})
        assert v["checks"]["edge_beats_shuffle_null"] is None
        assert v["checks"]["shuffle_null_p"] is None
        assert v["checks"]["walkforward_any_window_positive"] is None
        assert v["walkforward"] is None
        assert v["promote_ok"] is True
        assert any("could NOT run" in w for w in v["warnings"])
        assert any("walk-forward" in w for w in v["warnings"])


class TestLoadDailyMoves:
    def test_reads_newest_eod_table_and_computes_moves(self, tmp_path, monkeypatch):
        # WHY: hold-out tail selection needs per-session moves; the loader
        # must use the newest EOD snapshot (lex-sorted filename convention,
        # same as the strategy's spot seed) — NIFTY_daily.* went stale 06-25.
        import pandas as pd
        monkeypatch.chdir(tmp_path)
        dc = tmp_path / "data_cache"
        dc.mkdir()
        old = pd.DataFrame({"timestamp": ["2026-01-01 15:30:00"],
                            "underlying_price": [10.0]})
        old.to_csv(dc / "NIFTY_20260101_20260131_eod.csv", index=False)
        new = pd.DataFrame({
            "timestamp": ["2026-07-01 15:30:00", "2026-07-01 15:30:00",
                          "2026-07-02 15:30:00", "2026-07-03 15:30:00"],
            "underlying_price": [24000.0, 24000.0, 24240.0, 23997.6],
        })
        new.to_csv(dc / "NIFTY_20260701_20260731_eod.csv", index=False)
        moves = load_daily_moves("NIFTY")
        assert set(moves) == {"2026-07-02", "2026-07-03"}   # first day has no prior
        assert abs(moves["2026-07-02"] - 1.0) < 1e-9        # +1.0%
        assert abs(moves["2026-07-03"] - (-1.0)) < 1e-9     # −1.0%

    def test_missing_data_returns_empty_dict(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert load_daily_moves("NIFTY") == {}
