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


def test_max_pvalue_override_admits_marginal_pair():
    """Persistent quality-floor relaxation (2026-06-02): a pair whose
    cointegration p sits between the default 0.025 ceiling and the persistent
    0.05 ceiling is dropped as 'quality' by default but admitted when
    max_pvalue=0.05 is passed. corr / half-life still gate normally."""
    from run_paper_pairs import classify_pair_candidates
    df = pd.DataFrame([
        _candidate("COAL", "ITC", p=0.028, corr=0.89),   # p in the dead-band
    ])
    # Default ceiling (0.025): dropped on p-value.
    default = classify_pair_candidates(df, top=5)
    assert (default["skip_reason"] == "quality").sum() == 1
    assert default["processing_rank"].isna().all()
    # Persistent ceiling (0.05): admitted.
    loosened = classify_pair_candidates(df, top=5, max_pvalue=0.05)
    assert (loosened["processing_rank"] == 1).sum() == 1
    assert (loosened["skip_reason"] == "").sum() == 1


def test_max_pvalue_override_does_not_relax_corr_or_halflife():
    """Loosening the p ceiling must NOT admit pairs failing the economic gates
    (correlation, half-life) — those are system-agnostic. A weak-correlation
    pair with a fine p-value stays dropped even at max_pvalue=0.05."""
    from run_paper_pairs import classify_pair_candidates
    df = pd.DataFrame([
        _candidate("WEAK", "CORR", p=0.01, corr=0.54),   # great p, corr < 0.65
        _candidate("SLOW", "REV", p=0.01, corr=0.90, hl=9.0),  # HL > 5d
    ])
    out = classify_pair_candidates(df, top=5, max_pvalue=0.05)
    assert (out["skip_reason"] == "quality").sum() == 2
    assert out["processing_rank"].isna().all()


def test_max_pvalue_none_preserves_default_ceiling():
    """max_pvalue=None (every existing caller) behaves exactly as before:
    the module's QUALITY_MAX_PVALUE constant governs the floor."""
    from run_paper_pairs import classify_pair_candidates, QUALITY_MAX_PVALUE
    just_over = QUALITY_MAX_PVALUE + 0.001
    just_under = QUALITY_MAX_PVALUE - 0.001
    df = pd.DataFrame([
        _candidate("OVER", "CEIL", p=just_over, corr=0.90),
        _candidate("UNDR", "CEIL", p=just_under, corr=0.90),
    ])
    out = classify_pair_candidates(df, top=5)  # no override
    over = out[out["symbol_a"] == "OVER"].iloc[0]
    under = out[out["symbol_a"] == "UNDR"].iloc[0]
    assert over["skip_reason"] == "quality"
    assert under["skip_reason"] == ""


@pytest.mark.parametrize("bad", ["0.5", "5", "0", "-0.01", "0.1"])
def test_quality_max_pvalue_typo_tripwire_rejects(bad):
    """--quality-max-pvalue outside (0, 0.05] must fail at parse-time (exit 2)
    with a clear message, rather than silently trade on a wrong selection gate.
    parser.error fires before any env/network work, so this is hermetic."""
    import subprocess, sys, os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run(
        [sys.executable, "run_paper_pairs.py", "--system", "persistent",
         "--quality-max-pvalue", bad, "--force"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 2, (r.returncode, r.stderr[-500:])
    assert "quality-max-pvalue" in r.stderr and "(0, 0.05]" in r.stderr
