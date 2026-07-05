"""Scoreboard + standing kill rule (efficiency review 2026-07-05, E1).

WHY these tests matter: the kill rule is the book's portfolio-level risk
control — "two complete net-negative months → PARK". If the month bucketing
or completeness logic drifts (e.g. the current partial month starts counting,
or a strategy with one month of history gets parked), the rule either kills
healthy strategies or never kills bleeding ones. Both failure modes defeat
the point of E1.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_scoreboard as sb  # noqa: E402


# ── kill rule ──────────────────────────────────────────────────────────────
TODAY = date(2026, 7, 5)


def test_last_complete_months_excludes_current_month():
    assert sb.last_complete_months(TODAY, 2) == ["2026-05", "2026-06"]
    assert sb.last_complete_months(date(2026, 1, 15), 2) == ["2025-11", "2025-12"]


def test_two_complete_negative_months_is_park_candidate():
    verdict = sb.kill_verdict({"2026-05": -56_056.0, "2026-06": -64_746.0,
                               "2026-07": +999_999.0}, TODAY)
    assert verdict.startswith("PARK CANDIDATE")


def test_current_partial_month_never_counts():
    # Deeply negative month-to-date must NOT park a strategy whose last two
    # COMPLETE months were fine — the month isn't over.
    verdict = sb.kill_verdict({"2026-05": 1.0, "2026-06": 1.0,
                               "2026-07": -1e9}, TODAY)
    assert verdict == "OK"


def test_one_positive_complete_month_clears():
    verdict = sb.kill_verdict({"2026-05": -48_935.0, "2026-06": +77_413.0}, TODAY)
    assert verdict == "OK"


def test_missing_month_is_insufficient_history_not_a_pass_or_park():
    # A strategy born mid-June has no May figure: it can be neither parked
    # (didn't exist) nor declared OK (no evidence).
    verdict = sb.kill_verdict({"2026-06": -22_979.0}, TODAY)
    assert verdict == "insufficient history"


# ── monthly aggregation ────────────────────────────────────────────────────
def test_cumulative_series_monthly_diffs_at_month_boundaries():
    series = [("2026-05-20", 100.0), ("2026-05-30", -50.0),
              ("2026-06-15", 200.0), ("2026-06-30", 300.0),
              ("2026-07-03", 250.0)]
    monthly = sb.cumulative_series_monthly(series)
    # First month is measured within-month (partial): −50 − 100 = −150.
    assert monthly == {"2026-05": -150.0, "2026-06": 350.0, "2026-07": -50.0}


def test_pair_system_monthly_sums_session_deltas_net_of_costs(tmp_path):
    for d, deltas in [("2026-06-29", (100.0, -30.0)), ("2026-07-01", (50.0, 0.0))]:
        (tmp_path / f"pair_paper_eod_{d}.json").write_text(json.dumps({
            "date": d, "system": "baseline",
            "pairs": [{"session_realized_delta": x, "unrealized_pnl": -5.0}
                      for x in deltas]}))
    monthly, unreal = sb.pair_system_monthly(tmp_path, "pair_paper_eod_*.json")
    assert monthly == {"2026-06": 70.0, "2026-07": 50.0}
    assert unreal == -10.0  # from the LATEST snapshot only, not summed over days


def test_equity_swing_monthly_groups_by_exit_month_and_marks_open(tmp_path):
    db = tmp_path / "dash.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE equity_positions (symbol TEXT, status TEXT, "
                "exit_dt TEXT, pnl REAL, entry_px REAL, last_mtm_px REAL, qty INT)")
    con.executemany(
        "INSERT INTO equity_positions VALUES (?,?,?,?,?,?,?)",
        [("A", "CLOSED", "2026-06-10T00:00:00", -1000.0, 0, 0, 0),
         ("B", "CLOSED", "2026-06-20T00:00:00", 400.0, 0, 0, 0),
         ("C", "CLOSED", "2026-07-01T00:00:00", 250.0, 0, 0, 0),
         ("D", "OPEN", None, None, 100.0, 110.0, 10)])
    con.commit(); con.close()
    monthly, cum, unreal = sb.equity_swing_monthly(db)
    assert monthly == {"2026-06": -600.0, "2026-07": 250.0}
    assert cum == -350.0
    assert unreal == 100.0  # (110−100)×10 — open MTM, never mixed into realized


def test_same_day_backups_order_by_time_not_by_pnl(tmp_path):
    """WHY: two backups on month-end day (restart + EOD) must resolve the
    month-end value chronologically. Sorting bare (date, pnl) tuples would
    tie-break on the P&L float and pick the day's MAX as 'month end',
    corrupting the monthly delta the PARK verdict is computed from."""
    bdir = tmp_path / "state_backups"
    bdir.mkdir()
    # 2026-06-30: morning backup +5000, close backup −2000 (losing afternoon).
    for ts, pnl in [("20260630T100000", 5000.0), ("20260630T153000", -2000.0),
                    ("20260601T153000", 0.0)]:
        (bdir / f"taleb_paper_state.{ts}.json").write_text(
            json.dumps({"state": {"realized_pnl": pnl}}))
    (tmp_path / "taleb_paper_state.json").write_text(
        json.dumps({"state": {"realized_pnl": -2000.0, "unrealized_pnl": 0.0}}))
    monthly, cum, _ = sb.taleb_monthly(tmp_path)
    # June ends at −2000 (the LATER backup), so June = −2000 − 0, not +5000.
    assert monthly["2026-06"] == -2000.0
    assert cum == -2000.0


def test_baseline_zero_attributes_first_observation_to_first_month():
    """WHY: for a series born with its first file (kalman_trend EOD), the
    first observation's own accumulation IS that month's trading; measuring
    last−first inside the month silently drops it, understating the month
    the kill rule judges."""
    series = [("2026-06-29", 7000.0), ("2026-06-30", 9000.0),
              ("2026-07-03", 8500.0)]
    assert sb.cumulative_series_monthly(series, baseline=0.0) == {
        "2026-06": 9000.0, "2026-07": -500.0}
    # Without a baseline (series starts mid-life) only the observed window
    # can be attributed — the pre-series P&L is unknowable, not zero.
    assert sb.cumulative_series_monthly(series) == {
        "2026-06": 2000.0, "2026-07": -500.0}


def test_snapshot_missing_date_key_is_skipped_loudly_not_a_crash(tmp_path):
    """WHY: one malformed sidecar must not abort the whole scoreboard (no
    verdict for ANY strategy), and the skip must be counted so the output
    can flag that verdicts ran on incomplete data."""
    (tmp_path / "pair_paper_eod_2026-06-29.json").write_text(
        json.dumps({"date": "2026-06-29",
                    "pairs": [{"session_realized_delta": 10.0,
                               "unrealized_pnl": 0.0}]}))
    (tmp_path / "pair_paper_eod_2026-06-30.json").write_text(
        json.dumps({"pairs": [{"session_realized_delta": 99.0}]}))  # no date
    before = len(sb.SKIPPED)
    monthly, _ = sb.pair_system_monthly(tmp_path, "pair_paper_eod_*.json")
    assert monthly == {"2026-06": 10.0}          # good file still counted
    assert len(sb.SKIPPED) == before + 1         # bad file surfaced, not silent


def test_equity_cum_includes_closed_rows_without_exit_dt(tmp_path):
    """WHY: a closed trade whose exit_dt was never stamped can't be bucketed
    into a month, but its P&L vanishing from the TOTAL would understate the
    realized book the kill rule protects."""
    db = tmp_path / "dash.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE equity_positions (symbol TEXT, status TEXT, "
                "exit_dt TEXT, pnl REAL, entry_px REAL, last_mtm_px REAL, qty INT)")
    con.executemany(
        "INSERT INTO equity_positions VALUES (?,?,?,?,?,?,?)",
        [("A", "CLOSED", "2026-06-10T00:00:00", -1000.0, 0, 0, 0),
         ("B", "CLOSED", None, -5000.0, 0, 0, 0),           # no exit_dt
         ("C", "OPEN", None, None, 100.0, None, 10)])       # no MTM yet
    con.commit(); con.close()
    monthly, cum, unreal = sb.equity_swing_monthly(db)
    assert monthly == {"2026-06": -1000.0}   # unbucketable row not in months
    assert cum == -6000.0                    # …but never dropped from cum
    assert unreal == 0.0                     # unmarked open row counts 0, not NULL-skipped


def test_killed_sentinel_overrides_buy_on_gap_verdict(tmp_path):
    """WHY: after the runner's kill rule fires, EOD sidecars stop — months go
    blank and the verdict would read 'insufficient history', indistinguishable
    from a broken runner. The sentinel is the 'dead, not missing' signal."""
    (tmp_path / "HALT_BUY_ON_GAP_KILLED").write_text("net -60k breached\n")
    (tmp_path / "buy_on_gap_paper_eod_2026-06-29.json").write_text(json.dumps({
        "date": "2026-06-29",
        "report": {"session_realized_delta": -1.0, "realized_pnl": -1.0,
                   "unrealized_pnl": 0.0}}))
    db = tmp_path / "dashboard.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE equity_positions (symbol TEXT, status TEXT, "
                "exit_dt TEXT, pnl REAL, entry_px REAL, last_mtm_px REAL, qty INT)")
    con.commit(); con.close()
    rows = sb.build_rows(tmp_path, db, TODAY)
    bog = next(r for r in rows if r["name"].startswith("buy-on-gap"))
    assert bog["verdict"].startswith("KILLED by runner rule")
