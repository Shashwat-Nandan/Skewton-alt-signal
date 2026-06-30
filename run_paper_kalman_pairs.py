#!/usr/bin/env python3
"""
Kalman Pair-Trading Paper Runner
================================
Forward side-by-side test (Phase 3, tasks/kalman-pair-system-plan.md): runs
KalmanPairStrategy in PAPER on the same candidate universe as the static
pair runner, so we can compare Kalman-tracked γ vs static β on identical
forward data. Phase-2 found Kalman beats static in-regime (+43% on the live
book's last month) but the absolute edge is regime-dependent — only a forward
paper test settles it without selection bias.

Mirrors run_paper_pairs.py's operational scaffolding (TOTP auth, holiday/weekend
gate, 09:15→15:25 loop, shared HALT_* kill switches, atomic state persistence,
EOD sidecar, hourly heartbeat) but is its own system:
  - state  : data_cache/kalman_pairs_runner_state.json
  - EOD     : data_cache/pair_paper_kalman_eod_<date>.json
  - logfile : logs/paper-kalman-pairs-YYYY-MM-DD.log

The EOD sidecar uses run_paper_pairs.py's `pair_paper_{system="kalman"}_eod`
convention, so "kalman" is a first-class system in the existing read-only
tooling: the /pair-paper-compare dashboard tab and compare_paper_systems.py pick
it up alongside baseline/persistent with zero extra code — that comparison IS
the forward A/B test. The STATE file deliberately does NOT follow the
`pair_paper_state_*` convention: that name matches the glob
pair_trading._aggregate_book_notional uses to sum the LIVE runner's book cap,
and a PAPER book must not count against a real-money notional limit.

Kalman-specific vs the static runner:
  - each pair's filter is seeded from the bhavcopy daily-close history (log
    prices) via KalmanPairStrategy.from_training, then advanced ONE step per
    session at the close (step_daily_close) — the daily-update design (D1);
  - serialize/restore round-trips the FULL filter state (vector + covariance)
    and the rolling-z window, so a restart continues the filter exactly;
  - pairs whose log-elasticity γ is non-cointegrated (out of [0.1,10]) are
    skipped at build time (the guard the Phase-2 study relied on).

paper/signals only. --mode live is intentionally unsupported here (the strategy
raises NotImplementedError) until the forward test validates an edge.

CANNOT be verified live in CI (no Kite session); smoke-test on the host. NOTE:
do not run a fresh Kite login while the live static runner is active — reuse the
cached session (see tasks/lessons.md / memory).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

from runner_common import (
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
    assert_timezone_ist,
    install_signal_handlers,
    is_trading_day,
    load_holidays,
    sleep_until,
)
from _state_backup import archive_state_backup, assert_no_orphan_backups
from screen_pairs import load_front_month_panel
from strategies.kalman_pair_trading import KalmanPairStrategy

HERE = Path(__file__).resolve().parent
CONFIG_PATH = str(HERE / "config.ini")
DATA_CACHE = HERE / "data_cache"
LOG_DIR = HERE / "logs"
SYSTEM = "kalman"
# State file deliberately does NOT match the static runners' state-file globs
# (`*paper_state*.json` in pair_trading._aggregate_book_notional, and
# `pair_paper_state_*.json` in run_paper_pairs' H17 cross-runner concentration
# counter). This is an ISOLATED PAPER book (decision D5): it must not count
# against the live runner's notional cap NOR its per-symbol concentration
# limiter. (The EOD sidecar keeps the pair_paper_kalman_eod_<date> name so the
# dashboard compare tab still picks it up; only the STATE file needs hiding.)
STATE_PATH = DATA_CACHE / "kalman_pairs_runner_state.json"
CANDIDATES_PATH = DATA_CACHE / "pair_candidates.csv"
HOLIDAYS_PATH = HERE / "holidays.csv"
LOCK_PATH = DATA_CACHE / "kalman_pairs.lock"
SILENT_FAIL_PATH = DATA_CACHE / "SILENT_FAIL_kalman"
TRAIN_MIN_DAYS = 80  # need a healthy window to seed the filter

logger = logging.getLogger("kalman_pairs")


# ──────────────────────────────────────────────────────────────────
# Futures resolution + live quotes (the plumbing the strategy defers to us)
# ──────────────────────────────────────────────────────────────────
def resolve_front_month(nfo: List[dict], symbol: str,
                        today: date) -> Optional[dict]:
    """Front-month STF for `symbol`: {tradingsymbol, lot_size, instrument_token}.
    Smallest expiry on/after today (mirrors pair_trading._resolve_futures)."""
    def _exp(row):
        e = row.get("expiry")
        if isinstance(e, str):
            return datetime.strptime(e[:10], "%Y-%m-%d").date()
        return e.date() if hasattr(e, "date") else e

    fut = [r for r in nfo
           if r.get("name") == symbol and r.get("instrument_type") == "FUT"]
    fut = [r for r in fut if _exp(r) and _exp(r) >= today]
    if not fut:
        return None
    front = min(fut, key=_exp)
    return {
        "tradingsymbol": front["tradingsymbol"],
        "lot_size": int(front.get("lot_size", 0) or 0),
        "instrument_token": int(front.get("instrument_token", 0) or 0),
    }


def make_quote_fn(kite) -> Callable[[str], Optional[float]]:
    """A quote_fn(tradingsymbol)->last_price backed by kite.quote, tolerant of
    transient failures (returns None so the strategy skips the tick)."""
    def _quote(tradingsymbol: str) -> Optional[float]:
        key = f"NFO:{tradingsymbol}"
        try:
            return float(kite.quote([key])[key]["last_price"])
        except Exception as e:  # pragma: no cover - network path
            logger.debug("quote failed for %s: %s", tradingsymbol, e)
            return None
    return _quote


# ──────────────────────────────────────────────────────────────────
# Strategy construction (testable: inject kite, panel, nfo)
# ──────────────────────────────────────────────────────────────────
def build_strategies(pairs: pd.DataFrame, panel: pd.DataFrame, nfo: List[dict],
                     kite, config_path: str, today: date,
                     log: logging.Logger) -> List[KalmanPairStrategy]:
    quote_fn = make_quote_fn(kite)
    out: List[KalmanPairStrategy] = []
    for _, row in pairs.iterrows():
        a, b = row["symbol_a"], row["symbol_b"]
        if a not in panel.columns or b not in panel.columns:
            log.warning("%s/%s: not in bhavcopy panel — skipping", a, b)
            continue
        pair = panel[[a, b]].dropna()
        if len(pair) < TRAIN_MIN_DAYS:
            log.warning("%s/%s: only %d training days (<%d) — skipping",
                        a, b, len(pair), TRAIN_MIN_DAYS)
            continue
        fa, fb = resolve_front_month(nfo, a, today), resolve_front_month(nfo, b, today)
        if not fa or not fb or fa["lot_size"] <= 0 or fb["lot_size"] <= 0:
            log.warning("%s/%s: could not resolve front-month futures — skipping",
                        a, b)
            continue
        try:
            s = KalmanPairStrategy(
                kite=kite, config_path=config_path, mode="paper",
                symbol_a=a, symbol_b=b,
                tradingsymbol_a=fa["tradingsymbol"], tradingsymbol_b=fb["tradingsymbol"],
                lot_size_a=fa["lot_size"], lot_size_b=fb["lot_size"],
                training_a=pair[a].values, training_b=pair[b].values,
                model="momentum", quote_fn=quote_fn,
            )
        except ValueError as e:  # seed-γ guard / degenerate training
            log.warning("%s/%s: skipped — %s", a, b, e)
            continue
        log.info("Init %s/%s seed γ=%.4f legs=%s/%s lots=%d/%d hist=%d",
                 a, b, s._gamma_today, fa["tradingsymbol"], fb["tradingsymbol"],
                 fa["lot_size"], fb["lot_size"], len(s._spread_history))
        s.log_effective_params()
        out.append(s)
    if not out:
        raise RuntimeError("No Kalman strategies could be initialised")
    return out


# ──────────────────────────────────────────────────────────────────
# State persistence + EOD (mirror run_paper_pairs)
# ──────────────────────────────────────────────────────────────────
def write_state_file(strategies, log: logging.Logger, *, archive: bool = True) -> None:
    """Atomic, power-loss-safe persist (write tmp → fsync → rename → fsync dir),
    then a timestamped backup ring so a corrupted file is recoverable (parity
    with run_paper_pairs). `archive=False` for the per-tick intraday persist so
    the 30-slot ring isn't churned every minute."""
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    payload = {
        "system": SYSTEM, "mode": "paper",
        "updated_at": datetime.now().isoformat(), "pairs": [],
    }
    for s in strategies:
        try:
            payload["pairs"].append(s.serialize_state())
        except Exception as e:
            log.exception("serialize_state failed for %s/%s: %s",
                          s.symbol_a, s.symbol_b, e)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)
    dfd = os.open(STATE_PATH.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    if archive:
        archive_state_backup(STATE_PATH, log)


def load_prior_state(log: logging.Logger) -> Dict[str, Dict]:
    if not STATE_PATH.exists() or STATE_PATH.stat().st_size == 0:
        # Refuse to silently start fresh if timestamped backups exist — they may
        # hold positions we'd otherwise abandon (parity with run_paper_pairs).
        assert_no_orphan_backups(STATE_PATH, log)
        log.info("No prior Kalman state at %s — starting fresh.", STATE_PATH)
        return {}
    try:
        payload = json.loads(STATE_PATH.read_text())
    except Exception as e:
        log.exception("Failed to parse %s: %s.", STATE_PATH, e)
        assert_no_orphan_backups(STATE_PATH, log)
        log.info("No backups present — starting fresh.")
        return {}
    return {f"{b['pair'][0]}/{b['pair'][1]}": b
            for b in payload.get("pairs", []) if len(b.get("pair", [])) == 2}


def restore_matching(strategies, prior: Dict[str, Dict],
                     log: logging.Logger) -> None:
    """Restore each built strategy's full filter + position state from the prior
    session (no-op if absent). Fails loud per-pair but never aborts the run."""
    for s in strategies:
        blob = prior.get(f"{s.symbol_a}/{s.symbol_b}")
        if not blob:
            continue
        try:
            s.restore_state(blob)
            log.info("Restored %s/%s (pos=%s, hist=%d)", s.symbol_a, s.symbol_b,
                     s.state.position, len(s._spread_history))
        except Exception as e:
            log.exception("restore_state failed for %s/%s: %s — fresh seed kept",
                          s.symbol_a, s.symbol_b, e)


def catch_up_filters(strategies, panel: "pd.DataFrame", today: date,
                     log: logging.Logger) -> None:
    """After restoring a possibly-stale saved filter, replay any bhavcopy days
    that elapsed since the filter's last step so it (and its z-window) are
    current — the once-per-missed-day catch-up step_daily_close documents. Only
    steps days strictly AFTER _last_step_date (and strictly before today, since
    today's official close isn't published intraday), so no day is double-stepped.
    A filter that was never stepped (fresh seed) needs no catch-up."""
    for s in strategies:
        last = s._last_step_date
        if last is None:
            continue
        if s.symbol_a not in panel.columns or s.symbol_b not in panel.columns:
            continue
        pair = panel[[s.symbol_a, s.symbol_b]].dropna()
        missed = [(d, pair.loc[d]) for d in pair.index
                  if last < d.date() < today]
        if not missed:
            continue
        for d, row in missed:
            ca, cb = float(row[s.symbol_a]), float(row[s.symbol_b])
            if ca <= 0 or cb <= 0:
                continue
            s._clock = lambda d=d: datetime.combine(d.date() if hasattr(d, "date")
                                                    else d, datetime.min.time())
            try:
                s.step_daily_close(ca, cb)
            except Exception as e:
                log.warning("%s/%s catch-up step %s failed: %s",
                            s.symbol_a, s.symbol_b, d, e)
        s._clock = datetime.now  # restore live clock
        log.info("%s/%s caught up %d missed daily step(s) since %s",
                 s.symbol_a, s.symbol_b, len(missed), last)


def write_eod_sidecar(strategies, today: date, log: logging.Logger) -> None:
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    path = DATA_CACHE / f"pair_paper_kalman_eod_{today.isoformat()}.json"
    payload = {"date": today.isoformat(), "generated_at": datetime.now().isoformat(),
               "system": SYSTEM, "pairs": []}
    for s in strategies:
        try:
            r = s.generate_eod_report()
            r["pair"] = list(r.get("pair", (s.symbol_a, s.symbol_b)))
            payload["pairs"].append(r)
        except Exception as e:
            log.exception("EOD report failed for %s/%s: %s",
                          s.symbol_a, s.symbol_b, e)
    path.write_text(json.dumps(payload, default=str, indent=2))
    log.info("EOD sidecar: %s (%d pairs)", path.name, len(payload["pairs"]))


def step_filters_on_close(strategies, today: date, log: logging.Logger) -> None:
    """Advance each pair's Kalman filter ONE step on the session close (D1).
    Uses the last live quote per leg; a missing quote skips that pair's step
    (the filter just misses a day — acceptable for paper).

    Idempotent per day: skip any pair already stepped today (_last_step_date ==
    today). Without this, a restart in the 15:25–15:30 window after a clean
    session-end (systemd Restart= or a manual relaunch) would fall through the
    tick loop and double-step today's close — duplicate spread + double-advanced
    filter."""
    for s in strategies:
        if s._last_step_date == today:
            log.info("%s/%s: already stepped %s — skipping duplicate close step",
                     s.symbol_a, s.symbol_b, today)
            continue
        ca = s._quote_fn(s.tradingsymbol_a)
        cb = s._quote_fn(s.tradingsymbol_b)
        # `nan <= 0` is False, so an explicit finite check is needed or a NaN
        # quote slips through to step_daily_close → raises mid-loop.
        if (ca is None or cb is None or not math.isfinite(ca)
                or not math.isfinite(cb) or ca <= 0 or cb <= 0):
            log.warning("%s/%s: no valid close quote — filter not stepped today",
                        s.symbol_a, s.symbol_b)
            continue
        # Per-pair isolation (matches tick_one / write_state_file / etc.): one
        # pair's failure must not abort the others' daily step NOR the EOD
        # state/sidecar writes that run right after this in main().
        try:
            s.step_daily_close(float(ca), float(cb))
        except Exception as e:
            log.exception("%s/%s: step_daily_close failed: %s",
                          s.symbol_a, s.symbol_b, e)


# ──────────────────────────────────────────────────────────────────
# Expiry-day flatten (parity with run_paper_pairs.end_of_session step 1)
# ──────────────────────────────────────────────────────────────────
# This Kalman runner is a SEPARATE runner and originally shipped WITHOUT the
# static runner's expiry-day force-flatten — so it carried open STF legs through
# contract expiry (a contract dragged into cash settlement is the worst outcome).
# We mirror the static path, but resolve expiry from the runner's already-fetched
# NFO list instead of re-querying: map each open leg's tradingsymbol → expiry and
# square off any pair whose leg expires today, BEFORE state/EOD persistence.
def _expiry_by_tradingsymbol(nfo: List[dict]) -> Dict[str, date]:
    out: Dict[str, date] = {}
    for row in nfo:
        ts = row.get("tradingsymbol")
        if not ts:
            continue
        exp = row.get("expiry")
        if isinstance(exp, str):
            try:
                exp = datetime.strptime(exp[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
        elif hasattr(exp, "date"):
            exp = exp.date()
        if exp is not None:
            out[ts] = exp
    return out


def legs_expire_on(strategy, expiry_by_ts: Dict[str, date], today: date,
                   log: logging.Logger) -> bool:
    """True if any on-chain open leg's futures contract expires on OR BEFORE
    today. The static runner checks `== today`; we harden to `<= today` so a
    MISSED expiry day (runner didn't run / crashed at the close) still squares
    off the next session instead of carrying the expired contract indefinitely —
    as long as the contract is still on the chain (off-chain legs are handled by
    `flatten_expiring_legs`, since they can no longer be quoted to flatten)."""
    legs = getattr(strategy.state, "legs", None)
    if not legs:
        return False
    return any(
        (exp := expiry_by_ts.get(leg.tradingsymbol)) is not None and exp <= today
        for leg in legs
    )


def flatten_one(strategy, log: logging.Logger, reason: str = "EXPIRY") -> None:
    """Force-close an open pair via the strategy's own exit builder (parity with
    run_paper_pairs.flatten_one). No-op when already flat or quotes are missing."""
    label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    if strategy.state.position == "FLAT" or not strategy.state.legs:
        return
    try:
        _, prices = strategy._observe_spread()
        if not prices:
            log.warning("[%s] flatten: could not fetch quotes; leaving position "
                        "open", label)
            return
        strategy._update_unrealized(prices)
        close_props = strategy._build_exit_proposals(reason, 0.0, prices)
        if close_props:
            log.info("[%s] flattening %d leg(s) (%s)", label, len(close_props), reason)
            strategy.execute_proposals(close_props)
    except Exception as e:
        log.exception("[%s] flatten failed: %s", label, e)


def flatten_expiring_legs(strategies, nfo: List[dict], today: date,
                          log: logging.Logger) -> None:
    """Square off any pair whose futures leg expires on/before today, before
    persist+EOD. H18 parity — surface, never silently carry into settlement:

      • NFO list empty/unusable while a book is open → RAISE.
      • A held leg STILL on the chain and expired/expiring (exp <= today) →
        auto-flatten at the last quote.
      • A held leg OFF the chain (already delisted) → cannot be quoted to
        auto-flatten; log CRITICAL and RAISE so the operator squares off
        manually (the runner persists state+EOD first — see main())."""
    open_pairs = [s for s in strategies
                  if s.state.position != "FLAT" and s.state.legs]
    if not open_pairs:
        return
    expiry_by_ts = _expiry_by_tradingsymbol(nfo)
    if not expiry_by_ts:
        raise RuntimeError(
            "NFO instrument list is empty/unusable — cannot verify whether held "
            "legs expire today. Refusing to silently carry positions into "
            "possible cash settlement (H18).")
    stranded: List[str] = []
    for s in open_pairs:
        label = f"{s.symbol_a}/{s.symbol_b}"
        off_chain = [leg.tradingsymbol for leg in s.state.legs
                     if leg.tradingsymbol not in expiry_by_ts]
        if off_chain:
            log.critical("[%s] held leg(s) %s are OFF the NFO chain — already "
                         "delisted/expired; cannot quote to auto-flatten. "
                         "OPERATOR: square off manually.", label,
                         ", ".join(off_chain))
            stranded.append(label)
            continue
        try:
            if legs_expire_on(s, expiry_by_ts, today, log):
                log.info("[%s] expiry flatten — leg expires on/before today", label)
                flatten_one(s, log, reason="EXPIRY")
        except Exception as e:
            log.exception("[%s] expiry check/flatten failed: %s", label, e)
            stranded.append(label)
    if stranded:
        raise RuntimeError(
            f"{len(stranded)} pair(s) hold legs that could not be auto-flattened "
            f"on/after expiry: {', '.join(stranded)}. State+EOD are persisted; "
            "runner exits non-zero so notify-failure@ alerts the operator (H18).")


# ──────────────────────────────────────────────────────────────────
# Tick + halt
# ──────────────────────────────────────────────────────────────────
def tick_one(strategy, log: logging.Logger, *, halt_new: bool) -> bool:
    """One pair's tick. Returns True if it errored (caught) — the caller feeds
    the count to the silent-fail heartbeat. HALT_ALL is handled by the caller
    (it skips ticking entirely)."""
    label = f"{strategy.symbol_a}/{strategy.symbol_b}"
    errored = False
    if not halt_new:
        try:
            props = strategy.scan_and_propose()
            if props:
                strategy.execute_proposals(props)
        except Exception as e:
            errored = True
            log.exception("[%s] scan failed: %s", label, e)
    try:
        rh = strategy.check_and_rehedge()
        if rh:
            strategy.execute_proposals(rh)
    except Exception as e:
        errored = True
        log.exception("[%s] rehedge failed: %s", label, e)
    return errored


def _setup_logging(today: date) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(LOG_DIR / f"paper-kalman-pairs-{today.isoformat()}.log")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(h)
    logger.addHandler(logging.StreamHandler())
    return logger


def _write_config(args) -> str:
    """Write a derived config with the [kalman_pair_trading] section the
    constructor needs (config.ini may not carry it). CLI knobs win."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    if not cfg.has_section("strategy"):
        cfg.add_section("strategy")
    cfg["kalman_pair_trading"] = {
        "entry_z": str(args.entry_z), "exit_z": str(args.exit_z),
        "stop_z": str(args.stop_z), "lookback_days": str(args.lookback_days),
        "max_holding_days": str(args.max_holding_days),
        "lots_per_leg": str(args.lots_per_leg),
        "max_leg_notional": str(args.max_leg_notional),
        "min_edge_multiplier": str(args.min_edge_multiplier),
        "adf_gate_p": str(args.adf_gate_p),
        "adf_gate_window": str(args.adf_gate_window),
    }
    out = DATA_CACHE / "config_kalman_derived.ini"
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        cfg.write(f)
    return str(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Kalman pair-trading paper runner")
    p.add_argument("--top", type=int, default=10)
    # Defaults re-based on Palomar Ch.15 (thresholded strategy, book s₀=1, exit at
    # the mean, 6-month z-lookback) + the ADF regime gate — see
    # tasks/kalman-pairs-rebase-plan.md. Was entry 2.0 / exit 0.75 / lookback 60.
    p.add_argument("--entry-z", type=float, default=1.0)
    p.add_argument("--exit-z", type=float, default=0.0)
    # stop_z=4.0: a tighter stop exits mean-reversion winners before they revert
    # (backtest in-regime +290k @4.0 vs +8k @2.5); the regime gate handles
    # adverse-regime protection. See tasks/kalman-pairs-rebase-plan.md.
    p.add_argument("--stop-z", type=float, default=4.0)
    p.add_argument("--lookback", type=int, default=126, dest="lookback_days")
    p.add_argument("--max-hold", type=int, default=7, dest="max_holding_days")
    p.add_argument("--lots-per-leg", type=int, default=1)
    p.add_argument("--max-leg-notional", type=float, default=1_000_000)
    p.add_argument("--min-edge-multiplier", type=float, default=1.5)
    # Regime gate: only enter when the raw cointegration residual is stationary
    # (ADF p ≤ adf-gate-p) over the last adf-gate-window days. 0 disables it.
    p.add_argument("--adf-gate-p", type=float, default=0.05)
    p.add_argument("--adf-gate-window", type=int, default=60)
    p.add_argument("--candidates", default=str(CANDIDATES_PATH))
    p.add_argument("--force", action="store_true",
                   help="run even on a weekend/holiday (testing)")
    args = p.parse_args()

    today = date.today()
    log = _setup_logging(today)
    assert_timezone_ist(log)
    assert_disk_space_ok([DATA_CACHE, LOG_DIR], log)
    holidays = load_holidays(HOLIDAYS_PATH)
    if not args.force:
        ok, reason = is_trading_day(today, holidays)
        if not ok:
            log.info("Not a trading day (%s) — exiting.", reason)
            return 0

    # Hard-stop guard BEFORE auth. A post-15:30 invocation — e.g. an evening
    # `systemctl enable --now` catch-up fire (timer is Persistent=true) — must
    # exit WITHOUT a fresh Kite login, which would otherwise invalidate the
    # cached session the live runner reuses (no-auth-while-live-runner). The
    # session-timing checks after setup repeat this for the normal pre-open path.
    now0 = datetime.now()
    if now0 >= now0.replace(hour=HARD_STOP[0], minute=HARD_STOP[1],
                            second=0, microsecond=0):
        log.info("Started after hard stop %02d:%02d IST — nothing to do (no auth).",
                 *HARD_STOP)
        return 0

    # Assigned to a local that lives for main()'s scope so the FD stays
    # open (lock released on process exit, including SIGKILL).
    lock_fd = acquire_lock(LOCK_PATH, log, label="kalman_pairs")  # noqa: F841
    install_signal_handlers(log)

    pairs = pd.read_csv(args.candidates).sort_values("rank_score").head(args.top)
    config_path = _write_config(args)

    from kite_auth import KiteAuthManager
    from kite_throttle import KiteRateLimiter, throttle_kite
    auth = KiteAuthManager(CONFIG_PATH)
    kite = throttle_kite(auth.get_kite(), KiteRateLimiter(rate_per_sec=8.0, burst=8))
    prof = kite.profile()
    log.info("Authenticated as %s (%s)", prof["user_name"], prof["user_id"])
    nfo = kite.instruments("NFO") or []
    log.info("Prefetched %d NFO rows", len(nfo))

    panel_symbols = sorted(set(pairs["symbol_a"]) | set(pairs["symbol_b"]))
    panel = load_front_month_panel(panel_symbols, min_coverage=0.5)
    strategies = build_strategies(pairs, panel, nfo, kite, config_path, today, log)
    restore_matching(strategies, load_prior_state(log), log)
    catch_up_filters(strategies, panel, today, log)

    now = datetime.now()
    open_ts = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    end_ts = now.replace(hour=SESSION_END_AT[0], minute=SESSION_END_AT[1], second=0, microsecond=0)
    hard_ts = now.replace(hour=HARD_STOP[0], minute=HARD_STOP[1], second=0, microsecond=0)
    if now >= hard_ts:
        log.info("Started after hard stop — nothing to do.")
        return 0
    if now < open_ts:
        log.info("Sleeping until market open %s", open_ts.strftime("%H:%M"))
        sleep_until(open_ts, log)

    heartbeat = HeartbeatTracker(SILENT_FAIL_THRESHOLD, SILENT_FAIL_PATH, log)
    log.info("Entering tick loop (%d pairs) until %s", len(strategies),
             end_ts.strftime("%H:%M"))
    while datetime.now() < end_ts:
        halt_all = HALT_ALL_PATH.exists()
        halt_new = halt_all or HALT_NEW_ENTRIES_PATH.exists()
        errored = 0
        if not halt_all:
            for s in strategies:
                if tick_one(s, log, halt_new=halt_new):
                    errored += 1
        ran = 0 if halt_all else len(strategies)
        if heartbeat.record_tick(ran, errored):
            log.critical("Silent-fail threshold hit — exiting non-zero.")
            return 1
        write_state_file(strategies, log, archive=False)  # crash-safe intraday persist
        sleep_until(min(datetime.now() + timedelta(seconds=TICK_SECONDS), end_ts), log)

    # Session close: flatten any expiring/expired leg first (so state + EOD
    # reflect the post-flatten book and we never carry an STF into settlement),
    # then advance each filter one daily step, then persist + EOD. H18: if the
    # flatten can't fully verify/square a book it raises — we still persist
    # state+EOD (so the next runner isn't blind) BEFORE re-raising non-zero.
    log.info("Session end — expiry flatten, then stepping filters on close.")
    expiry_error: Optional[Exception] = None
    try:
        flatten_expiring_legs(strategies, nfo, today, log)
    except Exception as e:
        expiry_error = e
        log.exception("expiry flatten could not complete; persisting state+EOD "
                      "before exiting non-zero so the operator is alerted")
    step_filters_on_close(strategies, today, log)
    write_state_file(strategies, log)
    write_eod_sidecar(strategies, today, log)
    if expiry_error is not None:
        raise expiry_error
    log.info("Kalman paper session complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
