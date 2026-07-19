"""
Market Profile / TPO computation.

Inputs are 30-minute (or any other period) OHLC bars. The output is a
horizontal histogram of how much time price spent at each price bin,
plus the standard derived levels every Market Profile reader expects:

  - POC          point of control: the price bin with the most TPOs
  - VAH / VAL    value area high / low: the smallest contiguous range
                 around POC containing >=70% of total TPOs (1σ)
  - IB high/low  initial balance: high/low of the first two periods
                 (the canonical CBOT 60-min IB; we keep it general)
  - bins[]       per-price-bin records, in price-descending order ready
                 for direct rendering (top of the chart = highest price)

Each period (e.g. each 30-min bar) is assigned a single letter — A, B,
C, ... aa, bb, ... — and contributes one TPO to every bin its high-low
range touches. This is the standard TPO construction.

The math is pure: no kite, no DB, no I/O. The router and tests both
import this directly. Keep it that way.
"""
from __future__ import annotations

import math
import string
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence


# ──────────────────────────────────────────────────────────
# Public types
# ──────────────────────────────────────────────────────────

@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass
class PriceBin:
    """One horizontal row of the profile."""
    price_low: float            # inclusive lower edge
    price_high: float           # exclusive upper edge (price_low + tick_size)
    price_mid: float
    tpo_count: int
    letters: str                # concatenated period letters that hit this bin
    in_value_area: bool = False
    is_poc: bool = False


@dataclass
class DayProfile:
    """One trading day's profile."""
    day: date
    bins: List[PriceBin]
    poc: float                  # price_mid of the POC bin
    vah: float                  # value area high (price_mid)
    val: float                  # value area low (price_mid)
    ib_high: Optional[float]
    ib_low: Optional[float]
    open: float
    close: float
    high: float
    low: float
    n_periods: int
    total_tpos: int
    total_volume: int


@dataclass
class CompositeProfile:
    """A multi-day composite (e.g. last N days as one profile)."""
    bins: List[PriceBin]
    poc: float
    vah: float
    val: float
    high: float
    low: float
    total_tpos: int
    total_volume: int
    n_days: int = 0
    n_periods: int = 0


# ──────────────────────────────────────────────────────────
# Tick / period helpers
# ──────────────────────────────────────────────────────────

def auto_tick_size(prices: Sequence[float]) -> float:
    """
    Pick a sensible bin size when the caller doesn't supply one.

    Indian equity market convention is 0.05 paise tick for cash. For a
    market profile we want roughly 30–60 bins across the day's range
    so neither the chart is empty (too-coarse bins) nor smeared (too-fine).
    Aim for ~50 bins across the full sample range, snapped to a "nice"
    increment.
    """
    if not prices:
        return 0.05
    rng = max(prices) - min(prices)
    if rng <= 0:
        return 0.05
    target = rng / 50.0
    # Snap to nice increments: 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10...
    nice = [0.05, 0.10, 0.25, 0.50, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0, 100.0]
    for n in nice:
        if target <= n:
            return n
    return nice[-1]


def period_letter(idx: int) -> str:
    """
    A, B, …, Z, a, b, …, z, AA, BB, …  — covers up to 104 periods/day,
    far more than India's 13 30-min periods or any CBOT extension.
    """
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    if idx < 26:
        return upper[idx]
    if idx < 52:
        return lower[idx - 26]
    if idx < 78:
        return upper[idx - 52] * 2
    if idx < 104:
        return lower[idx - 78] * 2
    return f"#{idx}"   # extreme fallback


# ──────────────────────────────────────────────────────────
# Profile compute
# ──────────────────────────────────────────────────────────

