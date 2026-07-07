#!/usr/bin/env python3
"""
Paper-Trading Runner
====================
Unattended intraday loop:
  - Refuses to run on weekends or dates in holidays.csv
  - Authenticates (TOTP auto-login via kite_auth)
  - Restores any prior-session open position from
    data_cache/taleb_paper_state.json
  - Blocks until 09:15 IST, ticks until 15:25 IST
  - At session end, persists state to disk (no EOD flatten by default —
    open positions exit only on strategy triggers like max_holding_period,
    daily loss limit, vega/gap exit) or on the contract's last trading day
  - Writes EOD report + IV history, then exits 0
  - Per-day logfile under logs/paper-YYYY-MM-DD.log

Operations:
  --force-flatten-on-exit: emergency hatch to revert to old behaviour for
    a single session (e.g. before a maintenance window or contract switch).

Assumes the process sees wall-clock IST (systemd sets TZ=Asia/Kolkata).
"""

import json
import os
import sys
import time
import logging
import argparse
from datetime import datetime, date
from pathlib import Path
from typing import NamedTuple, Optional

from dotenv import load_dotenv

from _state_backup import archive_state_backup, assert_no_orphan_backups

# Shared runner scaffolding (audit 2.1). The Taleb daily runner used to
# carry its own (older, un-hardened) copies of the holiday helpers and the
# session-time constants; point it at the one scaffold so they can't drift,
# and pick up the disk/tz pre-flights + single-instance lock it lacked.
from runner_common import (
    HARD_STOP,
    MARKET_OPEN,
    SESSION_END_AT,
    SILENT_FAIL_THRESHOLD,
    TICK_SECONDS,
    HeartbeatTracker,
    acquire_lock,
    assert_disk_space_ok,
    assert_holiday_data_fresh,
    assert_timezone_ist,
    install_signal_handlers,
    is_trading_day,
    load_holidays,
    sleep_until,
)


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = str(HERE / "config.ini")
HOLIDAYS_PATH = HERE / "holidays.csv"
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
# Underlyings the Taleb strategy supports (index spot mapped in
# taleb_karpathy._INDEX_SPOT_SYMBOLS). The runner rejects anything else so a
# typo can't derive a rogue isolated instance.
SUPPORTED_UNDERLYINGS = {"NIFTY", "BANKNIFTY"}
# Open positions are NOT flattened at SESSION_END_AT — they survive to the
# next session via the state file; the only session-end exits are
# --force-flatten-on-exit and a held leg whose contract expires today.


class RunnerPaths(NamedTuple):
    """Per-underlying runtime paths, so a second instance (e.g. BANKNIFTY, issue
    #62) is fully isolated from the incumbent NIFTY runner — no shared state,
    lock, silent-fail flag, or logfile. NIFTY keeps the LEGACY unsuffixed names
    (state continuity + existing monitoring/watchdog references); any other
    underlying suffixes `_{underlying}`."""
    state_file: Path
    lock_file: Path
    silent_fail_flag: Path
    log_prefix: str


def resolve_underlying(config_path: str) -> str:
    """Read + validate the [strategy] underlying from `config_path`. Requires the
    EXACT canonical spelling (case-sensitive) — the strategy reads the same raw
    string, so a variant like "nifty" would BOTH derive suffixed paths (silently
    orphaning the real NIFTY state file) AND break the strategy's spot lookup.
    Fail loud (SystemExit) on a missing/unreadable/unparseable config or an
    unknown underlying, rather than falling back to NIFTY and clobbering it."""
    import configparser
    cfg = configparser.ConfigParser()
    try:
        if config_path not in cfg.read(config_path):
            raise SystemExit(f"Config not found/unreadable: {config_path}")
    except configparser.Error as e:
        raise SystemExit(f"Config parse error in {config_path}: {e}")
    underlying = cfg.get("strategy", "underlying", fallback="").strip()
    if underlying not in SUPPORTED_UNDERLYINGS:
        raise SystemExit(
            f"[strategy] underlying must be exactly one of "
            f"{sorted(SUPPORTED_UNDERLYINGS)} (case-sensitive); got "
            f"{underlying!r} in {config_path}")
    return underlying


