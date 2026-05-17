"""Tests for run_paper_pairs.select_pairs() — focused on the stale-CSV
safety check added 2026-05-17 so a failed weekly screen can't leave the
runner trading on day-old hedge ratios."""
from __future__ import annotations

import logging
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from run_paper_pairs import select_pairs


# Same FULL_CSV shape as tests/test_pair_candidates.py — one row that the
# 4-pass quality filter admits, enough for select_pairs() to return non-empty.
FULL_CSV = textwrap.dedent(
    """\
    symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,half_life_days,spread_vol_pct,spread_mean,spread_std,latest_spread,latest_z_score,last_close_a,last_close_b,last_data_date,n_obs,rank_score
    COALINDIA,ITC,0.92,-0.54,0.0024,2.22,3.56,607.73,10.81,608.45,0.066,442.35,305.25,2026-04-20,121,0.220
    """
)


@pytest.fixture
def log():
    return logging.getLogger("test_select_pairs")


def _write_csv(path: Path, mtime_seconds_ago: float = 0):
    """Write FULL_CSV and optionally backdate its mtime."""
    path.write_text(FULL_CSV)
    if mtime_seconds_ago > 0:
        old = time.time() - mtime_seconds_ago
        os.utime(path, (old, old))


def test_fresh_csv_loads(tmp_path, log):
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv)
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    assert len(picks) == 1
    assert picks.iloc[0]["symbol_a"] == "COALINDIA"


def test_stale_csv_raises(tmp_path, log):
    csv = tmp_path / "pair_candidates.csv"
    # 8 days old — over the 7-day default ceiling.
    _write_csv(csv, mtime_seconds_ago=8 * 86400)
    with pytest.raises(RuntimeError, match=r"is \d+\.\d+d old"):
        select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)


def test_stale_csv_error_message_is_helpful(tmp_path, log):
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv, mtime_seconds_ago=10 * 86400)
    with pytest.raises(RuntimeError) as exc_info:
        select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    msg = str(exc_info.value)
    # Path, age, and an actionable hint must all be in the message — operator
    # reading journalctl shouldn't have to dig to figure out the fix.
    assert str(csv) in msg
    assert "screen_pairs.py" in msg
    assert "--max-csv-age-days" in msg


def test_max_age_zero_disables_check(tmp_path, log):
    """Operator escape hatch: max_age_days=0 must bypass the freshness check
    so a manual rerun on a stale CSV is still possible."""
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv, mtime_seconds_ago=30 * 86400)  # 30 days old, definitely stale
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=0)
    assert len(picks) == 1


def test_csv_just_under_ceiling_loads(tmp_path, log):
    """Boundary case: a CSV that's 6.5 days old should still load under the
    7-day default — exactly the friday-screen → next-thursday-runner case."""
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv, mtime_seconds_ago=6.5 * 86400)
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    assert len(picks) == 1


def test_missing_csv_still_raises_filenotfound(tmp_path, log):
    """Pre-existing behaviour: nonexistent path raises FileNotFoundError
    BEFORE the freshness check runs."""
    csv = tmp_path / "does-not-exist.csv"
    with pytest.raises(FileNotFoundError, match="screen_pairs.py first"):
        select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
