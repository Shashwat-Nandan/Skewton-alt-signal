#!/usr/bin/env python3
"""
Pair-Trading Paper Runner
=========================
Unattended intraday loop for the pair-trading strategy. Runs alongside the
Taleb-Karpathy paper runner (runners/run_paper.py) on its own systemd timer.

  - Refuses to run on weekends or dates in holidays.csv (override with --force)
  - Authenticates via TOTP (kite_auth.KiteAuthManager)
  - Loads top-N rows from data_cache/pair_candidates.csv and instantiates one
    PairTradingStrategy per pair
  - Restores any prior-session open positions from
    data_cache/pair_paper_state_<system>.json (orphans — pairs no longer in
    today's candidates but with an open position — are loaded too)
  - Blocks until 09:15 IST, ticks every 60s until 15:25 IST
  - Persists strategy state to the state file (no EOD flatten by default;
    open positions exit only on strategy triggers — mean-revert, stop-z,
    max-hold — or on the contract's last trading day)
  - Writes data_cache/pair_paper_eod_<date>.json with per-pair
    generate_eod_report() for the verifier and dashboard
  - Per-day logfile under logs/paper-pairs-YYYY-MM-DD.log

Operations:
  --force-flatten-on-exit: emergency hatch to revert to old behaviour for
    a single session (e.g. before a maintenance window or system change).

Assumes the process sees wall-clock IST (systemd sets TZ=Asia/Kolkata).
"""
from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import pandas as pd
from dotenv import load_dotenv

from core._state_backup import archive_state_backup, assert_no_orphan_backups

# Shared runner scaffolding (audit 2.1). Re-exported below so the many
# importers of run_paper_pairs (tests, backtests, dashboard, the arbitrage
# runner) keep working unchanged. Behaviour is byte-identical — these were
# extracted verbatim from this file.
from core.runner_common import (  # noqa: F401  (re-exported)
    HOLIDAYS_PATH,
    HALT_ALL_PATH,
    HALT_NEW_ENTRIES_PATH,
    HARD_STOP,
    HOLIDAY_HORIZON_DAYS,
    HOLIDAYS_PER_YEAR_FLOOR,
    MARKET_OPEN,
    SESSION_END_AT,
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
    sleep_until,
)

# classify_pair_candidates + its quality/leg constants live in screen_pairs
# (audit 3.8) so backtests/sweeps import the screen logic without importing
# this live runner. Re-exported here for select_pairs, the dashboard, and
# back-compat with existing importers/tests.
from core.screen_pairs import (  # noqa: F401  (re-exported)
    LEG_CONCENTRATION_CAP,
    QUALITY_MAX_HALFLIFE,
    QUALITY_MAX_PVALUE,
    QUALITY_MIN_CORR,
    classify_pair_candidates,
)


HERE = Path(__file__).resolve().parent.parent
CONFIG_PATH = str(HERE / "config.ini")
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
CANDIDATES_PATH = DATA_CACHE / "pair_candidates.csv"

# HALT_ALL_PATH / HALT_NEW_ENTRIES_PATH are imported from runner_common
# (shared kill switches; every runner shares data_cache, so an operator flag
# halts ALL of them at once — that is the intended behaviour of a manual kill
# switch). The runner-set daily-loss breaker is the opposite: it is PER-RUNNER
# (see halt_daily_loss_path). Two pair runners share data_cache, so a single
# shared flag would let one runner's loss breach freeze the other's entries —
# the same isolation runners/run_paper_arbitrage.py gets from HALT_ARBITRAGE_DAILY_LOSS.
# HALT_DAILY_LOSS_PATH is the CANONICAL flag kept by the persistent (LIVE)
# runner so its operator alert (deploy/pair-halt-alert.path) and `rm` runbook
# stay valid unchanged; other --system tags get a suffixed flag.
HALT_DAILY_LOSS_PATH = DATA_CACHE / "HALT_DAILY_LOSS"


def halt_daily_loss_path(system: str) -> Path:
    """Per-runner daily-loss breaker flag for a given --system tag.

    The persistent runner (the live book) keeps the canonical HALT_DAILY_LOSS
    so its Telegram alert and runbook are untouched; every other runner —
    e.g. the baseline paper runner — gets HALT_DAILY_LOSS_<system>, so its own
    breach touches only its own flag and cannot halt a co-running pair runner.
    """
    if system == "persistent":
        return HALT_DAILY_LOSS_PATH
    return DATA_CACHE / f"HALT_DAILY_LOSS_{system}"

# 3.7 / M-6: how often to re-reconcile against the broker DURING a live
# session. The startup gate alone leaves a drift window from one start to the
# next (a manual square-off, an un-captured partial, an expiry) — re-check
# hourly so drift is caught within the session, not the next morning.
RECONCILE_INTERVAL_S = 3600


def ensure_pair_config(orig_path: str, cli_max_leg_notional: float, log: logging.Logger) -> str:
    """PairTradingStrategy.__init__ refuses to construct in paper mode without
    a [pair_trading] section that defines max_leg_notional. config.ini is
    gitignored (the operator's local copy of credentials), so a fresh VPS may
    not yet have that section.

    Read the operator's config; if [pair_trading] is missing or has no
    max_leg_notional, write a derived copy under data_cache/ with the section
    backfilled from CLI defaults, and return its path. The runner's CLI flags
    override per-instance attributes after construction anyway, so this only
    needs to satisfy the constructor's pre-flight check."""
    cfg = configparser.ConfigParser()
    cfg.read(orig_path)
    needs_inject = (
        not cfg.has_section("pair_trading")
        or not cfg.get("pair_trading", "max_leg_notional", fallback="").strip()
    )
    if not needs_inject:
        return orig_path

    if not cfg.has_section("pair_trading"):
        cfg.add_section("pair_trading")
    cfg.set("pair_trading", "max_leg_notional", str(cli_max_leg_notional))

    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    derived = DATA_CACHE / ".pair_paper_config.ini"
    with derived.open("w") as f:
        cfg.write(f)
    log.info("Derived config (operator config + [pair_trading] backfill): %s", derived)
    return str(derived)

# MARKET_OPEN / SESSION_END_AT / HARD_STOP / TICK_SECONDS imported from
# runner_common. The pair runner does NOT flatten open positions at
# SESSION_END_AT — they survive to the next session via the state file; the
# only EOD exits are --force-flatten-on-exit and a leg expiring today.

# QUALITY_MIN_CORR / QUALITY_MAX_HALFLIFE / QUALITY_MAX_PVALUE /
# LEG_CONCENTRATION_CAP are imported from screen_pairs (audit 3.8).


def load_cross_runner_leg_counts(own_state_path: Optional[Path],
                                  data_cache: Path = DATA_CACHE
                                  ) -> dict[str, int]:
    # H17: read sibling pair-runner state files and count each symbol's
    # appearances across pairs that currently hold an open position. The
    # caller's own state file is excluded so the per-runner selection walk
    # doesn't double-count its own held pairs (which are already preserved
    # via restore_matching_strategies + orphans).
    counts: dict[str, int] = {}
    if not data_cache.exists():
        return counts
    own = own_state_path.resolve() if own_state_path else None
    for jp in data_cache.glob("pair_paper_state_*.json"):
        try:
            if own and jp.resolve() == own:
                continue
        except OSError:
            continue
        try:
            import json as _json
            blob = _json.loads(jp.read_text())
        except Exception:
            continue
        for pair in blob.get("pairs", []) or []:
            state = pair.get("state") or {}
            if not state.get("legs"):
                continue
            sym_pair = pair.get("pair") or []
            for sym in sym_pair[:2]:
                if sym:
                    counts[sym] = counts.get(sym, 0) + 1
    return counts

# --max-csv-age-days defaults. Paper/signals can tolerate a slightly stale
# weekly screen because nothing real moves; live cannot — 6-day-old hedge
# ratios are an implicit directional exposure on each leg (H10).
DEFAULT_CSV_AGE_LIVE = 1.0
DEFAULT_CSV_AGE_PAPER = 7.0


def resolve_max_csv_age_days(mode: str,
                              value: Optional[float]) -> float:
    """H10: explicit operator value wins; otherwise live tolerates only
    fresh hedge ratios and paper/signals keep the legacy 7-day window."""
    if value is not None:
        return float(value)
    return DEFAULT_CSV_AGE_LIVE if mode == "live" else DEFAULT_CSV_AGE_PAPER

# Silent-fail heartbeat. If every strategy has at least one operation
# (scan or rehedge) raise for this many consecutive ticks, the runner
# touches a sentinel file, logs CRITICAL, and exits non-zero so
# notify-failure@%n alerts the operator. See TickOutcome.errored for why
# the per-strategy signal is "any op raised" rather than "every op
# raised". Real failure modes: token expired mid-session, kite API
# outage, accidental network isolation. Default 3 ticks ≈ 3 min of
# total silence.
SILENT_FAIL_FLAG_TEMPLATE = "pair_paper_silent_fail_{system}.flag"


