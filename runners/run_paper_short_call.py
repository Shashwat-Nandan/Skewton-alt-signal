#!/usr/bin/env python3
"""
Short-Call-Into-Earnings Paper Runner — PAPER ONLY.

Forward-tests ``strategies/short_call_earnings.py``. Read that module's header
before reading this one: the backtest shows NO EDGE and the headline result is
100% directional (docs/research/pre-earnings-iv-crush-2026-08-29.md §5.3). This
runner exists to measure the thing forward, above all how often an earnings gap
beats the nominal 1R stop.

Shape (mirrors run_paper_buy_on_gap):

  1. Pre-flight (TZ / disk / holiday gates) + auth + restore prior state.
  2. Refresh the earnings calendar, then build/extend the ATM-IV panel from the
     bhavcopy cache THROUGH YESTERDAY (no auth needed, no look-ahead).
  3. In a late ENTRY WINDOW (default 15:00–15:20 IST — the study entered at the
     T-1 close) snapshot each candidate's ATM call via kite.quote and sell.
  4. Tick every 60s managing target / stop / gap-stop.
  5. Flatten anything at ``max_hold_sessions`` and write state + EOD sidecar.

Positions ARE carried across sessions (entry T-1, exit by T+1), unlike
buy_on_gap. That is why the state file is restored unconditionally rather than
intraday-only.

Assumes wall-clock IST (systemd sets TZ=Asia/Kolkata). LIVE MODE IS NOT
SUPPORTED — the strategy raises in __init__ for mode=live.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv

from core.runner_common import (
    HALT_ALL_PATH,
    HALT_NEW_ENTRIES_PATH,
    HARD_STOP,
    HOLIDAYS_PATH,
    SILENT_FAIL_THRESHOLD,
    TICK_SECONDS,
    HeartbeatTracker,
    acquire_lock,
    assert_disk_space_ok,
    assert_holiday_data_fresh,
    assert_timezone_ist,
    durable_write_text,
    install_signal_handlers,
    is_trading_day,
    load_holidays,
    scoped_halt_new_entries_path,
    sleep_until,
)
from market_data.fetch_board_meetings import load_results_calendar
from strategies import _atm_iv
from strategies.short_call_earnings import ShortCallEarningsStrategy

HERE = Path(__file__).resolve().parent.parent
CONFIG_PATH = str(HERE / "config.ini")
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"

# This runner's OWN daily-loss flag — a breach here must never freeze the other
# strategies' runners (the 2026-07-23 HALT_NEW_ENTRIES scoping incident).
HALT_SHORT_CALL_DAILY_LOSS_PATH = DATA_CACHE / "HALT_SHORT_CALL_DAILY_LOSS"
# Scoped entry halt an automated monitor may trip WITHOUT freezing every other
# runner. The shared HALT_NEW_ENTRIES stays operator-owned and is also honoured.
HALT_SHORT_CALL_ENTRIES_PATH = scoped_halt_new_entries_path("short_call")
STATE_FILE = DATA_CACHE / "short_call_paper_state.json"
SILENT_FAIL_FLAG = DATA_CACHE / "SILENT_FAIL_short_call_paper"
LOCK_FILE = DATA_CACHE / ".short_call_paper.lock"

ENTRY_WINDOW_START = dtime(15, 0)
ENTRY_WINDOW_END = dtime(15, 20)
SESSION_END = dtime(15, 30)

logger = logging.getLogger("run_paper_short_call")


def _setup_logging(level: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"short_call_paper_{date.today():%Y%m%d}.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _next_trading_day(today: date, holidays) -> date:
    d = today
    for _ in range(14):
        d = (pd.Timestamp(d) + pd.Timedelta(days=1)).date()
        ok, _reason = is_trading_day(d, holidays)
        if ok:
            return d
    raise RuntimeError(f"no trading day within 14 days of {today}")


def _atm_call_snapshot(kite, symbol: str, panel_row, instruments_nfo) -> Optional[dict]:
    """
    Build the per-symbol option snapshot the strategy consumes.

    Strike/expiry come from the live NFO instrument dump; prices from
    kite.quote. ``atm_iv`` is solved from the live call+put mid so the IV
    percentile ranks TODAY against the EOD panel through yesterday.
    """
    from core.greeks_engine import implied_volatility_bisect

    rows = [i for i in instruments_nfo
            if i.get("name") == symbol and i.get("instrument_type") in ("CE", "PE")]
    if not rows:
        logger.info("%s — no NFO option rows", symbol)
        return None
    today = pd.Timestamp.now().normalize()
    expiries = sorted({pd.Timestamp(i["expiry"]) for i in rows})
    expiries = [e for e in expiries if (e - today).days >= _atm_iv.MIN_DTE]
    if not expiries:
        logger.info("%s — no expiry with DTE >= %d", symbol, _atm_iv.MIN_DTE)
        return None
    exp = expiries[0]
    dte = int((exp - today).days)

    try:
        spot_q = kite.quote([f"NSE:{symbol}"])
        spot = float(spot_q[f"NSE:{symbol}"]["last_price"])
    except Exception as e:                                    # noqa: BLE001
        logger.warning("%s — spot quote failed: %s", symbol, e)
        return None
    strikes = sorted({float(i["strike"]) for i in rows
                      if pd.Timestamp(i["expiry"]) == exp and float(i["strike"]) > 0})
    if not strikes:
        return None
    strike = min(strikes, key=lambda k: abs(k - spot))
    leg = {i["instrument_type"]: i for i in rows
           if pd.Timestamp(i["expiry"]) == exp and float(i["strike"]) == strike}
    if "CE" not in leg or "PE" not in leg:
        logger.info("%s — incomplete ATM pair at strike %s", symbol, strike)
        return None

    keys = [f"NFO:{leg['CE']['tradingsymbol']}", f"NFO:{leg['PE']['tradingsymbol']}"]
    try:
        q = kite.quote(keys)
    except Exception as e:                                    # noqa: BLE001
        logger.warning("%s — option quote failed: %s", symbol, e)
        return None
    ce, pe = q.get(keys[0]), q.get(keys[1])
    if not ce or not pe or ce["last_price"] <= 0 or pe["last_price"] <= 0:
        logger.info("%s — ATM pair not quoting", symbol)
        return None

    T = max(dte, 1) / 365.0
    iv_ce = implied_volatility_bisect(ce["last_price"], spot, strike, T, 0.065, "CE")
    iv_pe = implied_volatility_bisect(pe["last_price"], spot, strike, T, 0.065, "PE")
    ohlc = ce.get("ohlc") or {}
    return {
        "symbol": symbol, "spot": spot, "strike": strike,
        "expiry": exp.strftime("%Y-%m-%d"), "dte": dte,
        "tradingsymbol": leg["CE"]["tradingsymbol"],
        "lot_size": int(leg["CE"].get("lot_size") or panel_row.lot),
        "call_px": float(ce["last_price"]),
        "atm_iv": 0.5 * (iv_ce + iv_pe),
        "open": float(ohlc.get("open") or ce["last_price"]),
        "high": float(ohlc.get("high") or ce["last_price"]),
        "low": float(ohlc.get("low") or ce["last_price"]),
        "date": pd.Timestamp.now(),
    }


def _check_daily_loss_limit(strategy, baseline: float, limit_inr: float) -> None:
    """
    Trip this runner's OWN entry halt on a session drawdown.

    The flag was read by the entry gate from the first commit but nothing ever
    wrote it, so the guard read as present in review while being dead — on an
    unbounded-loss naked short with no loss breaker at all. Mirrors
    run_paper_buy_on_gap.check_daily_loss_limit; scoped so a breach here never
    freezes the other strategies' runners.
    """
    if limit_inr <= 0 or HALT_SHORT_CALL_DAILY_LOSS_PATH.exists():
        return
    session_delta = (strategy.realized_pnl + strategy._unrealized()) - baseline
    if session_delta <= -limit_inr:
        logger.critical("DAILY LOSS LIMIT BREACHED: session ΔP&L=₹%.0f vs ₹%.0f. "
                        "Touching %s — entries suspended; positions still exit. "
                        "Operator: `rm %s` to resume.",
                        session_delta, -limit_inr,
                        HALT_SHORT_CALL_DAILY_LOSS_PATH, HALT_SHORT_CALL_DAILY_LOSS_PATH)
        try:
            HALT_SHORT_CALL_DAILY_LOSS_PATH.touch()
        except Exception as e:                            # noqa: BLE001
            logger.error("could not write the daily-loss flag (%s) — "
                         "entries are NOT halted", e)


def _held_snapshot(kite, pos) -> Optional[dict]:
    """
    Snapshot for an ALREADY-OPEN position, quoted on the contract actually sold.

    Never re-derive the ATM strike for a held position. `_atm_call_snapshot`
    re-strikes from the current spot and rolls to the next expiry once DTE drops
    below MIN_DTE, so on the very session this strategy exists to measure — the
    results gap — it would hand back a freshly-struck option that has not moved,
    the stop would not fire, and `gap_through_stop_count` would read zero while
    the real position bled (code review 2026-08-29, finding 1).
    """
    key = f"NFO:{pos.tradingsymbol}"
    try:
        q = kite.quote([key])
    except Exception as e:                                    # noqa: BLE001
        logger.warning("%s — held-position quote failed: %s", pos.tradingsymbol, e)
        return None
    row = q.get(key)
    if not row or not row.get("last_price"):
        logger.warning("%s — held position not quoting", pos.tradingsymbol)
        return None
    ohlc = row.get("ohlc") or {}
    px = float(row["last_price"])
    return {
        "symbol": pos.symbol, "spot": pos.spot_at_entry, "strike": pos.strike,
        "expiry": pos.expiry, "tradingsymbol": pos.tradingsymbol,
        "lot_size": pos.lot_size, "call_px": px, "atm_iv": 0.0,
        "open": float(ohlc.get("open") or px),
        "high": float(ohlc.get("high") or px),
        "low": float(ohlc.get("low") or px),
        "date": pd.Timestamp.now(),
    }


def _load_state(strategy) -> None:
    if not STATE_FILE.exists():
        return
    try:
        strategy.restore_state(json.loads(STATE_FILE.read_text()))
        logger.info("restored %d open / %d closed positions",
                    len(strategy.positions), len(strategy.closed_positions))
    except Exception as e:                                    # noqa: BLE001
        logger.error("state restore FAILED (%s) — refusing to start blind", e)
        raise


def _save_state(strategy) -> None:
    DATA_CACHE.mkdir(exist_ok=True)
    durable_write_text(STATE_FILE,
                       json.dumps(strategy.serialize_state(), indent=1, default=str))


def _write_eod(strategy, today: date) -> None:
    rep = strategy.generate_eod_report()
    out = DATA_CACHE / f"short_call_paper_eod_{today:%Y-%m-%d}.json"
    out.write_text(json.dumps(rep, indent=1, default=str))
    td, cum = rep["today"], rep["cumulative"]
    logger.info("EOD %s: TODAY realized ₹%+,.0f | %d closed | mean R %s | "
                "gap-through-stop %d (worst R %s)",
                today, td["realized_pnl"], td["closed_trades"], td["mean_realised_R"],
                td["gap_through_stop_count"], td["gap_through_worst_R"])
    logger.info("EOD %s: CUMULATIVE realized ₹%+,.0f | %d closed | mean R %s | "
                "gap-through-stop %d (worst R %s) | %d still open",
                today, cum["realized_pnl"], cum["closed_trades"], cum["mean_realised_R"],
                cum["gap_through_stop_count"], cum["gap_through_worst_R"],
                rep["open_positions"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Short-call-into-earnings PAPER runner")
    p.add_argument("--force", action="store_true",
                   help="run outside market hours / on a non-trading day")
    p.add_argument("--ignore-entry-window", action="store_true",
                   help="also allow entries outside %s-%s IST. Separate from "
                        "--force on purpose: the study entered at the T-1 "
                        "close, so entering at another time tests something "
                        "the backtest never measured."
                        % (ENTRY_WINDOW_START, ENTRY_WINDOW_END))
    p.add_argument("--config", default=CONFIG_PATH)
    p.add_argument("--log-level", default="INFO")
    p.add_argument("--once", action="store_true", help="single scan+manage pass, then exit")
    p.add_argument("--skip-calendar-refresh", action="store_true")
    p.add_argument("--max-daily-loss-inr", type=float, default=40000.0,
                   help="session ΔP&L at which new entries are halted "
                        "(0 disables). Default 2x the 1R budget.")
    p.add_argument("--dry-run", action="store_true",
                   help="pre-flight + panel + calendar only; never authenticates or trades")
    args = p.parse_args(argv)

    _setup_logging(args.log_level)
    load_dotenv(HERE / ".env")
    os.chdir(HERE)
    today = date.today()

    # Pre-flight gates — all fail loud.
    assert_timezone_ist(logger)
    assert_disk_space_ok([DATA_CACHE, LOG_DIR], logger)
    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, logger)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        logger.info("No-op: %s. Exiting.", reason)
        return 0
    if HALT_ALL_PATH.exists():
        logger.warning("HALT_ALL present — exiting")
        return 0

    _lock_fd = acquire_lock(LOCK_FILE, logger, label="short-call-paper runner")  # noqa: F841
    logger.info("=" * 62)
    logger.info("SHORT-CALL-INTO-EARNINGS PAPER SESSION — %s", today)
    logger.info("NO MEASURED EDGE — forward test only "
                "(docs/research/pre-earnings-iv-crush-2026-08-29.md §5.3)")

    if not args.skip_calendar_refresh:
        try:
            from market_data.fetch_board_meetings import sync
            sync(today, today + timedelta(days=45))
        except Exception as e:                                # noqa: BLE001
            logger.warning("calendar refresh failed (%s) — using cache", e)
    calendar = load_results_calendar()
    if calendar.empty:
        logger.error("results calendar is EMPTY — no entries are possible. Run "
                     "`python -m market_data.fetch_board_meetings` and check NSE access.")
    panel = _atm_iv.build_panel()
    next_session = _next_trading_day(today, holidays)
    # An empty calendar has object-dtype columns, so `.dt` would raise here —
    # right after we logged that it is empty and intended to carry on
    # exit-only (review finding 2).
    due = (calendar.iloc[0:0] if calendar.empty else
           calendar[calendar.event_date.dt.normalize() == pd.Timestamp(next_session)])
    universe = set(panel.symbol.unique())
    due_fno = sorted(set(due.symbol) & universe)
    logger.info("panel through %s (%d rows, %d symbols); calendar %d symbol-quarters",
                panel.date.max().date(), len(panel), len(universe), len(calendar))
    logger.info("next session %s — %d results meetings, %d in the F&O universe: %s",
                next_session, len(due), len(due_fno), due_fno or "(none)")

    if args.dry_run:
        logger.info("--dry-run: skipping auth and trading.")
        return 0

    from core.kite_auth import KiteAuthManager
    kite = KiteAuthManager(args.config).get_kite()
    strategy = ShortCallEarningsStrategy(kite, config_path=args.config, mode="paper")
    strategy.log_effective_params()
    strategy.set_panel(panel)
    strategy.set_calendar(calendar)
    _load_state(strategy)

    install_signal_handlers(logger)
    heartbeat = HeartbeatTracker(threshold=SILENT_FAIL_THRESHOLD,
                                 sentinel_path=SILENT_FAIL_FLAG, log=logger)
    instruments_nfo: List[dict] = []
    session_end_ts = datetime.combine(today, dtime(*HARD_STOP))
    # Anchor for the session drawdown so a restart mid-session does not
    # re-measure the day from zero.
    session_baseline = strategy.realized_pnl + strategy._unrealized()

    def tick() -> bool:
        """One scan+manage pass. Returns True if anything errored (heartbeat)."""
        nonlocal instruments_nfo
        now = datetime.now()
        errored = False
        strategy.set_current_date(pd.Timestamp(now), next_session=pd.Timestamp(next_session))

        entries_allowed = (
            (ENTRY_WINDOW_START <= now.time() <= ENTRY_WINDOW_END
             or args.ignore_entry_window)
            and not HALT_NEW_ENTRIES_PATH.exists()
            and not HALT_SHORT_CALL_ENTRIES_PATH.exists()
            and not HALT_SHORT_CALL_DAILY_LOSS_PATH.exists()
        )
        want = set(strategy.positions.keys())
        if entries_allowed:
            want |= set(due_fno)
        if not want:
            return False
        try:
            if not instruments_nfo:
                instruments_nfo = kite.instruments("NFO")
            snaps: Dict[str, dict] = {}
            # Held positions first, quoted on their own tradingsymbol.
            for sym, pos in strategy.positions.items():
                snap = _held_snapshot(kite, pos)
                if snap is not None:
                    snaps[sym] = snap
            # Entry candidates: re-derive the ATM contract. Never for a symbol
            # already held — that snapshot would describe a different option.
            cands = {s for s in want if s not in strategy.positions}
            if cands:
                latest = _atm_iv.latest_rows(panel, pd.Timestamp(now), cands)
                rowmap = {r.symbol: r for r in latest.itertuples()}
                for sym in sorted(cands):
                    row = rowmap.get(sym)
                    if row is None:
                        continue
                    snap = _atm_call_snapshot(kite, sym, row, instruments_nfo)
                    if snap is not None:
                        snaps[sym] = snap
            if not snaps:
                logger.warning("empty option snapshot for %d symbol(s) — "
                               "token/API outage or nothing quoting", len(want))
                errored = True
            strategy.set_snapshots(snaps)
            exits = strategy.check_and_rehedge()
            if exits:
                strategy.execute_proposals(exits)
            if entries_allowed:
                props = strategy.scan_and_propose()
                if props:
                    strategy.execute_proposals(props)
        except Exception as e:                                # noqa: BLE001
            errored = True
            logger.exception("tick failed: %s", e)
        _save_state(strategy)
        return errored

    # --- finding 3: --force bypasses the market-hours/holiday gate, it does NOT
    # collapse the session to one tick. Doing so left naked short calls carried
    # in from T-1 completely unmanaged for the rest of the day. --once is the
    # single-pass flag; --force outside session hours degrades to one tick
    # because there is no session left to loop over, and says so.
    try:
        if args.once:
            err = tick()
            heartbeat.record_tick(n_ran=1, n_errored=1 if err else 0)
        elif datetime.now() >= session_end_ts:
            if not args.force:
                logger.info("started after %s with no session left — nothing to do",
                            session_end_ts.strftime("%H:%M"))
            else:
                logger.warning("--force outside session hours: running ONE tick. "
                               "Open positions will NOT be managed further today.")
                err = tick()
                heartbeat.record_tick(n_ran=1, n_errored=1 if err else 0)
        else:
            while datetime.now() < session_end_ts:
                err = tick()
                _check_daily_loss_limit(strategy, session_baseline,
                                        args.max_daily_loss_inr)
                if heartbeat.record_tick(n_ran=1, n_errored=1 if err else 0):
                    logger.error("silent-fail threshold hit — exiting")
                    break
                nxt = datetime.now() + timedelta(seconds=TICK_SECONDS)
                if nxt >= session_end_ts:
                    break
                sleep_until(nxt, logger)
        _write_eod(strategy, today)
    except KeyboardInterrupt:
        # install_signal_handlers maps SIGTERM to KeyboardInterrupt precisely so
        # `systemctl stop` runs an orderly teardown. Without this the EOD
        # sidecar was skipped and main() raised out of the process (finding 2).
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        logger.info("Interrupted — persisting state and writing the EOD sidecar.")
        _save_state(strategy)
        _write_eod(strategy, today)
        return 130
    finally:
        _save_state(strategy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
