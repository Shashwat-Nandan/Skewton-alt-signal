#!/usr/bin/env python3
"""
Paper-Trading Runner
====================
Unattended intraday loop:
  - Refuses to run on weekends or dates in holidays.csv
  - Authenticates (TOTP auto-login via kite_auth)
  - Blocks until 09:15 IST, ticks until 15:25 IST
  - Flattens positions + writes EOD report, then exits 0
  - Per-day logfile under logs/paper-YYYY-MM-DD.log

Assumes the process sees wall-clock IST (systemd sets TZ=Asia/Kolkata).
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
CONFIG_PATH = str(HERE / "config.ini")
HOLIDAYS_PATH = HERE / "holidays.csv"
LOG_DIR = HERE / "logs"

MARKET_OPEN = (9, 15)
FLATTEN_AT = (15, 25)   # close positions before the 15:30 bell
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


def flatten_and_report(hedger, log: logging.Logger):
    try:
        if hedger.state.positions or abs(hedger.state.futures_hedge_delta) > 0:
            log.info("Flattening %d positions before close", len(hedger.state.positions))
            close_props = hedger._generate_close_all_proposals()
            if close_props:
                hedger.execute_proposals(close_props)
    except Exception as e:
        log.exception("Flatten failed: %s", e)

    try:
        report = hedger.generate_eod_report()
        log.info("EOD report: %s", report)
    except Exception as e:
        log.exception("EOD report failed: %s", e)

    try:
        hedger._save_iv_history()
    except Exception as e:
        log.exception("IV history save failed: %s", e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Run even on weekends/holidays (testing only)")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    os.chdir(HERE)

    today = datetime.now().date()
    log = setup_logging(today)

    holidays = load_holidays(HOLIDAYS_PATH)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    log.info("=" * 60)
    log.info("PAPER TRADING SESSION — %s", today)
    log.info("=" * 60)

    from kite_auth import KiteAuthManager
    from dynamic_hedger import TalebHedger

    log.info("Authenticating...")
    auth = KiteAuthManager(CONFIG_PATH)
    kite = auth.get_kite()
    profile = kite.profile()
    log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])

    hedger = TalebHedger(kite, config_path=CONFIG_PATH)
    if not hedger._is_paper_mode:
        log.error("config.ini has trading_mode != paper. Refusing to run.")
        return 2
    log.info("Mode: PAPER  underlying=%s  capital=%.0f",
             hedger.underlying, hedger.immutable_params["total_capital"])
    log.info("Tunable params: %s", hedger.tunable_params)

    now = datetime.now()
    open_ts = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    flatten_ts = now.replace(hour=FLATTEN_AT[0], minute=FLATTEN_AT[1], second=0, microsecond=0)
    hard_stop_ts = now.replace(hour=HARD_STOP[0], minute=HARD_STOP[1], second=0, microsecond=0)

    if now >= hard_stop_ts:
        log.info("Started after %s — nothing to do today.", hard_stop_ts.strftime("%H:%M"))
        return 0

    if now < open_ts:
        sleep_until(open_ts, log)

    log.info("Entering tick loop (every %ds until %s)",
             TICK_SECONDS, flatten_ts.strftime("%H:%M"))

    try:
        while datetime.now() < flatten_ts:
            tick(hedger, log)
            remaining = (flatten_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        log.info("Flatten window reached.")
        flatten_and_report(hedger, log)

    except KeyboardInterrupt:
        log.info("Interrupted — attempting graceful flatten.")
        flatten_and_report(hedger, log)
        return 130

    log.info("Session complete. Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
