"""
Audit 2026-06-10 task 2.5: tests for the screen_pairs estimators.

A wrong hedge ratio mis-sizes leg B on every live pair; a wrong half-life
mis-ranks the whole candidate list. Both are estimated by small OLS
helpers with no tests until now. These pin them against synthetic series
with a KNOWN beta and a KNOWN mean-reversion speed, so a sign flip or an
x/y transposition fails loudly.
"""
import numpy as np
import pytest

from screen_pairs import _half_life, _hedge_ratio


class TestHedgeRatio:
    def test_recovers_known_beta(self):
        # y = 2.0 * x + intercept + small noise → OLS slope ≈ 2.0
        rng = np.random.default_rng(7)
        x = np.cumsum(rng.normal(0, 1, 500)) + 100.0
        y = 50.0 + 2.0 * x + rng.normal(0, 0.5, 500)
        beta = _hedge_ratio(y, x)
        assert beta == pytest.approx(2.0, abs=0.02)

    def test_is_directional_not_symmetric(self):
        # _hedge_ratio(y, x) is slope of y on x, NOT x on y. With y = 2x,
        # the y-on-x slope is ~2.0 and the x-on-y slope is ~0.5 — pin the
        # orientation so a future swap of the arguments is caught.
        rng = np.random.default_rng(11)
        x = np.cumsum(rng.normal(0, 1, 500)) + 100.0
        y = 2.0 * x + rng.normal(0, 0.3, 500)
        assert _hedge_ratio(y, x) == pytest.approx(2.0, abs=0.02)
        assert _hedge_ratio(x, y) == pytest.approx(0.5, abs=0.02)

    def test_negative_beta_keeps_its_sign(self):
        rng = np.random.default_rng(3)
        x = np.cumsum(rng.normal(0, 1, 500)) + 100.0
        y = 30.0 - 1.5 * x + rng.normal(0, 0.4, 500)
        assert _hedge_ratio(y, x) == pytest.approx(-1.5, abs=0.02)


class TestHalfLife:
    def test_mean_reverting_series_has_finite_known_half_life(self):
        # AR(1) on the LEVEL with rho=0.9 → the code's phi = rho-1 = -0.1,
        # so half-life = -ln2 / ln(0.9) ≈ 6.58 days. A long series keeps
        # the estimate tight; assert a band around the analytic value.
        rho = 0.9
        rng = np.random.default_rng(42)
        n = 4000
        s = np.zeros(n)
        for t in range(1, n):
            s[t] = rho * s[t - 1] + rng.normal(0, 1)
        expected = -np.log(2) / np.log(rho)        # ≈ 6.58
        hl = _half_life(s)
        assert hl == pytest.approx(expected, rel=0.25)

    def test_faster_reversion_gives_shorter_half_life(self):
        rng = np.random.default_rng(1)
        n = 4000

        def ar1(rho):
            s = np.zeros(n)
            for t in range(1, n):
                s[t] = rho * s[t - 1] + rng.normal(0, 1)
            return _half_life(s)

        # rho=0.5 reverts much faster than rho=0.95 → strictly shorter HL.
        assert ar1(0.5) < ar1(0.95)

    def test_explosive_series_returns_inf(self):
        # rho > 1 → phi = rho-1 > 0 → no mean reversion → inf (the phi>=0
        # guard branch). Deterministic so the branch is pinned exactly.
        s = np.array([1.05 ** t for t in range(200)])
        assert _half_life(s) == float("inf")

    def test_random_walk_is_effectively_non_reverting(self):
        # A pure random walk has phi ≈ 0; finite samples land it slightly
        # either side, so the result is inf OR a very long half-life — never
        # the ~6-day band of a genuinely mean-reverting spread. Pin that
        # separation rather than an exact inf.
        rng = np.random.default_rng(5)
        s = np.cumsum(rng.normal(0, 1, 2000))
        hl = _half_life(s)
        assert hl == float("inf") or hl > 100

    def test_too_short_returns_inf(self):
        assert _half_life(np.array([1.0])) == float("inf")
