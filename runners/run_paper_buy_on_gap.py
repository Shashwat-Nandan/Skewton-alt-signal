#!/usr/bin/env python3
"""
Buy-on-Gap Paper Runner
=======================
Unattended intraday loop for the Buy-on-Gap mean-reversion strategy (strategy
#5). Unlike the swing/pair/arbitrage runners that scan continuously, this one
matches the model's shape:

  1. Pre-flight + auth + restore prior state (intraday restart only — the book
     is never carried across sessions).
  2. Block until the open, then in a short ENTRY WINDOW (default 09:20–09:45
     IST) take ONE snapshot of every name's open via kite.quote, rank the
     statistically-unusual gap-downs, and BUY the top N.
  3. Tick every 60s refreshing quotes — the only intraday exit is the wide
     catastrophic stop (Chan §8.3: tight stops harm mean reversion).
  4. At session end FLATTEN everything at the last price (the same-day-close
     exit), write state + the EOD sidecar.

Historical features (prev-close, return-σ, long-MA, turnover) come from the
daily bhavcopy panel THROUGH YESTERDAY (load_equity_panel, no auth); today's
open / LTP / low come from kite.quote. Both feed the SAME BuyOnGapStrategy used
by the backtest, so paper and backtest accounting never diverge (Rule 7).

The generic safety scaffolding (TZ / disk / holiday gates, the silent-fail
heartbeat, the HALT_* kill switches, session-time constants) is imported from
runner_common. This runner keeps its OWN lock / state / daily-loss flag so it
never clobbers or freezes the other strategies' runners.

Assumes wall-clock IST (systemd sets TZ=Asia/Kolkata). LIVE MODE IS NOT
SUPPORTED — BuyOnGapStrategy raises in __init__ for mode=live (prove the edge
on paper first, mirroring varsity_equity_swing).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from core._state_backup import archive_state_backup, assert_no_orphan_backups
from core.runner_common import (
    HOLIDAYS_PATH,
    HALT_ALL_PATH,
    HALT_NEW_ENTRIES_PATH,
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

HERE = Path(__file__).resolve().parent.parent
CONFIG_PATH = str(HERE / "config.ini")
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"

# Buy-on-Gap's OWN daily-loss flag (not the shared HALT_DAILY_LOSS) — a breach
# here must not freeze the other strategies. Manual HALT_ALL / HALT_NEW_ENTRIES
# stay shared.
HALT_GAP_DAILY_LOSS_PATH = DATA_CACHE / "HALT_BUY_ON_GAP_DAILY_LOSS"

STATE_FILE_TEMPLATE = "buy_on_gap_paper_state_{system}.json"
LOCK_FILE_TEMPLATE = ".buy_on_gap_paper_{system}.lock"
SILENT_FAIL_FLAG_TEMPLATE = "buy_on_gap_paper_silent_fail_{system}.flag"

# Entry window. The gap-reversion edge is an OPEN phenomenon; once the morning
# is half over the gap has largely filled, so we refuse to open the day's book
# after the cutoff (a late restart then runs as exit-only).
ENTRY_AT = (9, 20)       # wait a few minutes so the opening print settles
ENTRY_CUTOFF = (9, 45)


def setup_logging(today: date, system: str = "baseline") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if system == "baseline" else f"-{system}"
    logfile = LOG_DIR / f"paper-buy-on-gap{suffix}-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logfile)],
        force=True,
    )
    return logging.getLogger("run_paper_buy_on_gap")


def silent_fail_flag_path(system: str) -> Path:
    return DATA_CACHE / SILENT_FAIL_FLAG_TEMPLATE.format(system=system)


def state_file_path(system: str) -> Path:
    return DATA_CACHE / STATE_FILE_TEMPLATE.format(system=system)


def acquire_runner_lock(system: str, log: logging.Logger) -> int:
    return acquire_lock(
        DATA_CACHE / LOCK_FILE_TEMPLATE.format(system=system), log,
        label=f"buy-on-gap-paper runner (--system={system})",
    )


def load_prior_state(system: str, log: logging.Logger) -> Optional[Dict]:
    """Return the prior session's serialised blob, or None. A blob is only
    relevant for an intraday RESTART (positions opened earlier today); a clean
    next-day start finds yesterday's flat book and simply re-anchors."""
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
    log.info("Loaded prior state from %s (updated_at %s)",
             path.name, payload.get("updated_at", "?"))
    return payload.get("state")


