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
