"""Tests for regime_classifier.classify — Phase 3.1."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from regime_classifier import (
    RegimeFeatures, Structure, Thresholds, classify,
)


class TestRegimeClassifier:
    def test_low_iv_neutral_skew_returns_straddle(self):
        """Calm regime with RV slightly ≥ IV → plain ATM straddle."""
        f = RegimeFeatures(
            iv_percentile=40.0, rv_iv_ratio=1.05, skew_percentile=50.0,
        )
        assert classify(f) == Structure.STRADDLE

    def test_high_vvol_dominates_returns_backspread(self):
        """Even with rich skew, a high vvol regime routes to backspread
        — fourth-moment bet beats third-moment bet when vol-of-vol is
        the dominant regime feature (Taleb Ch 16)."""
        f = RegimeFeatures(
            iv_percentile=60.0, rv_iv_ratio=1.2, skew_percentile=85.0,
            vol_of_vol=0.20,
        )
        assert classify(f) == Structure.BACKSPREAD

    def test_post_event_returns_asymmetric_strangle(self):
        """RV ≫ IV and rich skew → biased-asset Type-2 regime,
        asymmetric strangle harvests the directional vol."""
        f = RegimeFeatures(
            iv_percentile=50.0, rv_iv_ratio=1.4, skew_percentile=75.0,
            vol_of_vol=0.08,  # below backspread threshold
        )
        assert classify(f) == Structure.ASYMMETRIC_STRANGLE

    def test_rich_skew_alone_returns_risk_reversal(self):
        """Skew rich, RV in line with IV → buy the cheap put, sell the
        expensive call."""
        f = RegimeFeatures(
            iv_percentile=55.0, rv_iv_ratio=1.0, skew_percentile=85.0,
            vol_of_vol=0.08,
        )
        assert classify(f) == Structure.RISK_REVERSAL_LONG_PUT

    def test_rich_iv_flat_skew_returns_calendar(self):
        """Front-month IV richer than back, no skew premium →
        calendar spread (long back, short front)."""
        f = RegimeFeatures(
            iv_percentile=85.0, rv_iv_ratio=0.9, skew_percentile=40.0,
            vol_of_vol=0.05,
        )
        assert classify(f) == Structure.CALENDAR_SHORT_FRONT

    def test_unmatched_regime_returns_no_trade(self):
        """iv_pct between straddle-max and calendar-min, low skew, low
        RV/IV, low vvol — fits no regime."""
        f2 = RegimeFeatures(
            iv_percentile=65.0, rv_iv_ratio=0.7, skew_percentile=65.0,
            vol_of_vol=0.05,
        )
        # iv_pct = 65 below calendar_iv_pct_min=70, above
        # straddle_iv_pct_max=60. skew_pct = 65, below RR threshold 80.
        # vvol below backspread threshold. → NO_TRADE.
        assert classify(f2) == Structure.NO_TRADE

    def test_thresholds_are_overrideable(self):
        """A regime that triggers under defaults must NOT trigger when
        the threshold is tightened — confirms autoresearch can mutate."""
        f = RegimeFeatures(
            iv_percentile=40.0, rv_iv_ratio=1.05, skew_percentile=50.0,
        )
        # Default: STRADDLE. With tighter RV/IV requirement, no trade.
        strict = Thresholds(straddle_rv_iv_ratio_min=1.5)
        assert classify(f, strict) == Structure.NO_TRADE

    def test_none_vvol_skips_backspread_branch(self):
        """If vvol isn't observable, the classifier shouldn't go to
        BACKSPREAD even if skew is rich — vol_of_vol=None is a real
        warmup case worth handling."""
        f = RegimeFeatures(
            iv_percentile=60.0, rv_iv_ratio=1.2, skew_percentile=85.0,
            vol_of_vol=None,
        )
        # Skew 85 ≥ risk_reversal_skew_pct_min=80 → RISK_REVERSAL.
        assert classify(f) == Structure.RISK_REVERSAL_LONG_PUT