def silent_fail_flag_path(system: str) -> Path:
    return DATA_CACHE / SILENT_FAIL_FLAG_TEMPLATE.format(system=system)


# load_holidays, is_trading_day, assert_timezone_ist, assert_disk_space_ok,
# assert_holiday_data_fresh (+ its HORIZON/FLOOR constants) are imported
# from runner_common (audit 2.1).


def setup_logging(today: date, system: str = "baseline") -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Baseline preserves the original filename; non-baseline systems suffix
    # the log so two parallel runners don't clobber each other.
    suffix = "" if system == "baseline" else f"-{system}"
    logfile = LOG_DIR / f"paper-pairs{suffix}-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(logfile),
        ],
        force=True,
    )
    return logging.getLogger("run_paper_pairs")


def select_pairs(top: int, log: logging.Logger,
                 candidates_path: Path = CANDIDATES_PATH,
                 max_age_days: float = 7.0,
                 *,
                 max_pvalue: float | None = None,
                 seed_leg_count: dict[str, int] | None = None) -> pd.DataFrame:
    """Pick up to `top` pairs from pair_candidates.csv via four layered passes:

      1) Tradeable hedge ratio: |β| ∈ [HEDGE_RATIO_MIN, HEDGE_RATIO_MAX].
      2) Quality floor: corr ≥ QUALITY_MIN_CORR AND half_life ≤
         QUALITY_MAX_HALFLIFE AND p ≤ QUALITY_MAX_PVALUE (the p ceiling is
         overridable via `max_pvalue` — the persistent system passes 0.05).
         Drops candidates that are statistically cointegrated but
         economically untradeable.
      3) Composite select_score = mean of percentile ranks over (p, half-life,
         spread_vol, correlation). The screener's `rank_score` weights only
         p/half-life/vol — adding correlation here rewards spreads whose legs
         actually co-move, breaking ties in favour of mean-reversion confidence
         instead of pure bps-per-reversion.
      4) Walk in select_score order and admit pairs until `top` is reached,
         skipping any that would push a symbol past LEG_CONCENTRATION_CAP
         appearances across the book.

    `max_age_days` is a safety check: if the CSV's mtime is older than this
    many days, refuse to load it. Protects against a failed weekly screen
    leaving the runner trading on stale hedge ratios. Set to 0 to disable.
    """
    if not candidates_path.exists():
        raise FileNotFoundError(
            f"{candidates_path} not found — run `python -m core.screen_pairs` first."
        )

    df_raw = pd.read_csv(candidates_path)
    if max_age_days > 0:
        # M-S2: prefer the `last_data_date` column over filesystem mtime.
        # mtime can be fresh (cp/touch by an unrelated process) while the
        # underlying screen data is stale; last_data_date is written by
        # core/screen_pairs.py and reflects the actual data window's end.
        # Fall back to mtime if the column is missing (legacy CSVs).
        data_dates = (
            pd.to_datetime(df_raw["last_data_date"], errors="coerce").dropna()
            if "last_data_date" in df_raw.columns
            else pd.Series(dtype="datetime64[ns]")
        )
        if not data_dates.empty:
            last_data = data_dates.max()
            age_days = (pd.Timestamp(datetime.now()).normalize()
                        - last_data.normalize()).days
            if age_days > max_age_days:
                raise RuntimeError(
                    f"{candidates_path}: last_data_date={last_data.date()} is "
                    f"{age_days}d old, exceeds max_age_days={max_age_days}. "
                    "Re-run `python -m core.screen_pairs` to refresh, or pass "
                    "--max-csv-age-days 0 to bypass (not recommended in paper/live)."
                )
            log.info("Candidates last_data_date: %s (%dd old)",
                     last_data.date(), age_days)
        else:
            mtime = datetime.fromtimestamp(candidates_path.stat().st_mtime)
            age_days_f = (datetime.now() - mtime).total_seconds() / 86400.0
            if age_days_f > max_age_days:
                raise RuntimeError(
                    f"{candidates_path} is {age_days_f:.1f}d old (mtime "
                    f"{mtime:%Y-%m-%d %H:%M}), exceeds max_age_days={max_age_days}. "
                    "The weekly screen has not run recently — refusing to trade on "
                    "stale hedge ratios. Run core/screen_pairs.py to refresh, or pass "
                    "--max-csv-age-days 0 to bypass (not recommended in paper/live)."
                )
            log.warning(
                "Candidates CSV missing last_data_date column — using mtime "
                "(%.1fd). Re-screen to populate the column (M-S2 fallback).",
                age_days_f,
            )

    annotated = classify_pair_candidates(
        df_raw, top, log,
        max_pvalue=max_pvalue,
        seed_leg_count=seed_leg_count,
    )
    picks = (
        annotated[annotated["processing_rank"].notna()]
        .sort_values("processing_rank")
        .drop(columns=["processing_rank", "skip_reason", "select_score"])
        .reset_index(drop=True)
    )

    if picks.empty:
        raise RuntimeError(
            "No tradeable pairs after β + quality + concentration filters")
    if len(picks) < top:
        log.warning("Selected only %d of %d requested pairs "
                    "(quality/concentration filters exhausted)",
                    len(picks), top)

    return picks


def build_strategies(
    pairs: pd.DataFrame, args, kite, config_path: str, log: logging.Logger,
    *, nfo_instruments: Optional[List[dict]] = None,
    kite_refresh=None,
    book_notional_fn=None,
    max_book_notional: float = 0.0,
    spread_panel: Optional[pd.DataFrame] = None,
    signal_publisher=None,
):
    from strategies.pair_trading import PairTradingStrategy

    instances: List[PairTradingStrategy] = []
    for _, row in pairs.iterrows():
        a, b, beta = row["symbol_a"], row["symbol_b"], float(row["hedge_ratio"])
        try:
            s = PairTradingStrategy(
                kite=kite,
                config_path=config_path,
                mode=args.mode,
                symbol_a=a,
                symbol_b=b,
                hedge_ratio=beta,
                nfo_instruments=nfo_instruments,
                kite_refresh=kite_refresh,
                book_notional_fn=book_notional_fn,
                spread_panel=spread_panel,
                signal_publisher=signal_publisher,
            )
            if max_book_notional > 0:
                s.max_book_notional = max_book_notional
            s.signal_system_tag = args.system
        except Exception as e:
            log.exception("Could not init %s/%s: %s — skipping", a, b, e)
            continue

        # Override per-instance tunables from CLI args (post-init mutation,
        # same pattern run_manager.py uses for max_leg_notional).
        s.entry_z = args.entry_z
        s.exit_z = args.exit_z
        s.stop_z = args.stop_z
        s.lookback_days = args.lookback_days
        s.max_holding_days = args.max_holding_days
        s.lots_per_leg = args.lots_per_leg
        s.max_leg_notional = args.max_leg_notional
        s.stop_cooldown_minutes = args.stop_cooldown_minutes

        log.info(
            "Init %s/%s β=%.4f entry_z=%.2f exit_z=%.2f stop_z=%.2f "
            "lookback=%dd max_hold=%dd lots=%d max_leg_notional=₹%.0f "
            "stop_cooldown=%dmin spread_history_seed=%d",
            a, b, beta, s.entry_z, s.exit_z, s.stop_z,
            s.lookback_days, s.max_holding_days, s.lots_per_leg,
            s.max_leg_notional, s.stop_cooldown_minutes,
            len(s._spread_history),
        )
        # After all CLI overrides — this line, not config.ini, is the
        # drift ground truth (audit 2.3).
        s.log_effective_params()
        instances.append(s)

    if not instances:
        raise RuntimeError("All pair strategies failed to initialise; nothing to run")
    return instances


class _HaltState:
    # Tracks halt-flag state across ticks so transitions are logged once.
    # Kill switch hierarchy: HALT_ALL implies HALT_NEW_ENTRIES.
    def __init__(self, daily_loss_path: Path = HALT_DAILY_LOSS_PATH):
        # Per-runner daily-loss flag (see halt_daily_loss_path). Defaults to the
        # canonical flag so any caller/test that omits it keeps prior behaviour.
        self.daily_loss_path = daily_loss_path
        self.halt_all = False
        self.halt_new = False

    def refresh(self, log: logging.Logger) -> None:
        prev_all, prev_new = self.halt_all, self.halt_new
        self.halt_all = HALT_ALL_PATH.exists()
        halt_loss = self.daily_loss_path.exists()
        self.halt_new = (self.halt_all
                         or HALT_NEW_ENTRIES_PATH.exists()
                         or halt_loss)
        if self.halt_all and not prev_all:
            log.critical("KILL SWITCH: HALT_ALL flag present (%s) — all "
                         "entries AND exits suspended. Positions frozen "
                         "until flag is removed.", HALT_ALL_PATH)
        elif prev_all and not self.halt_all:
            log.warning("HALT_ALL flag cleared — resuming normal tick loop")
        if self.halt_new and not prev_new and not self.halt_all:
            sources = []
            if HALT_NEW_ENTRIES_PATH.exists():
                sources.append("HALT_NEW_ENTRIES")
            if halt_loss:
                sources.append("HALT_DAILY_LOSS")
            log.warning("Entries suspended (flags: %s); exits and rehedges "
                        "continue normally", "+".join(sources))
        elif prev_new and not self.halt_new:
            log.warning("Entry-halt flags cleared — resuming entries")


