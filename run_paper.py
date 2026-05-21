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
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from _state_backup import archive_state_backup, assert_no_orphan_backups


HERE = Path(__file__).resolve().parent
CONFIG_PATH = str(HERE / "config.ini")
HOLIDAYS_PATH = HERE / "holidays.csv"
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
STATE_FILE = DATA_CACHE / "taleb_paper_state.json"

MARKET_OPEN = (9, 15)
# Wall-clock when the tick loop ends. Open positions are NOT flattened
# here — they survive to the next session via the state file. The only
# session-end exits are (a) --force-flatten-on-exit (ops hatch) and
# (b) a held leg whose contract expires today.
SESSION_END_AT = (15, 25)
HARD_STOP = (15, 30)    # never tick past this
TICK_SECONDS = 60


def load_holidays(path: Path) -> set[date]:
    if not path.exists():
        return set()
    days: set[date] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split(",", 1)[0].strip()
        days.add(date.fromisoformat(token))
    return days


def is_trading_day(d: date, holidays: set[date]) -> tuple[bool, str]:
    if d.weekday() >= 5:
        return False, f"{d} is a weekend"
    if d in holidays:
        return False, f"{d} is an NSE holiday"
    return True, ""


HOLIDAY_HORIZON_DAYS = 30
HOLIDAYS_PER_YEAR_FLOOR = 8


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


def setup_logging(today: date) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"paper-{today.isoformat()}.log"
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


def sleep_until(target: datetime, log: logging.Logger):
    while True:
        delta = (target - datetime.now()).total_seconds()
        if delta <= 0:
            return
        log.info("Waiting %.0fs until %s", delta, target.strftime("%H:%M:%S"))
        time.sleep(min(delta, 60))


def tick(hedger, log: logging.Logger):
    """One intraday iteration. Failures logged but do not kill the loop."""
    try:
        proposals = hedger.scan_and_propose()
        if proposals:
            hedger.execute_proposals(proposals)
    except Exception as e:
        log.exception("scan_and_propose failed: %s", e)

    try:
        rehedge = hedger.check_and_rehedge()
        if rehedge:
            hedger.execute_proposals(rehedge)
    except Exception as e:
        log.exception("check_and_rehedge failed: %s", e)


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


def load_prior_state(log: logging.Logger) -> Optional[dict]:
    """Return the parsed state-file payload, or None if no file / corrupt.
    Corrupt-JSON safe: missing/bad file falls through to fresh-start so
    a one-time crash can't orphan every position."""
    if not STATE_FILE.exists() or STATE_FILE.stat().st_size == 0:
        # Refuse to silently start fresh if backups exist — broker may
        # still hold positions from the last backup.
        assert_no_orphan_backups(STATE_FILE, log)
        log.info("No prior state file at %s — starting fresh.", STATE_FILE.name)
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception as e:
        log.exception("Failed to parse %s: %s.", STATE_FILE.name, e)
        assert_no_orphan_backups(STATE_FILE, log)
        log.info("No backups present — starting fresh.")
        return None


def restore_state_if_any(hedger, log: logging.Logger):
    payload = load_prior_state(log)
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


def write_state_file(hedger, log: logging.Logger):
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
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2))
    os.replace(tmp, STATE_FILE)
    log.info("State persisted: %s (%d position(s), futures_lots=%d)",
             STATE_FILE.name, len(hedger.state.positions), hedger.state.futures_lots)
    archive_state_backup(STATE_FILE, log)


def end_of_session(hedger, today: date, args, log: logging.Logger):
    """At session end: (1) force-flatten on operator hatch or expiry-day,
    (2) write EOD report, (3) save IV history, (4) persist state.

    Order matters — flatten before EOD report so the report reflects the
    post-flatten reality; persist after report so any state mutations the
    report makes are captured."""
    if _has_open_position(hedger):
        if args.force_flatten_on_exit:
            force_flatten(hedger, log, reason="OPS_FORCE (--force-flatten-on-exit)")
        else:
            try:
                if hedger.legs_expire_on(today):
                    force_flatten(hedger, log,
                                  reason="EXPIRY (leg contract expires today)")
            except Exception as e:
                log.exception("Expiry check failed: %s — leaving position", e)

    try:
        report = hedger.generate_eod_report()
        log.info("EOD report: %s", report)
    except Exception as e:
        log.exception("EOD report failed: %s", e)

    try:
        hedger._save_iv_history()
    except Exception as e:
        log.exception("IV history save failed: %s", e)

    write_state_file(hedger, log)


def main():
    parser = argparse.ArgumentParser()
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

    today = datetime.now().date()
    log = setup_logging(today)

    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, log)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    log.info("=" * 60)
    log.info("PAPER TRADING SESSION — %s", today)
    log.info("=" * 60)

    from kite_auth import KiteAuthManager
    from strategies import TalebKarpathyStrategy

    log.info("Authenticating...")
    auth = KiteAuthManager(CONFIG_PATH)
    kite = auth.get_kite()
    profile = kite.profile()
    log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])

    # Force paper mode regardless of config — this script is the unattended
    # daily paper-trade runner; live execution belongs to a separate path.
    hedger = TalebKarpathyStrategy(kite, config_path=CONFIG_PATH, mode="paper")
    log.info("Mode: PAPER  underlying=%s  capital=%.0f",
             hedger.underlying, hedger.immutable_params["total_capital"])
    log.info("Tunable params: %s", hedger.tunable_params)

    # Restore any open position from yesterday's session before the tick loop.
    restore_state_if_any(hedger, log)

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

    try:
        while datetime.now() < session_end_ts:
            tick(hedger, log)
            remaining = (session_end_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        log.info("Session-end window reached.")
        end_of_session(hedger, today, args, log)

    except KeyboardInterrupt:
        log.info("Interrupted — persisting state and exiting.")
        end_of_session(hedger, today, args, log)
        return 130

    log.info("Session complete. Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
