"""The live pair unit's crash handling. Two failures to prevent:

- A crash systemd is about to restart must not latch HALT_ALL from
  OnFailure=: in the default RestartMode=normal it did, and one Kotak
  timeout at start-up froze the restarted live runner for all of
  2026-10-27.
- RestartMode=direct also skips OnFailure='s page, so the per-crash
  ExecStopPost= script must page every crash and latch HALT_ALL on one in
  market hours, or a mid-session crash would resume real money unseen.
"""
from __future__ import annotations

import configparser
import os
import re
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
UNIT = DEPLOY / "pair-paper-persistent-live.service"
CRASH_ALERT = DEPLOY / "pair-live-crash-alert.sh"
SCREEN_AND_CRASH_S = 6 * 60    # one realistic crash-loop iteration (screening ~6 min)


def _unit():
    parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    parser.optionxform = str
    parser.read_string(UNIT.read_text())
    return parser


def _seconds(span: str) -> float:
    """A systemd time span ("3600", "1h", "60min", "1h 30min") in seconds."""
    units = {"": 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}
    if span.strip() == "infinity":
        return float("inf")
    parts = re.findall(r"(\d+)\s*([a-z]*)", span)
    if not parts:
        raise ValueError(f"not a time span: {span!r}")
    return sum(int(n) * units[u] for n, u in parts)


def test_an_auto_restart_does_not_latch_halt_all():
    unit = _unit()
    assert "pair-live-halt-on-failure.service" in unit["Unit"]["OnFailure"]
    assert unit["Service"]["Restart"] == "on-failure"
    assert unit["Service"].get("RestartMode") == "direct"


def test_every_crash_still_runs_the_crash_alert():
    stop_post = _unit()["Service"].get("ExecStopPost", "")
    assert stop_post.startswith("-")              # its failure never fails the unit
    assert stop_post.endswith("/deploy/pair-live-crash-alert.sh")
    assert os.access(CRASH_ALERT, os.X_OK)


def test_a_realistic_crash_loop_reaches_the_start_limit():
    """direct only skips OnFailure= for restarts. A loop of one crash per
    ~6 minutes must still put StartLimitBurst starts inside the interval,
    so the unit reaches `failed` and the terminal latch fires (the
    2026-06-08 loop never tripped a 600 s window)."""
    unit = _unit()["Unit"]
    burst = int(unit["StartLimitBurst"])
    assert burst >= 2
    assert burst * SCREEN_AND_CRASH_S <= _seconds(unit["StartLimitIntervalSec"])


@pytest.mark.parametrize("span,secs", [("3600", 3600), ("1h", 3600), ("60min", 3600),
                                        ("1h 30min", 5400), ("infinity", float("inf"))])
def test_time_spans_parse_like_systemd(span, secs):
    assert _seconds(span) == secs


def _crash_alert(tmp_path, result, hhmm):
    """Run the script with curl and logger stubbed; return (HALT_ALL set, calls)."""
    (tmp_path / "data_cache").mkdir(exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls"
    for name in ("curl", "logger"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n')
        stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "PROJECT_DIR": str(tmp_path),
           "SERVICE_RESULT": result, "EXIT_STATUS": "1", "CRASH_ALERT_HHMM": hhmm,
           "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
    done = subprocess.run(["bash", str(CRASH_ALERT)], env=env, capture_output=True, timeout=20)
    assert done.returncode == 0
    log = calls.read_text() if calls.exists() else ""
    return (tmp_path / "data_cache" / "HALT_ALL").exists(), log


def test_a_clean_stop_does_nothing(tmp_path):
    halted, log = _crash_alert(tmp_path, "success", "1100")
    assert not halted and log == ""


def test_a_start_up_crash_before_the_open_pages_but_does_not_freeze(tmp_path):
    """2026-10-27 09:13: a Kotak timeout before any order. Page, restart."""
    halted, log = _crash_alert(tmp_path, "exit-code", "0913")
    assert not halted
    assert "curl " in log


def test_a_crash_in_market_hours_pages_and_freezes(tmp_path):
    halted, log = _crash_alert(tmp_path, "exit-code", "1100")
    assert halted
    assert "curl " in log


@pytest.mark.parametrize("hhmm,halted", [("0914", False), ("0915", True), ("1530", True),
                                         ("1531", False), ("0900", False)])
def test_the_market_hours_edges(tmp_path, hhmm, halted):
    assert _crash_alert(tmp_path, "signal", hhmm)[0] is halted
