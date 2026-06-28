"""Compounding loop memory — STATE.md (loop memory) + SKILL.md (procedure manual).

These are the paper's two persistence primitives (§II-B, §II-C). The agent forgets
between sessions; these files do not. The contract is read-first / write-last:
every run reads STATE.md at the start and writes its outcome back at the end, so
lessons accumulate across sessions and tighten the rules over time (§IV).

Format is plain markdown so a human reads it directly (Fig. 3), but the parse is
deterministic — Rule 5: no model is involved in reading or writing these files.

STATE.md canonical shape (rendered by `_render_state`):

    # STATE.md — <strategy> loop memory

    ## Last run
    - <key>: <value>        # insertion-ordered key/value header
    ...

    ## Lessons
    - <YYYY-MM-DD>: <text>  # NEWEST FIRST (Fig. 3 shows reverse-chronological)
    ...

SKILL.md is read-only at runtime (humans + later the recalibration audit edit it):

    ## Goal      -> free text paragraph
    ## Rules     -> bullet list
    ## Lessons   -> bullet list
    ## Regime tags -> bullet list
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Dict, List, Optional

# Repo-root/state/<strategy>/. loop_engine/ is one level under the repo root, so
# the default state root is a sibling of this package. Tests pass their own root.
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "state"

STATE_FILE = "STATE.md"
SKILL_FILE = "SKILL.md"


# ──────────────────────────────────────────────────────────────────────────
# Data carriers
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class LoopState:
    """Parsed STATE.md. `last_run` preserves file order; `lessons` is newest-first."""

    last_run: Dict[str, str] = field(default_factory=dict)
    lessons: List[str] = field(default_factory=list)


@dataclass
class Skill:
    """Parsed SKILL.md procedure manual."""

    goal: str = ""
    rules: List[str] = field(default_factory=list)
    lessons: List[str] = field(default_factory=list)
    regime_tags: List[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────
# Path helpers
# ──────────────────────────────────────────────────────────────────────────
def _state_path(strategy: str, root: Optional[Path]) -> Path:
    return (root or _DEFAULT_ROOT) / strategy / STATE_FILE


def _skill_path(strategy: str, root: Optional[Path]) -> Path:
    return (root or _DEFAULT_ROOT) / strategy / SKILL_FILE


# ──────────────────────────────────────────────────────────────────────────
# Markdown parsing (shared)
# ──────────────────────────────────────────────────────────────────────────
def _split_sections(text: str) -> Dict[str, List[str]]:
    """Group non-empty lines by their owning `## Heading` (case-insensitive key).

    Lines before the first heading are ignored (the `# Title` line). Returns the
    raw stripped lines under each heading, in file order.
    """
    sections: Dict[str, List[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections.setdefault(current, [])
        elif current is not None and line:
            sections[current].append(line)
    return sections


def _bullets(lines: List[str]) -> List[str]:
    """Extract bullet payloads (`- foo` / `* foo`) from a section's lines."""
    out: List[str] = []
    for line in lines:
        if line.startswith(("- ", "* ")):
            out.append(line[2:].strip())
    return out


# ──────────────────────────────────────────────────────────────────────────
# STATE.md
# ──────────────────────────────────────────────────────────────────────────
def read_state(strategy: str, root: Optional[Path] = None) -> LoopState:
    """Read STATE.md. A missing file is an empty state (a fresh strategy)."""
    path = _state_path(strategy, root)
    if not path.exists():
        return LoopState()

    sections = _split_sections(path.read_text(encoding="utf-8"))

    last_run: Dict[str, str] = {}
    for item in _bullets(sections.get("last run", [])):
        if ":" in item:
            key, _, value = item.partition(":")
            last_run[key.strip()] = value.strip()

    lessons = _bullets(sections.get("lessons", []))
    return LoopState(last_run=last_run, lessons=lessons)


def _render_state(strategy: str, state: LoopState) -> str:
    lines = [f"# STATE.md — {strategy} loop memory", "", "## Last run"]
    if state.last_run:
        lines += [f"- {k}: {v}" for k, v in state.last_run.items()]
    else:
        lines.append("- (no run yet)")
    lines += ["", "## Lessons"]
    if state.lessons:
        lines += [f"- {lesson}" for lesson in state.lessons]
    return "\n".join(lines) + "\n"


def _write_state(strategy: str, state: LoopState, root: Optional[Path]) -> None:
    path = _state_path(strategy, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_state(strategy, state), encoding="utf-8")


def write_run_summary(strategy: str, root: Optional[Path] = None, **fields: object) -> LoopState:
    """Merge `fields` into the `## Last run` header and persist (write-last).

    Lessons are preserved untouched. New keys append in call order; existing keys
    update in place. Values are stringified so the file stays human-readable.
    """
    state = read_state(strategy, root)
    for key, value in fields.items():
        state.last_run[key] = "null" if value is None else str(value)
    _write_state(strategy, state, root)
    return state


def append_lesson(
    strategy: str,
    text: str,
    on_date: Optional[_date] = None,
    root: Optional[Path] = None,
) -> LoopState:
    """Prepend a dated lesson to `## Lessons` (newest-first, Fig. 3) and persist.

    The paper's self-improvement mechanism (§IV): every closed-out session writes
    what it learned so the next run reads it first.
    """
    state = read_state(strategy, root)
    stamp = (on_date or _date.today()).isoformat()
    state.lessons.insert(0, f"{stamp}: {text.strip()}")
    _write_state(strategy, state, root)
    return state


# ──────────────────────────────────────────────────────────────────────────
# SKILL.md
# ──────────────────────────────────────────────────────────────────────────
def load_skill(strategy: str, root: Optional[Path] = None) -> Skill:
    """Read SKILL.md. A missing file is an empty Skill (no rules learned yet)."""
    path = _skill_path(strategy, root)
    if not path.exists():
        return Skill()

    sections = _split_sections(path.read_text(encoding="utf-8"))
    goal = " ".join(sections.get("goal", [])).strip()
    return Skill(
        goal=goal,
        rules=_bullets(sections.get("rules", [])),
        lessons=_bullets(sections.get("lessons", [])),
        regime_tags=_bullets(sections.get("regime tags", [])),
    )
