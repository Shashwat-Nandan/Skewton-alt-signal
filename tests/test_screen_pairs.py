"""
Audit 2026-06-10 task 2.5: tests for the screen_pairs estimators.

A wrong hedge ratio mis-sizes leg B on every live pair; a wrong half-life
mis-ranks the whole candidate list. Both are estimated by small OLS
helpers with no tests until now. These pin them against synthetic series
with a KNOWN beta and a KNOWN mean-reversion speed, so a sign flip or an
x/y transposition fails loudly.
"""
import numpy as np
import pandas as pd
import pytest

from core.screen_pairs import (
    _half_life,
    _hedge_ratio,
    screen_pairs,
    screen_pairs_book,
)


def _cointegrated_panel(n=260, seed=1):
    """A small panel with two cointegrated pairs (A/B, C/D) and an unrelated
    walk E, positive-priced so both screeners (screen_pairs_book uses NPD /
    avg-leg-price) can run."""
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.normal(0, 1, n)) + 300.0
    z = np.cumsum(rng.normal(0, 1, n)) + 250.0
    df = pd.DataFrame({
        "A": x + rng.normal(0, 0.5, n),
        "B": 0.9 * x + 30.0 + rng.normal(0, 0.5, n),
        "C": z + rng.normal(0, 0.5, n),
        "D": 1.1 * z - 20.0 + rng.normal(0, 0.5, n),
        "E": np.cumsum(rng.normal(0, 1, n)) + 400.0,
    }, index=pd.date_range("2025-01-01", periods=n, freq="D"))
    return df


class TestScreenerSchemaParity:
    """The re-base de-dup (issue #68) routes both screeners' per-pair row through
    the shared `_pair_metrics_row`. Pin the invariant that motivated it: the two
    output the SAME candidate schema (book = screen + the extra `npd` column), so
    a new diagnostic column can't land in one screener but not the other."""

    def test_book_columns_are_screen_columns_plus_npd(self):
        panel = _cointegrated_panel()
        sp = screen_pairs(panel, p_threshold=0.05, min_correlation=0.5)
        spb = screen_pairs_book(panel, p_threshold=0.05, npd_prescreen_keep=50)
        assert not sp.empty and not spb.empty, "fixture must yield pairs in both"
        # Both carry rank_score (added post-row); book adds exactly `npd`.
        assert set(spb.columns) == set(sp.columns) | {"npd"}

    def test_shared_metrics_are_value_identical_across_screeners(self):
        """The real invariant the shared `_pair_metrics_row` guarantees: for a
        pair BOTH screeners admit, every column the helper builds is bit-identical
        regardless of caller — so a value regression in one path (not just a
        schema drift) fails. `correlation` is the one DELIBERATE difference
        (screen_pairs stores |corr| from its matrix; the book a signed corrcoef);
        `rank_score`/`npd` are screener-specific. A schema-only test would pass
        through a wrong hedge_ratio/latest_z_score; this one won't."""
        panel = _cointegrated_panel()
        sp = screen_pairs(panel, p_threshold=0.05, min_correlation=0.5)
        spb = screen_pairs_book(panel, p_threshold=0.05, npd_prescreen_keep=50)
        by_sp = {(r.symbol_a, r.symbol_b): r for _, r in sp.iterrows()}
        by_spb = {(r.symbol_a, r.symbol_b): r for _, r in spb.iterrows()}
        common = set(by_sp) & set(by_spb)
        assert common, "fixture must yield at least one pair admitted by both"
        shared = [c for c in sp.columns
                  if c not in ("correlation", "rank_score")]
        for key in common:
            a, b = by_sp[key][shared], by_spb[key][shared]
            pd.testing.assert_series_equal(a, b, check_names=False)
            # correlation IS expected to differ (|corr| vs signed) whenever the
            # book's signed value is negative — assert the documented relation.
            assert by_sp[key]["correlation"] == pytest.approx(
                abs(by_spb[key]["correlation"]))


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
