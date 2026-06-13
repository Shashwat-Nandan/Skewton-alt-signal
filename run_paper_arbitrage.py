#!/usr/bin/env python3
"""
Arbitrage Paper Runner
======================
Unattended intraday loop for the arbitrage strategy (calendar / term-structure
spreads on single-stock futures). Runs alongside the pair-trading runner
(run_paper_pairs.py) on its own systemd timer.

  - Refuses to run on weekends or dates in holidays.csv (override with --force)
  - Authenticates via TOTP (kite_auth.KiteAuthManager)
  - Instantiates ONE ArbitrageStrategy over the configured universe (a single
    strategy that monitors the whole universe — unlike pairs, which is one
    strategy per pair)
  - Restores any prior-session open calendar spreads from
    data_cache/arbitrage_paper_state_<system>.json
  - Blocks until 09:15 IST, ticks every 60s until 15:25 IST
  - Persists state each tick (no EOD flatten by default; open spreads exit only
    on strategy triggers — convergence, max-hold, or the near-leg's last
    trading day, all handled inside check_and_rehedge)
  - Writes data_cache/arbitrage_paper_eod_<date>.json with the strategy's
    generate_eod_report() for the dashboard
  - Per-day logfile under logs/paper-arbitrage[-SYSTEM]-YYYY-MM-DD.log

The generic safety scaffolding (TZ / disk / holiday pre-flight gates, the
silent-fail heartbeat, the operator HALT_ALL / HALT_NEW_ENTRIES kill switches,
session-time constants) is imported from run_paper_pairs rather than copied —
those helpers carry no pair-specific state. Arbitrage keeps its OWN lock file,
state file, and daily-loss flag so the two runners never clobber or freeze each
other.

Assumes the process sees wall-clock IST (systemd sets TZ=Asia/Kolkata).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from _state_backup import archive_state_backup, assert_no_orphan_backups

# Reuse the pair runner's generic, state-free safety helpers + constants.
# These have no pair-specific coupling — they gate on wall-clock, disk, and
# the holidays file, which are shared infrastructure.
from run_paper_pairs import (
    HALT_ALL_PATH,
    HALT_NEW_ENTRIES_PATH,
    HARD_STOP,
    MARKET_OPEN,
    SESSION_END_AT,
    SILENT_FAIL_THRESHOLD,
    TICK_SECONDS,
    HeartbeatTracker,
    assert_disk_space_ok,
    assert_holiday_data_fresh,
    assert_timezone_ist,
    install_signal_handlers,
    is_trading_day,
    load_holidays,
    sleep_until,
)


HERE = Path(__file__).resolve().parent
CONFIG_PATH = str(HERE / "config.ini")
HOLIDAYS_PATH = HERE / "holidays.csv"
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"

# Arbitrage's OWN auto-set daily-loss flag — deliberately NOT the pairs runner's
# HALT_DAILY_LOSS. A loss breach in one strategy should not freeze entries in
# the other; the operator's HALT_ALL / HALT_NEW_ENTRIES switches remain shared
# (a manual kill switch is meant to stop everything).
HALT_ARB_DAILY_LOSS_PATH = DATA_CACHE / "HALT_ARBITRAGE_DAILY_LOSS"

STATE_FILE_TEMPLATE = "arbitrage_paper_state_{system}.json"
LOCK_FILE_TEMPLATE = ".arbitrage_paper_{system}.lock"
SILENT_FAIL_FLAG_TEMPLATE = "arbitrage_paper_silent_fail_{system}.flag"


def setup_logging(today: date, system: str = "baseline") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if system == "baseline" else f"-{system}"
    logfile = LOG_DIR / f"paper-arbitrage{suffix}-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile),
        ],
        force=True,
    )
    return logging.getLogger("run_paper_arbitrage")


def silent_fail_flag_path(system: str) -> Path:
    return DATA_CACHE / SILENT_FAIL_FLAG_TEMPLATE.format(system=system)


def state_file_path(system: str) -> Path:
    return DATA_CACHE / STATE_FILE_TEMPLATE.format(system=system)


def acquire_runner_lock(system: str, log: logging.Logger) -> int:
    """Refuse to start if another arbitrage runner already holds the lock for
    this --system tag. Two processes sharing a state file would clobber each
    other's writes. Returns the open FD — the caller must keep the reference
    alive so the OS holds the lock until the process exits.

    Note: the lock file name is arbitrage-specific so it does NOT collide with
    the pair runner's lock (sharing it would make the two runners block each
    other for no reason)."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    path = DATA_CACHE / LOCK_FILE_TEMPLATE.format(system=system)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RuntimeError(
            f"Another arbitrage-paper runner is already holding the lock for "
            f"--system={system} (lock file: {path}). Refusing to start a "
            f"second runner — concurrent writes to the state file would "
            f"silently lose mutations. If the previous runner died abnormally, "
            f"`rm {path}` after confirming no process is actually running."
        )
    log.info("Runner lock acquired: %s (pid %d)", path, os.getpid())
    return fd


