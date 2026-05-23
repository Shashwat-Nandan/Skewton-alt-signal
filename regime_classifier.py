"""
regime_classifier.py — Phase 3.1 of the Taleb-Karpathy profitability uplift.

Maps the four observable surface inputs (IV percentile, RV/IV ratio,
put-skew percentile, volatility-of-volatility) to a discrete trade-
structure label. Each label corresponds to one builder in TradeProposer.

Taleb Ch 15 / Ch 16 thesis: long-vs-short volatility is *not* the right
question — the right question is what STRUCTURE harvests the most
edge given the current vol/skew/distribution regime.

This classifier is the bridge between Phase 1's observation gates
(IV-pct, skew-pct, RV/IV) and Phase 3.2's structure builders. The
thresholds are conservatively tuned defaults; the autoresearch loop is
free to mutate them once they're surfaced in tunable_params.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Structure(str, Enum):
    """Discrete trade-structure labels the proposer can build.

    Values are string-typed so they round-trip through TSV / JSON
    without enum-aware serialization. Names match TradeProposer's
    builder methods one-to-one.
    """
    STRADDLE = "straddle"
    CALENDAR_SHORT_FRONT = "calendar_short_front"
    RISK_REVERSAL_LONG_PUT = "risk_reversal_long_put"
    BACKSPREAD = "backspread"
    ASYMMETRIC_STRANGLE = "asymmetric_strangle"
    NO_TRADE = "no_trade"


@dataclass
class RegimeFeatures:
    """Inputs to the classifier. All four are derived from data the
    strategy already collects each scan_and_propose pass:

      - iv_percentile: where ATM IV sits in its rolling history.
      - rv_iv_ratio: realized / implied (RV window configurable).
      - skew_percentile: where (IV(25Δ put) − IV(25Δ call)) sits.
      - vol_of_vol: stdev of recent ATM IV samples ÷ mean. Optional;
        if None, the classifier ignores the 4th-moment branch.

    All percentiles are 0-100. Ratios are unitless. vvol is in
    fractional units (e.g. 0.10 = ATM IV moves ~10% of its level
    one-sigma per day).
    """
    iv_percentile: float
    rv_iv_ratio: float
    skew_percentile: float
    vol_of_vol: Optional[float] = None


@dataclass
class Thresholds:
    """Configurable cutoffs the classifier uses. Defaults are
    conservative but plausible for NIFTY / BANKNIFTY. The autoresearch
    loop can mutate any of these via the strategy's tunable_params.

    Naming convention: <regime>_<bound> — e.g.
    `calendar_iv_pct_min` is the lower bound on IV percentile that
    qualifies an IV-rich regime as calendar-friendly.
    """
    # Long ATM straddle: low-to-mid IV, RV ≥ IV, neutral skew.
    straddle_iv_pct_max: float = 60.0
    straddle_rv_iv_ratio_min: float = 1.0
    straddle_skew_pct_max: float = 70.0

    # Short-front calendar (long gamma, short vega): rich IV, flat skew.
    calendar_iv_pct_min: float = 70.0
    calendar_skew_pct_max: float = 60.0

    # Risk reversal (long 25Δ put, short 25Δ call): rich downside skew.
    risk_reversal_skew_pct_min: float = 80.0

    # Backspread (1×ATM short, 2×OTM long): high vol-of-vol regime, any IV.
    backspread_vvol_min: float = 0.15

    # Asymmetric strangle: biased-asset Type-2 regime (post-event),
    # asymmetric distribution. Triggered when RV ≫ IV and skew is rich.
    asymmetric_strangle_rv_iv_min: float = 1.30
    asymmetric_strangle_skew_pct_min: float = 70.0


def classify(features: RegimeFeatures,
             thresholds: Optional[Thresholds] = None) -> Structure:
    """Map (iv_pct, rv_iv, skew_pct, vvol) → structure label.

    Decision order (priority-ranked, first match wins):
      1. BACKSPREAD — vol-of-vol regime dominates everything else.
         Taleb Ch 16 "fourth moment bet": OTM longs vs ATM shorts.
      2. ASYMMETRIC_STRANGLE — high RV/IV AND rich skew. Post-event
         biased-asset case from Ch 15 Type-2 regime.
      3. RISK_REVERSAL_LONG_PUT — rich skew without elevated RV/IV.
         Buy the underpriced 25Δ put, finance with the overpriced
         25Δ call.
      4. CALENDAR_SHORT_FRONT — IV rich at front month, flat skew.
         Long gamma / short vega via term-structure play.
      5. STRADDLE — RV ≥ IV, neutral skew, IV not extreme.
      6. NO_TRADE — nothing fits.

    The ordering matters: e.g. a high-vvol day with rich skew is
    classified as BACKSPREAD not RISK_REVERSAL_LONG_PUT because
    backspread also benefits from the skew but doesn't carry the
    risk-reversal's left-tail exposure.

    Returns Structure.NO_TRADE when no regime cleanly fits — better to
    skip than book a structurally mismatched trade.
    """
    t = thresholds or Thresholds()
    f = features

    # 1. Vol-of-vol regime
    if f.vol_of_vol is not None and f.vol_of_vol >= t.backspread_vvol_min:
        return Structure.BACKSPREAD

    # 2. Biased-asset post-event regime
    if (f.rv_iv_ratio >= t.asymmetric_strangle_rv_iv_min
            and f.skew_percentile >= t.asymmetric_strangle_skew_pct_min):
        return Structure.ASYMMETRIC_STRANGLE

    # 3. Rich skew without RV blow-out
    if f.skew_percentile >= t.risk_reversal_skew_pct_min:
        return Structure.RISK_REVERSAL_LONG_PUT

    # 4. Rich IV, flat skew → calendar
    if (f.iv_percentile >= t.calendar_iv_pct_min
            and f.skew_percentile <= t.calendar_skew_pct_max):
        return Structure.CALENDAR_SHORT_FRONT

    # 5. Plain straddle case
    if (f.iv_percentile <= t.straddle_iv_pct_max
            and f.rv_iv_ratio >= t.straddle_rv_iv_ratio_min
            and f.skew_percentile <= t.straddle_skew_pct_max):
        return Structure.STRADDLE

    return Structure.NO_TRADE
