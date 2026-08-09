"""research/backtest_pairs.py must trade the universe the runner would trade.

2026-08-07: the harness picked pairs with `sort_values("rank_score").head(n)`,
while runners/run_paper_pairs.select_pairs runs them through
core.screen_pairs.classify_pair_candidates first — the |beta| band, the
corr / half-life / p-value quality floor, and the leg-concentration cap.

That is not a cosmetic difference. On the same out-of-sample split with
identical strategy parameters, the raw-rank universe reported -Rs 5.63M
against +Rs 213k for the live-selected one, because raw rank happily admits
|beta| ~ 0.1 pairs and stacks six of eight legs onto a single symbol. A
backtest that trades pairs the runner refuses cannot validate the runner, and
every parameter sweep run through it was measuring the wrong book.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.screen_pairs import (
    QUALITY_MAX_HALFLIFE,
    QUALITY_MAX_PVALUE,
    QUALITY_MIN_CORR,
)
from research.backtest_pairs import select_top_pairs


def _row(a, b, *, beta=1.0, corr=0.90, p=0.001, hl=2.0, vol=3.0, rank=0.1):
    return {
        "symbol_a": a, "symbol_b": b, "correlation": corr, "hedge_ratio": beta,
        "coint_pvalue": p, "half_life_days": hl, "spread_vol_pct": vol,
        "rank_score": rank,
    }


def _admitted(rows, n=8):
    out = select_top_pairs(pd.DataFrame(rows), n)
    return {(r.symbol_a, r.symbol_b) for r in out.itertuples()}


def test_untradeable_hedge_ratio_is_refused():
    """|beta| below HEDGE_RATIO_MIN means leg B is so small the "hedge" is
    really leg A outright — a directional stock bet wearing a pair's costs.
    Raw rank_score ordering admitted exactly these (the OOS run's worst pair
    was beta=0.122)."""
    rows = [_row("AAA", "BBB", beta=0.02, rank=0.01),   # best rank, worst beta
            _row("CCC", "DDD", beta=1.0, rank=0.90)]
    assert _admitted(rows) == {("CCC", "DDD")}


def test_hedge_ratio_above_the_band_is_refused():
    rows = [_row("AAA", "BBB", beta=25.0, rank=0.01),
            _row("CCC", "DDD", beta=1.0, rank=0.90)]
    assert _admitted(rows) == {("CCC", "DDD")}


def test_quality_floor_drops_statistically_cointegrated_but_untradeable():
    """Cointegration alone is not tradeability: a spread that takes weeks to
    revert, or whose legs barely co-move, burns the time stop and the cost
    hurdle. The runner refuses these; the harness used to buy them."""
    bad_corr = _row("AAA", "BBB", corr=QUALITY_MIN_CORR - 0.05, rank=0.01)
    slow = _row("CCC", "DDD", hl=QUALITY_MAX_HALFLIFE + 1.0, rank=0.02)
    weak_p = _row("EEE", "FFF", p=QUALITY_MAX_PVALUE * 2, rank=0.03)
    good = _row("GGG", "HHH", rank=0.99)
    assert _admitted([bad_corr, slow, weak_p, good]) == {("GGG", "HHH")}


def test_leg_concentration_cap_limits_one_symbol_across_the_book():
    """Six of eight legs on one symbol is not a market-neutral book, it is a
    leveraged position in that symbol. The runner caps appearances; raw rank
    did not, which is how CIPLA ended up in most of the OOS selection."""
    rows = [_row("CIPLA", x, rank=0.01 * i)
            for i, x in enumerate(["AAA", "BBB", "CCC", "DDD"], start=1)]
    admitted = _admitted(rows, n=4)
    assert len(admitted) == 2, f"leg cap not applied: {admitted}"
    assert all(p[0] == "CIPLA" for p in admitted)


def test_admits_in_runner_order_and_respects_n():
    """Admit order is the runner's composite select_score, not rank_score, and
    `n` caps the result — a harness that returned more pairs than --top would
    overstate the book's size and its P&L."""
    rows = [_row(f"S{i}", f"T{i}", p=0.001 * i, hl=1.0 + 0.1 * i)
            for i in range(1, 6)]
    out = select_top_pairs(pd.DataFrame(rows), 3)
    assert len(out) == 3
    assert list(out["processing_rank"]) == sorted(out["processing_rank"])


def test_no_qualifying_pair_yields_empty_not_a_silent_fallback():
    """If nothing clears the filters the answer is "nothing", never "trade the
    best of a bad universe" — the caller aborts on empty."""
    rows = [_row("AAA", "BBB", beta=0.01), _row("CCC", "DDD", corr=0.10)]
    assert select_top_pairs(pd.DataFrame(rows), 8).empty


def test_max_pvalue_override_reaches_the_quality_floor():
    """research/backtest_pairs must be able to reproduce the PERSISTENT
    runner's universe, which runs --quality-max-pvalue 0.05.

    Hardcoding core.screen_pairs.QUALITY_MAX_PVALUE (0.025) made the harness
    refuse pairs the live runner trades — the same universe-mismatch defect
    select_top_pairs exists to fix, pointing the other way. Validating the
    real-money book against a stricter screen silently drops exactly the
    0.025 < p <= 0.05 band that system was built to hold.
    """
    borderline = _row("AAA", "BBB", p=0.04)
    assert select_top_pairs(pd.DataFrame([borderline]), 8).empty
    admitted = select_top_pairs(pd.DataFrame([borderline]), 8, max_pvalue=0.05)
    assert {(r.symbol_a, r.symbol_b) for r in admitted.itertuples()} == {("AAA", "BBB")}


def test_default_still_applies_the_tight_baseline_ceiling():
    """The override is opt-in: the baseline system must keep 0.025."""
    assert select_top_pairs(pd.DataFrame([_row("AAA", "BBB", p=0.04)]), 8).empty
