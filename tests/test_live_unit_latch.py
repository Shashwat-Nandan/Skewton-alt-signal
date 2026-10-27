"""The live pair unit latches HALT_ALL from OnFailure=. That must happen
only when systemd gives up, not on a crash it is about to restart: in the
default RestartMode=normal, systemd runs OnFailure= on every failure, so a
single Kotak timeout at start-up froze the restarted live runner for the
whole of 2026-10-27."""
from __future__ import annotations

import configparser
from pathlib import Path

UNIT = Path(__file__).resolve().parent.parent / "deploy" / "pair-paper-persistent-live.service"


def _unit():
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(UNIT.read_text())
    return parser


def test_an_auto_restart_does_not_latch_halt_all():
    unit = _unit()
    assert "pair-live-halt-on-failure.service" in unit["Unit"]["OnFailure"]
    assert unit["Service"]["Restart"] == "on-failure"
    assert unit["Service"].get("RestartMode") == "direct"


def test_a_crash_loop_still_reaches_failed():
    """direct only skips OnFailure= for restarts; the start limit must still
    be able to stop the loop and fire the latch."""
    unit = _unit()
    assert int(unit["Unit"]["StartLimitBurst"]) >= 2
    assert int(unit["Unit"]["StartLimitIntervalSec"]) > 0
