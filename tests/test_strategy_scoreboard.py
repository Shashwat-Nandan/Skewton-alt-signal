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

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_scoreboard as sb  # noqa: E402


# ── kill rule (now the decay state machine — see strategy_decay.py) ───────
TODAY = date(2026, 7, 5)


def _decay_row(monthly, slug="s1", parked_reason=None, partial_months=()):
    return {"slug": slug, "name": slug, "monthly": monthly, "cum": 0.0,
            "unreal": 0.0, "verdict": None, "parked_reason": parked_reason,
            "partial_months": partial_months}


def _fresh_ledger():
    import strategy_decay as sd
    return {"version": sd.LEDGER_VERSION, "strategies": {}}


def test_last_complete_months_excludes_current_month():
    assert sb.last_complete_months(TODAY, 2) == ["2026-05", "2026-06"]
    assert sb.last_complete_months(date(2026, 1, 15), 2) == ["2025-11", "2025-12"]


def test_two_complete_negative_months_is_park_recommended():
    row = _decay_row({"2026-05": -56_056.0, "2026-06": -64_746.0,
                      "2026-07": +999_999.0})
    sb.apply_decay([row], _fresh_ledger(), TODAY, {})
    assert row["verdict"].startswith("PARK RECOMMENDED")


def test_current_partial_month_never_counts():
    # Deeply negative month-to-date must NOT park a strategy whose last two
    # COMPLETE months were fine — the month isn't over.
    row = _decay_row({"2026-05": 1.0, "2026-06": 1.0, "2026-07": -1e9})
    sb.apply_decay([row], _fresh_ledger(), TODAY, {})
    assert row["verdict"] == "ACTIVE"


def test_one_positive_complete_month_clears_monitoring():
    row = _decay_row({"2026-05": -48_935.0, "2026-06": +77_413.0})
    sb.apply_decay([row], _fresh_ledger(), TODAY, {})
    assert row["verdict"] == "ACTIVE"


def test_newborn_with_one_losing_month_is_monitoring_not_parked():
    # A strategy born mid-June has no May figure: it cannot be parked on one
    # losing month (the old rule said "insufficient history"; the machine
    # says MONITORING — same protection, more information).
    row = _decay_row({"2026-06": -22_979.0})
    sb.apply_decay([row], _fresh_ledger(), TODAY, {})
    assert row["verdict"].startswith("MONITORING")


def test_ledger_state_persists_across_runs():
    # WHY the machine exists at all: the June+July losses must accumulate
    # across separate scoreboard runs, not be recomputed from a fixed
    # two-month window each time.
    ledger = _fresh_ledger()
    row = _decay_row({"2026-06": -1.0})
    sb.apply_decay([row], ledger, date(2026, 7, 5), {})
    assert row["verdict"].startswith("MONITORING")
    row2 = _decay_row({"2026-06": -1.0, "2026-07": -1.0})
    sb.apply_decay([row2], ledger, date(2026, 8, 3), {})
    assert row2["verdict"].startswith("PARK RECOMMENDED")


def test_data_outage_does_not_freeze_a_month_out_of_the_kill_rule():
    # WHY (2026-07-19 review, top finding): the first design consumed each
    # month once. A single run while dashboard.db was unreadable scored June
    # NO_DATA permanently, so a real −₹80k June never counted and June+July
    # never reached PARK. The replay must re-score it when data returns.
    ledger = _fresh_ledger()
    sb.apply_decay([_decay_row({"2026-05": 1_000.0})], ledger, date(2026, 6, 5), {})
    # July run: June's sidecars are unreadable (the _skip path), so June is
    # absent from the series and scores NO_DATA.
    sb.apply_decay([_decay_row({"2026-05": 1_000.0})], ledger, date(2026, 7, 5), {})
    assert ledger["strategies"]["s1"]["state"] == "ACTIVE"
    assert ledger["strategies"]["s1"]["history"][-1]["signal"] == "NO_DATA"
    # August run: the files are readable again — June was a −₹80k month and
    # July −₹70k, so the pair must now park.
    row = _decay_row({"2026-05": 1_000.0, "2026-06": -80_000.0,
                      "2026-07": -70_000.0})
    lines = sb.apply_decay([row], ledger, date(2026, 8, 3), {})
    assert row["verdict"].startswith("PARK RECOMMENDED")
    assert any("REVISED" in ln for ln in lines)