class TickOutcome(NamedTuple):
    """What happened in one strategy's tick.

    attempted_execution — True iff `execute_proposals` was called. Drives
        the per-attempt state persist (H1). True does NOT guarantee a
        broker-side fill: execute_proposals can return normally without
        mutating state in signals-mode, when every leg comes back
        non-COMPLETE, or when a partial entry batch is reversed to FLAT.
        Persisting in those no-op cases is harmless.

    errored — True iff any operation this tick raised. Drives the
        silent-fail heartbeat (H3): three consecutive ticks where every
        strategy reports errored = systemic failure (token expired,
        kite API down, network isolation), and the runner escalates.

        Why "any" rather than "every op raised": scan_and_propose and
        check_and_rehedge each short-circuit before touching kite
        depending on whether the pair is FLAT (scan returns [] when
        held; rehedge returns [] when flat). Under a dead token, one
        op raises while the *other* returns trivially without exercising
        the failing dependency — counting only "every op raised" would
        miss the systemic failure for both held and flat pairs. The
        n_errored == n_ran heartbeat-level threshold still requires
        every strategy to be flagged, so isolated transient errors on
        a single pair don't escalate.

        halt_all → errored=False (nothing ran, no signal).
    """
    attempted_execution: bool
    errored: bool


def tick_one(strategy, log: logging.Logger,
             halt_all: bool = False,
             halt_new_entries: bool = False) -> TickOutcome:
    """One pair's iteration. Failures logged but do not kill the loop.

    Returns a TickOutcome with two booleans the caller uses to drive
    per-attempt state persist (H1) and silent-fail heartbeat (H3) — see
    `TickOutcome` docstring for the per-field contract.

    HALT_ALL skips both entries and exit/rehedge checks (book frozen).
    HALT_NEW_ENTRIES skips only entries; exits/rehedges continue."""
    pair_label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    if halt_all:
        return TickOutcome(attempted_execution=False, errored=False)

    attempted_execution = False
    error_count = 0

    if not halt_new_entries:
        try:
            proposals = strategy.scan_and_propose()
            if proposals:
                strategy.execute_proposals(proposals)
                attempted_execution = True
        except Exception as e:
            error_count += 1
            log.exception("[%s] scan_and_propose failed: %s", pair_label, e)

    try:
        rehedge = strategy.check_and_rehedge()
        if rehedge:
            strategy.execute_proposals(rehedge)
            attempted_execution = True
    except Exception as e:
        error_count += 1
        log.exception("[%s] check_and_rehedge failed: %s", pair_label, e)

    return TickOutcome(attempted_execution=attempted_execution,
                       errored=error_count > 0)


# install_signal_handlers and HeartbeatTracker are imported from
# runner_common (audit 2.1).


def check_daily_loss_limit(strategies, limit_inr: float,
                            log: logging.Logger,
                            halt_path: Path = HALT_DAILY_LOSS_PATH) -> None:
    # Aggregates session ΔP&L across all in-flight strategies (matched +
    # orphans). On breach, touches this runner's own daily-loss flag
    # (halt_path, per halt_daily_loss_path) — caught by _HaltState next tick →
    # entries suspended, exits continue, persists across restart so the
    # operator must explicitly acknowledge before resuming.
    if limit_inr <= 0:
        return
    if halt_path.exists():
        return
    session_delta = 0.0
    for s in strategies:
        try:
            session_delta += (
                (s.state.realized_pnl + s.state.unrealized_pnl)
                - (s._session_start_realized + s._session_start_unrealized)
            )
        except AttributeError:
            # Defensively skip strategies missing baseline (shouldn't
            # happen post-init, but a half-built orphan could trip this).
            continue
    if session_delta <= -limit_inr:
        log.critical(
            "DAILY LOSS LIMIT BREACHED: session ΔP&L = ₹%.0f vs limit "
            "₹%.0f. Touching %s — entries suspended; existing positions "
            "continue to exit. Operator: `rm %s` to acknowledge and "
            "resume entries.",
            session_delta, -limit_inr, halt_path,
            halt_path,
        )
        try:
            halt_path.touch()
        except Exception as e:
            log.exception("Failed to write %s: %s",
                          halt_path, e)


def flatten_one(strategy, log: logging.Logger, reason: str = "EOD_CLOSE"):
    """Force-close any open position using the strategy's own exit-builder.
    Mirrors research/backtest_pairs.py's force-close path so behaviour is consistent.
    Used only by the operator-forced flatten or by the expiry-day flatten;
    the default session end persists state instead."""
    pair_label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    if strategy.state.position == "FLAT" or not strategy.state.legs:
        return
    try:
        spread, prices = strategy._observe_spread()
        if not prices:
            log.warning("[%s] flatten: could not fetch quotes; "
                        "leaving position open", pair_label)
            return
        strategy._update_unrealized(prices)
        close_props = strategy._build_exit_proposals(reason, 0.0, prices)
        if close_props:
            log.info("[%s] flattening %d leg(s) (%s)", pair_label, len(close_props), reason)
            strategy.execute_proposals(close_props)
    except Exception as e:
        log.exception("[%s] flatten failed: %s", pair_label, e)


# ════════════════════════════════════════════════════════════════════════
# CROSS-SESSION STATE PERSISTENCE
# ════════════════════════════════════════════════════════════════════════
# The paper runner no longer flattens at session end (2026-05-19). Open
# positions are serialised to data_cache/pair_paper_state_<system>.json and
# restored at the start of the next session. Strategy-defined exit triggers
# (mean-revert, stop-z, max-hold) are the only path to flat — plus the
# expiry-day force-flatten below.

STATE_FILE_TEMPLATE = "pair_paper_state_{system}.json"
LOCK_FILE_TEMPLATE = ".pair_paper_{system}.lock"


def state_file_path(system: str) -> Path:
    return DATA_CACHE / STATE_FILE_TEMPLATE.format(system=system)


def acquire_runner_lock(system: str, log: logging.Logger) -> int:
    """H9: refuse to start if another pair runner already holds the lock
    for this --system tag (two processes sharing a state file silently
    clobber each other's writes). Thin wrapper over runner_common's
    generic acquire_lock with the pair-specific lock path (audit 2.1)."""
    return acquire_lock(
        DATA_CACHE / LOCK_FILE_TEMPLATE.format(system=system), log,
        label=f"pair-paper runner (--system={system})",
    )


def load_prior_state(system: str, log: logging.Logger) -> Dict[str, Dict]:
    """Return {pair_key → blob} from the prior session's state file, or {}
    if no file exists. pair_key is 'A/B' (matches strategy serialise format)."""
    path = state_file_path(system)
    if not path.exists() or path.stat().st_size == 0:
        # Refuse to silently start fresh if backups exist — broker may
        # still hold positions from the last backup.
        assert_no_orphan_backups(path, log)
        log.info("No prior state file at %s — starting fresh.", path)
        return {}
    try:
        payload = json.loads(path.read_text())
    except Exception as e:
        log.exception("Failed to parse state file %s: %s.", path, e)
        assert_no_orphan_backups(path, log)
        log.info("No backups present — starting fresh.")
        return {}
    out: Dict[str, Dict] = {}
    for blob in payload.get("pairs", []):
        pair = blob.get("pair")
        if isinstance(pair, list) and len(pair) == 2:
            out[f"{pair[0]}/{pair[1]}"] = blob
    log.info("Loaded prior state for %d pair(s) from %s (updated_at %s)",
             len(out), path.name, payload.get("updated_at", "?"))
    return out


# A post-STOP re-arm latch must outlive deselection, not just the session.
# `strategies` only ever contains today's picks plus orphans, and
# build_orphan_strategies skips prior-state pairs that are FLAT ("closed
# before this session — nothing to manage"). A pair that stopped out and is
# therefore FLAT is neither, so if it drops out of today's top-N its whole
# blob — including stop_rearm_pending — was erased by the next tick's write,
# and the pair re-entered its diverged spread the day it was re-admitted.
# Deselection is routine: select_pairs is seeded with the sibling runner's
# open legs, so the leg-concentration cap re-orders admits day to day.
#
# Bounded so a latch for a pair that never returns cannot accumulate forever.
LATCH_CARRY_MAX_AGE_DAYS = 30