def derive_paths(underlying: str) -> RunnerPaths:
    from runner_common import taleb_state_suffix
    sfx = taleb_state_suffix(underlying)
    return RunnerPaths(
        state_file=DATA_CACHE / f"taleb_paper_state{sfx}.json",
        lock_file=DATA_CACHE / f".taleb_paper{sfx}.lock",
        silent_fail_flag=DATA_CACHE / f"taleb_paper_silent_fail{sfx}.flag",
        log_prefix=f"paper{sfx}",
    )


# load_holidays, is_trading_day, assert_holiday_data_fresh and sleep_until
# are imported from runner_common (audit 2.1) — the hardened versions
# (precise malformed-date errors, CSV-header tolerance) replace the older
# local copies this runner carried.


def setup_logging(today: date, log_prefix: str = "paper") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"{log_prefix}-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile),
        ],
        force=True,
    )
    return logging.getLogger("run_paper")


def tick(hedger, log: logging.Logger) -> bool:
    """One intraday iteration. Failures logged but do not kill the loop.
    Returns True if the tick completed without a swallowed exception, False
    if scan or rehedge raised — the HeartbeatTracker uses this to detect a
    systemic fault (e.g. token expired mid-session) where every tick fails
    and the runner would otherwise exit 0 SUCCESS having traded nothing."""
    ok = True
    try:
        proposals = hedger.scan_and_propose()
        if proposals:
            hedger.execute_proposals(proposals)
    except Exception as e:
        log.exception("scan_and_propose failed: %s", e)
        ok = False

    try:
        rehedge = hedger.check_and_rehedge()
        if rehedge:
            hedger.execute_proposals(rehedge)
    except Exception as e:
        log.exception("check_and_rehedge failed: %s", e)
        ok = False
    return ok


def _has_open_position(hedger) -> bool:
    return bool(hedger.state.positions) or abs(hedger.state.futures_hedge_delta) > 0


def force_flatten(hedger, log: logging.Logger, reason: str):
    """Old EOD-flatten path — used only by --force-flatten-on-exit or by
    the expiry-day guard. The default session end persists state instead."""
    if not _has_open_position(hedger):
        return
    try:
        log.info("Flattening %d position(s) — %s",
                 len(hedger.state.positions), reason)
        close_props = hedger._generate_close_all_proposals()
        if close_props:
            hedger.execute_proposals(close_props)
    except Exception as e:
        log.exception("Flatten failed: %s", e)


def load_prior_state(log: logging.Logger, state_file: Path) -> Optional[dict]:
    """Return the parsed state-file payload, or None if no file / corrupt.
    Corrupt-JSON safe: missing/bad file falls through to fresh-start so
    a one-time crash can't orphan every position."""
    if not state_file.exists() or state_file.stat().st_size == 0:
        # Refuse to silently start fresh if backups exist — broker may
        # still hold positions from the last backup.
        assert_no_orphan_backups(state_file, log)
        log.info("No prior state file at %s — starting fresh.", state_file.name)
        return None
    try:
        return json.loads(state_file.read_text())
    except Exception as e:
        log.exception("Failed to parse %s: %s.", state_file.name, e)
        assert_no_orphan_backups(state_file, log)
        log.info("No backups present — starting fresh.")
        return None


def restore_state_if_any(hedger, log: logging.Logger, state_file: Path):
    payload = load_prior_state(log, state_file)
    if not payload:
        return
    try:
        hedger.restore_state(payload)
        log.info(
            "Restored prior session (saved_at %s): %d position(s), "
            "futures_lots=%d, cum_realized=₹%.0f, total_pnl=₹%.0f",
            payload.get("saved_at", "?"),
            len(hedger.state.positions),
            hedger.state.futures_lots,
            hedger.state.realized_pnl,
            hedger.state.total_pnl,
        )
    except Exception as e:
        log.exception("restore_state failed: %s — keeping fresh strategy "
                      "(saved position will be ABANDONED; manual review)", e)


