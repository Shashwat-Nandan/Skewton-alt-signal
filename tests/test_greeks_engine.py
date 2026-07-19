"""Tests for core/greeks_engine.py — BS pricing, Greeks, and portfolio calculations."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import math
import pytest
from core.greeks_engine import (
    GreeksEngine, OptionContract, implied_volatility_bisect,
)


@pytest.fixture
def engine():
    return GreeksEngine(risk_free_rate=0.065)


class TestBSPricing:
    def test_call_put_parity(self, engine):
        """C - P = S - K*exp(-rT) for European options."""
        S, K, T, sigma = 22000, 22000, 30 / 365, 0.15
        C = engine.bs_price(S, K, T, sigma, "CE")
        P = engine.bs_price(S, K, T, sigma, "PE")
        parity = S - K * math.exp(-0.065 * T)
        assert abs((C - P) - parity) < 1.0, f"Put-call parity violated: C-P={C-P}, expected {parity}"

    def test_atm_call_positive(self, engine):
        price = engine.bs_price(22000, 22000, 30 / 365, 0.15, "CE")
        assert price > 0

    def test_expired_itm_call(self, engine):
        assert engine.bs_price(22000, 21000, 0, 0.15, "CE") == pytest.approx(1000.0)

    def test_expired_otm_call(self, engine):
        assert engine.bs_price(22000, 23000, 0, 0.15, "CE") == 0.0

    def test_futures_price_equals_spot(self, engine):
        assert engine.bs_price(22000, 0, 0.1, 0, "FUT") == 22000


class TestGreeks:
    def test_atm_call_delta_near_half(self, engine):
        d = engine.delta(22000, 22000, 30 / 365, 0.15, "CE")
        assert 0.45 < d < 0.65

    def test_atm_put_delta_near_minus_half(self, engine):
        d = engine.delta(22000, 22000, 30 / 365, 0.15, "PE")
        assert -0.65 < d < -0.35  # r>0 shifts ATM put delta above -0.5

    def test_futures_delta_is_one(self, engine):
        assert engine.delta(22000, 0, 0.1, 0, "FUT") == 1.0

    def test_futures_theta_is_zero(self, engine):
        assert engine.theta(22000, 0, 0.1, 0, "FUT") == 0.0

    def test_gamma_positive(self, engine):
        g = engine.gamma(22000, 22000, 30 / 365, 0.15)
        assert g > 0

    def test_vega_positive(self, engine):
        v = engine.vega(22000, 22000, 30 / 365, 0.15)
        assert v > 0

    def test_theta_negative_for_long(self, engine):
        """ATM options should have negative theta."""
        t = engine.theta(22000, 22000, 30 / 365, 0.15, "CE")
        assert t < 0

    def test_discrete_delta_close_to_bs_delta(self, engine):
        S, K, T, sigma = 22000, 22000, 30 / 365, 0.15
        bs_d = engine.delta(S, K, T, sigma, "CE")
        disc_d = engine.discrete_delta(S, K, T, sigma, "CE")
        assert abs(bs_d - disc_d) < 0.05


class TestImpliedVol:
    def test_round_trip(self, engine):
        """IV bisection should recover the vol used to price."""
        S, K, T, r, sigma = 22000, 22000, 30 / 365, 0.065, 0.18
        price = engine.bs_price(S, K, T, sigma, "CE")
        recovered_iv = implied_volatility_bisect(price, S, K, T, r, "CE")
        assert abs(recovered_iv - sigma) < 0.001

    def test_high_vol_round_trip(self, engine):
        S, K, T, r, sigma = 22000, 22000, 30 / 365, 0.065, 0.50
        price = engine.bs_price(S, K, T, sigma, "PE")
        recovered_iv = implied_volatility_bisect(price, S, K, T, r, "PE")
        assert abs(recovered_iv - sigma) < 0.005


class TestPortfolioGreeks:
    def test_straddle_delta_near_zero(self, engine):
        """Long straddle should have near-zero delta."""
        positions = [
            OptionContract("NIFTY_CE", 0, 22000, "2026-04-30", "CE", 25, 1, 300, 300, 0.15),
            OptionContract("NIFTY_PE", 0, 22000, "2026-04-30", "PE", 25, 1, 280, 280, 0.15),
        ]
        T = 30 / 365
        pf = engine.compute_portfolio_greeks(positions, 22000, T)
        # Delta should be close to 0 for ATM straddle (within 1 lot-delta)
        assert abs(pf.net_delta) < 25, f"Straddle delta too large: {pf.net_delta}"

    def test_straddle_positive_gamma(self, engine):
        positions = [
            OptionContract("NIFTY_CE", 0, 22000, "2026-04-30", "CE", 25, 1, 300, 300, 0.15),
            OptionContract("NIFTY_PE", 0, 22000, "2026-04-30", "PE", 25, 1, 280, 280, 0.15),
        ]
        T = 30 / 365
        pf = engine.compute_portfolio_greeks(positions, 22000, T)
        assert pf.net_gamma > 0

    def test_short_position_negative_gamma(self, engine):
        """Short straddle should have negative gamma."""
        positions = [
            OptionContract("NIFTY_CE", 0, 22000, "2026-04-30", "CE", 25, -1, 300, 300, 0.15),
            OptionContract("NIFTY_PE", 0, 22000, "2026-04-30", "PE", 25, -1, 280, 280, 0.15),
        ]
        T = 30 / 365
        pf = engine.compute_portfolio_greeks(positions, 22000, T)
        assert pf.net_gamma < 0

    def test_per_leg_T_buckets_back_month_separately(self, engine):
        """Code-review fix #3: a calendar with front T=3d and back T=45d
        must put each leg's vega in its own bucket. Before the fix, both
        legs used the default T and BOTH landed in '0-30d', erasing the
        term-structure exposure the per_leg_T plumbing was meant to
        surface."""
        positions = [
            OptionContract("NIFTY_FRONT_CE", 0, 22000, "2026-04-03", "CE", 25, 1, 200, 200, 0.15),
            OptionContract("NIFTY_BACK_CE",  0, 22000, "2026-05-22", "CE", 25, 1, 350, 350, 0.18),
        ]
        per_leg_T = {
            "NIFTY_FRONT_CE": 3 / 365,
            "NIFTY_BACK_CE":  45 / 365,
        }
        pf = engine.compute_portfolio_greeks(
            positions, 22000, T=3/365, per_leg_T=per_leg_T,
        )
        assert "0-30d" in pf.vega_buckets, (
            f"Front-month vega missing from 0-30d: {pf.vega_buckets}"
        )
        assert "30-60d" in pf.vega_buckets, (
            f"Back-month vega missing from 30-60d (review-fix #3 "
            f"regression): {pf.vega_buckets}"
        )
        # Both buckets should hold strictly positive vega for long calls
        assert pf.vega_buckets["0-30d"] > 0
        assert pf.vega_buckets["30-60d"] > 0