def latch_carry_forward_blobs(prior_state: Dict[str, Dict],
                              represented_keys: set,
                              log: logging.Logger,
                              *, now: Optional[datetime] = None,
                              max_age_days: int = LATCH_CARRY_MAX_AGE_DAYS,
                              ) -> List[Dict]:
    """Prior-state blobs to re-persist verbatim because they still carry a
    pending post-STOP re-arm latch, even though no strategy represents them
    this session.

    Only FLAT, latch-pending pairs qualify: open positions are already
    carried as orphans, and a pair with no latch has nothing to preserve.
    """
    now = now or datetime.now()
    carried: List[Dict] = []
    for key, blob in sorted(prior_state.items()):
        if key in represented_keys:
            continue
        state_blob = blob.get("state") or {}
        if state_blob.get("position", "FLAT") != "FLAT":
            continue          # open → build_orphan_strategies owns it
        if not state_blob.get("stop_rearm_pending"):
            continue          # nothing worth preserving
        last_exit = state_blob.get("last_exit_time")
        if last_exit:
            try:
                age_days = (now - datetime.fromisoformat(last_exit)).days
            except (TypeError, ValueError):
                age_days = 0
            if age_days > max_age_days:
                log.info(
                    "[%s] dropping post-STOP re-arm latch: stopped %dd ago "
                    "(> %dd). The pair has not been selected since; a latch "
                    "this old is stale state, not live risk.",
                    key, age_days, max_age_days,
                )
                continue
        carried.append(blob)
        log.info(
            "[%s] not selected today but post-STOP re-arm is still pending — "
            "carrying its state forward so re-admission cannot silently "
            "re-enter the spread that stopped it.", key,
        )
    return carried


def write_state_file(strategies, system: str, log: logging.Logger,
                     archive: bool = True, mode: str = "paper",
                     carry_forward: Optional[List[Dict]] = None):
    """Atomically and durably persist current strategy state. Each strategy
    emits its own serialize_state() blob; runner adds a system/timestamp
    header. Crash/power-loss safety is runner_common.durable_write_text
    (tmp → fsync → atomic rename → dir fsync; rationale documented there).

    archive=False skips the timestamped backup + log line — used by the
    intraday tick-loop persist, which fires every minute and would
    otherwise churn through the 30-slot backup ring in half an hour and
    drown the journal in "State persisted" lines.
    """
    path = state_file_path(system)
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    payload = {
        "system": system,
        # live/paper flag for read-only consumers (dashboard positions API).
        # Single source of truth is the runner's --mode; the API renders this
        # rather than a global setting so paper systems never read as live.
        "mode": "live" if mode == "live" else "paper",
        "updated_at": datetime.now().isoformat(),
        "pairs": [],
    }
    for s in strategies:
        try:
            payload["pairs"].append(s.serialize_state())
        except Exception as e:
            log.exception("serialize_state failed for %s/%s: %s",
                          s.symbol_a, s.symbol_b, e)
    # Latch-only blobs for pairs no strategy represents this session. They
    # carry no legs, so they contribute nothing to the notional or
    # leg-concentration readers of this file — only the re-arm flag.
    payload["pairs"].extend(carry_forward or [])
    # durable_write_text owns the tmp→fsync→replace→dir-fsync steps
    # (extracted to runner_common in the PR #96 review; identical
    # behaviour, one shared copy). Default mode = 0o666 & ~umask, so
    # prod (UMask=0027 → 0o640) and dev (umask 0022 → 0o644) behaviour
    # is unchanged.
    durable_write_text(path, json.dumps(payload, default=str, indent=2))
    if archive:
        log.info("State persisted: %s (%d pairs)", path.name, len(payload["pairs"]))
        archive_state_backup(path, log)


def restore_matching_strategies(
    strategies, prior_state: Dict[str, Dict], log: logging.Logger,
) -> set:
    """For each built strategy: if prior_state has a blob for its pair,
    restore it. For an OPEN saved position, lock hedge_ratio to the saved β
    (the trade was entered at that ratio) and re-seed spread history at that
    β. Returns the set of pair keys that were restored.
    """
    matched: set = set()
    for s in strategies:
        key = f"{s.symbol_a}/{s.symbol_b}"
        blob = prior_state.get(key)
        if not blob:
            continue
        saved_position = blob.get("state", {}).get("position", "FLAT")
        try:
            if saved_position != "FLAT":
                saved_beta = float(blob["hedge_ratio"])
                if abs(saved_beta - s.hedge_ratio) > 1e-9:
                    log.info(
                        "[%s] screener β=%.4f differs from saved entry "
                        "β=%.4f; honouring saved β for held position",
                        key, s.hedge_ratio, saved_beta,
                    )
                    s.hedge_ratio = saved_beta
                    s._spread_history = []
                    s._seed_spread_history()
            s.restore_state(blob)
            matched.add(key)
            log.info(
                "[%s] restored: position=%s entry_z=%.2f legs=%d "
                "cum_realized=₹%.0f closed_trades=%d",
                key, s.state.position, s.state.entry_z,
                len(s.state.legs), s.state.realized_pnl,
                len(s.state.closed_trades),
            )
        except Exception as e:
            log.exception(
                "[%s] restore_state failed: %s — keeping fresh strategy "
                "(saved position will be ABANDONED; manual review)", key, e,
            )
    return matched


def build_orphan_strategies(
    prior_state: Dict[str, Dict], matched_keys: set,
    args, kite, config_path: str, log: logging.Logger,
    *, nfo_instruments: Optional[List[dict]] = None,
    kite_refresh=None,
    book_notional_fn=None,
    max_book_notional: float = 0.0,
    spread_panel: Optional[pd.DataFrame] = None,
    signal_publisher=None,
):
    """Build strategies for prior-state pairs with an OPEN position that are
    NOT in today's candidate list. Without this, a held position would simply
    be orphaned when the screener drops the pair — no one would manage it to
    exit.

    Orphans use the saved hedge_ratio and the saved state; they don't get
    new entries because their position is already open."""
    from strategies.pair_trading import PairTradingStrategy
    orphans = []
    for key, blob in prior_state.items():
        if key in matched_keys:
            continue
        state_blob = blob.get("state", {})
        if state_blob.get("position", "FLAT") == "FLAT":
            # Closed before this session — nothing to manage.
            continue
        try:
            pair = blob["pair"]
            sa, sb = pair[0], pair[1]
            saved_beta = float(blob["hedge_ratio"])
            s = PairTradingStrategy(
                kite=kite, config_path=config_path, mode=args.mode,
                symbol_a=sa, symbol_b=sb, hedge_ratio=saved_beta,
                nfo_instruments=nfo_instruments,
                kite_refresh=kite_refresh,
                book_notional_fn=book_notional_fn,
                spread_panel=spread_panel,
                signal_publisher=signal_publisher,
            )
            if max_book_notional > 0:
                s.max_book_notional = max_book_notional
            s.signal_system_tag = args.system
            s.entry_z = args.entry_z
            s.exit_z = args.exit_z
            s.stop_z = args.stop_z
            s.lookback_days = args.lookback_days
            s.max_holding_days = args.max_holding_days
            s.lots_per_leg = args.lots_per_leg
            s.max_leg_notional = args.max_leg_notional
            s.stop_cooldown_minutes = args.stop_cooldown_minutes
            s.restore_state(blob)
            s.log_effective_params()
            orphans.append(s)
            log.info(
                "[%s] ORPHAN — held position is not in today's candidates; "
                "loaded for management-to-exit (position=%s legs=%d β=%.4f)",
                key, s.state.position, len(s.state.legs), saved_beta,
            )
        except Exception as e:
            log.exception(
                "Orphan strategy for %s could not be built: %s — "
                "position ABANDONED (manual review)", key, e,
            )
    return orphans