def write_state_file(hedger, log: logging.Logger, state_file: Path):
    """Atomically persist current strategy state. Atomic write via
    '.tmp' → os.replace so a crash mid-write can't leave a half-truncated
    file that fails to parse next session."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        payload = hedger.serialize_state()
    except Exception as e:
        log.exception("serialize_state failed: %s — state NOT persisted "
                      "(next session will start fresh)", e)
        return
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2))
    os.replace(tmp, state_file)
    log.info("State persisted: %s (%d position(s), futures_lots=%d)",
             state_file.name, len(hedger.state.positions), hedger.state.futures_lots)
    archive_state_backup(state_file, log)


def end_of_session(hedger, today: date, args, log: logging.Logger,
                   state_file: Path):
    """At session end: (1) force-flatten on operator hatch or expiry-day,
    (2) write EOD report, (3) save IV history, (4) persist state.

    Order matters — flatten before EOD report so the report reflects the
    post-flatten reality; persist after report so any state mutations the
    report makes are captured.

    H18: legs_expire_on now retries kite.instruments('NFO') 3× and raises
    on persistent failure rather than silently returning False. We always
    write the EOD report + state + IV history first (so tomorrow's runner
    isn't blind) then re-raise so the runner exits non-zero and
    notify-failure@ alerts the operator to manually flatten before cash
    settlement.
    """
    expiry_check_failure: Optional[Exception] = None
    if _has_open_position(hedger):
        if args.force_flatten_on_exit:
            force_flatten(hedger, log, reason="OPS_FORCE (--force-flatten-on-exit)")
        else:
            try:
                if hedger.legs_expire_on(today):
                    force_flatten(hedger, log,
                                  reason="EXPIRY (leg contract expires today)")
            except Exception as e:
                log.exception("Expiry check failed after retries: %s", e)
                expiry_check_failure = e

    try:
        report = hedger.generate_eod_report()
        log.info("EOD report: %s", report)
    except Exception as e:
        log.exception("EOD report failed: %s", e)

    try:
        hedger._save_iv_history()
    except Exception as e:
        log.exception("IV history save failed: %s", e)

    write_state_file(hedger, log, state_file)

    if expiry_check_failure is not None:
        log.critical(
            "EXPIRY CHECK FAILED for taleb-karpathy session. EOD report + "
            "state + IV history have been written; runner will now exit "
            "non-zero so notify-failure@ alerts. OPERATOR ACTION: manually "
            "verify whether any option/futures leg's contract expires "
            "today and square off BEFORE cash settlement.",
        )
        raise RuntimeError(
            "Expiry-day check failed; refusing to silently proceed (H18)."
        ) from expiry_check_failure


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="Config file (default config.ini = NIFTY). Point at a "
                             "self-contained BANKNIFTY config (config_banknifty.ini) "
                             "for the isolated 2nd instance (issue #62); state/lock/"
                             "log isolate by the config's `underlying`. Creds come "
                             "from the KITE_* env (.env), so the BANKNIFTY config "
                             "needs no real [kite] secrets.")
    parser.add_argument("--force", action="store_true",
                        help="Run even on weekends/holidays (testing only)")
    parser.add_argument("--force-flatten-on-exit", action="store_true",
                        help="Flatten any open position at session end "
                             "before persisting state. Operations safety "
                             "hatch — the default is to hold positions "
                             "across sessions and exit only on strategy "
                             "triggers (max_holding_period_hours, daily "
                             "loss stop, vega/gap exit) or contract expiry.")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    os.chdir(HERE)

    config_path = args.config
    # Derive the underlying UP FRONT — before constructing the strategy — so a
    # BANKNIFTY instance never touches the NIFTY runner's state/lock/log (#62).
    underlying = resolve_underlying(config_path)
    paths = derive_paths(underlying)

    today = datetime.now().date()
    log = setup_logging(today, paths.log_prefix)

    # Pre-flight gates (audit 2.1 — protections this runner previously
    # lacked). TZ first: a wrong-TZ run misquotes market hours and holiday
    # boundaries silently (the unit sets TZ=Asia/Kolkata). Disk next: a full
    # partition corrupts the state-file write. Both fail loud.
    assert_timezone_ist(log)
    assert_disk_space_ok([DATA_CACHE, LOG_DIR], log)

    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, log)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    # Single-instance lock — a second concurrent run of the SAME underlying would
    # clobber its state file. The lock is per-underlying (paths.lock_file), so the
    # NIFTY and BANKNIFTY instances can run concurrently but neither can run twice.
    _lock_fd = acquire_lock(paths.lock_file, log,
                            label=f"taleb paper runner ({underlying})")  # noqa: F841

    # SIGTERM → KeyboardInterrupt so `systemctl stop`/restart runs the EOD
    # teardown (and returns 130, whitelisted via SuccessExitStatus=130 in
    # taleb-hedger.service) instead of dying without persisting state.
    install_signal_handlers(log)

    log.info("=" * 60)
    log.info("PAPER TRADING SESSION — %s", today)
    log.info("=" * 60)

    from kite_auth import KiteAuthManager
    from strategies import TalebKarpathyStrategy

    log.info("Authenticating...")
    auth = KiteAuthManager(config_path)
    kite = auth.get_kite()
    profile = kite.profile()
    log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])

    # Force paper mode regardless of config — this script is the unattended
    # daily paper-trade runner; live execution belongs to a separate path.
    hedger = TalebKarpathyStrategy(kite, config_path=config_path, mode="paper")
    log.info("Mode: PAPER  underlying=%s  capital=%.0f  state=%s",
             hedger.underlying, hedger.immutable_params["total_capital"],
             paths.state_file.name)
    if hedger.underlying != underlying:
        # The strategy read a different underlying than the runner derived paths
        # from — the isolation would be wrong. Fail loud rather than clobber.
        raise SystemExit(
            f"Config underlying mismatch: runner derived {underlying!r} but "
            f"strategy constructed {hedger.underlying!r}")
    log.info("Tunable params: %s", hedger.tunable_params)
    hedger.log_effective_params()

    # Restore any open position from yesterday's session before the tick loop.
    restore_state_if_any(hedger, log, paths.state_file)

    now = datetime.now()
    open_ts = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    session_end_ts = now.replace(hour=SESSION_END_AT[0], minute=SESSION_END_AT[1],
                                  second=0, microsecond=0)
    hard_stop_ts = now.replace(hour=HARD_STOP[0], minute=HARD_STOP[1], second=0, microsecond=0)

    if now >= hard_stop_ts:
        log.info("Started after %s — nothing to do today.", hard_stop_ts.strftime("%H:%M"))
        return 0

    if now < open_ts:
        sleep_until(open_ts, log)

    log.info("Entering tick loop (every %ds until %s)",
             TICK_SECONDS, session_end_ts.strftime("%H:%M"))

    # Silent-dead-trader detector: a systemic fault (token expired, kite
    # outage) makes every tick fail while the loop swallows the errors and
    # would otherwise exit 0 at 15:25 having traded nothing. On
    # SILENT_FAIL_THRESHOLD consecutive failed ticks, break and exit
    # non-zero so taleb-hedger.service's OnFailure= alerts.
    heartbeat = HeartbeatTracker(SILENT_FAIL_THRESHOLD, paths.silent_fail_flag, log)
    silent_fail = False

    try:
        while datetime.now() < session_end_ts:
            ok = tick(hedger, log)
            if heartbeat.record_tick(1, 0 if ok else 1):
                silent_fail = True
                break
            remaining = (session_end_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        if silent_fail:
            log.critical("Silent-fail heartbeat breached — persisting state "
                         "and exiting non-zero so OnFailure alerts.")
            end_of_session(hedger, today, args, log, paths.state_file)
            return 1

        log.info("Session-end window reached.")
        end_of_session(hedger, today, args, log, paths.state_file)

    except KeyboardInterrupt:
        log.info("Interrupted — persisting state and exiting.")
        end_of_session(hedger, today, args, log, paths.state_file)
        return 130

    log.info("Session complete. Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
