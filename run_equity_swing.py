#!/usr/bin/env python3
"""
Equity-swing scan runner — invoked twice daily by systemd timers.

Two scan kinds, both run by the same script with ``--scan {open|close}``:

  open  -- fires shortly after market open (09:30 IST). Refreshes the
           panel (live spot via Kite quote for today, daily bars from
           the last bhavcopy ingest), runs SL/target checks against
           today's intraday levels we can observe, scans for new
           setups using yesterday's close as the indicator anchor.
           Conservative: no new entries proposed at open scan in v1.

  close -- fires after the bell (15:35 IST). Bhavcopy must have
           landed already — pair-paper.timer's pattern. Computes the
           full feature set, runs entries + exits, persists to
           ``dashboard.db.equity_positions``.

Mode selection:

  --mode signals  -> JSONL only (no positions persisted)
  --mode paper    -> persist to equity_positions + equity_scans

Live mode is intentionally absent here; the strategy raises if asked.

Per-day logfile under ``logs/equity-YYYY-MM-DD.log``. Exits 0 on a
clean run, 1 on errors that didn't bring the process down.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

CONFIG_PATH = str(HERE / "config.ini")
HOLIDAYS_PATH = HERE / "holidays.csv"
LOG_DIR = HERE / "logs"


def _load_holidays() -> set:
    if not HOLIDAYS_PATH.exists():
        return set()
    out: set = set()
    for raw in HOLIDAYS_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.add(date.fromisoformat(line.split(",", 1)[0].strip()))
        except ValueError:
            continue
    return out


def _setup_logging(today: date) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"equity-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logfile)],
        force=True,
    )
    return logging.getLogger("run_equity_swing")


def _is_trading_day(today: date) -> tuple[bool, str]:
    if today.weekday() >= 5:
        return False, f"{today} is a weekend"
    if today in _load_holidays():
        return False, f"{today} is an NSE holiday"
    return True, ""


_HOLIDAY_HORIZON_DAYS = 30
_HOLIDAYS_PER_YEAR_FLOOR = 8


def _assert_holiday_data_fresh(today: date, log: logging.Logger) -> None:
    # holidays.csv is hand-maintained from the NSE circular; a partial
    # or expired list silently treats lunar holidays (Holi, Diwali, etc.)
    # as trading days. Fail loud per CLAUDE.md Rule 12.
    holidays = _load_holidays()
    if not holidays:
        msg = ("holidays.csv loaded zero entries — refusing to start. "
               "Populate from the NSE 'Holidays — Trading' circular.")
        log.error(msg)
        raise RuntimeError(msg)
    last = max(holidays)
    horizon = today + timedelta(days=_HOLIDAY_HORIZON_DAYS)
    if last < horizon:
        msg = (f"holidays.csv last entry is {last}, less than "
               f"{_HOLIDAY_HORIZON_DAYS} days past today ({today}). "
               f"Refusing to start — update from the NSE circular and "
               f"redeploy.")
        log.error(msg)
        raise RuntimeError(msg)
    this_year_count = sum(1 for h in holidays if h.year == today.year)
    if this_year_count < _HOLIDAYS_PER_YEAR_FLOOR:
        msg = (f"holidays.csv has only {this_year_count} entries for "
               f"{today.year}; NSE typically has 13-17 per year. The list "
               f"is likely missing lunar holidays (Holi, Diwali, etc.). "
               f"Refusing to start — update from the NSE circular.")
        log.error(msg)
        raise RuntimeError(msg)


def _load_open_positions_into_strategy(strategy, log: logging.Logger) -> int:
    """Resume in-memory position state from dashboard.db on startup."""
    from backend import db
    from strategies.varsity_equity_swing import EquityPosition
    rows = db.list_equity_positions(status="OPEN")
    n = 0
    for r in rows:
        try:
            pos = EquityPosition(
                symbol=r["symbol"], side=r["side"],
                entry_dt=pd.Timestamp(r["entry_dt"]),
                entry_px=float(r["entry_px"]),
                qty=int(r["qty"]),
                initial_sl=float(r["initial_sl"]),
                target=float(r["target"]),
                atr_at_entry=float(r["atr_at_entry"]),
                rationale=r["rationale"] or "",
            )
            pos.current_sl = float(r["current_sl"])
            pos.high_watermark = float(r["high_watermark"] or pos.entry_px)
            pos.last_mtm_px = float(r["last_mtm_px"] or pos.entry_px)
            if r["last_mtm_dt"]:
                pos.last_mtm_dt = pd.Timestamp(r["last_mtm_dt"])
            strategy.positions[pos.symbol] = pos
            strategy._db_id_by_symbol = getattr(strategy, "_db_id_by_symbol", {})
            strategy._db_id_by_symbol[pos.symbol] = int(r["id"])
            n += 1
        except (KeyError, TypeError, ValueError) as e:
            log.warning("skip stale row id=%s: %s", r.get("id"), e)
    return n


def _persist_proposals(strategy, scan_kind: str, log: logging.Logger) -> tuple[int, int]:
    """After execute_proposals ran, mirror in-memory state into the DB.

    Returns ``(n_opens_persisted, n_closes_persisted)``.
    """
    from backend import db
    db_ids = getattr(strategy, "_db_id_by_symbol", {})
    n_open = n_close = 0

    # Newly opened positions (in strategy.positions but not yet in db_ids)
    for sym, pos in strategy.positions.items():
        if sym in db_ids:
            # MTM update on existing open
            db.update_equity_position_mtm(
                position_id=db_ids[sym],
                last_mtm_dt=(pos.last_mtm_dt or datetime.now()).isoformat()
                            if pos.last_mtm_dt else datetime.now().isoformat(),
                last_mtm_px=float(pos.last_mtm_px),
                current_sl=float(pos.current_sl),
                high_watermark=float(pos.high_watermark),
            )
            continue
        pid = db.insert_equity_position({
            "symbol": pos.symbol, "side": pos.side,
            "entry_dt": pos.entry_dt.isoformat(),
            "entry_px": pos.entry_px, "qty": pos.qty,
            "initial_sl": pos.initial_sl, "current_sl": pos.current_sl,
            "target": pos.target, "atr_at_entry": pos.atr_at_entry,
            "rationale": pos.rationale,
            "last_mtm_dt": pos.last_mtm_dt.isoformat() if pos.last_mtm_dt else None,
            "last_mtm_px": pos.last_mtm_px,
            "high_watermark": pos.high_watermark,
        }, opened_by_scan=scan_kind)
        db_ids[sym] = pid
        log.info("[DB OPEN] id=%d %s qty=%d @ ₹%.2f", pid, pos.symbol, pos.qty, pos.entry_px)
        n_open += 1

    # Closed positions (strategy.closed_positions has the freshly-closed ones,
    # but only those with a db_id we tracked)
    for pos in strategy.closed_positions:
        pid = db_ids.pop(pos.symbol, None)
        if pid is None:
            continue
        db.close_equity_position(
            position_id=pid,
            exit_dt=pos.exit_dt.isoformat() if pos.exit_dt else datetime.now().isoformat(),
            exit_px=float(pos.exit_px or 0.0),
            exit_reason=pos.exit_reason or "MANUAL",
            pnl=float(pos.pnl),
        )
        log.info("[DB CLOSE] id=%d %s @ ₹%.2f reason=%s pnl=₹%+,.0f",
                 pid, pos.symbol, pos.exit_px or 0, pos.exit_reason, pos.pnl)
        n_close += 1

    strategy._db_id_by_symbol = db_ids
    return n_open, n_close


def main() -> int:
    p = argparse.ArgumentParser(description="Equity-swing scan runner")
    p.add_argument("--scan", choices=["open", "close"], required=True,
                   help="Scan kind: 'open' (post-bell) or 'close' (post-bhavcopy)")
    p.add_argument("--mode", choices=["signals", "paper"], default="paper")
    p.add_argument("--force", action="store_true",
                   help="Run even on weekends/holidays (testing only)")
    args = p.parse_args()

    today = datetime.now().date()
    log = _setup_logging(today)

    _assert_holiday_data_fresh(today, log)
    ok, reason = _is_trading_day(today)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    log.info("=" * 60)
    log.info("EQUITY SWING SCAN — kind=%s  mode=%s  date=%s", args.scan, args.mode, today)
    log.info("=" * 60)

    # Lazy imports so the script loads quickly when no-op'd.
    from backend import db
    from strategies.varsity_equity_swing import VarsityEquitySwingStrategy
    db.init_schema()

    # Auth — required even in signals mode (we use kite quote for today's
    # spot in the open scan; close scan uses bhavcopy and doesn't strictly
    # need it but the existing strategy ctor takes the kite handle either
    # way for parity with paper/live paths).
    try:
        from kite_auth import KiteAuthManager
        auth = KiteAuthManager(CONFIG_PATH)
        kite = auth.get_kite()
        prof = kite.profile()
        log.info("Authenticated as %s (%s)", prof["user_name"], prof["user_id"])
    except Exception as e:
        log.warning("Kite auth failed (%s) — continuing in degraded mode", e)
        class _NullKite:
            pass
        kite = _NullKite()

    strategy = VarsityEquitySwingStrategy(kite, config_path=CONFIG_PATH, mode=args.mode)
    log.info("Active params: trend=%s/%s adx>%s atr×%s rr=%s mp=%s oi=%s fii=%s",
             int(strategy.params["trend_short_window"]), int(strategy.params["trend_long_window"]),
             strategy.params["adx_threshold"], strategy.params["atr_stop_multiplier"],
             strategy.params["risk_reward"],
             int(strategy.params["mp_enabled"]), int(strategy.params["oi_enabled"]),
             int(strategy.params["fii_enabled"]))

    # Resume open paper positions from the DB so SL/target/time-stop logic
    # has full state across cron runs.
    if args.mode == "paper":
        n_resumed = _load_open_positions_into_strategy(strategy, log)
        log.info("Resumed %d open paper positions from DB", n_resumed)
    n_opens_before = len(strategy.positions)
    n_closed_before = len(strategy.closed_positions)

    # Run rehedge first (exit triggers), then scan (new entries).
    try:
        exits = strategy.check_and_rehedge()
        if exits:
            log.info("rehedge produced %d exit proposal(s)", len(exits))
            strategy.execute_proposals(exits)
    except Exception as e:
        log.exception("check_and_rehedge failed: %s", e)

    n_signals = 0
    if args.scan == "close":
        try:
            entries = strategy.scan_and_propose()
            n_signals = len(entries)
            if entries:
                log.info("scan produced %d entry proposal(s)", len(entries))
                strategy.execute_proposals(entries)
        except Exception as e:
            log.exception("scan_and_propose failed: %s", e)
    else:
        log.info("open scan: skipping new entries (v1 design — exits-only at open)")

    if args.mode == "paper":
        n_open_persisted, n_close_persisted = _persist_proposals(strategy, args.scan, log)
        log.info("DB writes: %d new opens, %d closes", n_open_persisted, n_close_persisted)

    # Scan summary row
    if args.mode == "paper":
        n_closed_today = len(strategy.closed_positions) - n_closed_before
        db.insert_equity_scan(
            scan_dt=datetime.now().isoformat(),
            scan_kind=args.scan, mode=args.mode,
            n_signals=n_signals,
            n_trades=n_closed_today + (len(strategy.positions) - n_opens_before),
            n_open_positions=len(strategy.positions),
            n_closed_today=n_closed_today,
        )

    # EOD-ish report
    try:
        report = strategy.generate_eod_report()
        log.info("Report: %s", report)
    except Exception as e:
        log.exception("generate_eod_report failed: %s", e)

    log.info("Session complete. Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