def restore_strategy(strategy, prior: Optional[Dict], today: date,
                     log: logging.Logger) -> None:
    """Restore the book ONLY if it belongs to today — a stale book from a prior
    session must never be resurrected (intraday strategy holds nothing
    overnight). Any leftover positions dated before today are dropped loud."""
    if not prior:
        return
    try:
        strategy.restore_state(prior)
    except Exception as e:
        log.critical("restore_state FAILED: %s — starting with an EMPTY book.", e)
        return
    stale = [s for s, p in strategy.positions.items()
             if p.entry_dt is None or p.entry_dt.date() != today]
    for s in stale:
        log.warning("Dropping stale position %s (entry %s != today) — intraday "
                    "book is never carried overnight", s, strategy.positions[s].entry_dt)
        strategy.positions.pop(s)
    log.info("Restored: open=%d cum_realized=₹%.0f cum_costs=₹%.0f closed=%d",
             len(strategy.positions), strategy.realized_pnl,
             strategy.transaction_costs, len(strategy.closed_positions))


def write_state_file(strategy, system: str, log: logging.Logger,
                     archive: bool = True) -> None:
    """Crash-safe durable persist (write-tmp → fsync → atomic rename → fsync
    dir). Mirrors run_paper_arbitrage.write_state_file."""
    path = state_file_path(system)
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    payload = {"strategy": strategy.name, "system": system,
               "updated_at": datetime.now().isoformat(), "state": None}
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
        log.info("State persisted: %s (%d open)", path.name, len(strategy.positions))
        archive_state_backup(path, log)


def write_eod_sidecar(strategy, today: date, log: logging.Logger,
                      system: str = "baseline") -> None:
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    if system == "baseline":
        filename = f"buy_on_gap_paper_eod_{today.isoformat()}.json"
    else:
        filename = f"buy_on_gap_paper_{system}_eod_{today.isoformat()}.json"
    path = DATA_CACHE / filename
    payload = {"date": today.isoformat(), "generated_at": datetime.now().isoformat(),
               "system": system, "report": None}
    try:
        payload["report"] = strategy.generate_eod_report()
    except Exception as e:
        log.exception("EOD report failed: %s", e)
    path.write_text(json.dumps(payload, default=str, indent=2))
    log.info("EOD sidecar: %s", path)


def experiment_kill_reason(realized_pnl: float, closed_pnls: List[float],
                           net_loss_floor_inr: float = 50_000.0,
                           min_trades: int = 15,
                           max_win_rate: float = 0.35) -> Optional[str]:
    """CUMULATIVE kill rule for the buy-on-gap experiment
    (docs/strategy-efficiency-review-2026-07-05.md §2.4).

    The backtest edge was known-overfit before deployment (train Sharpe 2.71 →
    test -0.83); this paper run exists to answer one question — does the
    forward record confirm the overfit? Once it has (deep cumulative net loss,
    or enough closed trades at a losing win rate), continuing is paying costs
    and attention for an answered question. Unlike the DAILY loss flag above
    (session-scoped, operator-clearable), this is cumulative and permanent
    until the thresholds are changed on the command line.

    Evaluated once at session START against the persisted book (it takes the
    raw per-trade pnl list so it can run BEFORE the panel/auth startup cost);
    a breach that happens mid-session is bounded by --max-daily-loss-inr that
    day and caught by this rule the next morning.

    Returns a human-readable reason string, or None while the experiment may
    continue. net_loss_floor_inr <= 0 disables the loss leg; min_trades <= 0
    disables the win-rate leg.
    """
    if net_loss_floor_inr > 0 and realized_pnl <= -net_loss_floor_inr:
        return (f"cumulative net realized ₹{realized_pnl:+,.0f} breached the "
                f"-₹{net_loss_floor_inr:,.0f} experiment floor")
    if min_trades > 0 and len(closed_pnls) >= min_trades:
        wins = sum(1 for p in closed_pnls if (p or 0.0) > 0)
        win_rate = wins / len(closed_pnls)
        if win_rate < max_win_rate:
            return (f"{len(closed_pnls)} closed trades at win rate "
                    f"{win_rate:.0%} < the {max_win_rate:.0%} experiment floor")
    return None