def test_partial_months_are_not_scored_as_losing_months():
    # WHY: Taleb's first month is an observed-window partial; without this
    # it can supply one of the two months that trigger PARK.
    row = _decay_row({"2026-05": -800.0, "2026-06": -38_742.0},
                     partial_months=("2026-05",))
    sb.apply_decay([row], _fresh_ledger(), TODAY, {})
    assert row["verdict"].startswith("MONITORING")


def test_critical_cap_from_config_fast_paths(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text("[decay]\ncritical_monthly_loss_s1 = 30000\n")
    caps = sb.load_decay_caps(cfg)
    assert caps == {"s1": 30_000.0}
    row = _decay_row({"2026-06": -40_000.0})
    sb.apply_decay([row], _fresh_ledger(), TODAY, caps)
    assert row["verdict"].startswith("PARK RECOMMENDED")


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


def _empty_db(tmp_path):
    db = tmp_path / "dashboard.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE equity_positions (symbol TEXT, status TEXT, "
                "exit_dt TEXT, pnl REAL, entry_px REAL, last_mtm_px REAL, qty INT)")
    con.commit(); con.close()
    return db


def test_killed_sentinel_parks_buy_on_gap_and_removing_it_revives(tmp_path):
    """WHY: after the runner's kill rule fires, EOD sidecars stop — months go
    blank and the verdict would read as no-data, indistinguishable from a
    broken runner. The sentinel is the 'dead, not missing' signal. It is
    DERIVED from the file each run (2026-07-19 review): removing the file
    revives the row, as the pre-machine verdict did."""
    (tmp_path / "HALT_BUY_ON_GAP_KILLED").write_text("net -60k breached\n")
    (tmp_path / "buy_on_gap_paper_eod_2026-06-29.json").write_text(json.dumps({
        "date": "2026-06-29",
        "report": {"session_realized_delta": -1.0, "realized_pnl": -1.0,
                   "unrealized_pnl": 0.0}}))
    db = _empty_db(tmp_path)
    ledger = _fresh_ledger()
    rows = sb.build_rows(tmp_path, db, TODAY)
    bog = next(r for r in rows if r["name"].startswith("buy-on-gap"))
    assert bog["parked_reason"].startswith("HALT_BUY_ON_GAP_KILLED")
    transitions = sb.apply_decay(rows, ledger, TODAY, {})
    assert bog["verdict"].startswith("PARKED [sentinel]")
    assert any("PARKED by runner sentinel" in t for t in transitions)
    # And the control arm stays machine-exempt.
    ctl = next(r for r in rows if "MA ctl" in r["name"])
    assert ctl["verdict"] == "control arm"

    # Operator removes the sentinel to restart the runner → auto-revive.
    (tmp_path / "HALT_BUY_ON_GAP_KILLED").unlink()
    rows2 = sb.build_rows(tmp_path, db, TODAY)
    lines = sb.apply_decay(rows2, ledger, TODAY, {})
    bog2 = next(r for r in rows2 if r["name"].startswith("buy-on-gap"))
    assert not bog2["verdict"].startswith("PARKED")
    assert any("sentinel removed" in ln for ln in lines)


def test_render_refuses_unevaluated_rows_instead_of_typeerror(tmp_path):
    # WHY: build_rows leaves verdict=None for machine rows; calling render
    # without apply_decay used to raise a bare TypeError from string
    # concatenation, hiding the real ordering mistake (2026-07-19 review).
    rows = sb.build_rows(tmp_path, _empty_db(tmp_path), TODAY)
    with pytest.raises(ValueError, match="call apply_decay"):
        sb.render(rows, ["2026-06"], TODAY)


def test_operator_can_park_a_slug_before_any_ledger_exists(tmp_path):
    # WHY: --park/--unpark used to validate against the ledger, so on a fresh
    # install a VALID slug was reported "unknown (known: none yet)" until a
    # full run had written the file (2026-07-19 review).
    import strategy_decay as sd
    ledger = _fresh_ledger()
    rows = sb.build_rows(tmp_path, _empty_db(tmp_path), TODAY)
    sb.ensure_entries(rows, ledger)
    assert "taleb_nifty" in ledger["strategies"]
    lines = sd.set_operator_park(ledger["strategies"]["taleb_nifty"],
                                 "manual review", "2026-07")
    assert any("PARKED by operator" in ln for ln in lines)
