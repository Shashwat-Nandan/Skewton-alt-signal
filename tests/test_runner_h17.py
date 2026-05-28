"""H17 — cross-runner leg-concentration cap.

The screener's intra-runner LEG_CONCENTRATION_CAP can be defeated by two
runners (baseline + persistent) each independently admitting the same
symbol up to the per-runner cap. The fix seeds the selection walk's
leg_count with sibling runners' active legs read from
data_cache/pair_paper_state_*.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest


def _candidate(symbol_a, symbol_b, *, beta=0.5, corr=0.85, hl=3.0, p=0.01,
               spread_vol=1.0):
    return {
        "symbol_a": symbol_a,
        "symbol_b": symbol_b,
        "hedge_ratio": beta,
        "correlation": corr,
        "half_life_days": hl,
        "coint_pvalue": p,
        "spread_vol_pct": spread_vol,
    }


def test_load_cross_runner_leg_counts_skips_own(tmp_path):
    from run_paper_pairs import load_cross_runner_leg_counts
    own = tmp_path / "pair_paper_state_baseline.json"
    sib = tmp_path / "pair_paper_state_persistent.json"
    own.write_text(json.dumps({"pairs": [
        {"pair": ["AAA", "BBB"], "state": {"legs": [{"symbol": "AAA"}]}},
    ]}))
    sib.write_text(json.dumps({"pairs": [
        {"pair": ["AAA", "CCC"], "state": {"legs": [{"symbol": "AAA"}]}},
        {"pair": ["DDD", "EEE"], "state": {"legs": []}},  # FLAT — excluded
    ]}))
    counts = load_cross_runner_leg_counts(own, data_cache=tmp_path)
    # Only sibling counts; own is excluded; FLAT pairs are excluded.
    assert counts == {"AAA": 1, "CCC": 1}


def test_load_cross_runner_leg_counts_handles_malformed(tmp_path):
    from run_paper_pairs import load_cross_runner_leg_counts
    (tmp_path / "pair_paper_state_bad.json").write_text("not json")
    (tmp_path / "pair_paper_state_good.json").write_text(json.dumps({
        "pairs": [{"pair": ["X", "Y"], "state": {"legs": [{"q": 1}]}}],
    }))
    counts = load_cross_runner_leg_counts(None, data_cache=tmp_path)
    assert counts == {"X": 1, "Y": 1}


def test_classify_seeds_block_admit_at_cap():
    from run_paper_pairs import classify_pair_candidates, LEG_CONCENTRATION_CAP
    df = pd.DataFrame([
        _candidate("AAA", "BBB"),
        _candidate("AAA", "CCC"),
    ])
    # Seed AAA at the cap; both candidates use AAA, so both should be
    # blocked via the cross-runner seed.
    seed = {"AAA": LEG_CONCENTRATION_CAP}
    out = classify_pair_candidates(df, top=5, seed_leg_count=seed)
    assert (out["skip_reason"] == "leg_cap").sum() == 2
    assert out["processing_rank"].isna().all()


def test_classify_seeds_admit_when_below_cap():
    from run_paper_pairs import classify_pair_candidates
    df = pd.DataFrame([
        _candidate("AAA", "BBB"),
    ])
    # Seed below cap → still admitted, increments only once (intra-runner).
    seed = {"AAA": 1}
    out = classify_pair_candidates(df, top=5, seed_leg_count=seed)
    assert (out["processing_rank"] == 1).sum() == 1