def reconcile_with_broker(strategies, kite, log: logging.Logger) -> None:
    # Live-mode safety: state-file is the runner's view of open positions;
    # kite.positions() is the broker's truth. They must agree before the
    # tick loop touches anything. Paper-mode runs skip silently — there is
    # no real broker position to reconcile against.
    live_strategies = [s for s in strategies if getattr(s, "mode", "paper") == "live"]
    if not live_strategies:
        log.info("Broker reconciliation skipped (no live strategies)")
        return

    try:
        broker_positions = kite.positions().get("net", []) or []
    except Exception as e:
        log.exception("kite.positions() failed: %s", e)
        raise RuntimeError(
            f"Broker reconciliation could not run: kite.positions() raised "
            f"{e!r}. Refusing to start — broker state is unknown."
        )

    # Index NFO positions by tradingsymbol → (signed shares, avg price).
    # Kite's `quantity` is already signed: positive long, negative short.
    # `average_price` is the broker's truth for the leg's cost basis.
    broker_qty: Dict[str, int] = {}
    broker_avg_price: Dict[str, float] = {}
    for pos in broker_positions:
        if pos.get("exchange") != "NFO":
            continue
        ts = pos.get("tradingsymbol", "")
        qty = int(pos.get("quantity", 0))
        if not ts:
            continue
        broker_qty[ts] = broker_qty.get(ts, 0) + qty
        # If the same tradingsymbol appears twice, last write wins —
        # acceptable because Kite collapses to a single net row per
        # tradingsymbol in the "net" bucket.
        if pos.get("average_price") not in (None, 0, 0.0):
            broker_avg_price[ts] = float(pos.get("average_price"))

    # Aggregate expected shares per tradingsymbol across ALL live
    # strategies before comparing. Kite's "net" bucket reports ONE net row
    # per contract, so two pairs holding offsetting legs in the same
    # contract (e.g. BHARTIARTL/M&M +200 vs M&M/HDFCLIFE −200 M&M26JULFUT,
    # 2026-07-09/10 incident) legitimately show broker qty 0 — comparing
    # any single leg against the net row is a false mismatch that halted
    # entries mid-session and then refused the next day's start.
    expected_qty: Dict[str, int] = {}
    leg_holders: Dict[str, List[tuple]] = {}
    for s in live_strategies:
        if s.state.position == "FLAT":
            continue
        for leg in s.state.legs:
            shares = leg.quantity * leg.lot_size  # signed
            expected_qty[leg.tradingsymbol] = (
                expected_qty.get(leg.tradingsymbol, 0) + shares
            )
            leg_holders.setdefault(leg.tradingsymbol, []).append(
                (f"{s.symbol_a}/{s.symbol_b}", shares, leg)
            )

    mismatches: List[str] = []
    price_warnings: List[str] = []
    expected_tradingsymbols: set = set(expected_qty)
    for ts, expected_shares in expected_qty.items():
        actual_shares = broker_qty.get(ts, 0)
        if expected_shares != actual_shares:
            held_by = " + ".join(f"{label} {shares:+d}"
                                 for label, shares, _leg in leg_holders[ts])
            mismatches.append(
                f"{ts}: state expects {expected_shares} shares net "
                f"({held_by}), broker has {actual_shares}"
            )
            continue
        # M-R2: cross-check entry_price against broker's average_price.
        # State-schema corruption or wrong-state-file-copied would
        # otherwise leave stop-z math calibrated to a baseline the
        # broker doesn't agree with. 0.5% tolerance covers normal
        # rounding + intra-trade adds without flagging routine drift.
        # Only meaningful when exactly ONE leg holds this contract — the
        # broker's single net average_price cannot be attributed across
        # multiple legs (and nets to 0/absent for offsetting legs).
        if len(leg_holders[ts]) != 1:
            continue
        label, _shares, leg = leg_holders[ts][0]
        broker_px = broker_avg_price.get(ts)
        if broker_px is None or broker_px == 0:
            continue  # broker didn't report price — skip silently
        tol = max(0.005 * broker_px, 0.5)  # 0.5% or ₹0.50 floor
        if abs(leg.entry_price - broker_px) > tol:
            price_warnings.append(
                f"{label} {ts}: "
                f"state entry_price ₹{leg.entry_price:.2f} vs broker "
                f"average_price ₹{broker_px:.2f} (diff "
                f"₹{abs(leg.entry_price - broker_px):.2f}, tol "
                f"₹{tol:.2f})"
            )

    # Broker positions we don't know about — flag (don't refuse). Could be
    # manual orders or another runner's positions on the same account.
    unknown = [ts for ts, qty in broker_qty.items()
               if qty != 0 and ts not in expected_tradingsymbols]
    if unknown:
        log.warning(
            "Broker has %d NFO position(s) not tracked by this runner — "
            "this runner will NOT manage them: %s",
            len(unknown), unknown,
        )

    if price_warnings:
        # M-R2: entry_price drift is non-fatal — broker agrees on shares
        # so trading can proceed, but stop-z math is calibrated to a
        # different baseline. Operator should audit state vs broker.
        log.warning(
            "M-R2: entry_price mismatch on %d leg(s) — broker shares "
            "match but cost basis drifted. Stop-z math may fire against "
            "an unintended baseline:\n  %s",
            len(price_warnings), "\n  ".join(price_warnings),
        )

    if mismatches:
        msg = ("Broker reconciliation FAILED — refusing to start.\n  " +
               "\n  ".join(mismatches) +
               "\nResolve before retry: either restore the state file from "
               "data_cache/state_backups/ (see core/_state_backup.py) OR square "
               "off the broker positions manually OR (last resort) move the "
               "state file aside and `mv state_backups state_backups.archived` "
               "to acknowledge a clean restart.")
        log.error(msg)
        raise RuntimeError(msg)

    log.info("Broker reconciliation OK: %d expected NFO position(s) match",
             len(expected_tradingsymbols))


def reconcile_mid_session(strategies, kite, log: logging.Logger) -> bool:
    """3.7 / M-6: periodic in-session drift check. Unlike the startup gate
    (reconcile_with_broker, which RAISES to refuse start), a mismatch or a
    kite.positions() failure here must NOT crash the live loop. On drift, log
    CRITICAL and touch HALT_NEW_ENTRIES so no NEW exposure opens while existing
    positions can still exit — then leave escalation (HALT_ALL / manual
    square-off) to the operator. Returns True iff drift was detected.

    No-op when no live strategy is present (paper books have no broker truth)."""
    if not any(getattr(s, "mode", "paper") == "live" for s in strategies):
        return False
    try:
        reconcile_with_broker(strategies, kite, log)
        return False
    except Exception as e:
        log.critical(
            "MID-SESSION RECONCILE DRIFT: %s — touching HALT_NEW_ENTRIES "
            "(existing positions keep exiting; investigate broker vs state "
            "before clearing the flag).", e,
        )
        try:
            HALT_NEW_ENTRIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            HALT_NEW_ENTRIES_PATH.touch()
        except Exception as te:
            log.exception("Failed to touch HALT_NEW_ENTRIES: %s", te)
        return True