def compute_day_profile(
    bars: Sequence[Bar],
    *,
    tick_size: Optional[float] = None,
    value_area_pct: float = 0.70,
    ib_periods: int = 2,
) -> Optional[DayProfile]:
    """
    Build the TPO profile for one day's worth of bars (already filtered to
    that single day, in chronological order).

    Returns None for an empty `bars` list. The compute is O(periods × bins
    crossed) — at 13 periods × 50 bins it's a couple hundred ops. There's
    no reason to pull pandas in here.
    """
    if not bars:
        return None

    day = bars[0].ts.date()
    high = max(b.high for b in bars)
    low = min(b.low for b in bars)
    if low >= high:
        # Pathological zero-range day — still emit a single-bin profile so
        # downstream rendering doesn't have to special-case it.
        ts = tick_size or 0.05
        letters = "".join(period_letter(i) for i in range(len(bars)))
        return DayProfile(
            day=day,
            bins=[PriceBin(low, low + ts, low, len(bars), letters, True, True)],
            poc=low, vah=low, val=low,
            ib_high=None, ib_low=None,
            open=bars[0].open, close=bars[-1].close,
            high=high, low=low,
            n_periods=len(bars), total_tpos=len(bars),
            total_volume=sum(b.volume for b in bars),
        )

    if tick_size is None or tick_size <= 0:
        tick_size = auto_tick_size([b.high for b in bars] + [b.low for b in bars])

    # Snap the bin grid to multiples of tick_size starting from `low`. Each
    # bin is [base + i*tick, base + (i+1)*tick). A bar's [low, high] hits
    # every bin whose price_mid lies within that range.
    base = _floor_to_tick(low, tick_size)
    n_bins = max(int(round((_ceil_to_tick(high, tick_size) - base) / tick_size)), 1)

    counts = [0] * n_bins
    letters: List[List[str]] = [[] for _ in range(n_bins)]

    for idx, bar in enumerate(bars):
        letter = period_letter(idx)
        # Inclusive low, exclusive high — but the *highest* bar gets the
        # last bin too (otherwise the day's highest tick is invisible).
        lo_idx = max(int((bar.low - base) // tick_size), 0)
        hi_idx = min(int((bar.high - base) // tick_size), n_bins - 1)
        if bar.high == base + (hi_idx + 1) * tick_size:
            hi_idx = min(hi_idx + 1, n_bins - 1)
        for i in range(lo_idx, hi_idx + 1):
            counts[i] += 1
            letters[i].append(letter)

    # POC: the bin with the most TPOs (ties go to the bin closest to the
    # day's mid-price, the standard tie-breaker).
    mid_price = (high + low) / 2
    poc_idx = max(
        range(n_bins),
        key=lambda i: (counts[i], -abs(_bin_mid(base, tick_size, i) - mid_price)),
    )

    # Value Area: expand symmetrically from POC, at each step adding the
    # bin (above or below the current envelope) with the larger TPO count,
    # until cumulative TPO fraction >= value_area_pct.
    total = sum(counts)
    target = value_area_pct * total
    va_lo = va_hi = poc_idx
    cum = counts[poc_idx]
    while cum < target and (va_lo > 0 or va_hi < n_bins - 1):
        up = counts[va_hi + 1] if va_hi + 1 < n_bins else -1
        dn = counts[va_lo - 1] if va_lo - 1 >= 0 else -1
        if up == -1 and dn == -1:
            break
        if up >= dn:
            va_hi += 1
            cum += up
        else:
            va_lo -= 1
            cum += dn

    # Initial Balance — first `ib_periods` periods' high/low.
    ib_bars = bars[: max(ib_periods, 0)]
    if ib_bars:
        ib_high = max(b.high for b in ib_bars)
        ib_low = min(b.low for b in ib_bars)
    else:
        ib_high = ib_low = None

    bins_out: List[PriceBin] = []
    for i in range(n_bins - 1, -1, -1):  # highest price first → top of chart
        bins_out.append(PriceBin(
            price_low=base + i * tick_size,
            price_high=base + (i + 1) * tick_size,
            price_mid=_bin_mid(base, tick_size, i),
            tpo_count=counts[i],
            letters="".join(letters[i]),
            in_value_area=(va_lo <= i <= va_hi),
            is_poc=(i == poc_idx),
        ))

    return DayProfile(
        day=day,
        bins=bins_out,
        poc=_bin_mid(base, tick_size, poc_idx),
        vah=_bin_mid(base, tick_size, va_hi),
        val=_bin_mid(base, tick_size, va_lo),
        ib_high=ib_high, ib_low=ib_low,
        open=bars[0].open, close=bars[-1].close,
        high=high, low=low,
        n_periods=len(bars), total_tpos=total,
        total_volume=sum(b.volume for b in bars),
    )


def compute_composite(
    bars: Sequence[Bar],
    *,
    tick_size: Optional[float] = None,
    value_area_pct: float = 0.70,
) -> Optional[CompositeProfile]:
    """
    Composite profile across multiple days. Each bar contributes one TPO
    to every bin its [low, high] range touches — no per-day letters, the
    composite is purely a histogram.
    """
    if not bars:
        return None

    high = max(b.high for b in bars)
    low = min(b.low for b in bars)
    if low >= high:
        ts = tick_size or 0.05
        return CompositeProfile(
            bins=[PriceBin(low, low + ts, low, len(bars), "", True, True)],
            poc=low, vah=low, val=low, high=high, low=low,
            total_tpos=len(bars),
            total_volume=sum(b.volume for b in bars),
            n_days=len({b.ts.date() for b in bars}),
            n_periods=len(bars),
        )

    if tick_size is None or tick_size <= 0:
        tick_size = auto_tick_size([b.high for b in bars] + [b.low for b in bars])

    base = _floor_to_tick(low, tick_size)
    n_bins = max(int(round((_ceil_to_tick(high, tick_size) - base) / tick_size)), 1)
    counts = [0] * n_bins

    for bar in bars:
        lo_idx = max(int((bar.low - base) // tick_size), 0)
        hi_idx = min(int((bar.high - base) // tick_size), n_bins - 1)
        if bar.high == base + (hi_idx + 1) * tick_size:
            hi_idx = min(hi_idx + 1, n_bins - 1)
        for i in range(lo_idx, hi_idx + 1):
            counts[i] += 1

    mid_price = (high + low) / 2
    poc_idx = max(
        range(n_bins),
        key=lambda i: (counts[i], -abs(_bin_mid(base, tick_size, i) - mid_price)),
    )
    total = sum(counts)
    target = value_area_pct * total
    va_lo = va_hi = poc_idx
    cum = counts[poc_idx]
    while cum < target and (va_lo > 0 or va_hi < n_bins - 1):
        up = counts[va_hi + 1] if va_hi + 1 < n_bins else -1
        dn = counts[va_lo - 1] if va_lo - 1 >= 0 else -1
        if up == -1 and dn == -1:
            break
        if up >= dn:
            va_hi += 1
            cum += up
        else:
            va_lo -= 1
            cum += dn

    bins_out: List[PriceBin] = []
    for i in range(n_bins - 1, -1, -1):
        bins_out.append(PriceBin(
            price_low=base + i * tick_size,
            price_high=base + (i + 1) * tick_size,
            price_mid=_bin_mid(base, tick_size, i),
            tpo_count=counts[i],
            letters="",
            in_value_area=(va_lo <= i <= va_hi),
            is_poc=(i == poc_idx),
        ))

    distinct_days = {b.ts.date() for b in bars}
    return CompositeProfile(
        bins=bins_out,
        poc=_bin_mid(base, tick_size, poc_idx),
        vah=_bin_mid(base, tick_size, va_hi),
        val=_bin_mid(base, tick_size, va_lo),
        high=high, low=low,
        total_tpos=total,
        total_volume=sum(b.volume for b in bars),
        n_days=len(distinct_days),
        n_periods=len(bars),
    )


def split_by_day(bars: Sequence[Bar]) -> List[List[Bar]]:
    """Group a flat bar list into per-day lists (chronological)."""
    if not bars:
        return []
    out: List[List[Bar]] = []
    cur_day = bars[0].ts.date()
    cur: List[Bar] = []
    for b in bars:
        if b.ts.date() != cur_day:
            if cur:
                out.append(cur)
            cur = [b]
            cur_day = b.ts.date()
        else:
            cur.append(b)
    if cur:
        out.append(cur)
    return out


# ──────────────────────────────────────────────────────────
# Market-generated indicators (Dalton, *Markets in Profile*)
# ──────────────────────────────────────────────────────────
#
# The engine above gives the static levels every profile reader draws
# (POC / VAH / VAL / IB). Dalton's *profitable* reading, though, lives in the
# higher-order indicators the raw levels don't name: how the day OPENED (the
# conviction gauge — Ch 8), what SHAPE the day took (trend vs the p/b
# short-covering / long-liquidation shells — Ch 7), where today's value sits
# versus YESTERDAY's (the balance/imbalance call — Fig 4.5), whether the auction
# left EXCESS at its extremes (a finished auction vs a "poor" high/low that gets
# revisited), and whether it is ONE-TIMEFRAMING (a directional, trending
# auction).
#
# These are codifications of Dalton's qualitative descriptions into
# deterministic geometry (Rule 5: geometry, not model judgment). The thresholds
# are stated inline and are the assumptions a reader should challenge (Rule 1);
# they exist to be MEASURED against real tape (Phase 2), not trusted a priori.


@dataclass
class DayIndicators:
    """Higher-order market-generated indicators for one day's profile."""
    day: date
    # Ch 8 — opening conviction (Steidlmayer's four opens):
    #   open_drive_{up,down} > open_test_drive_{up,down} >
    #   open_rejection_reverse_{up,down} > open_auction
    open_type: str
    # Ch 7 — day/profile shape: trend_{up,down} / neutral / p_shape / b_shape /
    #   normal.  p = short-covering shell (fat value up top, tail below);
    #   b = long-liquidation shell (fat value at bottom, tail above).
    day_shape: str
    # Always-on "where is the value" read, independent of whether the day also
    # trended: p (POC in upper third) / b (POC in lower third) / balanced. Kept
    # separate from day_shape because a trend day and a p/b shell are the same
    # geometry in a moving market — Dalton separates them by context, so we
    # report both rather than blend them (Rule 7).
    profile_skew: str
    # Fig 4.5 — today's value area vs prior day's:
    #   higher / lower / overlapping_higher / overlapping_lower / inside /
    #   outside / unknown (no prior).  Overlapping/inside = balance;
    #   higher/lower/outside = imbalance.
    balance_state: str
    in_balance: bool
    # Initial-balance range extension (which side broke the first-2-period range,
    # and which side broke FIRST).
    range_ext_up: bool
    range_ext_down: bool
    range_ext_first: str        # up / down / both / none
    # Excess (single-print tail ≥2 bins at an extreme = auction finished there)
    # vs a poor high/low (multiple prints at the extreme, no tail → likely
    # revisited).
    excess_high: bool
    excess_low: bool
    poor_high: bool
    poor_low: bool
    single_print_count: int
    single_print_levels: List[float]
    # One-timeframing: longest run of periods each making a higher low (up) or
    # lower high (down) — a directional/trending auction.
    one_timeframing: str        # up / down / none
    one_timeframing_run: int
    # Context (copied from the profile so a logged row is self-contained).
    open: float
    close: float
    high: float
    low: float
    poc: float
    vah: float
    val: float
    ib_high: Optional[float]
    ib_low: Optional[float]


def _tail_single_runs(bins: Sequence[PriceBin]) -> tuple[int, int]:
    """(top_run, bottom_run) — consecutive single-TPO bins at each extreme.

    `bins` is price-descending (bins[0] = highest price), as emitted by
    `compute_day_profile`. A run ≥2 at an extreme is Dalton's *excess*.
    """
    top = 0
    for b in bins:
        if b.tpo_count == 1:
            top += 1
        else:
            break
    bottom = 0
    for b in reversed(bins):
        if b.tpo_count == 1:
            bottom += 1
        else:
            break
    return top, bottom


def _one_timeframing(bars: Sequence[Bar]) -> tuple[str, int]:
    """Longest one-timeframing run and its direction.

    Up = each period makes a *strictly* higher low (real directional progress);
    down = each period makes a strictly lower high. Strict (not ≥) so that flat
    "camping" bars — equal highs/lows in a rotational day — do NOT masquerade as
    a trend. Returns the longer of the two runs; ties resolve to "up" only if
    that run is ≥2, else "none".
    """
    n = len(bars)
    if n < 2:
        return "none", 0
    best_up = up = 1
    best_dn = dn = 1
    for i in range(1, n):
        up = up + 1 if bars[i].low > bars[i - 1].low else 1
        dn = dn + 1 if bars[i].high < bars[i - 1].high else 1
        best_up = max(best_up, up)
        best_dn = max(best_dn, dn)
    if best_up < 2 and best_dn < 2:
        return "none", max(best_up, best_dn)
    if best_up >= best_dn:
        return "up", best_up
    return "down", best_dn


def _classify_open(
    bars: Sequence[Bar],
    *,
    prior_high: Optional[float],
    prior_low: Optional[float],
    prior_val: Optional[float],
    prior_vah: Optional[float],
) -> str:
    """Steidlmayer's four opening types, most-confident first (Ch 8).

    Tolerances are expressed as fractions of the day's range so the classifier
    is scale-free across NIFTY (~₹100s range) and a ₹50 stock.
    """
    n = len(bars)
    o = bars[0].open
    high = max(b.high for b in bars)
    low = min(b.low for b in bars)
    close = bars[-1].close
    rng = high - low
    if n < 2 or rng <= 0:
        return "open_auction"

    near = 0.10 * rng            # "at" an extreme
    eps = max(0.02 * rng, 1e-9)  # "meaningfully through" the open

    # Include the opening bar: a dip/spike through the open inside period 1 is a
    # trade back through the open and must disqualify an Open-Drive (the drive's
    # defining property is that the open is never revisited).
    traded_above = any(b.high > o + eps for b in bars)
    traded_below = any(b.low < o - eps for b in bars)

    # Open-Drive: opens at one extreme and never trades back through the open.
    if (o - low) <= near and not traded_below and close > o + eps:
        return "open_drive_up"
    if (high - o) <= near and not traded_above and close < o - eps:
        return "open_drive_down"

    # Early extreme (first ~2 periods) and whether it tested a prior reference.
    k = min(2, n)
    early_high = max(b.high for b in bars[:k])
    early_low = min(b.low for b in bars[:k])
    day_low_early = (early_low - low) <= eps
    day_high_early = (high - early_high) <= eps
    tested_below = early_low < o - eps
    tested_above = early_high > o + eps
    ref_below = ((prior_low is not None and early_low <= prior_low + eps)
                 or (prior_val is not None and early_low <= prior_val + eps))
    ref_above = ((prior_high is not None and early_high >= prior_high - eps)
                 or (prior_vah is not None and early_high >= prior_vah - eps))

    # Open-Test-Drive: pokes a known reference, finds no business, reverses and
    # drives the other way — the failed test secures one extreme.
    if tested_below and day_low_early and ref_below and close > o + eps and traded_above:
        return "open_test_drive_up"
    if tested_above and day_high_early and ref_above and close < o - eps and traded_below:
        return "open_test_drive_down"

    # Open-Rejection-Reverse: drives one way, gets rejected back through the
    # open, closes the other way (no specific reference test).
    if tested_below and close > o + eps and traded_above:
        return "open_rejection_reverse_up"
    if tested_above and close < o - eps and traded_below:
        return "open_rejection_reverse_down"

    # Open-Auction: no conviction off the open.
    return "open_auction"


def _classify_balance(
    val: float, vah: float,
    prior_val: Optional[float], prior_vah: Optional[float],
) -> str:
    """Today's value area vs prior day's (Fig 4.5)."""
    if (prior_val is None or prior_vah is None
            or math.isnan(prior_val) or math.isnan(prior_vah)):
        return "unknown"
    if val > prior_vah:
        return "higher"
    if vah < prior_val:
        return "lower"
    if val <= prior_val and vah >= prior_vah:
        return "outside"
    if val >= prior_val and vah <= prior_vah:
        return "inside"
    mid = (val + vah) / 2
    prior_mid = (prior_val + prior_vah) / 2
    return "overlapping_higher" if mid >= prior_mid else "overlapping_lower"


def market_generated_indicators(
    bars: Sequence[Bar],
    *,
    prior: Optional[DayProfile] = None,
    tick_size: Optional[float] = None,
    value_area_pct: float = 0.70,
    ib_periods: int = 2,
) -> Optional[DayIndicators]:
    """Compute Dalton's market-generated indicators for one day of bars.

    `bars` must be a single day, chronological (as for `compute_day_profile`).
    `prior` is yesterday's `DayProfile`; without it, `balance_state` is
    "unknown" and Open-Test-Drive detection loses its reference test (it then
    falls through to Open-Rejection-Reverse, which is the honest, lower-
    confidence read). Returns None for empty input.
    """
    profile = compute_day_profile(
        bars, tick_size=tick_size, value_area_pct=value_area_pct,
        ib_periods=ib_periods,
    )
    if profile is None:
        return None

    prior_high = prior.high if prior else None
    prior_low = prior.low if prior else None
    prior_val = prior.val if prior else None
    prior_vah = prior.vah if prior else None

    open_type = _classify_open(
        bars, prior_high=prior_high, prior_low=prior_low,
        prior_val=prior_val, prior_vah=prior_vah,
    )
    balance_state = _classify_balance(profile.val, profile.vah, prior_val, prior_vah)
    in_balance = balance_state in {"inside", "overlapping_higher", "overlapping_lower"}

    one_tf_dir, one_tf_run = _one_timeframing(bars)

    # Range extension vs the initial balance, and which side broke first.
    ext_up = profile.ib_high is not None and profile.high > profile.ib_high
    ext_down = profile.ib_low is not None and profile.low < profile.ib_low
    ext_first = "none"
    for b in bars[max(ib_periods, 0):]:
        up = profile.ib_high is not None and b.high > profile.ib_high
        dn = profile.ib_low is not None and b.low < profile.ib_low
        if up and dn:
            ext_first = "both"
            break
        if up:
            ext_first = "up"
            break
        if dn:
            ext_first = "down"
            break

    top_single, bottom_single = _tail_single_runs(profile.bins)
    excess_high = top_single >= 2
    excess_low = bottom_single >= 2
    poor_high = (not excess_high) and profile.bins[0].tpo_count >= 2
    poor_low = (not excess_low) and profile.bins[-1].tpo_count >= 2
    single_levels = [b.price_mid for b in profile.bins if b.tpo_count == 1]

    day_shape = _classify_shape(
        profile, bars, one_tf_dir, one_tf_run, top_single, bottom_single,
        ext_up, ext_down,
    )

    rng = profile.high - profile.low
    poc_pos = (profile.poc - profile.low) / rng if rng > 0 else 0.5
    profile_skew = "p" if poc_pos > 0.60 else "b" if poc_pos < 0.40 else "balanced"

    return DayIndicators(
        day=profile.day,
        open_type=open_type,
        day_shape=day_shape,
        profile_skew=profile_skew,
        balance_state=balance_state,
        in_balance=in_balance,
        range_ext_up=ext_up,
        range_ext_down=ext_down,
        range_ext_first=ext_first,
        excess_high=excess_high,
        excess_low=excess_low,
        poor_high=poor_high,
        poor_low=poor_low,
        single_print_count=len(single_levels),
        single_print_levels=single_levels,
        one_timeframing=one_tf_dir,
        one_timeframing_run=one_tf_run,
        open=profile.open,
        close=profile.close,
        high=profile.high,
        low=profile.low,
        poc=profile.poc,
        vah=profile.vah,
        val=profile.val,
        ib_high=profile.ib_high,
        ib_low=profile.ib_low,
    )


def _classify_shape(
    profile: DayProfile,
    bars: Sequence[Bar],
    one_tf_dir: str,
    one_tf_run: int,
    top_single: int,
    bottom_single: int,
    ext_up: bool,
    ext_down: bool,
) -> str:
    """Day/profile shape (Ch 7). Checked most-specific first."""
    rng = profile.high - profile.low
    n = len(bars)
    if rng <= 0 or n < 2:
        return "normal"
    poc_pos = (profile.poc - profile.low) / rng   # 0 = at low, 1 = at high

    # Neutral: two-sided range extension — buyers AND sellers extended the IB,
    # i.e. genuine two-way indecision.
    if ext_up and ext_down:
        return "neutral"
    # Trend: sustained one-timeframing across most of the session.
    if one_tf_run >= max(3, int(0.6 * n)):
        if one_tf_dir == "up":
            return "trend_up"
        if one_tf_dir == "down":
            return "trend_down"
    # p-shape (short-covering): fat value up top, single-print tail below.
    if poc_pos >= 0.60 and bottom_single >= 2:
        return "p_shape"
    # b-shape (long-liquidation): fat value at bottom, single-print tail above.
    if poc_pos <= 0.40 and top_single >= 2:
        return "b_shape"
    return "normal"


def indicators_to_dict(ind: DayIndicators) -> Dict[str, Any]:
    return {
        "day": ind.day.isoformat(),
        "open_type": ind.open_type,
        "day_shape": ind.day_shape,
        "profile_skew": ind.profile_skew,
        "balance_state": ind.balance_state,
        "in_balance": ind.in_balance,
        "range_ext_up": ind.range_ext_up,
        "range_ext_down": ind.range_ext_down,
        "range_ext_first": ind.range_ext_first,
        "excess_high": ind.excess_high,
        "excess_low": ind.excess_low,
        "poor_high": ind.poor_high,
        "poor_low": ind.poor_low,
        "single_print_count": ind.single_print_count,
        "single_print_levels": ind.single_print_levels,
        "one_timeframing": ind.one_timeframing,
        "one_timeframing_run": ind.one_timeframing_run,
        "open": ind.open,
        "close": ind.close,
        "high": ind.high,
        "low": ind.low,
        "poc": ind.poc,
        "vah": ind.vah,
        "val": ind.val,
        "ib_high": ind.ib_high,
        "ib_low": ind.ib_low,
    }


# ──────────────────────────────────────────────────────────
# Serialization (router → JSON)
# ──────────────────────────────────────────────────────────

def bin_to_dict(b: PriceBin) -> Dict[str, Any]:
    return {
        "price_low": b.price_low,
        "price_high": b.price_high,
        "price_mid": b.price_mid,
        "tpo_count": b.tpo_count,
        "letters": b.letters,
        "in_value_area": b.in_value_area,
        "is_poc": b.is_poc,
    }


def day_profile_to_dict(p: DayProfile) -> Dict[str, Any]:
    return {
        "day": p.day.isoformat(),
        "bins": [bin_to_dict(b) for b in p.bins],
        "poc": p.poc,
        "vah": p.vah,
        "val": p.val,
        "ib_high": p.ib_high,
        "ib_low": p.ib_low,
        "open": p.open,
        "close": p.close,
        "high": p.high,
        "low": p.low,
        "n_periods": p.n_periods,
        "total_tpos": p.total_tpos,
        "total_volume": p.total_volume,
    }


def composite_to_dict(p: CompositeProfile) -> Dict[str, Any]:
    return {
        "bins": [bin_to_dict(b) for b in p.bins],
        "poc": p.poc,
        "vah": p.vah,
        "val": p.val,
        "high": p.high,
        "low": p.low,
        "total_tpos": p.total_tpos,
        "total_volume": p.total_volume,
        "n_days": p.n_days,
        "n_periods": p.n_periods,
    }


# ──────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────

def _floor_to_tick(price: float, tick: float) -> float:
    """Largest multiple of `tick` <= price."""
    return (int(price / tick + 1e-9)) * tick if price >= 0 else -((-price + tick - 1e-9) // tick) * tick


def _ceil_to_tick(price: float, tick: float) -> float:
    """Smallest multiple of `tick` >= price."""
    floored = _floor_to_tick(price, tick)
    return floored if abs(floored - price) < 1e-9 else floored + tick


def _bin_mid(base: float, tick: float, i: int) -> float:
    return base + tick * (i + 0.5)