# On-disk marker dropped when the kill rule fires. Follows the HALT_* sentinel
# convention (visible to the operator and the scoreboard) because once the
# rule trips, the runner stops writing EOD sidecars — without a marker,
# "killed" would be indistinguishable from "broken" on every surface that
# watches those files (dashboard router, scripts/strategy_scoreboard.py).
KILLED_SENTINEL_PATH = DATA_CACHE / "HALT_BUY_ON_GAP_KILLED"


def _drop_killed_sentinel(reason: str, log: logging.Logger) -> None:
    try:
        KILLED_SENTINEL_PATH.write_text(
            f"{reason}\nfired {datetime.now().isoformat(timespec='seconds')}; "
            "informational marker — clearing it does NOT re-enable trading "
            "(the rule re-evaluates from state each session).\n")
    except Exception as e:
        log.exception("Failed to write %s: %s", KILLED_SENTINEL_PATH, e)


class GapHaltState:
    """Operator kill switches (shared HALT_ALL / HALT_NEW_ENTRIES) plus
    buy-on-gap's OWN auto daily-loss flag. HALT_ALL implies HALT_NEW_ENTRIES.
    `kill_rule=True` (cumulative experiment kill rule fired while positions
    were still open) pins halt_new for the whole session — exit-only mode."""

    def __init__(self, kill_rule: bool = False):
        self.halt_all = False
        self.halt_new = False
        self.kill_rule = kill_rule

    def refresh(self, log: logging.Logger) -> None:
        prev_all, prev_new = self.halt_all, self.halt_new
        self.halt_all = HALT_ALL_PATH.exists()
        halt_loss = HALT_GAP_DAILY_LOSS_PATH.exists()
        self.halt_new = (self.halt_all or HALT_NEW_ENTRIES_PATH.exists()
                         or halt_loss or self.kill_rule)
        if self.halt_all and not prev_all:
            log.critical("KILL SWITCH: HALT_ALL present (%s) — entries AND exits "
                         "suspended.", HALT_ALL_PATH)
        elif prev_all and not self.halt_all:
            log.warning("HALT_ALL cleared — resuming")
        if self.halt_new and not prev_new and not self.halt_all:
            log.warning("Entries suspended; exits continue")
        elif prev_new and not self.halt_new:
            log.warning("Entry-halt cleared")


def check_daily_loss_limit(strategy, limit_inr: float, log: logging.Logger) -> None:
    if limit_inr <= 0 or HALT_GAP_DAILY_LOSS_PATH.exists():
        return
    session_delta = (
        (strategy.realized_pnl + strategy._unrealized())
        - (strategy._session_start_realized + strategy._session_start_unrealized)
    )
    if session_delta <= -limit_inr:
        log.critical("DAILY LOSS LIMIT BREACHED: session ΔP&L=₹%.0f vs ₹%.0f. "
                     "Touching %s — entries suspended; positions still exit. "
                     "Operator: `rm %s` to resume.",
                     session_delta, -limit_inr, HALT_GAP_DAILY_LOSS_PATH,
                     HALT_GAP_DAILY_LOSS_PATH)
        try:
            HALT_GAP_DAILY_LOSS_PATH.touch()
        except Exception as e:
            log.exception("Failed to write %s: %s", HALT_GAP_DAILY_LOSS_PATH, e)


