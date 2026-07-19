#!/usr/bin/env python3
"""
Shared runner scaffolding (audit 2026-06-10 task 2.1)
=====================================================
The hardened session-control primitives every unattended runner needs:
session-time constants, the kill-switch flag paths, holiday/timezone/disk
pre-flights, the sleep-until-open helper, the SIGTERM→KeyboardInterrupt
handler, and the silent-fail HeartbeatTracker.

These were grown and battle-tested inside runners/run_paper_pairs.py (the live
pair runner); extracting them verbatim here lets runners/run_paper_arbitrage.py
stop importing from a sibling *runner*, and lets runners/run_paper.py /
runners/run_equity_swing.py adopt the same protections without copy-paste drift.
The pair runner re-exports every name below so its many importers (tests,
backtests, dashboard) are unaffected.

Behavior must stay byte-identical to the pair runner's originals — the
pair test suite is the guard. Pair-SPECIFIC pieces (state-file naming,
the derived-config backfill, the pair logfile name, the daily-loss flag)
deliberately stay in runners/run_paper_pairs.py.
"""
from __future__ import annotations

import fcntl
import logging
import os
import signal
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List

HERE = Path(__file__).resolve().parent.parent
DATA_CACHE = HERE / "data_cache"

# Single anchor for the NSE holiday calendar. Every runner used to define its
# own copy of this constant, and one (run_paper_kalman_trend) resolved it
# relative to the CWD — which silently loads ZERO holidays when the process
# starts anywhere but the repo root, i.e. trades on a holiday. Absolute and
# defined once (reorg review, 2026-07-19).
HOLIDAYS_PATH = HERE / "market_data" / "holidays.csv"

# Kill-switch flag files (operator-managed), shared across all runners
# because they share data_cache/. HALT_ALL freezes the book (no entries,
# no exits — positions cannot exit while set; use sparingly). HALT_NEW_
# ENTRIES stops adding to the book; existing positions exit normally via
# stop / mean-revert / max-hold. To halt: `touch <path>`; to resume:
# `rm <path>`.
HALT_ALL_PATH = DATA_CACHE / "HALT_ALL"
HALT_NEW_ENTRIES_PATH = DATA_CACHE / "HALT_NEW_ENTRIES"


def taleb_state_suffix(underlying: str) -> str:
    """Underlying suffix for a Taleb instance's per-underlying files (state,
    lock, log, IV history). NIFTY keeps the LEGACY unsuffixed names (the
    original single-instance runner); every other underlying is suffixed.

    Single source of truth so the runner (run_paper.derive_paths) and any
    reader of those files (the dashboard positions router) can't drift — a
    mismatch would make a live instance silently invisible, the exact bug
    issue #87 fixed."""
    return "" if underlying == "NIFTY" else f"_{underlying}"

# Session-time boundaries (IST wall-clock; systemd sets TZ=Asia/Kolkata).
MARKET_OPEN = (9, 15)
# Wall-clock when the tick loop ends and state is persisted. Open
# positions are NOT flattened here in the persistent runners — they
# survive to the next session via the state file.
SESSION_END_AT = (15, 25)
HARD_STOP = (15, 30)    # never tick past this
TICK_SECONDS = 60

# Silent-dead-trader detector threshold: consecutive all-errored ticks
# before the runner touches its sentinel and exits non-zero.
SILENT_FAIL_THRESHOLD = 3

HOLIDAY_HORIZON_DAYS = 30
HOLIDAYS_PER_YEAR_FLOOR = 8


