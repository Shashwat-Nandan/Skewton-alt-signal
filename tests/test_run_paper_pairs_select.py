"""Tests for run_paper_pairs.select_pairs() — focused on the stale-CSV
safety check.

M-S2 (2026-05-28) switched the freshness signal from filesystem mtime
to the CSV's `last_data_date` column, so a `touch`/`cp` by an unrelated
process can no longer make the data look fresh. These tests parametrise
`last_data_date` per case; the mtime path remains as a fallback when
the column is missing (test_legacy_csv_without_column).
"""
from __future__ import annotations

import logging
import os
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from run_paper_pairs import select_pairs


def _row(last_data_date: str) -> str:
    # One quality-passing row; only `last_data_date` is parametrised so
    # the freshness check has a controllable knob.
    return (
        "COALINDIA,ITC,0.92,-0.54,0.0024,2.22,3.56,607.73,10.81,608.45,"
        f"0.066,442.35,305.25,{last_data_date},121,0.220"
    )


HEADER = ("symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,"
          "half_life_days,spread_vol_pct,spread_mean,spread_std,latest_spread,"
          "latest_z_score,last_close_a,last_close_b,last_data_date,n_obs,"
          "rank_score")


def _full_csv(last_data_date: str) -> str:
    return HEADER + "\n" + _row(last_data_date) + "\n"


@pytest.fixture
def log():
    return logging.getLogger("test_select_pairs")


def _write_csv(path: Path, last_data_days_ago: int = 0,
                last_data_date: str | None = None,
                mtime_seconds_ago: float = 0):
    """Write a 1-row CSV. By default `last_data_date` = today, so the
    freshness check passes. Pass `last_data_days_ago` to backdate it, or
    `last_data_date` for an explicit string."""
    if last_data_date is None:
        d = datetime.now().date() - timedelta(days=last_data_days_ago)
        last_data_date = d.isoformat()
    path.write_text(_full_csv(last_data_date))
    if mtime_seconds_ago > 0:
        old = time.time() - mtime_seconds_ago
        os.utime(path, (old, old))


def test_fresh_csv_loads(tmp_path, log):
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv)  # last_data_date = today
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    assert len(picks) == 1
    assert picks.iloc[0]["symbol_a"] == "COALINDIA"


def test_stale_csv_raises(tmp_path, log):
    csv = tmp_path / "pair_candidates.csv"
    # 8 days old — over the 7-day default ceiling.
    _write_csv(csv, last_data_days_ago=8)
    with pytest.raises(RuntimeError, match=r"\d+d old"):
        select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)


def test_stale_csv_error_message_is_helpful(tmp_path, log):
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv, last_data_days_ago=10)
    with pytest.raises(RuntimeError) as exc_info:
        select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    msg = str(exc_info.value)
    assert str(csv) in msg
    assert "screen_pairs.py" in msg
    assert "--max-csv-age-days" in msg


def test_max_age_zero_disables_check(tmp_path, log):
    """Operator escape hatch: max_age_days=0 must bypass the freshness check
    so a manual rerun on a stale CSV is still possible."""
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv, last_data_days_ago=30)
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=0)
    assert len(picks) == 1


def test_csv_just_under_ceiling_loads(tmp_path, log):
    """Boundary case: 6 days old should still load under the 7-day default —
    exactly the friday-screen → next-thursday-runner case."""
    csv = tmp_path / "pair_candidates.csv"
    _write_csv(csv, last_data_days_ago=6)
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    assert len(picks) == 1


# ──────────────────────────────────────────────────────────
# M-S2 — last_data_date vs mtime contract
# ──────────────────────────────────────────────────────────

def test_fresh_data_date_but_old_mtime_loads(tmp_path, log):
    """M-S2 contract: a `cp` / `touch` by an unrelated process can leave
    mtime fresh; the underlying screen data is what matters. Conversely,
    a fresh `last_data_date` with old mtime must pass."""
    csv = tmp_path / "pair_candidates.csv"
    # last_data_date = today, but mtime 30d old.
    _write_csv(csv, last_data_days_ago=0, mtime_seconds_ago=30 * 86400)
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    assert len(picks) == 1


def test_stale_data_date_with_fresh_mtime_still_raises(tmp_path, log):
    """The inverse: an unrelated `touch` (fresh mtime) on a stale-screen
    CSV must NOT mask the stale data. last_data_date is the source of
    truth."""
    csv = tmp_path / "pair_candidates.csv"
    # last_data_date 30d ago, mtime fresh (just-written).
    _write_csv(csv, last_data_days_ago=30)
    with pytest.raises(RuntimeError, match=r"\d+d old"):
        select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)


def test_legacy_csv_without_last_data_date_falls_back_to_mtime(tmp_path, log, caplog):
    """Legacy CSVs (pre-M-S2 screener output) lack the column entirely.
    Must fall back to mtime and emit a one-line WARNING so the operator
    knows to re-screen."""
    csv = tmp_path / "pair_candidates.csv"
    # Header without `last_data_date`, just enough for the screen
    legacy_header = ("symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,"
                     "half_life_days,spread_vol_pct,spread_mean,spread_std,"
                     "latest_spread,latest_z_score,last_close_a,last_close_b,"
                     "n_obs,rank_score")
    legacy_row = ("COALINDIA,ITC,0.92,-0.54,0.0024,2.22,3.56,607.73,10.81,"
                  "608.45,0.066,442.35,305.25,121,0.220")
    csv.write_text(legacy_header + "\n" + legacy_row + "\n")
    caplog.set_level(logging.WARNING)
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    assert len(picks) == 1
    assert any("M-S2 fallback" in r.message for r in caplog.records)


def test_all_unparseable_dates_falls_through(tmp_path, log, caplog):
    """M-S2 defensive: if the column exists but every row's value is
    unparseable, pd.to_datetime(errors='coerce') returns all NaT and the
    freshness check has nothing to anchor on. Must NOT raise a cryptic
    NaT-comparison error — falls through to mtime."""
    csv = tmp_path / "pair_candidates.csv"
    csv.write_text(HEADER + "\n" + _row("not-a-date") + "\n")
    caplog.set_level(logging.WARNING)
    # mtime is fresh (just-written) — should pass via mtime fallback
    picks = select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
    assert len(picks) == 1


def test_missing_csv_still_raises_filenotfound(tmp_path, log):
    """Pre-existing behaviour: nonexistent path raises FileNotFoundError
    BEFORE the freshness check runs."""
    csv = tmp_path / "does-not-exist.csv"
    with pytest.raises(FileNotFoundError, match="screen_pairs.py first"):
        select_pairs(top=3, log=log, candidates_path=csv, max_age_days=7.0)
