"""Decay state machine (Vibe-Trading review item 2, 2026-07-19).

WHY these tests matter: the machine replaced the stateless kill_verdict as
the book's portfolio-level risk control. If a transition or streak rule
drifts — the current partial month starts counting, a lucky month silently
resets decay, a parked strategy quietly revives — the rule either kills
healthy strategies or never kills bleeding ones, exactly the E1 failure
modes, now with persistence to get wrong too.

The state is a REPLAY of the whole monthly series (2026-07-19 code review):
the earlier accumulate-and-advance design let a one-run data outage freeze a
month as NO_DATA forever, swallowing a real losing month. Several tests
below pin that data corrections re-score.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from scripts import strategy_decay as sd  # noqa: E402


def _replay(monthly, first, last, **kw):
    return sd.replay(monthly, sd.month_range(first, last), **kw)


# ── month arithmetic (one implementation, both directions) ────────────────

def test_month_arithmetic_crosses_year_boundaries():
    assert sd.shift_month("2026-01", -1) == "2025-12"
    assert sd.shift_month("2025-12", +1) == "2026-01"
    assert sd.month_range("2025-11", "2026-02") == [
        "2025-11", "2025-12", "2026-01", "2026-02"]
    assert sd.month_range("2026-05", "2026-04") == []      # empty, not a loop


# ── classify_month ─────────────────────────────────────────────────────────

def test_flat_month_is_healthy_matching_standing_rule():
    # WHY: the old rule was `all(v < 0)` — ₹0 never counted against a
    # strategy. The machine must keep that semantics, not tighten it.
    assert sd.classify_month(0.0) == sd.HEALTHY
    assert sd.classify_month(1.0) == sd.HEALTHY
    assert sd.classify_month(-1.0) == sd.WARNING
    assert sd.classify_month(None) == sd.NO_DATA


def test_critical_requires_opt_in_cap():
    # WHY (operator decision 2026-07-19): the fast path ships disabled — a
    # huge loss without a configured cap is WARNING, not CRITICAL.
    assert sd.classify_month(-1e9) == sd.WARNING
    assert sd.classify_month(-30_001.0, critical_cap=30_000) == sd.CRITICAL
    assert sd.classify_month(-29_999.0, critical_cap=30_000) == sd.WARNING


# ── replay transitions ─────────────────────────────────────────────────────

def test_two_consecutive_losing_months_park():
    # WHY: this IS the standing kill rule.
    r = _replay({"2026-05": -56_056.0, "2026-06": -64_746.0}, "2026-05", "2026-06")
    assert r["state"] == sd.PARK_RECOMMENDED
    assert r["since"] == "2026-06"


def test_healthy_month_between_losses_prevents_park():
    r = _replay({"2026-04": -1.0, "2026-05": 2.0, "2026-06": -1.0},
                "2026-04", "2026-06")
    assert r["state"] == sd.MONITORING
    assert r["warn_streak"] == 1


def test_recovery_needs_two_consecutive_healthy_months():
    # WHY (operator decision 2026-07-19): hysteresis — one lucky month must
    # not silently reset a decayed strategy, the flip-flop the stateless
    # rule allowed.
    base = {"2026-03": -1.0, "2026-04": -1.0}
    assert _replay(base, "2026-03", "2026-04")["state"] == sd.PARK_RECOMMENDED
    one = _replay({**base, "2026-05": 5.0}, "2026-03", "2026-05")
    assert one["state"] == sd.PARK_RECOMMENDED          # 1 healthy: still parked
    two = _replay({**base, "2026-05": 5.0, "2026-06": 5.0}, "2026-03", "2026-06")
    assert two["state"] == sd.ACTIVE                    # 2nd consecutive: recovered


def test_losing_month_resets_recovery_streak():
    r = _replay({"2026-02": -1.0, "2026-03": -1.0, "2026-04": 5.0,
                 "2026-05": -1.0, "2026-06": 5.0}, "2026-02", "2026-06")
    assert r["state"] == sd.PARK_RECOMMENDED            # never 2 healthy in a row


def test_critical_month_fast_paths_from_active():
    r = _replay({"2026-06": -40_000.0}, "2026-06", "2026-06", critical_cap=30_000)
    assert r["state"] == sd.PARK_RECOMMENDED
    assert "critical month" in r["history"][-1]["why"]


def test_no_data_month_is_neutral_but_not_exculpatory():
    # WHY: "a missing month never counts against a strategy" (standing
    # rule) — but it is also no evidence of recovery: two losing months
    # separated by a data gap still park. NO_DATA preserves streaks in both
    # directions.
    r = _replay({"2026-04": -1.0, "2026-06": -1.0}, "2026-04", "2026-06")
    assert r["state"] == sd.PARK_RECOMMENDED
    assert [h["signal"] for h in r["history"]] == [
        sd.WARNING, sd.NO_DATA, sd.WARNING]


def test_partial_month_is_neutral_not_a_losing_month():
    # WHY (2026-07-19 review): Taleb's first month is an observed-window
    # partial (backup series starts mid-life). Scoring it as real let a
    # −₹800 artifact supply one of the two months that trigger PARK.
    monthly = {"2026-05": -800.0, "2026-06": -38_742.0}
    assert _replay(monthly, "2026-05", "2026-06")["state"] == sd.PARK_RECOMMENDED
    flagged = _replay(monthly, "2026-05", "2026-06", partial_months=["2026-05"])
    assert flagged["state"] == sd.MONITORING
    assert flagged["history"][0]["signal"] == sd.PARTIAL


def test_replay_is_pure_and_idempotent():
    # WHY: the scoreboard may run any number of times per month; the state
    # is a function of the series alone, so repeats cannot drift.
    monthly = {"2026-05": -1.0, "2026-06": -1.0}
    a = _replay(monthly, "2026-05", "2026-06")
    b = _replay(monthly, "2026-05", "2026-06")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ── backfill: the bug the replay design exists to prevent ─────────────────

def test_backfilled_month_rescores_and_is_reported_revised():
    # WHY (2026-07-19 review, the top finding): with the old accumulate-and-
    # advance design, a month whose data source failed on one run was frozen
    # NO_DATA forever — a real −₹80k month could never count, and two losing
    # months either side of the gap never reached PARK RECOMMENDED.
    entry = sd.new_entry("equity swing")
    outage = _replay({"2026-05": 1_000.0}, "2026-05", "2026-06")  # June missing
    sd.apply_replay(entry, outage, "equity swing", "2026-06")
    assert entry["state"] == sd.ACTIVE

    restored = _replay({"2026-05": 1_000.0, "2026-06": -80_000.0,
                        "2026-07": -70_000.0}, "2026-05", "2026-07")
    lines = sd.apply_replay(entry, restored, "equity swing", "2026-07")
    assert entry["state"] == sd.PARK_RECOMMENDED          # the −80k now counts
    assert any("REVISED" in ln for ln in lines)           # and is announced


def test_unchanged_history_reports_no_transitions_on_rerun():
    entry = sd.new_entry("s")
    r = _replay({"2026-05": -1.0, "2026-06": -1.0}, "2026-05", "2026-06")
    first = sd.apply_replay(entry, r, "s", "2026-06")
    assert len(first) == 2                                # MONITORING, then PARK
    assert sd.apply_replay(entry, r, "s", "2026-06") == []   # quiet re-run


# ── park overlay ──────────────────────────────────────────────────────────

def test_sentinel_park_auto_revives_when_the_file_goes():
    # WHY (2026-07-19 review): the pre-machine verdict was derived from the
    # sentinel every run, so deleting it revived the row. A sticky ledger
    # state broke that and kept quoting a file that no longer exists.
    entry = sd.new_entry("buy-on-gap")
    on = sd.apply_park_overlay(entry, "HALT_BUY_ON_GAP_KILLED: net -60k", "2026-07")
    assert entry["park"]["source"] == sd.SENTINEL
    assert sd.verdict_line(entry).startswith("PARKED [sentinel]")
    assert any("PARKED by runner sentinel" in ln for ln in on)
    assert sd.apply_park_overlay(entry, "HALT_BUY_ON_GAP_KILLED: net -60k",
                                 "2026-08") == []          # idempotent
    off = sd.apply_park_overlay(entry, None, "2026-09")
    assert entry["park"] is None
    assert any("sentinel removed" in ln for ln in off)


def test_unpark_refuses_to_override_a_sentinel():
    # WHY: --unpark used to "succeed" (exit 0) and then be silently re-parked
    # by the same run, printing two contradictory transitions. The file is
    # the authority; the operator must be told to remove it.
    entry = sd.new_entry("buy-on-gap")
    sd.apply_park_overlay(entry, "HALT_BUY_ON_GAP_KILLED: net -60k", "2026-07")
    with pytest.raises(ValueError, match="remove that file"):
        sd.clear_operator_park(entry, "2026-07")
    with pytest.raises(ValueError, match="remove it"):
        sd.set_operator_park(entry, "operator", "2026-07")
    assert entry["park"]["source"] == sd.SENTINEL          # unchanged


def test_operator_park_is_sticky_and_health_keeps_running_underneath():
    # WHY: an operator park must survive healthy months (only --unpark
    # clears it), but the health replay underneath must stay current so the
    # verdict is immediately truthful when it IS cleared.
    entry = sd.new_entry("s")
    sd.set_operator_park(entry, "manual review", "2026-05")
    r = _replay({"2026-05": -1.0, "2026-06": -1.0}, "2026-05", "2026-06")
    sd.apply_replay(entry, r, "s", "2026-06")
    assert sd.verdict_line(entry).startswith("PARKED [operator]")
    lines = sd.clear_operator_park(entry, "2026-07")
    assert sd.verdict_line(entry).startswith("PARK RECOMMENDED")   # true health
    assert any("health state resumes" in ln for ln in lines)


# ── ledger ─────────────────────────────────────────────────────────────────

def test_ledger_round_trip(tmp_path):
    p = tmp_path / "state" / "strategy_decay.json"
    ledger = sd.load_ledger(p)                    # missing file → empty
    assert ledger == {"version": sd.LEDGER_VERSION, "strategies": {}}
    entry = sd.new_entry("x")
    sd.apply_replay(entry, _replay({"2026-06": -1.0}, "2026-06", "2026-06"),
                    "x", "2026-06")
    ledger["strategies"]["x"] = entry
    sd.save_ledger(p, ledger)
    assert sd.load_ledger(p) == ledger
    assert not p.with_name(p.name + ".tmp").exists()   # atomic, no debris


def test_corrupt_or_malformed_ledger_fails_loud_not_silent_reset(tmp_path):
    # WHY: silently restarting every strategy at ACTIVE would erase decay
    # memory — the one thing the ledger exists to keep (Rule 12). A
    # malformed shape must name the problem, not surface as a bare KeyError
    # deep in a caller (2026-07-19 review).
    p = tmp_path / "strategy_decay.json"
    p.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        sd.load_ledger(p)
    p.write_text(json.dumps({"version": 99, "strategies": {}}))
    with pytest.raises(ValueError, match="unsupported ledger version"):
        sd.load_ledger(p)
    p.write_text(json.dumps({"version": sd.LEDGER_VERSION}))
    with pytest.raises(ValueError, match="missing a 'strategies' object"):
        sd.load_ledger(p)
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match="must be a JSON object"):
        sd.load_ledger(p)
