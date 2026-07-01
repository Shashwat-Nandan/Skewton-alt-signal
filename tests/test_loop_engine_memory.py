"""Tests for loop_engine.memory — the compounding STATE.md / SKILL.md layer.

Rule 9: these encode WHY the memory layer exists, not just that it returns
strings. The whole paper rests on lessons COMPOUNDING across sessions, so the
load-bearing tests are: (1) a run summary written at the end of a session must
not wipe the lessons accumulated by earlier sessions — a naive whole-file rewrite
that dropped them would silently destroy the self-improvement property; (2)
lessons are read newest-first (Fig. 3) so the next run sees the latest constraint;
(3) the committed seed files actually parse, so a hand-edit that breaks the format
fails CI instead of silently zeroing the loop's memory at runtime.
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from loop_engine import memory


# ──────────────────────────────────────────────────────────────────────────
# STATE.md round-trip
# ──────────────────────────────────────────────────────────────────────────
def test_missing_state_is_empty_not_an_error(tmp_path):
    """A fresh strategy has no STATE.md yet; that must read as empty, not crash —
    the orchestrator's first-ever run reads before it has ever written."""
    state = memory.read_state("never_run", root=tmp_path)
    assert state.last_run == {}
    assert state.lessons == []


def test_append_lesson_is_newest_first_and_round_trips(tmp_path):
    memory.append_lesson("s", "first lesson", on_date=date(2026, 1, 1), root=tmp_path)
    memory.append_lesson("s", "second lesson", on_date=date(2026, 1, 2), root=tmp_path)

    lessons = memory.read_state("s", root=tmp_path).lessons
    # Newest first so the next run reads the latest constraint at the top (Fig. 3).
    assert lessons == ["2026-01-02: second lesson", "2026-01-01: first lesson"]


def test_write_run_summary_preserves_accumulated_lessons(tmp_path):
    """THE load-bearing invariant: writing the end-of-session header must not
    erase the lessons earlier sessions paid for. Without this, the loop looks
    like it compounds while silently forgetting (paper §IV / §VII verification
    rot)."""
    memory.append_lesson("s", "hard-won lesson", on_date=date(2026, 1, 1), root=tmp_path)

    memory.write_run_summary("s", root=tmp_path, status="ok", active_positions=2)

    state = memory.read_state("s", root=tmp_path)
    assert state.lessons == ["2026-01-01: hard-won lesson"]   # survived the rewrite
    assert state.last_run["status"] == "ok"
    assert state.last_run["active_positions"] == "2"


def test_write_run_summary_updates_in_place_and_serialises_none(tmp_path):
    """Re-writing a key updates it (no duplicate header lines), and None becomes
    the literal 'null' so the markdown stays readable and re-parseable."""
    memory.write_run_summary("s", root=tmp_path, rolling_30d_sharpe=None)
    memory.write_run_summary("s", root=tmp_path, rolling_30d_sharpe=1.82)

    last_run = memory.read_state("s", root=tmp_path).last_run
    assert last_run["rolling_30d_sharpe"] == "1.82"
    # exactly one occurrence in the rendered file (in-place update, not append)
    raw = (tmp_path / "s" / "STATE.md").read_text()
    assert raw.count("rolling_30d_sharpe") == 1


# ──────────────────────────────────────────────────────────────────────────
# SKILL.md parsing
# ──────────────────────────────────────────────────────────────────────────
def test_load_skill_parses_all_sections(tmp_path):
    (tmp_path / "s").mkdir(parents=True)
    (tmp_path / "s" / "SKILL.md").write_text(
        "# SKILL.md — s\n\n"
        "## Goal\nMake money in trends.\n\n"
        "## Rules\n- paper only\n- sharpe_min: 1.5\n\n"
        "## Lessons\n- overfit is the enemy\n\n"
        "## Regime tags\n- trend\n- chop\n",
        encoding="utf-8",
    )
    skill = memory.load_skill("s", root=tmp_path)
    assert skill.goal == "Make money in trends."
    assert skill.rules == ["paper only", "sharpe_min: 1.5"]
    assert skill.lessons == ["overfit is the enemy"]
    assert skill.regime_tags == ["trend", "chop"]


def test_missing_skill_is_empty_not_an_error(tmp_path):
    skill = memory.load_skill("never_run", root=tmp_path)
    assert skill.goal == ""
    assert skill.rules == []


# ──────────────────────────────────────────────────────────────────────────
# parse_rule_floats — the single shared SKILL.md threshold parser
# ──────────────────────────────────────────────────────────────────────────
def test_parse_rule_floats_filters_by_allowed_and_tolerates_annotations():
    skill = memory.Skill(rules=[
        "sharpe_min: 1.5",
        "max_dd_max: 0.08 (was 0.10)",        # annotated → leading token
        "kill_switch_drawdown_rupees: 20000  # tighten",  # comment → leading token
        "Some prose rule: not a number",      # allowed-filtered out anyway
    ])
    vals = memory.parse_rule_floats(skill, allowed={"sharpe_min", "max_dd_max"})
    assert vals == {"sharpe_min": 1.5, "max_dd_max": 0.08}


def test_parse_rule_floats_warns_on_unparseable_value(caplog):
    import logging
    skill = memory.Skill(rules=["sharpe_min: 1.5x"])
    with caplog.at_level(logging.WARNING):
        vals = memory.parse_rule_floats(skill, allowed={"sharpe_min"})
    assert vals == {}                          # not silently 0 / not crashing
    assert any("unparseable" in r.message for r in caplog.records)


def test_append_lesson_flattens_multiline_text(tmp_path):
    """A multi-line lesson must collapse to ONE bullet — the markdown round-trip
    only preserves bullet lines, so a raw newline would silently truncate it."""
    memory.append_lesson("s", "line one\nline two\n  indented", root=tmp_path)
    lessons = memory.read_state("s", root=tmp_path).lessons
    assert len(lessons) == 1
    assert "line one line two indented" in lessons[0]


# ──────────────────────────────────────────────────────────────────────────
# Committed seed files must parse (guard against hand-edit drift)
# ──────────────────────────────────────────────────────────────────────────
def test_committed_kalman_trend_skill_parses():
    """The committed state/kalman_trend/SKILL.md — the loop's DURABLE memory (goal,
    gate thresholds, lessons) — must parse: a broken hand-edit should fail here, not
    silently zero the loop's rules at runtime. STATE.md is deliberately NOT checked:
    it's runtime-only memory (gitignored), regenerated each session, and absent in a
    fresh checkout; `read_state` tolerating that (empty LoopState) is the contract."""
    skill = memory.load_skill("kalman_trend")
    assert skill.goal                                # non-empty goal
    assert "NO-GO" in skill.goal                     # the honest "not a proven edge" framing
    # the checker reads its gate thresholds out of Rules — they must be present
    assert any("sharpe_min" in rule for rule in skill.rules)
    assert any(tag.startswith("trend") for tag in skill.regime_tags)