def write_eod_sidecar(strategies, today: date, log: logging.Logger,
                       system: str = "baseline"):
    """Per-pair EOD reports for scripts/verify_pair_paper.py to consume.

    Baseline keeps the original filename (pair_paper_eod_<date>.json) so the
    existing verifier and dashboard ingest are untouched. Non-baseline systems
    suffix the filename and label the payload — the comparison tooling reads
    both families."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    if system == "baseline":
        filename = f"pair_paper_eod_{today.isoformat()}.json"
    else:
        filename = f"pair_paper_{system}_eod_{today.isoformat()}.json"
    path = DATA_CACHE / filename
    payload = {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "system": system,
        "pairs": [],
    }
    for s in strategies:
        try:
            report = s.generate_eod_report()
            # Normalise non-JSON-native types (tuple, datetime) for the verifier.
            report["pair"] = list(report.get("pair", (s.symbol_a, s.symbol_b)))
            payload["pairs"].append(report)
        except Exception as e:
            log.exception("EOD report failed for %s/%s: %s",
                          s.symbol_a, s.symbol_b, e)
    path.write_text(json.dumps(payload, default=str, indent=2))
    log.info("EOD sidecar: %s (%d pairs)", path, len(payload["pairs"]))


def main():
    parser = argparse.ArgumentParser(description="Automated pair-trading paper runner")
    parser.add_argument("--top", type=int, default=3,
                        help="Number of top pairs from pair_candidates.csv (default 3)")
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--exit-z", type=float, default=0.75)
    parser.add_argument("--stop-z", type=float, default=4.0)
    parser.add_argument("--lookback", type=int, default=60, dest="lookback_days")
    parser.add_argument("--max-hold", type=int, default=7, dest="max_holding_days")
    parser.add_argument("--lots-per-leg", type=int, default=1)
    parser.add_argument("--ack-large-size", action="store_true",
                        help="H12: explicit acknowledgement required when "
                             "--lots-per-leg > 5. Pre-flight tip: cutover-week "
                             "sizing is --lots-per-leg 1; this flag exists so a "
                             "typo (e.g. --lots-per-leg 100) cannot silently "
                             "deploy a 100× larger book.")
    parser.add_argument("--kite-rate-per-sec", type=float, default=8.0,
                        dest="kite_rate_per_sec",
                        help="Token-bucket refill rate (req/s) for the kite "
                             "client. Kite's per-key ceiling is 10/s; we "
                             "default 2 below to leave headroom for retries "
                             "and the dashboard process sharing the key. "
                             "Calls block on the bucket — never 429. "
                             "(default: 8.0)")
    parser.add_argument("--kite-burst", type=int, default=8,
                        dest="kite_burst",
                        help="Token-bucket burst size. The first N≤burst "
                             "calls after idle pass through without "
                             "throttling; sustained pressure throttles to "
                             "--kite-rate-per-sec. (default: 8)")
    parser.add_argument("--stop-cooldown-minutes", type=int, default=60,
                        dest="stop_cooldown_minutes",
                        help="After a STOP-OUT exit, refuse re-entry on the "
                             "same pair for this many minutes. Without it, a "
                             "pair stopped at z=4.2 re-enters on the very "
                             "next tick (z still > entry_z) — observed as "
                             "runaway churn at ₹6-10k/hr per pair. The "
                             "cooldown survives state-file restore so a "
                             "stop at 14:30 still gates next-morning entry. "
                             "Other exit reasons (MEAN_REVERT, MAX_HOLD) "
                             "are unaffected. 0 disables (default: 60).")
    parser.add_argument("--max-leg-notional", type=float, default=1_000_000,
                        help="Per-leg ₹ cap (required for paper mode)")
    parser.add_argument("--max-book-notional-inr", type=float, default=0.0,
                        dest="max_book_notional_inr",
                        help="H13: total Σ open_notional ceiling across all "
                             "paper/live runners' state files in data_cache/. "
                             "Refuses new entries when current book is at or "
                             "above this cap. 0 disables (default).")
    parser.add_argument("--force", action="store_true",
                        help="Run even on weekends/holidays (testing only)")
    parser.add_argument("--candidates", type=str, default=str(CANDIDATES_PATH),
                        help="Path to pair_candidates CSV (default: "
                             "data_cache/pair_candidates.csv)")
    parser.add_argument("--system", type=str, default="baseline",
                        help="System tag — used to suffix log/EOD filenames "
                             "and label the EOD payload so the comparison "
                             "tool can split P&L by system. Defaults to "
                             "'baseline' which preserves the original "
                             "filenames.")
    parser.add_argument("--quality-max-pvalue", type=float, default=None,
                        help="Override the cointegration p-value ceiling in "
                             "the quality floor (default QUALITY_MAX_PVALUE = "
                             "0.025). The persistent system passes 0.05: its "
                             "CSV already cleared the persistence screen's "
                             "p<0.05 in ≥2 of N rolling windows, so re-testing "
                             "the latest window at 0.025 is double-jeopardy. "
                             "corr / half-life floors are unaffected.")
    parser.add_argument("--max-csv-age-days", type=float, default=None,
                        help="Refuse to load candidates CSV older than this "
                             "many days. Safety net for a failed weekly screen "
                             "leaving stale hedge ratios in production. 0 to "
                             "disable. When unset, defaults to 1 day in "
                             "--mode live (H10: stale β's are a directional "
                             "exposure we won't accept on real money) and "
                             "7 days in paper/signals.")
    parser.add_argument("--force-flatten-on-exit", action="store_true",
                        help="Flatten every open position at session end "
                             "before persisting state. Operations safety "
                             "hatch — the default is to hold open positions "
                             "across sessions and exit only on strategy "
                             "triggers (mean-revert, stop, max-hold) or "
                             "contract expiry.")
    parser.add_argument("--max-daily-loss-inr", type=float, default=50_000.0,
                        help="Aggregate session ΔP&L floor (₹). When the "
                             "session loss across all in-flight strategies "
                             "exceeds this, the runner touches its own "
                             "per-runner daily-loss flag (canonical "
                             "data_cache/HALT_DAILY_LOSS for --system "
                             "persistent, else HALT_DAILY_LOSS_<system>) — "
                             "entries suspend, exits continue. Persists so "
                             "the operator must `rm` the flag to resume. "
                             "0 disables the check (NOT recommended for "
                             "live). Default: 50000.")
    parser.add_argument("--mode", choices=["paper", "live", "signals"],
                        default="paper",
                        help="Execution mode. paper (default): mock fills, "
                             "no real orders. live: real money via Kite — "
                             "requires ALLOW_LIVE_MODE=true in .env AND "
                             "--i-understand-this-is-real-money AND "
                             "--max-daily-loss-inr > 0. signals: emit "
                             "JSONL signals only, no fills.")
    parser.add_argument("--publish-signals", action="store_true",
                        dest="publish_signals",
                        help="Issue #90 signal plane: publish every "
                             "book-mutating decision (entries, all exit "
                             "reasons, failed-entry cancels) as §4 contract "
                             "signals to the file-backed bus "
                             "(logs/signal-bus/pair_trading/). Opt-in; "
                             "off = behaviour unchanged.")
    parser.add_argument("--publish-signals-redis", dest="publish_signals_redis",
                        metavar="REDIS_URL", default=None,
                        help="Issue #90 §6: also project published signals to a "
                             "Redis Streams bus (e.g. redis://localhost:6379/0). "
                             "Requires --publish-signals. The fsync'd file bus "
                             "stays the system of record; Redis is a rebuildable "
                             "projection reconciled from the file on startup, and "
                             "a Redis outage never stops the session.")
    parser.add_argument("--i-understand-this-is-real-money",
                        dest="i_understand", action="store_true",
                        help="Required confirmation flag for --mode live. "
                             "Doubles as a typo-tripwire so a refactor "
                             "can't accidentally flip the runner to live.")
    args = parser.parse_args()

    # H12: hard cap on --lots-per-leg to catch operator typos before they
    # deploy real notional. Pre-flight (deploy/VPS_DEPLOYMENT.md §7.9)
    # calls for --lots-per-leg 1; anything >5 must be explicitly
    # acknowledged with --ack-large-size.
    if args.lots_per_leg > 5 and not args.ack_large_size:
        parser.error(
            f"--lots-per-leg={args.lots_per_leg} exceeds the soft cap of 5. "
            "Pass --ack-large-size to acknowledge intentional large sizing, "
            "or reduce --lots-per-leg. (H12 typo-tripwire.)"
        )

    if args.publish_signals_redis and not args.publish_signals:
        parser.error("--publish-signals-redis requires --publish-signals "
                     "(the Redis stream projects the file bus, which "
                     "--publish-signals produces)")

    # Typo-tripwire for --quality-max-pvalue. Must be in (0, 0.05]: 0 or
    # negative rejects every pair (empty book); anything above 0.05 is
    # meaningless — the screen only writes candidates with p < 0.05, so a
    # higher ceiling can't admit more, and the value is almost certainly a
    # fat-finger (0.5 / 5 for 0.05). Fail loud rather than trade on a
    # silently-wrong selection gate.
    if args.quality_max_pvalue is not None and not (
        0 < args.quality_max_pvalue <= 0.05
    ):
        parser.error(
            f"--quality-max-pvalue={args.quality_max_pvalue} is outside the "
            "valid range (0, 0.05]. The screen gate is p<0.05, so a higher "
            "ceiling has no effect; 0 or negative empties the book. Check for "
            "a decimal-point typo (0.05, not 0.5/5)."
        )

    load_dotenv(HERE / ".env")
    os.chdir(HERE)

    today = datetime.now().date()
    log = setup_logging(today, args.system)

    # H10: resolve --max-csv-age-days from mode when the operator didn't
    # pass it explicitly. Live tolerates only fresh hedge ratios; paper
    # keeps the legacy 7-day window.
    explicit_override = args.max_csv_age_days is not None
    args.max_csv_age_days = resolve_max_csv_age_days(
        args.mode, args.max_csv_age_days,
    )
    if not explicit_override:
        log.info("--max-csv-age-days unset → defaulting to %.1f day(s) for "
                 "--mode %s", args.max_csv_age_days, args.mode)

    # Live-mode safety gate. Three independent locks so a refactor or
    # typo can't push real money into the market:
    #   (1) --mode live CLI flag (default paper)
    #   (2) ALLOW_LIVE_MODE=true env var (mirrors backend/settings.py)
    #   (3) --i-understand-this-is-real-money CLI confirmation flag
    # Plus the circuit-breaker must be armed (--max-daily-loss-inr > 0).
    if args.mode == "live":
        env_allow = os.environ.get("ALLOW_LIVE_MODE", "").strip().lower()
        if env_allow != "true":
            raise RuntimeError(
                "--mode live requires ALLOW_LIVE_MODE=true in the "
                "environment (set in .env). Refusing to start. "
                f"Current value: {env_allow!r}"
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
        log.critical("LIVE TRADING SESSION — REAL MONEY [system=%s]",
                     args.system)
        log.critical("=" * 60)

    # M-O4 / M-O2: pre-flight gates before any market-time-dependent or
    # disk-touching work. Both fail loud — operator must fix TZ or free
    # disk before the next run.
    assert_timezone_ist(log)
    assert_disk_space_ok([DATA_CACHE, HERE / "logs"], log)

    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, log)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    # H9: refuse to start a second runner with the same --system tag.
    # Assigned to a local that lives for main()'s scope so the FD stays
    # open (lock released on process exit, including SIGKILL).
    _runner_lock_fd = acquire_runner_lock(args.system, log)  # noqa: F841

    log.info("=" * 60)
    log.info("PAIR-TRADING %s SESSION — %s [system=%s]",
             args.mode.upper(), today, args.system)
    log.info("Candidates: %s", args.candidates)
    log.info("=" * 60)

    # H17: count active legs across other paper-state files so the
    # leg-concentration cap applies across runners (baseline + persistent),
    # not just within this runner. Excludes own state file because that
    # runner's own held pairs are restored via restore_matching_strategies
    # and orphans — counting them twice would block legitimate re-entries.
    own_state = state_file_path(args.system)
    cross_runner_counts = load_cross_runner_leg_counts(own_state)
    if cross_runner_counts:
        log.info(
            "Cross-runner leg counts (H17 seed): %s",
            ", ".join(f"{s}={n}" for s, n in sorted(cross_runner_counts.items())),
        )
    if args.quality_max_pvalue is not None:
        log.info("Quality p-value ceiling overridden: %.3f (default %.3f)",
                 args.quality_max_pvalue, QUALITY_MAX_PVALUE)
    pairs = select_pairs(args.top, log,
                          candidates_path=Path(args.candidates),
                          max_age_days=args.max_csv_age_days,
                          max_pvalue=args.quality_max_pvalue,
                          seed_leg_count=cross_runner_counts)
    log.info("Selected %d pair(s):", len(pairs))
    for _, row in pairs.iterrows():
        log.info("  %s/%s  β=%.4f  z=%.2f  half-life=%.1fd  p=%.4f",
                 row["symbol_a"], row["symbol_b"], row["hedge_ratio"],
                 row["latest_z_score"], row["half_life_days"], row["coint_pvalue"])

    config_path = ensure_pair_config(CONFIG_PATH, args.max_leg_notional, log)

    from core.kite_auth import KiteAuthManager
    log.info("Authenticating...")
    auth = KiteAuthManager(CONFIG_PATH)
    kite = auth.get_kite()

    # H14: wrap the kite client in a token-bucket throttler before any
    # call goes through. 12 pairs × 2 quote calls/tick at second-0 of
    # every minute would otherwise cluster above Kite's 10 r/s ceiling
    # and start 429-ing — symptoms in old logs: "quote failed for
    # X26MAYFUT: Unknown Content-Type (text/html ... 502: Bad gateway)"
    # which is the upstream reaction. Calls block on the bucket; they
    # don't fail.
    from core.kite_throttle import KiteRateLimiter, throttle_kite
    kite_limiter = KiteRateLimiter(
        rate_per_sec=args.kite_rate_per_sec, burst=args.kite_burst,
    )
    kite = throttle_kite(kite, kite_limiter)

    profile = kite.profile()
    log.info("Authenticated as %s (%s)", profile["user_name"], profile["user_id"])
    log.info(
        "Kite throttle armed: rate=%.1f req/s, burst=%d",
        args.kite_rate_per_sec, args.kite_burst,
    )

    # H19: prefetch the ~150k-row NFO instruments dump once and inject it
    # into every strategy. Before this, each pair re-fetched on first
    # _resolve_futures call (2 per pair) and on every legs_expire_on tick
    # (12 pairs × 360 ticks = 4,320 calls/day on expiry day). Fetched once
    # here means N strategies share a single ~5MB roundtrip.
    try:
        nfo_instruments = kite.instruments("NFO") or []
        log.info("Prefetched NFO instruments dump: %d rows", len(nfo_instruments))
    except Exception as e:
        log.warning(
            "instruments('NFO') prefetch failed (%s) — strategies will "
            "fall back to per-instance lazy fetch", e,
        )
        nfo_instruments = None

    # H8: closure for mid-session token refresh. Calls auth.get_kite() to
    # re-authenticate (re-uses cached refresh path if available, else full
    # TOTP login), then re-wraps with the same throttler so the strategies
    # don't bypass H14 after a refresh.
    def _refresh_kite():
        fresh = auth.get_kite()
        return throttle_kite(fresh, kite_limiter)

    # H13: closure summing open-leg notional, called by each strategy before
    # it generates entry proposals.
    #
    # Scoped to THIS runner's own state file (2026-08-07). It used to sum every
    # data_cache/*paper_state*.json, which made the cap a shared gate: the LIVE
    # persistent runner's open notional counted against the baseline PAPER
    # runner's ceiling, so a real-money position could freeze a simulation's
    # entries. That is the HALT_NEW_ENTRIES failure shape (PR #196) — one
    # runner's state silently halting another's. The daily-loss breaker is
    # already namespaced per --system via halt_daily_loss_path; the notional
    # cap now matches. Cross-runner coupling that IS wanted stays explicit and
    # separate: H17's leg-concentration counter above still reads siblings.
    from strategies.pair_trading import _aggregate_book_notional
    book_notional_fn = (
        partial(_aggregate_book_notional, only_state_path=own_state)
        if args.max_book_notional_inr > 0 else None
    )

    # Audit 2026-06-10 task 1.1: preload the bhavcopy front-month panel ONCE
    # for every symbol any strategy will need — today's candidates plus any
    # prior-state pair still holding a position (those become orphans below).
    # Same pattern as the H19 NFO prefetch above. Before this, EVERY pair
    # re-read all ~520 bhavcopy CSVs inside _seed_spread_history (~70s each),
    # so a 09:12 start entered the tick loop after 09:19 — blind through the
    # open while the market traded. prior_state is loaded here (pure JSON
    # read) instead of after build_strategies for the same reason.
    prior_state = load_prior_state(args.system, log)
    panel_symbols = set(pairs["symbol_a"]) | set(pairs["symbol_b"])
    for blob in prior_state.values():
        if blob.get("state", {}).get("position", "FLAT") != "FLAT":
            panel_symbols.update(blob.get("pair", []))
    try:
        from core.screen_pairs import load_front_month_panel
        spread_panel = load_front_month_panel(sorted(panel_symbols), min_coverage=0.5)
        log.info(
            "Preloaded spread panel: %d trading days × %d symbols "
            "(one bhavcopy read for all pairs)",
            len(spread_panel), spread_panel.shape[1],
        )
    except Exception as e:
        log.warning(
            "Spread-panel preload failed (%s) — strategies fall back to "
            "per-pair bhavcopy reads (slow startup, pre-1.1 behavior)", e,
        )
        spread_panel = None

    # Issue #90: one shared publisher per runner — sequence numbering is
    # per strategy_id, and every pair instance publishes onto the same
    # ordered stream. Fail-loud at startup (a broken publisher state file
    # should stop the session before the market opens, not mid-tick).
    signal_publisher = None
    if args.publish_signals:
        from signal_plane import SignalPublisher
        from signal_plane.pair_trading_signals import STRATEGY_ID
        redis_bus = None
        if args.publish_signals_redis:
            from signal_plane.bus import RedisStreamBus
            # Non-fatal by design: the Redis projection must NEVER stop a live
            # trading session (it is a rebuildable cache, not the system of
            # record — the file bus is). If Redis is unreachable or the URL is
            # bad at startup, log loudly and run file-only; a later startup
            # reconciles Redis from the file once it is reachable. Surfaced by
            # the consumer --redis-url watchdog, not by a dead runner.
            try:
                redis_bus = RedisStreamBus(STRATEGY_ID, args.publish_signals_redis)
            except Exception as e:  # noqa: BLE001 — BusUnavailable, URL parse, …
                log.error(
                    "SIGNAL REDIS DISABLED for this session: could not set up "
                    "the Redis projection (%s: %s). Publishing continues to the "
                    "file bus (system of record); Redis reconciles from the file "
                    "on a later startup once reachable.",
                    type(e).__name__, e,
                )
                redis_bus = None
        signal_publisher = SignalPublisher(
            strategy_id=STRATEGY_ID,
            bus_dir=LOG_DIR / "signal-bus",
            state_dir=DATA_CACHE,
            redis_bus=redis_bus,
        )
        log.info(
            "Signal publishing armed: strategy_id=%s bus=%s last_sequence=%d "
            "open_groups=%d",
            STRATEGY_ID, signal_publisher.bus_file(),
            signal_publisher.last_sequence, len(signal_publisher.open_groups),
        )

    strategies = build_strategies(
        pairs, args, kite, config_path, log,
        nfo_instruments=nfo_instruments,
        kite_refresh=_refresh_kite,
        book_notional_fn=book_notional_fn,
        max_book_notional=args.max_book_notional_inr,
        spread_panel=spread_panel,
        signal_publisher=signal_publisher,
    )

    # Restore prior-session state (no-op if no state file exists yet).
    matched_keys = restore_matching_strategies(strategies, prior_state, log)
    orphans = build_orphan_strategies(
        prior_state, matched_keys, args, kite, config_path, log,
        nfo_instruments=nfo_instruments,
        kite_refresh=_refresh_kite,
        book_notional_fn=book_notional_fn,
        max_book_notional=args.max_book_notional_inr,
        spread_panel=spread_panel,
        signal_publisher=signal_publisher,
    )
    strategies = strategies + orphans

    # Must be computed AFTER orphans are folded in: a pair represented by any
    # strategy serialises its own blob and needs no carry-forward.
    latch_carry = latch_carry_forward_blobs(
        prior_state, {f"{s.symbol_a}/{s.symbol_b}" for s in strategies}, log,
    )

    reconcile_with_broker(strategies, kite, log)

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

    log.info("Entering tick loop (every %ds until %s) over %d pair(s)",
             TICK_SECONDS, session_end_ts.strftime("%H:%M"), len(strategies))

    halt_loss_path = halt_daily_loss_path(args.system)
    halt_state = _HaltState(halt_loss_path)
    if args.max_daily_loss_inr <= 0:
        log.warning("--max-daily-loss-inr is disabled (0) — no automatic "
                    "circuit breaker for runaway losses this session")
    else:
        log.info("Daily-loss breaker: ₹%.0f → touches %s (own flag; does not "
                 "halt other runners)", args.max_daily_loss_inr, halt_loss_path)
    install_signal_handlers(log)
    heartbeat = HeartbeatTracker(
        threshold=SILENT_FAIL_THRESHOLD,
        sentinel_path=silent_fail_flag_path(args.system),
        log=log,
    )
    silent_fail = False
    last_reconcile = time.monotonic()
    try:
        while datetime.now() < session_end_ts:
            halt_state.refresh(log)
            # 3.7 / M-6: re-reconcile against the broker hourly during live
            # sessions so drift is caught in-session, not at next startup.
            # Non-fatal: on drift it touches HALT_NEW_ENTRIES (caught by the
            # next halt_state.refresh) rather than crashing the loop.
            if (args.mode == "live"
                    and time.monotonic() - last_reconcile >= RECONCILE_INTERVAL_S):
                reconcile_mid_session(strategies, kite, log)
                last_reconcile = time.monotonic()
            n_errored = 0
            for s in strategies:
                outcome = tick_one(s, log,
                                   halt_all=halt_state.halt_all,
                                   halt_new_entries=halt_state.halt_new)
                if outcome.attempted_execution:
                    # Persist immediately so a SIGKILL before the next
                    # strategy in this tick can't lose state mutations
                    # from the call we just made. write_state_file is
                    # fsync-durable (H4), so the post-rename state
                    # survives power loss too. A no-op execute_proposals
                    # (all-rejected, signals-mode) still triggers a write
                    # here; that's harmless and the safer side to err on.
                    try:
                        write_state_file(strategies, args.system, log,
                                         archive=False, mode=args.mode,
                                         carry_forward=latch_carry)
                    except Exception as e:
                        log.exception("Per-fill state persist failed: %s "
                                      "— continuing", e)
                if outcome.errored:
                    n_errored += 1
            n_ran = 0 if halt_state.halt_all else len(strategies)
            if heartbeat.record_tick(n_ran=n_ran, n_errored=n_errored):
                silent_fail = True
                break
            check_daily_loss_limit(strategies, args.max_daily_loss_inr, log,
                                   halt_loss_path)
            try:
                write_state_file(strategies, args.system, log, archive=False,
                                 mode=args.mode, carry_forward=latch_carry)
            except Exception as e:
                log.exception("Intraday state persist failed: %s — continuing", e)
            remaining = (session_end_ts - datetime.now()).total_seconds()
            time.sleep(max(1, min(TICK_SECONDS, remaining)))

        # Run end_of_session for both normal and silent-fail exits, but
        # wrap it in the silent-fail case: an uncaught exception in
        # teardown (e.g. write_state_file fsync fails on a dying disk,
        # legs_expire_on hits the dead token that triggered the breach)
        # would otherwise propagate past `return 1` and mask the
        # heartbeat-specific exit code with a generic traceback. The
        # sentinel + CRITICAL log have already fired by this point.
        if silent_fail:
            try:
                end_of_session(strategies, today, args, log, holidays=holidays,
                               carry_forward=latch_carry)
            except Exception as e:
                log.exception(
                    "end_of_session failed during silent-fail teardown: "
                    "%s — heartbeat alert still fires via sentinel + "
                    "non-zero exit", e,
                )
        else:
            log.info("Session-end window reached.")
            end_of_session(strategies, today, args, log, holidays=holidays,
                           carry_forward=latch_carry)

    except KeyboardInterrupt:
        # If a second SIGTERM arrives while end_of_session is writing the
        # state file / EOD sidecar, we don't want it to raise KI again
        # mid-write. Reset SIGTERM to the default action (terminate) so
        # an impatient operator's second `systemctl stop` kills cleanly
        # *after* this teardown completes, rather than interrupting it.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        log.info("Interrupted — persisting state and exiting.")
        end_of_session(strategies, today, args, log, holidays=holidays,
                       carry_forward=latch_carry)
        return 130

    if silent_fail:
        # Heartbeat fired — non-zero exit triggers notify-failure@%n
        # (deploy/notify-failure@.service, wired via C8). The sentinel
        # has been touched and CRITICAL has been logged; just return.
        return 1
    log.info("Session complete. Exiting cleanly.")
    return 0


def _calendar_days_until_next_trading_day(today: date, holidays: set) -> int:
    # M-R1: how many calendar days until the next NSE-trading day (today
    # excluded). Used to detect long-weekend / Diwali-week gaps so the
    # session-end summary can warn the operator that the open book will
    # sit unmonitored across the break.
    from datetime import timedelta
    d = today
    for step in range(1, 11):  # cap at 10 days, way past any real break
        d = d + timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            return step
    return 10  # fallback — refuses to spin forever


def end_of_session(strategies, today: date, args, log: logging.Logger,
                    *, holidays: Optional[set[date]] = None,
                    carry_forward: Optional[List[Dict]] = None):
    """At session end: (1) force-flatten any leg whose contract expires today;
    (2) honour --force-flatten-on-exit if set; (3) persist state for the
    next session; (4) write the EOD sidecar for the verifier/dashboard.

    Order matters — flatten must run before persist so the state file reflects
    the post-flatten reality, and persist must run before sidecar so the
    sidecar's session_realized_delta is a snapshot of the same moment.

    H18: legs_expire_on now raises on persistent kite.instruments('NFO')
    failure rather than silently returning False. We collect any
    unverifiable pairs, ALWAYS persist state + sidecar first (so the
    next runner doesn't start blind), then re-raise. The non-zero exit
    fires notify-failure@ so the operator can manually flatten before
    cash settlement.
    """
    # M-R1: long-break warning. If the next trading day is 3+ calendar
    # days away (long weekend, Diwali week, etc.) AND any pair holds an
    # open book AND --force-flatten-on-exit is NOT set, emit a WARNING.
    # Static check; the operator decides whether to override on the
    # next run.
    if holidays is not None and not args.force_flatten_on_exit:
        gap_days = _calendar_days_until_next_trading_day(today, holidays)
        open_pairs = [f"{s.symbol_a}/{s.symbol_b}" for s in strategies
                      if s.state.position != "FLAT"]
        if gap_days >= 3 and open_pairs:
            log.warning(
                "M-R1: next trading day is %d calendar days away and %d "
                "pair(s) hold open positions: %s. --force-flatten-on-exit "
                "is OFF; the book will sit unmonitored across the break. "
                "Consider re-running with --force-flatten-on-exit or "
                "manually squaring off before close.",
                gap_days, len(open_pairs), ", ".join(open_pairs),
            )

    unverified_expiry: List[str] = []
    for s in strategies:
        pair_label = f"{s.symbol_a}/{s.symbol_b}"
        if s.state.position == "FLAT":
            continue
        if args.force_flatten_on_exit:
            log.info("[%s] forced flatten (--force-flatten-on-exit)", pair_label)
            flatten_one(s, log, reason="OPS_FORCE")
            continue
        try:
            if s.legs_expire_on(today):
                log.info("[%s] expiry-day flatten — leg contract expires today",
                         pair_label)
                flatten_one(s, log, reason="EXPIRY")
        except Exception as e:
            log.exception("[%s] expiry check failed after retries: %s",
                          pair_label, e)
            unverified_expiry.append(pair_label)
    write_state_file(strategies, args.system, log, mode=args.mode,
                     carry_forward=carry_forward)
    write_eod_sidecar(strategies, today, log, args.system)
    if unverified_expiry:
        log.critical(
            "EXPIRY CHECK FAILED for %d open pair(s): %s. State and EOD "
            "sidecar have been written; runner will now exit non-zero so "
            "notify-failure@ alerts. OPERATOR ACTION: manually verify "
            "whether any leg's futures contract expires today and square "
            "off BEFORE cash settlement — a contract carried into "
            "settlement is the worst possible outcome.",
            len(unverified_expiry), ", ".join(unverified_expiry),
        )
        raise RuntimeError(
            f"Expiry-day check failed for {len(unverified_expiry)} pair(s); "
            "refusing to silently proceed (H18)."
        )


if __name__ == "__main__":
    sys.exit(main())
