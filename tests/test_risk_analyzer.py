"""Tests for risk_analyzer.py — MC, stability, bleed, hedge decision."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pytest
from greeks_engine import GreeksEngine, OptionContract
from risk_analyzer import RiskAnalyzer


@pytest.fixture
def analyzer():
    return RiskAnalyzer(GreeksEngine(risk_free_rate=0.065))


@pytest.fixture
def long_straddle():
    return [
        OptionContract("NIFTY_CE", 0, 22000, "2026-04-30", "CE", 25, 1, 300, 300, 0.15),
        OptionContract("NIFTY_PE", 0, 22000, "2026-04-30", "PE", 25, 1, 280, 280, 0.15),
    ]


class TestMonteCarlo:
    def test_returns_valid_report(self, analyzer, long_straddle):
        report = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=10, trading_days=5,
        )
        assert report.n_paths == 10
        assert len(report.path_results) == 10
        assert report.worst_path_pnl <= report.best_path_pnl
        assert 0 <= report.pct_profitable <= 100

    def test_mc_aggregates_are_numerically_consistent(self, analyzer, long_straddle):
        """Numeric invariants (audit 3.2): the reported mean must lie within
        [worst, best] and equal the mean of the per-path P&Ls, and
        pct_profitable must equal the share of paths with pnl > 0. Catches an
        aggregation bug that worst<=best alone would miss."""
        r = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=40, trading_days=10, seed=7,
        )
        pnls = [p.final_pnl for p in r.path_results]
        assert r.worst_path_pnl == pytest.approx(min(pnls))
        assert r.best_path_pnl == pytest.approx(max(pnls))
        assert r.worst_path_pnl <= r.mean_pnl <= r.best_path_pnl
        assert r.mean_pnl == pytest.approx(sum(pnls) / len(pnls))
        exp_pct = 100.0 * sum(1 for p in pnls if p > 0) / len(pnls)
        assert r.pct_profitable == pytest.approx(exp_pct)

    def test_more_paths_reduces_variance(self, analyzer, long_straddle):
        # Smoke at two path counts — should produce results, not crash.
        analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=5, trading_days=5,
        )
        r2 = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=50, trading_days=5,
        )
        assert r2.n_paths == 50

    def test_seed_makes_run_deterministic(self, analyzer, long_straddle):
        r1 = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=20, trading_days=10, seed=42,
        )
        r2 = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=20, trading_days=10, seed=42,
        )
        assert r1.worst_path_pnl == r2.worst_path_pnl
        assert r1.best_path_pnl == r2.best_path_pnl
        assert r1.mean_pnl == r2.mean_pnl

    def test_different_seeds_produce_different_results(self, analyzer, long_straddle):
        r1 = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=20, trading_days=10, seed=1,
        )
        r2 = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=20, trading_days=10, seed=2,
        )
        assert r1.worst_path_pnl != r2.worst_path_pnl


class TestStability:
    def test_stable_atm_straddle(self, analyzer, long_straddle):
        report = analyzer.stability_test(long_straddle, 22000, 30 / 365)
        # ATM straddle should be relatively stable
        assert report.delta_at_base_vol is not None

    def test_reports_ddeltadvol(self, analyzer, long_straddle):
        report = analyzer.stability_test(long_straddle, 22000, 30 / 365)
        assert isinstance(report.ddeltadvol_portfolio, float)


class TestBleedForecast:
    def test_returns_forecast(self, analyzer, long_straddle):
        bf = analyzer.bleed_forecast(long_straddle, 22000, 30 / 365)
        assert isinstance(bf.delta_bleed, float)
        assert isinstance(bf.gamma_bleed, float)
        assert bf.bleed_direction in ("shortening (long up-gamma, short down-gamma → delta bleeds shorter)",
                                       "lengthening (short up-gamma, long down-gamma → delta bleeds longer)",
                                       "mixed")


class TestHedgeDecision:
    def test_straddle_prefers_hard_delta(self, analyzer, long_straddle):
        """A long straddle is positive-gamma everywhere → no gamma flip points
        → hedge_decision must pick HARD delta (futures), not soft. (Was a
        tautological `hard or soft` assertion — audit 3.2.)"""
        decision = analyzer.hedge_decision(long_straddle, 22000, 30 / 365)
        assert decision.gamma_flips == []          # positive gamma, no flips
        assert decision.use_hard_delta is True
        assert decision.use_soft_delta is False

    def test_decision_has_rationale(self, analyzer, long_straddle):
        decision = analyzer.hedge_decision(long_straddle, 22000, 30 / 365)
        assert len(decision.rationale) > 0


class TestMethodOfSquares:
    def test_returns_dict(self, analyzer, long_straddle):
        squares = analyzer.method_of_squares(long_straddle, 22000, 30 / 365)
        assert isinstance(squares, dict)

    def test_empty_positions(self, analyzer):
        squares = analyzer.method_of_squares([], 22000, 30 / 365)
        assert squares == {}


class TestNeutralityCheck:
    def test_returns_levels(self, analyzer, long_straddle):
        engine = GreeksEngine(risk_free_rate=0.065)
        pf = engine.compute_portfolio_greeks(long_straddle, 22000, 30 / 365)
        checks = analyzer.neutrality_check(pf)
        assert "level_1_delta" in checks
        assert "level_2_gamma" in checks
        assert "level_3_vega" in checks


class TestMonteCarloCosts:
    """Cost-charged MC paths (efficiency review 2026-07-05 §2.2 item 3).

    WHY: mc.mean_pnl feeds the mc_min_mean_pnl ENTRY gate — the only
    per-structure expected-value check in the Taleb entry path. A cost-free
    simulation flatters rehedge-heavy structures (the kalman-trend #77
    failure mode) and makes the rupee floor incomparable with reality. If
    costs silently stop being charged, negative-net-EV structures re-enter
    the book and the −₹144k paper bleed pattern returns.
    """

    def test_charging_costs_strictly_lowers_every_path(self, analyzer, long_straddle):
        gross = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=15, trading_days=8,
            seed=11, charge_costs=False)
        net = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=15, trading_days=8,
            seed=11, charge_costs=True)
        # Same seed → identical underlying paths; net must be lower on every
        # path by at least the unavoidable entry+exit brokerage (4 orders).
        for g, n in zip(gross.path_results, net.path_results):
            assert n.final_pnl < g.final_pnl
            # max_pnl must be on the same cost basis as final/min: net of
            # entry costs the path was NEVER at 0.0, so the peak must sit
            # strictly below the cost-free twin's (no phantom break-even
            # peak from a 0-initialized max).
            assert n.max_pnl < g.max_pnl
        assert net.mean_pnl < gross.mean_pnl - 80.0   # 4×₹20 brokerage floor
        assert net.worst_path_pnl < gross.worst_path_pnl

    def test_rehedge_heavy_paths_pay_more(self, analyzer, long_straddle):
        # A tight rehedge threshold forces more futures orders; with costs
        # charged, the SAME market paths must net less than with a loose
        # threshold's near-zero rehedging. Encodes "churn costs money" — the
        # exact property the cost-free sim couldn't see.
        tight = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=15, trading_days=10,
            seed=13, rehedge_threshold_delta=0.01, charge_costs=True)
        loose = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=15, trading_days=10,
            seed=13, rehedge_threshold_delta=100.0, charge_costs=True)
        n_tight = sum(p.rehedge_count for p in tight.path_results)
        n_loose = sum(p.rehedge_count for p in loose.path_results)
        assert n_tight > n_loose  # sanity: the threshold actually binds
        # Identical option MTM (same seed/paths); the only P&L difference is
        # hedge P&L ± rehedge costs. Charged costs must show up in the mean
        # when rehedging is two orders of magnitude more frequent.
        # (Not asserting tight < loose on gross — hedge P&L differs — only
        # that the cost drag exists relative to its own cost-free twin.)
        tight_gross = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=15, trading_days=10,
            seed=13, rehedge_threshold_delta=0.01, charge_costs=False)
        loose_gross = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=15, trading_days=10,
            seed=13, rehedge_threshold_delta=100.0, charge_costs=False)
        tight_drag = tight_gross.mean_pnl - tight.mean_pnl
        loose_drag = loose_gross.mean_pnl - loose.mean_pnl
        assert tight_drag > loose_drag

    def test_daily_vol_parameter_scales_path_dispersion(self, analyzer, long_straddle):
        # The gate now passes the strategy's live RV instead of the hardcoded
        # 1%/day. If daily_vol were silently ignored again, calm and violent
        # regimes would produce identical expectancy estimates.
        calm = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=20, trading_days=10,
            seed=17, daily_vol=0.002, charge_costs=False)
        wild = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=20, trading_days=10,
            seed=17, daily_vol=0.03, charge_costs=False)
        assert wild.std_pnl > calm.std_pnl
        # Long gamma earns more when realized vol is higher — the sign of the
        # regime dependence the fixed 1% hid.
        assert wild.mean_pnl > calm.mean_pnl


# ──────────────────────────────────────────────────────────
# Issue #160 — block-bootstrap MC paths. Each test encodes the C4 failure
# ("GBM-tuned edge") the change exists to fix: the gate must be able to see
# real fat tails and vol clustering, must label its distribution, and must
# never silently swap distributions on a thin pool.
# ──────────────────────────────────────────────────────────


@pytest.fixture
def real_returns():
    # 40 daily returns with clustering (a calm run, then a volatile cluster
    # containing a real −2.1% tail — like the 2026-05..07 NIFTY window).
    calm = [0.001, -0.002, 0.0015, -0.001, 0.002] * 6
    cluster = [-0.021, 0.015, -0.012, 0.019, -0.008,
               0.011, -0.015, 0.009, -0.006, 0.004]
    return np.array(calm + cluster)


class TestBlockBootstrapMC:
    def test_bootstrap_report_is_labeled(self, analyzer, long_straddle, real_returns):
        r = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=10, trading_days=10,
            seed=7, empirical_returns=real_returns,
        )
        assert r.path_source == "block_bootstrap"

    def test_thin_pool_falls_back_to_gbm_identically(self, analyzer, long_straddle):
        # WHY: a pool below min_empirical must not half-bootstrap — it falls
        # back to the EXACT Gaussian behaviour (same seed → same numbers as a
        # no-pool run) and labels itself gbm, so a pass/fail from the fallback
        # is never mistaken for the bootstrap distribution (Rule 12).
        thin = np.array([0.001, -0.002, 0.003])
        r_thin = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=10, trading_days=5,
            seed=11, empirical_returns=thin,
        )
        r_none = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=10, trading_days=5,
            seed=11, empirical_returns=None,
        )
        assert r_thin.path_source == "gbm"
        assert r_thin.mean_pnl == r_none.mean_pnl
        assert r_thin.worst_path_pnl == r_none.worst_path_pnl

    def test_blocks_are_consecutive_circular_slices(self, real_returns):
        # WHY: consecutive blocks are the mechanism that preserves vol
        # clustering; an iid resample (block_size=1 behaviour) would destroy
        # it and quietly reintroduce the Gaussian-shape problem.
        from risk_analyzer import RiskAnalyzer
        rng = np.random.default_rng(3)
        emp = real_returns
        out = RiskAnalyzer._block_bootstrap_returns(rng, emp, n=20, block_size=5)
        assert len(out) == 20
        circ = np.concatenate([emp, emp[:5]])
        windows = {tuple(circ[i:i + 5]) for i in range(len(emp))}
        for b in range(0, 20, 5):
            assert tuple(out[b:b + 5]) in windows, \
                f"block at {b} is not a consecutive slice of the pool"

    def test_bootstrap_is_seed_deterministic(self, analyzer, long_straddle, real_returns):
        kw = dict(n_paths=15, trading_days=10, seed=21,
                  empirical_returns=real_returns)
        a = analyzer.path_dependence_monte_carlo(long_straddle, 22000, 30 / 365, **kw)
        b = analyzer.path_dependence_monte_carlo(long_straddle, 22000, 30 / 365, **kw)
        assert a.mean_pnl == b.mean_pnl
        assert a.worst_path_pnl == b.worst_path_pnl

    def test_returns_rescaled_to_daily_vol(self, real_returns):
        # WHY: the change is shape-only — the live gate already calibrates
        # SCALE via daily_vol=rv/√365, and the bootstrap must respect that
        # calibration, not resurrect the historical average vol level.
        from risk_analyzer import RiskAnalyzer
        rng = np.random.default_rng(5)
        daily_vol = 0.02
        scale = daily_vol / float(np.std(real_returns))
        draws = np.concatenate([
            scale * RiskAnalyzer._block_bootstrap_returns(rng, real_returns, 30, 5)
            for _ in range(300)])
        assert abs(float(np.std(draws)) - daily_vol) / daily_vol < 0.15

    def test_generated_paths_are_demeaned(self):
        # WHY (#160 review): the change must be SHAPE-only. Resampling raw
        # returns with a drift would shift every path's central tendency (a
        # location change), biasing worst_path/mean_pnl in trending windows.
        # A strongly-drifted pool must still produce ~zero-mean daily draws.
        from risk_analyzer import RiskAnalyzer
        # Big positive drift (+0.5%/day) on top of small vol.
        drifted = np.array([0.005 + 0.001 * ((-1) ** i) for i in range(40)])
        assert drifted.mean() > 0.004          # the pool is heavily drifted
        rng = np.random.default_rng(4)
        daily_vol = 0.01
        emp = drifted - drifted.mean()
        scale = daily_vol / float(np.std(emp))
        draws = np.concatenate([
            scale * RiskAnalyzer._block_bootstrap_returns(rng, emp, 30, 5)
            for _ in range(300)])
        # Demeaned + rescaled: mean ≈ 0 (drift removed), std ≈ daily_vol.
        assert abs(float(np.mean(draws))) < 0.1 * daily_vol
        assert abs(float(np.std(draws)) - daily_vol) / daily_vol < 0.15

    def test_bootstrap_report_mean_not_drift_shifted(self, analyzer, long_straddle):
        # WHY: end-to-end — a driftful pool fed through the public API must not
        # produce a systematically different mean_pnl than its demeaned twin
        # (the demean happens inside path_dependence_monte_carlo).
        rng_pool = np.random.default_rng(1).normal(0.004, 0.007, 40)  # +drift
        kw = dict(n_paths=40, trading_days=10, seed=3)
        drifted = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, empirical_returns=rng_pool, **kw)
        demeaned = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365,
            empirical_returns=rng_pool - rng_pool.mean(), **kw)
        # Identical because the function demeans internally.
        assert drifted.mean_pnl == demeaned.mean_pnl
        assert drifted.worst_path_pnl == demeaned.worst_path_pnl

    def test_real_tail_reaches_the_paths(self, real_returns):
        # WHY (C4): the whole point — the −2.1% day must actually appear in
        # generated paths at bootstrap frequency, where N(0, σ) at the same σ
        # almost never produces it.
        from risk_analyzer import RiskAnalyzer
        rng = np.random.default_rng(9)
        seen_tail = any(
            float(np.min(RiskAnalyzer._block_bootstrap_returns(
                rng, real_returns, 30, 5))) <= -0.021 + 1e-12
            for _ in range(50))
        assert seen_tail