def acquire_lock(lock_path: Path, log: logging.Logger,
                 *, label: str = "runner") -> int:
    """Single-instance flock. Refuse to start if another process already
    holds `lock_path` — two processes sharing a state file would clobber
    each other's writes (last-write-wins silently drops mutations).

    Opens the lock file and takes fcntl.flock(LOCK_EX | LOCK_NB). Returns
    the open FD — the caller MUST keep the reference alive for the process
    lifetime so the OS holds the lock until the process exits (the kernel
    releases on close, which includes crash / SIGKILL).

    Raises RuntimeError if the lock is already held (BlockingIOError from
    the non-blocking flock). `label` names the runner in that error.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            f"Another {label} is already holding the lock "
            f"(lock file: {lock_path}). Refusing to start a second "
            f"instance — concurrent writes to the state file would "
            f"silently lose mutations. If the previous process died "
            f"abnormally, `rm {lock_path}` after confirming none is "
            f"actually running."
        )
    log.info("Runner lock acquired: %s (pid %d)", lock_path, os.getpid())
    return fd


def durable_write_text(path: Path, text: str) -> None:
    """Crash- and power-loss-safe file replace:

      1. write to '<path>.tmp' and fsync the fd — forces data blocks to
         disk before any metadata change is journaled (ext4 data=ordered
         would otherwise happily journal the rename against unflushed
         data, replaying a rename that points at an empty file).
      2. os.replace(tmp, path) — atomic rename, no half-truncated file.
      3. fsync the parent dir fd — the rename's directory entry is
         metadata the journal records but doesn't commit synchronously;
         force it so the rename itself survives power loss.

    Extracted from run_paper_pairs.write_state_file (PR #96 review): the
    pair runner, the kalman-pairs runner, and the signal publisher all
    need the identical discipline — one copy so a future hardening fix
    can't land in one and silently miss the others.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_holidays(path: Path) -> set[date]:
    # M-O1: lint each non-comment line and raise a precise error that
    # names the offending line number + content. Pre-fix, a typo like
    # "2026-13-05" raised a bare ValueError on date.fromisoformat with
    # no file context, abort-the-runner-with-no-clue style.
    if not path.exists():
        return set()
    days: set[date] = set()
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split(",", 1)[0].strip()
        if token.lower() in ("date", "holiday_date"):
            # tolerate a CSV header row
            continue
        try:
            days.add(date.fromisoformat(token))
        except ValueError as e:
            raise ValueError(
                f"{path}:{lineno}: malformed holiday date {token!r} "
                f"({e}). Expected YYYY-MM-DD."
            )
    return days


def is_trading_day(d: date, holidays: set[date]) -> tuple[bool, str]:
    if d.weekday() >= 5:
        return False, f"{d} is a weekend"
    if d in holidays:
        return False, f"{d} is an NSE holiday"
    return True, ""


def assert_timezone_ist(log: logging.Logger) -> None:
    # M-O4: datetime.now() is naive and inherits the process timezone
    # from systemd's TZ=Asia/Kolkata. A misconfigured deploy without
    # that env var would silently quote UTC times everywhere — wrong
    # market-open / close boundaries, wrong entry_time, wrong holiday
    # gating. Assert the process really is on IST before the runner
    # touches anything market-time-dependent.
    import time as _time
    tznames = _time.tzname
    is_dst = _time.daylight and _time.localtime().tm_isdst > 0
    current = tznames[1] if is_dst else tznames[0]
    # IST is the canonical name; some glibc builds report "+0530" when
    # the zone file isn't installed. Both are equivalent in offset.
    expected = ("IST", "+0530")
    if current not in expected:
        raise RuntimeError(
            f"M-O4: process timezone is {current!r} (tzname={tznames!r}, "
            f"is_dst={is_dst}). Expected IST. The systemd unit must set "
            f"`Environment=TZ=Asia/Kolkata` (or `TZ=Asia/Kolkata` on the "
            f"shell). Refusing to start — wrong-TZ runs misquote market "
            f"hours and holiday boundaries silently."
        )
    log.info("Timezone check passed: tzname=%s, dst=%s", current, is_dst)


def assert_disk_space_ok(paths: List[Path], log: logging.Logger,
                          min_free_mb: int = 500,
                          min_free_pct: float = 5.0) -> None:
    # M-O2: refuse to start if the partition hosting any critical
    # directory (data_cache/, logs/) has less than min_free_mb MB free
    # OR less than min_free_pct % of its capacity. State snapshots,
    # rolling logs, and bhavcopy cache all live there; running out
    # mid-session would corrupt the state-file write (no atomic rename
    # if the destination partition is full) and silently drop log lines.
    import shutil
    breaches: List[str] = []
    seen_mountpoints: set = set()
    for p in paths:
        try:
            usage = shutil.disk_usage(p if p.exists() else p.parent)
        except FileNotFoundError:
            continue  # caller's responsibility — don't pretend to know
        # Deduplicate by mountpoint so we don't double-report logs/ +
        # data_cache/ when they live on the same volume.
        mp = (usage.total, usage.free)
        if mp in seen_mountpoints:
            continue
        seen_mountpoints.add(mp)
        free_mb = usage.free / (1024 * 1024)
        free_pct = 100.0 * usage.free / usage.total if usage.total else 0
        if free_mb < min_free_mb or free_pct < min_free_pct:
            breaches.append(
                f"{p}: free={free_mb:.0f}MB ({free_pct:.1f}%) — "
                f"below threshold (min {min_free_mb}MB / {min_free_pct}%)"
            )
        else:
            log.info("Disk OK at %s: %.0fMB free (%.1f%%)",
                     p, free_mb, free_pct)
    if breaches:
        raise RuntimeError(
            "M-O2: disk-space pre-flight failed:\n  " +
            "\n  ".join(breaches) +
            "\nFree space and retry. State writes / log rolls would "
            "otherwise corrupt or truncate silently."
        )


def assert_holiday_data_fresh(holidays: set[date], today: date,
                              log: logging.Logger) -> None:
    # holidays.csv is hand-maintained from the NSE circular; a partial
    # or expired list silently treats lunar holidays (Holi, Diwali, etc.)
    # as trading days. Fail loud per CLAUDE.md Rule 12.
    if not holidays:
        msg = ("holidays.csv loaded zero entries — refusing to start. "
               "Populate from the NSE 'Holidays — Trading' circular.")
        log.error(msg)
        raise RuntimeError(msg)
    last = max(holidays)
    horizon = today + timedelta(days=HOLIDAY_HORIZON_DAYS)
    if last < horizon:
        msg = (f"holidays.csv last entry is {last}, less than "
               f"{HOLIDAY_HORIZON_DAYS} days past today ({today}). "
               f"Refusing to start — update from the NSE circular and "
               f"redeploy.")
        log.error(msg)
        raise RuntimeError(msg)
    this_year_count = sum(1 for h in holidays if h.year == today.year)
    if this_year_count < HOLIDAYS_PER_YEAR_FLOOR:
        msg = (f"holidays.csv has only {this_year_count} entries for "
               f"{today.year}; NSE typically has 13-17 per year. The list "
               f"is likely missing lunar holidays (Holi, Diwali, etc.). "
               f"Refusing to start — update from the NSE circular.")
        log.error(msg)
        raise RuntimeError(msg)


def sleep_until(target: datetime, log: logging.Logger):
    while True:
        delta = (target - datetime.now()).total_seconds()
        if delta <= 0:
            return
        log.info("Waiting %.0fs until %s", delta, target.strftime("%H:%M:%S"))
        time.sleep(min(delta, 60))


def install_signal_handlers(log: logging.Logger) -> None:
    """Map SIGTERM to KeyboardInterrupt so `systemctl stop` (and any other
    normal-flow process termination) runs `end_of_session` instead of
    killing the runner without persisting the EOD sidecar.

    `signal.default_int_handler` is the stdlib function bound to SIGINT by
    default — it raises KeyboardInterrupt at the next interpreter check
    point. Re-binding it to SIGTERM mirrors Ctrl+C behaviour exactly, so
    the existing `except KeyboardInterrupt:` path in main() catches both
    signals through the same teardown.
    """
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    log.info("SIGTERM handler installed (treated as KeyboardInterrupt; "
             "systemd stop will run end_of_session)")


class HeartbeatTracker:
    """Counts consecutive ticks where every running strategy errored.

    The runner's per-strategy try/except blocks swallow scan/rehedge
    failures so one bad pair can't take down the loop. That's right for
    isolated faults — but a *systemic* fault (token expired mid-session,
    kite API down) makes every pair fail every tick, and the runner
    would otherwise exit 0 SUCCESS at 15:25 with nothing traded ("silent
    dead trader"). This tracker is the loud-failure detector.

    On `threshold` consecutive ticks where n_errored == n_ran > 0, it
    touches a sentinel file and returns True so the caller can break the
    loop and exit non-zero (firing notify-failure@%n). One successful
    tick (n_errored < n_ran) resets the counter.

    Idle ticks (n_ran == 0, i.e. halt_all set) carry no signal and don't
    affect the counter — neither incrementing nor resetting it. This
    lets the operator pause the book without triggering false alarms.
    """

    def __init__(self, threshold: int, sentinel_path: Path,
                 log: logging.Logger):
        self.threshold = threshold
        self.sentinel_path = sentinel_path
        self.log = log
        self.consecutive_ticks = 0

    def record_tick(self, n_ran: int, n_errored: int) -> bool:
        """Account for one tick's outcomes. Returns True iff the threshold
        is now (or was already) breached — caller should exit non-zero."""
        if n_ran == 0:
            # halt_all or no strategies — no signal either way.
            return False
        if n_errored == n_ran:
            self.consecutive_ticks += 1
            self.log.warning(
                "Heartbeat: all %d running strategies errored this tick "
                "(consecutive: %d/%d)",
                n_ran, self.consecutive_ticks, self.threshold,
            )
            if self.consecutive_ticks >= self.threshold:
                try:
                    self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)
                    self.sentinel_path.touch()
                except Exception as e:
                    self.log.exception(
                        "Failed to touch heartbeat sentinel %s: %s",
                        self.sentinel_path, e,
                    )
                self.log.critical(
                    "SILENT-FAIL HEARTBEAT BREACHED: every strategy has "
                    "errored on every operation for %d consecutive ticks "
                    "(threshold %d). Touched %s and will exit non-zero so "
                    "notify-failure alerts. Likely causes: token expired, "
                    "kite API outage, network isolation. The sentinel "
                    "file is informational only (not checked at startup) "
                    "— investigate the root cause from the journal before "
                    "the next session runs.",
                    self.consecutive_ticks, self.threshold,
                    self.sentinel_path,
                )
                return True
            return False
        # At least one strategy succeeded this tick — reset.
        if self.consecutive_ticks > 0:
            self.log.info(
                "Heartbeat recovered: at least one strategy succeeded "
                "(was %d/%d consecutive all-errored ticks)",
                self.consecutive_ticks, self.threshold,
            )
        self.consecutive_ticks = 0
        return False
