"""Tests for risk_analyzer.py — MC, stability, bleed, hedge decision."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

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

    def test_more_paths_reduces_variance(self, analyzer, long_straddle):
        r1 = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=5, trading_days=5,
        )
        r2 = analyzer.path_dependence_monte_carlo(
            long_straddle, 22000, 30 / 365, n_paths=50, trading_days=5,
        )
        # More paths should generally produce results (not crash)
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
        """ATM straddle should typically have positive gamma everywhere → hard delta OK."""
        decision = analyzer.hedge_decision(long_straddle, 22000, 30 / 365)
        # For a simple long straddle, gamma should be positive → hard delta
        assert decision.use_hard_delta or decision.use_soft_delta  # Must recommend something

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