def fetch_today_quotes(kite, universe: List[str], log: logging.Logger) -> Dict[str, dict]:
    """One kite.quote call for the whole universe → {symbol: {open, ltp, low}}.
    Per-symbol gaps in the response are dropped (the strategy treats a missing
    symbol as 'no signal / can't exit this tick'); a TOTAL failure returns {}
    so the heartbeat can catch a dead token."""
    keys = [f"NSE:{s}" for s in universe]
    try:
        raw = kite.quote(keys) or {}
    except Exception as e:
        log.warning("kite.quote failed for %d symbols: %s", len(keys), e)
        return {}
    out: Dict[str, dict] = {}
    for sym in universe:
        q = raw.get(f"NSE:{sym}")
        if not q:
            continue
        ohlc = q.get("ohlc") or {}
        out[sym] = {"open": ohlc.get("open"), "ltp": q.get("last_price"),
                    "low": ohlc.get("low")}
    return out


def append_intraday_capture(strategy, quotes: Dict[str, dict], today: date,
                            system: str, log: logging.Logger) -> None:
    """Persist this tick's marks for the day's qualifying gappers + open
    positions (issue #63: no 5-min equity data exists — capture forward).
    A handful of symbols at 60s cadence ≈ a few KB/day; a future backtest can
    replay the real intraday path instead of approximating stops by the
    day-low. Append-only TSV, one file per day; best-effort (a write failure
    must never take down the session)."""
    global _capture_fail_streak
    syms = set(strategy.last_scan_candidates) | set(strategy.positions)
    if not syms:
        return
    suffix = "" if system == "baseline" else f"-{system}"
    path = DATA_CACHE / f"buy_on_gap_intraday{suffix}_{today.isoformat()}.tsv"
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        write_header = not path.exists()
        with open(path, "a", encoding="utf-8") as f:
            if write_header:
                f.write("ts\tsymbol\tltp\tday_low\n")
            for sym in sorted(syms):
                q = quotes.get(sym)
                if q and q.get("ltp") is not None:
                    # `low` KEY always exists (fetch_today_quotes sets it, maybe
                    # None) — `q.get('low','')` would render the string "None"
                    # into the column (code-review 2026-07-11).
                    lo = q.get("low")
                    f.write(f"{ts}\t{sym}\t{q['ltp']}\t{'' if lo is None else lo}\n")
        _capture_fail_streak = 0
    except Exception as e:
        # Best-effort by design (a write failure must never take down the
        # session) — but a PERSISTENT failure means the capture's sole
        # deliverable is silently not being collected, so escalate once per
        # streak instead of warning forever (Rule 12, code-review 2026-07-11).
        _capture_fail_streak += 1
        if _capture_fail_streak == CAPTURE_FAIL_ESCALATE_AT:
            log.error("intraday capture has failed %d consecutive ticks "
                      "(%s: %s) — today's forward-capture file is NOT being "
                      "written; check disk/permissions",
                      _capture_fail_streak, path.name, e)
        else:
            log.warning("intraday capture append failed (%s): %s", path.name, e)


_capture_fail_streak = 0
CAPTURE_FAIL_ESCALATE_AT = 10


def _now_hm() -> dtime:
    n = datetime.now()
    return dtime(n.hour, n.minute)


