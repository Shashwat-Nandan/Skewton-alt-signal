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


class TestBetaSignStability:
    """A pair whose hedge ratio changes DIRECTION across the sample is not
    cointegrated in any usable sense — the fit is re-estimating a relationship
    that isn't there, and the "hedge" it produces points the wrong way for part
    of the holding period.

    Motivation (2026-08-29): of 28 pairs the three pair systems actually traded,
    26 had a γ whose sign flipped across rolling windows — including 8/8 of the
    pairs that were traded with both legs on the SAME side. `DRREDDY/HCLTECH`
    ranged −10.01 to +1.69. The existing |β| ∈ [0.1, 10] guard passes all of
    them, because it only ever looks at one full-sample fit.
    """

    def _panel(self, n=400, seed=3):
        """STABLE: B tracks A with a fixed positive slope throughout.
        FLIPPY: F's relationship to A reverses direction halfway through, so a
        full-sample fit reports one slope the pair never actually held."""
        rng = np.random.default_rng(seed)
        a = np.cumsum(rng.normal(0, 1, n)) + 300.0
        half = n // 2
        flip = np.concatenate([0.8 * a[:half], -0.8 * a[half:] + 1.6 * a[half]])
        return pd.DataFrame({
            "A": a,
            "STABLE": 0.8 * a + 25.0 + rng.normal(0, 0.4, n),
            "FLIPPY": flip + 200.0 + rng.normal(0, 0.4, n),
        }, index=pd.date_range("2025-01-01", periods=n, freq="D"))

    def test_agreement_is_one_for_a_stable_hedge(self):
        """Every rolling window agrees with the full-sample sign → 1.0."""
        from core.screen_pairs import _beta_sign_agreement
        p = self._panel()
        assert _beta_sign_agreement(p, "STABLE", "A", window=120, step=10) == 1.0

    def test_agreement_falls_when_the_hedge_reverses(self):
        """The metric must actually detect a direction reversal, not just noise."""
        from core.screen_pairs import _beta_sign_agreement
        p = self._panel()
        agree = _beta_sign_agreement(p, "FLIPPY", "A", window=120, step=10)
        assert agree < 0.9, f"a reversing hedge scored {agree}, gate would not bite"

    def test_agreement_is_nan_when_the_window_does_not_fit(self):
        """Too little history to judge must read as NOT EVALUATED (nan), never as
        a passing score — the persistent screener runs on ~130-day sub-windows."""
        from core.screen_pairs import _beta_sign_agreement
        import math
        p = self._panel(n=60)
        assert math.isnan(_beta_sign_agreement(p, "STABLE", "A", window=120, step=10))

    def test_screeners_emit_the_diagnostic_column(self):
        """The column must always be present even when the gate is off, so the
        operator can pick a threshold from real data rather than guessing."""
        p = _cointegrated_panel()
        for df in (screen_pairs(p), screen_pairs_book(p)):
            if df.empty:
                continue
            assert "beta_sign_agreement" in df.columns

    def test_gate_is_inert_by_default(self):
        """Default must not change the tradeable universe: this gate would drop
        every one of the 59 live candidates at a zero-flip threshold, and the
        static system's candidates feed a LIVE money runner. Arming it is an
        operator decision."""
        p = _cointegrated_panel()
        assert len(screen_pairs(p)) == len(screen_pairs(p, min_beta_sign_agreement=0.0))

    def test_gate_drops_pairs_below_the_threshold(self):
        """With the gate armed and enough history to judge, a pair scoring below
        the threshold is excluded."""
        p = _cointegrated_panel(n=600)     # long enough for the 250d window
        base = screen_pairs(p)
        if base.empty:
            pytest.skip("no cointegrated pairs in the synthetic panel")
        assert base["beta_sign_agreement"].notna().any(), \
            "panel must be long enough for the gate to be evaluated at all"
        strict = screen_pairs(p, min_beta_sign_agreement=1.01)   # unreachable
        assert len(strict) < len(base), "an armed gate must actually exclude pairs"

    def test_unevaluable_pair_is_kept_but_counted(self, caplog):
        """A pair whose stability window does not fit must be KEPT (a filter that
        could not run must not masquerade as one that passed) — and said out
        loud, so "0 dropped" is never mistaken for "all pairs are stable"."""
        import logging
        p = _cointegrated_panel(n=260)     # shorter than the 250d window + step
        with caplog.at_level(logging.INFO):
            df = screen_pairs(p, min_beta_sign_agreement=0.9)
        if df.empty:
            pytest.skip("no cointegrated pairs in the synthetic panel")
        assert df["beta_sign_agreement"].isna().all()
        assert any("could not be evaluated" in r.message for r in caplog.records)