def load_prior_state(system: str, log: logging.Logger) -> Optional[Dict]:
    """Return the prior session's serialised strategy blob, or None if no state
    file exists. On a parse failure, refuse to silently start fresh if backups
    exist (the broker may still hold open spreads from the last good state)."""
    path = state_file_path(system)
    if not path.exists() or path.stat().st_size == 0:
        assert_no_orphan_backups(path, log)
        log.info("No prior state file at %s — starting fresh.", path)
        return None
    try:
        payload = json.loads(path.read_text())
    except Exception as e:
        log.exception("Failed to parse state file %s: %s.", path, e)
        assert_no_orphan_backups(path, log)
        log.info("No backups present — starting fresh.")
        return None
    blob = payload.get("state")
    log.info("Loaded prior state from %s (updated_at %s)",
             path.name, payload.get("updated_at", "?"))
    return blob


def restore_strategy(strategy, prior: Optional[Dict], log: logging.Logger) -> None:
    """Restore open calendar spreads from the prior session. Fails loud inside
    restore_state on a shape mismatch; here we catch so a corrupted file is
    surfaced but doesn't abandon the whole session — the operator gets a
    CRITICAL line and a fresh-but-empty book to inspect."""
    if not prior:
        return
    try:
        strategy.restore_state(prior)
        log.info(
            "Restored: open_calendars=%d cum_realized=₹%.0f "
            "cum_costs=₹%.0f closed_trades=%d",
            len(strategy.state.open_calendars), strategy.state.realized_pnl,
            strategy.state.total_transaction_costs,
            len(strategy.state.closed_trades),
        )
    except Exception as e:
        log.critical(
            "restore_state FAILED: %s — starting with an EMPTY book. Any open "
            "spreads in the prior state file are now UNMANAGED; review the "
            "state file under data_cache/ manually.", e,
        )


def write_state_file(strategy, system: str, log: logging.Logger,
                     archive: bool = True) -> None:
    """Atomically and durably persist current strategy state (write-tmp →
    fsync → atomic rename → fsync parent dir). Mirrors the crash-safe write in
    run_paper_pairs.write_state_file.

    archive=False skips the timestamped backup + log line — used by the
    intraday tick-loop persist which fires every minute."""
    path = state_file_path(system)
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    payload = {
        "strategy": strategy.name,
        "system": system,
        "updated_at": datetime.now().isoformat(),
        "state": None,
    }
    try:
        payload["state"] = strategy.serialize_state()
    except Exception as e:
        log.exception("serialize_state failed: %s", e)
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    if archive:
        log.info("State persisted: %s (%d open spreads)",
                 path.name, len(strategy.state.open_calendars))
        archive_state_backup(path, log)