def main():
    parser = argparse.ArgumentParser(description="Buy-on-Gap paper runner (intraday mean reversion)")
    parser.add_argument("--mode", choices=["paper", "signals"], default="paper",
                        help="paper (default): mock fills. signals: JSONL only. "
                             "live is NOT supported for this strategy.")
    parser.add_argument("--system", type=str, default="baseline",
                        help="System tag — suffixes log/state/EOD filenames.")
    parser.add_argument("--max-positions", type=int, default=None,
                        help="N most-oversold gappers to buy (overrides config).")
    parser.add_argument("--total-capital", type=float, default=None,
                        help="Capital deployed (overrides config).")
    parser.add_argument("--gap-std-mult", type=float, default=None,
                        help="k: require gap_ret <= -k*ret_std (overrides "
                             "config). The systemd unit deploys 2.0 — the only "
                             "config that stayed positive out-of-sample.")
    parser.add_argument("--no-trend-filter", action="store_true",
                        help="Disable the 'open > long-MA' refinement. The "
                             "backtest's OOS-survivor config drops it; the "
                             "systemd unit passes this flag.")
    parser.add_argument("--max-daily-loss-inr", type=float, default=30_000.0,
                        help="Session ΔP&L floor (₹). On breach touches "
                             "HALT_BUY_ON_GAP_DAILY_LOSS. 0 disables.")
    parser.add_argument("--kill-net-loss-inr", type=float, default=50_000.0,
                        help="Experiment kill rule: refuse to trade once "
                             "CUMULATIVE net realized <= -this (₹). 0 disables.")
    parser.add_argument("--kill-min-trades", type=int, default=15,
                        help="Experiment kill rule: with at least this many "
                             "closed trades, refuse to trade if win rate < "
                             "--kill-max-win-rate. 0 disables.")
    parser.add_argument("--kill-max-win-rate", type=float, default=0.35,
                        help="Win-rate floor for the --kill-min-trades leg.")
    parser.add_argument("--kite-rate-per-sec", type=float, default=8.0)
    parser.add_argument("--kite-burst", type=int, default=8)
    parser.add_argument("--force", action="store_true",
                        help="Run even on weekends/holidays (testing only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Wire everything up (preflight, strategy, panel) "
                             "but do NOT authenticate or trade — smoke test only.")
    args = parser.parse_args()

    load_dotenv(HERE / ".env")
    os.chdir(HERE)

    today = datetime.now().date()
    log = setup_logging(today, args.system)

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
    log.info("BUY-ON-GAP %s SESSION — %s [system=%s]",
             args.mode.upper(), today, args.system)
    log.info("=" * 60)

    # Experiment kill rule — evaluated on the RAW persisted book, BEFORE the
    # ~200-CSV panel load and Kite auth, so a permanently-killed experiment
    # exits in milliseconds instead of paying the full startup cost every
    # morning. `prior` is reused for restore_strategy below (single read).
    prior = load_prior_state(args.system, log)
    kill_reason = None
    if prior is not None:
        kill_reason = experiment_kill_reason(
            float(prior.get("realized_pnl", 0.0)),
            [p.get("pnl") for p in prior.get("closed_positions", [])],
            net_loss_floor_inr=args.kill_net_loss_inr,
            min_trades=args.kill_min_trades,
            max_win_rate=args.kill_max_win_rate)
    if kill_reason:
        _drop_killed_sentinel(kill_reason, log)
        # Raw open positions may include stale entries restore_strategy would
        # drop (intraday book, crash carry-over) — erring toward the exit-only
        # session in that rare case is harmless.
        if not prior.get("positions") and not args.dry_run:
            log.critical(
                "EXPERIMENT KILL RULE: %s — refusing to trade "
                "(docs/strategy-efficiency-review-2026-07-05.md §2.4). Marker: "
                "%s. Operator: disable buy-on-gap-paper.timer; to grant more "
                "runway, raise the --kill-* thresholds in the unit file.",
                kill_reason, KILLED_SENTINEL_PATH.name)
            return 0
        if args.dry_run:
            log.warning("EXPERIMENT KILL RULE would fire (%s) — continuing "
                        "because --dry-run validates the preflight pipeline.",
                        kill_reason)
        else:
            log.critical(
                "EXPERIMENT KILL RULE: %s — but position(s) are still OPEN; "
                "running this session EXIT-ONLY (entries suppressed) so the "
                "book closes cleanly.", kill_reason)

    # Daily history through yesterday (no auth needed). The strategy adds
    # today's open/LTP/low from quotes at runtime.
    from strategies._eq_data import load_equity_panel, load_universe
    from strategies.buy_on_gap import BuyOnGapStrategy

    universe = load_universe()
    panel = load_equity_panel(universe=universe, source="cache")
    log.info("Loaded panel: %d symbols, %d rows (%s → %s)",
             panel["symbol"].nunique(), len(panel),
             panel["date"].min().date(), panel["date"].max().date())

    kite = None
    if not args.dry_run:
        from core.broker import get_trading_client
        from core.kite_throttle import KiteRateLimiter, throttle_kite
        log.info("Authenticating...")
        kite = get_trading_client(CONFIG_PATH)
        kite = throttle_kite(kite, KiteRateLimiter(
            rate_per_sec=args.kite_rate_per_sec, burst=args.kite_burst))
        profile = kite.profile()
        log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])

    strategy = BuyOnGapStrategy(kite=kite, config_path=CONFIG_PATH, mode=args.mode)
    if args.max_positions is not None:
        strategy.params["max_positions"] = args.max_positions
    if args.total_capital is not None:
        strategy.params["total_capital"] = args.total_capital
    if args.gap_std_mult is not None:
        strategy.params["gap_std_mult"] = args.gap_std_mult
    if args.no_trend_filter:
        strategy.params["use_trend_filter"] = 0
    strategy.set_panel(panel, sorted(panel["symbol"].unique().tolist()))
    # Append today's placeholder row so the shifted features resolve at today's
    # index even though the daily bhavcopy panel only reaches yesterday at 09:20
    # (today's open/LTP/low come from kite.quote, not this row).
    strategy.prepare_live_session(today)
    strategy._ensure_features()
    strategy.set_current_date(today)
    strategy.log_effective_params()
    log.info("Strategy: universe=%d k=%.2fσ trend=%s stop=%.1f%% N=%d cap=₹%.0f",
             len(universe), strategy.params["gap_std_mult"],
             "on" if int(strategy.params["use_trend_filter"]) else "off",
             strategy.params["stop_loss_pct"], int(strategy.params["max_positions"]),
             strategy.params["total_capital"])

    restore_strategy(strategy, prior, today, log)
    strategy._capture_session_baseline()

    if args.dry_run:
        log.info("DRY RUN: preflight + panel + features OK, %d symbols ready. "
                 "Not authenticating or trading. Exiting 0.", len(universe))
        return 0

    now = datetime.now()
    open_ts = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    session_end_ts = now.replace(hour=SESSION_END_AT[0], minute=SESSION_END_AT[1], second=0, microsecond=0)
    hard_stop_ts = now.replace(hour=HARD_STOP[0], minute=HARD_STOP[1], second=0, microsecond=0)
    entry_at = dtime(*ENTRY_AT)
    entry_cutoff = dtime(*ENTRY_CUTOFF)

    if now >= hard_stop_ts:
        log.info("Started after %s — nothing to do today.", hard_stop_ts.strftime("%H:%M"))
        return 0
    if now < open_ts:
        sleep_until(open_ts, log)

    # Don't re-deploy if we already entered today — covers a restart inside the
    # entry window AFTER an early stop-out closed the positions (open-count
    # would read 0, but has_entered_today checks today's closed trades too).
    entered = strategy.has_entered_today(today)
    if entered:
        log.info("Already entered today (%d open, %d closed today) — entry "
                 "window used; running exit-only.", len(strategy.positions),
                 sum(1 for p in strategy.closed_positions
                     if p.entry_dt is not None and p.entry_dt.date() == today))

    halt_state = GapHaltState(kill_rule=bool(kill_reason))
    install_signal_handlers(log)
    heartbeat = HeartbeatTracker(threshold=SILENT_FAIL_THRESHOLD,
                                 sentinel_path=silent_fail_flag_path(args.system), log=log)
    silent_fail = False

    try:
        while datetime.now() < session_end_ts:
            halt_state.refresh(log)
            quotes = fetch_today_quotes(kite, universe, log)
            strategy.set_today_quotes(quotes)
            errored = (len(quotes) == 0)
            if errored:
                log.warning("Empty quote snapshot — likely token/API outage "
                            "(flagging for silent-fail heartbeat)")

            hm = _now_hm()
            # ENTRY: once, inside the window, when not halted and not already in.
            if (not entered and not halt_state.halt_new
                    and entry_at <= hm < entry_cutoff and quotes):
                try:
                    proposals = strategy.scan_and_propose()
                    if proposals:
                        strategy.execute_proposals(proposals)
                        log.info("ENTRY: opened %d position(s)", len(proposals))
                    else:
                        log.info("ENTRY: no qualifying gap-downs today")
                    entered = True
                except Exception as e:
                    errored = True
                    log.exception("entry scan failed: %s", e)
            elif not entered and hm >= entry_cutoff:
                log.info("Past entry cutoff %s with no book — exit-only for the "
                         "rest of the session.", entry_cutoff.strftime("%H:%M"))
                entered = True

            # EXITS: catastrophic stops only intraday (force_close stays False).
            if not halt_state.halt_all:
                try:
                    exits = strategy.check_and_rehedge()
                    if exits:
                        strategy.execute_proposals(exits)
                        log.info("Catastrophic stop: exited %d position(s)", len(exits))
                except Exception as e:
                    errored = True
                    log.exception("check_and_rehedge failed: %s", e)

            if quotes:
                append_intraday_capture(strategy, quotes, today, args.system, log)

            n_ran = 0 if halt_state.halt_all else 1
            if heartbeat.record_tick(n_ran=n_ran, n_errored=1 if errored else 0):
                silent_fail = True
                break
            check_daily_loss_limit(strategy, args.max_daily_loss_inr, log)
            try:
                write_state_file(strategy, args.system, log, archive=False)
            except Exception as e:
                log.exception("Intraday persist failed: %s — continuing", e)
            remaining = (session_end_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        end_of_session(strategy, kite, universe, today, args, log, silent_fail)
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        log.info("Interrupted — flattening, persisting, exiting.")
        end_of_session(strategy, kite, universe, today, args, log, True)
        return 130

    if silent_fail:
        return 1
    log.info("Session complete. Exiting cleanly.")
    return 0


def end_of_session(strategy, kite, universe, today, args, log, silent_fail):
    """Same-day-close exit: flatten EVERY open position at the last price, then
    persist state + write the EOD sidecar. A silent-fail teardown still tries to
    flatten (best effort) so the book doesn't sit open overnight."""
    try:
        quotes = fetch_today_quotes(kite, universe, log)
        strategy.set_today_quotes(quotes)
        strategy._force_close = True
        exits = strategy.check_and_rehedge()
        if exits:
            strategy.execute_proposals(exits)
            log.info("Session-close flatten: exited %d position(s)", len(exits))
        if strategy.positions:
            log.critical("Session-close flatten INCOMPLETE: %d position(s) still "
                         "open (no quote): %s — review manually.",
                         len(strategy.positions), list(strategy.positions))
    except Exception as e:
        log.exception("end_of_session flatten failed: %s", e)
    write_state_file(strategy, args.system, log)
    write_eod_sidecar(strategy, today, log, args.system)
    if silent_fail:
        log.critical("Silent-fail teardown complete — exiting non-zero so "
                     "notify-failure@ alerts the operator.")


if __name__ == "__main__":
    sys.exit(main())