def write_eod_sidecar(strategy, today: date, log: logging.Logger,
                      system: str = "baseline") -> None:
    """EOD report for the dashboard. Baseline keeps the un-suffixed filename so
    the backend router's default ingest is untouched; other systems suffix it."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    if system == "baseline":
        filename = f"arbitrage_paper_eod_{today.isoformat()}.json"
    else:
        filename = f"arbitrage_paper_{system}_eod_{today.isoformat()}.json"
    path = DATA_CACHE / filename
    payload = {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "system": system,
        "report": None,
    }
    try:
        payload["report"] = strategy.generate_eod_report()
    except Exception as e:
        log.exception("EOD report failed: %s", e)
    path.write_text(json.dumps(payload, default=str, indent=2))
    log.info("EOD sidecar: %s", path)


class ArbHaltState:
    """Tracks the operator kill switches (shared HALT_ALL / HALT_NEW_ENTRIES)
    plus arbitrage's OWN auto-set daily-loss flag. HALT_ALL implies
    HALT_NEW_ENTRIES. Transitions are logged once."""

    def __init__(self):
        self.halt_all = False
        self.halt_new = False

    def refresh(self, log: logging.Logger) -> None:
        prev_all, prev_new = self.halt_all, self.halt_new
        self.halt_all = HALT_ALL_PATH.exists()
        halt_loss = HALT_ARB_DAILY_LOSS_PATH.exists()
        self.halt_new = (self.halt_all
                         or HALT_NEW_ENTRIES_PATH.exists()
                         or halt_loss)
        if self.halt_all and not prev_all:
            log.critical("KILL SWITCH: HALT_ALL flag present (%s) — all "
                         "entries AND exits suspended. Spreads frozen until "
                         "the flag is removed.", HALT_ALL_PATH)
        elif prev_all and not self.halt_all:
            log.warning("HALT_ALL flag cleared — resuming normal tick loop")
        if self.halt_new and not prev_new and not self.halt_all:
            sources = []
            if HALT_NEW_ENTRIES_PATH.exists():
                sources.append("HALT_NEW_ENTRIES")
            if halt_loss:
                sources.append("HALT_ARBITRAGE_DAILY_LOSS")
            log.warning("Entries suspended (flags: %s); exits and rehedges "
                        "continue normally", "+".join(sources))
        elif prev_new and not self.halt_new:
            log.warning("Entry-halt flags cleared — resuming entries")


def check_daily_loss_limit(strategy, limit_inr: float,
                           log: logging.Logger) -> None:
    """On a session ΔP&L breach, touch HALT_ARBITRAGE_DAILY_LOSS — caught by
    ArbHaltState next tick → entries suspended, exits continue. The flag
    persists across restarts so the operator must `rm` it to resume."""
    if limit_inr <= 0:
        return
    if HALT_ARB_DAILY_LOSS_PATH.exists():
        return
    session_delta = (
        (strategy.state.realized_pnl + strategy.state.unrealized_pnl)
        - (strategy._session_start_realized + strategy._session_start_unrealized)
    )
    if session_delta <= -limit_inr:
        log.critical(
            "DAILY LOSS LIMIT BREACHED: session ΔP&L = ₹%.0f vs limit ₹%.0f. "
            "Touching %s — entries suspended; open spreads continue to exit. "
            "Operator: `rm %s` to acknowledge and resume entries.",
            session_delta, -limit_inr, HALT_ARB_DAILY_LOSS_PATH,
            HALT_ARB_DAILY_LOSS_PATH,
        )
        try:
            HALT_ARB_DAILY_LOSS_PATH.touch()
        except Exception as e:
            log.exception("Failed to write %s: %s", HALT_ARB_DAILY_LOSS_PATH, e)


def reconcile_with_broker(strategy, mode: str, log: logging.Logger) -> None:
    """Live-mode safety: the state file is the runner's view of open calendar
    legs; kite.positions() is the broker's truth. They must agree before the
    tick loop touches anything, or the runner would compute exits / MTM /
    daily-loss against legs the broker doesn't hold (or leave a real position
    unmanaged). Paper-mode runs skip — there is no real broker position.

    Refuses to start (raises) on any share mismatch; warns on broker NFO
    positions this runner doesn't track. Mirrors run_paper_pairs'
    reconcile_with_broker, scoped to the calendar-spread leg model."""
    if mode != "live":
        log.info("Broker reconciliation skipped (mode=%s)", mode)
        return
    try:
        broker_positions = strategy.kite.positions().get("net", []) or []
    except Exception as e:
        log.exception("kite.positions() failed: %s", e)
        raise RuntimeError(
            f"Broker reconciliation could not run: kite.positions() raised "
            f"{e!r}. Refusing to start — broker state is unknown."
        )

    broker_qty: Dict[str, int] = {}
    for pos in broker_positions:
        if pos.get("exchange") != "NFO":
            continue
        ts = pos.get("tradingsymbol", "")
        if not ts:
            continue
        broker_qty[ts] = broker_qty.get(ts, 0) + int(pos.get("quantity", 0))

    mismatches: List[str] = []
    expected_ts: set = set()
    for symbol, trade in strategy.state.open_calendars.items():
        for leg in trade.legs:
            expected_ts.add(leg.tradingsymbol)
            expected_shares = leg.quantity * leg.lot_size  # signed
            actual_shares = broker_qty.get(leg.tradingsymbol, 0)
            if expected_shares != actual_shares:
                mismatches.append(
                    f"{symbol} {leg.tradingsymbol}: state expects "
                    f"{expected_shares} shares, broker has {actual_shares}"
                )

    unknown = [ts for ts, qty in broker_qty.items()
               if qty != 0 and ts not in expected_ts]
    if unknown:
        log.warning("Broker has %d NFO position(s) not tracked by this runner "
                    "— it will NOT manage them: %s", len(unknown), unknown)

    if mismatches:
        msg = ("Broker reconciliation FAILED — refusing to start.\n  "
               + "\n  ".join(mismatches)
               + "\nResolve before retry: restore the state file from a backup, "
               "square off the broker positions manually, or move the state "
               "file aside to acknowledge a clean restart.")
        log.error(msg)
        raise RuntimeError(msg)
    log.info("Broker reconciliation OK: %d expected NFO leg(s) match",
             len(expected_ts))


def tick_once(strategy, log: logging.Logger,
              halt_all: bool = False,
              halt_new_entries: bool = False) -> tuple[bool, bool]:
    """One iteration of the single strategy. Returns (attempted_execution,
    errored). Failures are logged but never kill the loop.

    HALT_ALL skips both entries and exits (book frozen). HALT_NEW_ENTRIES skips
    only scan_and_propose; check_and_rehedge (exits/convergence/expiry) keeps
    running so open spreads can still close."""
    if halt_all:
        return False, False

    attempted = False
    errored = False

    if not halt_new_entries:
        try:
            proposals = strategy.scan_and_propose()
            # Data-health signal for the heartbeat. scan_and_propose sets
            # last_basis_snapshot to the observed universe; _safe_quote
            # swallows per-symbol quote failures, so a dead token / API outage
            # does NOT raise here — it just yields an empty observation and no
            # proposals. Treat an empty universe as an error so the silent-fail
            # heartbeat can catch a blind "dead trader" that would otherwise
            # exit 0 having done nothing all session.
            if not strategy.state.last_basis_snapshot:
                errored = True
                log.warning("scan observed an EMPTY universe (0 symbols) — "
                            "likely a quote/token outage; flagging for the "
                            "silent-fail heartbeat")
            if proposals:
                strategy.execute_proposals(proposals)
                attempted = True
        except Exception as e:
            errored = True
            log.exception("scan_and_propose failed: %s", e)

    try:
        rehedge = strategy.check_and_rehedge()
        if rehedge:
            strategy.execute_proposals(rehedge)
            attempted = True
    except Exception as e:
        errored = True
        log.exception("check_and_rehedge failed: %s", e)

    return attempted, errored


def _legs_expiring_on_or_before(trade, today: date) -> List[str]:
    """Tradingsymbols of the trade's legs whose contract expires on or before
    `today`. A leg with an unparseable expiry is returned too (treated as
    suspect) so it gets surfaced rather than silently assumed safe."""
    out: List[str] = []
    for leg in trade.legs:
        try:
            exp = date.fromisoformat(str(leg.expiry)[:10])
        except (ValueError, TypeError):
            out.append(leg.tradingsymbol)
            continue
        if exp <= today:
            out.append(leg.tradingsymbol)
    return out


def _flatten_open_spreads(strategy, log: logging.Logger, reason: str,
                          symbols: Optional[set] = None) -> List[str]:
    """Build + execute exits for open spreads (all, or just `symbols`). Returns
    the list of symbols that could NOT be flattened (no snapshot / error) so the
    caller can escalate. Prices a leg that has rolled off the instruments list
    at its last mark — best effort, the strategy logs the staleness."""
    failed: List[str] = []
    try:
        snapshots = {s["symbol"]: s for s in strategy._observe_universe()}
    except Exception as e:
        log.exception("Flatten: could not observe universe: %s", e)
        return [t for t in strategy.state.open_calendars
                if symbols is None or t in symbols]
    exits = []
    for symbol, trade in list(strategy.state.open_calendars.items()):
        if symbols is not None and symbol not in symbols:
            continue
        snap = snapshots.get(symbol)
        if snap is None:
            log.warning("[%s] flatten: no snapshot — leaving open", symbol)
            failed.append(symbol)
            continue
        exits.extend(strategy._build_calendar_exit(trade, snap, reason))
    if exits:
        try:
            strategy.execute_proposals(exits)
        except Exception as e:
            log.exception("Flatten: execute_proposals failed: %s", e)
    return failed


def end_of_session(strategy, today: date, args, log: logging.Logger) -> None:
    """At session end: (1) force-flatten any leg whose contract expires today
    (settlement risk); (2) honour --force-flatten-on-exit; (3) refresh MTM from
    live quotes; (4) persist state; (5) write the EOD sidecar.

    The intraday dte_near<=1 check in check_and_rehedge is the first line of
    defence, but once a near contract actually expires it rolls off the
    instruments list and snap["near"] becomes the NEXT month — so the held leg
    stops matching that branch. This EOD pass keys off each leg's OWN expiry
    (not the snapshot's near) and, if an expiring leg can't be flattened, writes
    state+sidecar then raises so notify-failure@ pages the operator before cash
    settlement (mirrors run_paper_pairs' H18 guard)."""
    # (1) Expiry-day flatten — settlement risk overrides "hold across sessions".
    expiring = {
        symbol for symbol, trade in strategy.state.open_calendars.items()
        if _legs_expiring_on_or_before(trade, today)
    }
    unverified_expiry: List[str] = []
    if expiring:
        log.warning("Expiry-day flatten: %d spread(s) hold a leg expiring on/"
                    "before %s: %s", len(expiring), today, ", ".join(sorted(expiring)))
        unverified_expiry = _flatten_open_spreads(
            strategy, log, "EXPIRY", symbols=expiring)

    # (2) Operator force-flatten of everything still open.
    if args.force_flatten_on_exit and strategy.state.open_calendars:
        log.info("Force-flatten requested for %d open spread(s)",
                 len(strategy.state.open_calendars))
        _flatten_open_spreads(strategy, log, "OPS_FORCE")

    # (3) Refresh MTM so the sidecar's unrealized / cumulative reflect the
    # close, not a mark that may be a tick or more stale (e.g. last tick was a
    # halt or a silent-fail break). Best effort — a dead feed keeps last mark.
    if strategy.state.open_calendars:
        try:
            snaps = {s["symbol"]: s for s in strategy._observe_universe()}
            strategy._update_unrealized(snaps)
        except Exception as e:
            log.warning("EOD mark refresh failed: %s — sidecar uses last mark", e)

    # (4) + (5) persist then publish.
    write_state_file(strategy, args.system, log)
    write_eod_sidecar(strategy, today, log, args.system)

    if unverified_expiry:
        log.critical(
            "EXPIRY FLATTEN FAILED for %d spread(s): %s. State and EOD sidecar "
            "written; exiting non-zero so notify-failure@ alerts. OPERATOR "
            "ACTION: manually square off the expiring leg(s) BEFORE cash "
            "settlement — a future carried into settlement is the worst case.",
            len(unverified_expiry), ", ".join(sorted(unverified_expiry)),
        )
        raise RuntimeError(
            f"Expiry-day flatten failed for {len(unverified_expiry)} spread(s); "
            "refusing to silently proceed."
        )


def main():
    parser = argparse.ArgumentParser(description="Automated arbitrage paper runner")
    parser.add_argument("--mode", choices=["paper", "live", "signals"],
                        default="paper",
                        help="Execution mode. paper (default): mock fills. "
                             "live: real money via Kite — requires "
                             "ALLOW_LIVE_MODE=true AND "
                             "--i-understand-this-is-real-money AND "
                             "--max-daily-loss-inr > 0. signals: basis JSONL "
                             "only, no fills.")
    parser.add_argument("--i-understand-this-is-real-money",
                        dest="i_understand", action="store_true",
                        help="Required confirmation flag for --mode live.")
    parser.add_argument("--system", type=str, default="baseline",
                        help="System tag — suffixes log/state/EOD filenames so "
                             "parallel runners don't clobber each other. "
                             "Defaults to 'baseline' (un-suffixed filenames).")
    parser.add_argument("--max-leg-notional", type=float, default=1_000_000,
                        help="Per-leg ₹ cap (overrides config; required so a "
                             "1-lot RELIANCE leg can't deploy ₹50L silently)")
    parser.add_argument("--lots-per-leg", type=int, default=1,
                        help="Lots per calendar leg (entry size)")
    parser.add_argument("--ack-large-size", action="store_true",
                        help="Required when --lots-per-leg > 5 — a typo "
                             "tripwire so --lots-per-leg 100 can't deploy a "
                             "100× book.")
    parser.add_argument("--max-open-calendars", type=int, default=None,
                        help="Max simultaneous calendar spreads (overrides "
                             "config). Default: use config value.")
    parser.add_argument("--max-daily-loss-inr", type=float, default=50_000.0,
                        help="Session ΔP&L floor (₹). On breach the runner "
                             "touches HALT_ARBITRAGE_DAILY_LOSS — entries "
                             "suspend, exits continue, persists across "
                             "restarts. 0 disables (not recommended for live).")
    parser.add_argument("--kite-rate-per-sec", type=float, default=8.0,
                        dest="kite_rate_per_sec",
                        help="Token-bucket refill rate (req/s). Kite's ceiling "
                             "is 10/s; default 8 leaves headroom. (default: 8)")
    parser.add_argument("--kite-burst", type=int, default=8, dest="kite_burst",
                        help="Token-bucket burst size. (default: 8)")
    parser.add_argument("--force", action="store_true",
                        help="Run even on weekends/holidays (testing only)")
    parser.add_argument("--force-flatten-on-exit", action="store_true",
                        help="Flatten every open spread at session end before "
                             "persisting. Ops hatch — default is to hold across "
                             "sessions and exit only on strategy triggers.")
    args = parser.parse_args()

    if args.lots_per_leg > 5 and not args.ack_large_size:
        parser.error(
            f"--lots-per-leg={args.lots_per_leg} exceeds the soft cap of 5. "
            "Pass --ack-large-size to acknowledge intentional large sizing."
        )

    load_dotenv(HERE / ".env")
    os.chdir(HERE)

    today = datetime.now().date()
    log = setup_logging(today, args.system)

    # Live-mode safety gate — three independent locks plus an armed circuit
    # breaker, mirroring run_paper_pairs.
    if args.mode == "live":
        env_allow = os.environ.get("ALLOW_LIVE_MODE", "").strip().lower()
        if env_allow != "true":
            raise RuntimeError(
                "--mode live requires ALLOW_LIVE_MODE=true in the environment "
                f"(set in .env). Refusing to start. Current value: {env_allow!r}"
            )
        if not args.i_understand:
            raise RuntimeError(
                "--mode live requires the --i-understand-this-is-real-money "
                "confirmation flag. Refusing to start."
            )
        if args.max_daily_loss_inr <= 0:
            raise RuntimeError(
                "--mode live requires --max-daily-loss-inr > 0 (circuit "
                "breaker). Refusing to start."
            )
        log.critical("=" * 60)
        log.critical("LIVE TRADING SESSION — REAL MONEY [system=%s]", args.system)
        log.critical("=" * 60)

    # Pre-flight gates — all fail loud.
    assert_timezone_ist(log)
    assert_disk_space_ok([DATA_CACHE, LOG_DIR], log)

    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, log)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    _runner_lock_fd = acquire_runner_lock(args.system, log)  # noqa: F841

    log.info("=" * 60)
    log.info("ARBITRAGE %s SESSION — %s [system=%s]",
             args.mode.upper(), today, args.system)
    log.info("=" * 60)

    from kite_auth import KiteAuthManager
    from kite_throttle import KiteRateLimiter, throttle_kite

    log.info("Authenticating...")
    auth = KiteAuthManager(CONFIG_PATH)
    kite = auth.get_kite()
    kite_limiter = KiteRateLimiter(
        rate_per_sec=args.kite_rate_per_sec, burst=args.kite_burst,
    )
    kite = throttle_kite(kite, kite_limiter)
    profile = kite.profile()
    log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])
    log.info("Kite throttle armed: rate=%.1f req/s, burst=%d",
             args.kite_rate_per_sec, args.kite_burst)

    from strategies.arbitrage import ArbitrageStrategy
    strategy = ArbitrageStrategy(kite=kite, config_path=CONFIG_PATH, mode=args.mode)
    # Per-instance CLI overrides (post-init mutation, same pattern as pairs).
    strategy.max_leg_notional = args.max_leg_notional
    strategy.lots_per_leg = args.lots_per_leg
    if args.max_open_calendars is not None:
        strategy.max_open_calendars = args.max_open_calendars
    log.info(
        "Strategy: universe=%d disable_calendar=%s calendar_entry=%.3f "
        "calendar_exit=%.3f max_open=%d lots=%d max_leg_notional=₹%.0f",
        len(strategy.universe), strategy.disable_calendar,
        strategy.calendar_entry_annual, strategy.calendar_exit_annual,
        strategy.max_open_calendars, strategy.lots_per_leg,
        strategy.max_leg_notional or 0.0,
    )
    if strategy.disable_calendar:
        log.warning(
            "disable_calendar=true — running as a basis-monitoring service "
            "only; NO calendar spreads will be traded this session.")

    # Restore prior-session open spreads. restore_strategy → restore_state
    # captures the session baseline internally; call it explicitly too so the
    # fresh-start path (no prior state) also anchors the baseline to now.
    restore_strategy(strategy, load_prior_state(args.system, log), log)
    strategy._capture_session_baseline()

    # Live-mode safety: the state file is the runner's view; the broker is the
    # truth. They must agree before the tick loop touches anything. Paper skips.
    reconcile_with_broker(strategy, args.mode, log)

    now = datetime.now()
    open_ts = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                          second=0, microsecond=0)
    session_end_ts = now.replace(hour=SESSION_END_AT[0], minute=SESSION_END_AT[1],
                                 second=0, microsecond=0)
    hard_stop_ts = now.replace(hour=HARD_STOP[0], minute=HARD_STOP[1],
                               second=0, microsecond=0)

    if now >= hard_stop_ts:
        log.info("Started after %s — nothing to do today.",
                 hard_stop_ts.strftime("%H:%M"))
        return 0
    if now < open_ts:
        sleep_until(open_ts, log)

    log.info("Entering tick loop (every %ds until %s)",
             TICK_SECONDS, session_end_ts.strftime("%H:%M"))

    halt_state = ArbHaltState()
    if args.max_daily_loss_inr <= 0:
        log.warning("--max-daily-loss-inr is disabled (0) — no automatic "
                    "circuit breaker this session")
    install_signal_handlers(log)
    heartbeat = HeartbeatTracker(
        threshold=SILENT_FAIL_THRESHOLD,
        sentinel_path=silent_fail_flag_path(args.system),
        log=log,
    )
    silent_fail = False
    tick_id = 0
    try:
        while datetime.now() < session_end_ts:
            tick_id += 1
            # One observation per tick: scan + rehedge + EOD all call
            # _observe_universe, which dedupes on this id (see its docstring).
            strategy._obs_tick_id = tick_id
            halt_state.refresh(log)
            _attempted, errored = tick_once(
                strategy, log,
                halt_all=halt_state.halt_all,
                halt_new_entries=halt_state.halt_new,
            )
            n_ran = 0 if halt_state.halt_all else 1
            n_errored = 1 if errored else 0
            if heartbeat.record_tick(n_ran=n_ran, n_errored=n_errored):
                silent_fail = True
                break
            check_daily_loss_limit(strategy, args.max_daily_loss_inr, log)
            # One durable write per tick — captures both fills and the
            # intraday MTM update from check_and_rehedge. (A separate per-fill
            # write would be byte-identical for a single strategy, since only
            # check_daily_loss_limit — a flag-file touch — runs in between.)
            try:
                write_state_file(strategy, args.system, log, archive=False)
            except Exception as e:
                log.exception("Intraday state persist failed: %s — continuing", e)
            remaining = (session_end_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        if silent_fail:
            try:
                end_of_session(strategy, today, args, log)
            except Exception as e:
                log.exception("end_of_session failed during silent-fail "
                              "teardown: %s — heartbeat alert still fires", e)
        else:
            log.info("Session-end window reached.")
            end_of_session(strategy, today, args, log)

    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        log.info("Interrupted — persisting state and exiting.")
        end_of_session(strategy, today, args, log)
        return 130

    if silent_fail:
        return 1
    log.info("Session complete. Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
