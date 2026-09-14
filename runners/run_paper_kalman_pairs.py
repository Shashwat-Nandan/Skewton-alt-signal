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

Mirrors runners/run_paper_pairs.py's operational scaffolding (TOTP auth, holiday/weekend
gate, 09:15→15:25 loop, shared HALT_* kill switches, atomic state persistence,
EOD sidecar, hourly heartbeat) but is its own system:
  - state  : data_cache/kalman_pairs_runner_state.json
  - EOD     : data_cache/pair_paper_kalman_eod_<date>.json
  - logfile : logs/paper-kalman-pairs-YYYY-MM-DD.log

The EOD sidecar uses runners/run_paper_pairs.py's `pair_paper_{system="kalman"}_eod`
convention, so "kalman" is a first-class system in the existing read-only
tooling: the /pair-paper-compare dashboard tab and research/compare_paper_systems.py pick
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
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd

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
    assert_timezone_ist,
    durable_write_text,
    install_signal_handlers,
    is_trading_day,
    load_holidays,
    sleep_until,
)
from core._state_backup import archive_state_backup, assert_no_orphan_backups
from core.screen_pairs import load_front_month_panel
from strategies.kalman_pair_trading import KalmanPairStrategy

HERE = Path(__file__).resolve().parent.parent
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
#
# 2026-08-07: run_paper_pairs now scopes its notional cap to its own --system
# state file, so that half of the isolation no longer depends on this name.
# The H17 concentration counter still globs, and _aggregate_book_notional's
# unscoped path still exists, so the naming choice stands.
STATE_PATH = DATA_CACHE / "kalman_pairs_runner_state.json"
CANDIDATES_PATH = DATA_CACHE / "pair_candidates.csv"
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
def write_state_file(strategies, log: logging.Logger, *, archive: bool = True,
                     extra_blobs: Optional[List[Dict]] = None) -> None:
    """Atomic, power-loss-safe persist (write tmp → fsync → rename → fsync dir),
    then a timestamped backup ring so a corrupted file is recoverable (parity
    with run_paper_pairs). `archive=False` for the per-tick intraday persist so
    the 30-slot ring isn't churned every minute.

    `extra_blobs` are prior-session state blobs carried through verbatim: pairs
    that hold an OPEN position but could not be rebuilt this session. Persisting
    only `strategies` is what silently erased three open books on 2026-08-28
    when the universe was rebuilt (tasks/todo.md 2026-08-29)."""
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
    live = {tuple(b.get("pair", ())) for b in payload["pairs"]}
    for blob in extra_blobs or []:
        if tuple(blob.get("pair", ())) not in live:
            payload["pairs"].append(blob)
    durable_write_text(STATE_PATH, json.dumps(payload, default=str, indent=2))
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


def open_position_symbols(prior: Dict[str, Dict]) -> set:
    """Underlyings named by prior state blobs that hold an OPEN position."""
    out = set()
    for blob in prior.values():
        st = blob.get("state") or {}
        if st.get("position", "FLAT") != "FLAT" and st.get("legs"):
            out.update(blob.get("pair", ()))
    return out


def carry_open_positions(strategies, prior: Dict[str, Dict], panel: pd.DataFrame,
                         nfo: List[dict], kite, config_path: str, today: date,
                         log: logging.Logger):
    """Keep managing any prior pair that holds an OPEN position but has dropped
    out of today's ranked universe.

    The runner rebuilds `strategies` from the top-N candidates every morning and
    persists only those, so a pair that falls out while still holding legs was
    simply erased — on 2026-08-28 that removed three open books and −₹77,989
    from the record. The bias is one-directional: a pair that has been losing is
    exactly the one that drops out of a rank-ordered universe.

    Carried pairs are rebuilt and restored so they can be managed to an exit;
    NEW entries are blocked for them by the caller (they are not in today's
    universe on merit). A pair that cannot be rebuilt — gone from the panel, no
    front-month contract — is returned as an orphan blob to be persisted
    verbatim and surfaced CRITICAL, never dropped (Rule 12).

    Returns (carried_strategies, orphan_blobs).
    """
    live = {f"{s.symbol_a}/{s.symbol_b}" for s in strategies}
    stale_open = {
        k: b for k, b in prior.items()
        if k not in live
        and (b.get("state") or {}).get("position", "FLAT") != "FLAT"
        and (b.get("state") or {}).get("legs")
    }
    if not stale_open:
        return [], []

    carried, orphans = [], []
    for key, blob in sorted(stale_open.items()):
        a, b = blob["pair"]
        one = pd.DataFrame({"symbol_a": [a], "symbol_b": [b]})
        try:
            built = build_strategies(one, panel, nfo, kite, config_path, today, log)
        except Exception as e:
            log.warning("[%s] carry-over rebuild failed: %s", key, e)
            built = []
        if not built:
            log.critical(
                "[%s] holds an OPEN position but dropped out of the universe and "
                "could NOT be rebuilt (not in the panel, or no front-month "
                "contract). Its state is preserved as-is and it will NOT be "
                "managed this session. OPERATOR: square off manually.", key)
            orphans.append(blob)
            continue
        s = built[0]
        try:
            s.restore_state(blob)
        except Exception as e:
            log.exception("[%s] carry-over restore failed: %s — preserving blob",
                          key, e)
            orphans.append(blob)
            continue
        log.warning(
            "[%s] carried over: holds %s but is no longer in the top-N universe. "
            "Managed to EXIT only; new entries blocked.", key, s.state.position)
        carried.append(s)
    return carried, orphans


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


def warn_if_gate_stale(strategies, log: logging.Logger) -> List:
    """After catch_up_filters has replayed every available bhavcopy day, any pair
    whose regime gate is STILL stale is genuinely behind — the DATA is stale (a
    bhavcopy hole / a VPS-wide outage that also stalled the fetch), not just the
    runner. Surface it ONCE per restart here (code-review #65): this is the loud,
    non-false-alarm signal — unlike a WARN inside _refresh_regime_adf, which fires
    on every normal restart before catch_up refills. Entries for these pairs stay
    fail-closed until fresh closes land; exits are unaffected. Returns the stale
    strategies (for the caller/tests)."""
    stale = [s for s in strategies if s._gate_is_stale()]
    for s in stale:
        log.warning(
            "[%s/%s] regime gate STALE after catch-up: newest residual %d trading "
            "days old (≥ %d) — bhavcopy is behind; NEW entries fail-closed until "
            "fresh closes refill the window (exits unaffected). Issue #65.",
            s.symbol_a, s.symbol_b, s._gate_stale_trading_days(),
            s._STALE_GATE_MAX_TRADING_DAYS,
        )
    return stale


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
        # Only a REAL date lands in the map. A pd.NaT (its .date() is NaT, not a
        # date) or any other junk is dropped, so a leg with a malformed/empty
        # expiry shows as OFF the chain → stranded+raised by flatten_expiring_legs
        # rather than silently mapped to a value that fails `exp <= today`.
        if isinstance(exp, date):
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
    run_paper_pairs.flatten_one). No-op (logs) when already flat or quotes are
    missing — the caller MUST re-check the position to surface a flatten that did
    not complete (a missing quote, a swallowed execution error, or a rolled leg
    the strategy's fill path can't re-map all leave the book non-FLAT)."""
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
        manually (the runner persists state+EOD first — see main()).
      • An on-chain leg that flatten_one could NOT square (unquotable, a mid-fill
        error swallowed by flatten_one, or a rolled leg whose contract no longer
        matches the strategy's front-month so its fill path can't re-map it) →
        re-checked and stranded too, so a silent no-op, a half-closed book, or a
        mis-priced rolled leg never passes as success."""
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
                # flatten_one logs-and-returns on an unquotable leg and swallows
                # execution errors, so a no-op or a HALF-closed book would
                # otherwise pass silently. Re-verify the pair actually reached
                # FLAT; if not, strand it so the runner raises (H18) instead of
                # carrying an open/naked leg into settlement.
                if s.state.position != "FLAT" or s.state.legs:
                    log.critical("[%s] expiry flatten did NOT reach FLAT (pos=%s, "
                                 "%d leg(s) left) — could not square off; OPERATOR "
                                 "must close manually before settlement.", label,
                                 s.state.position, len(s.state.legs))
                    stranded.append(label)
        except Exception as e:
            log.exception("[%s] expiry check/flatten failed: %s", label, e)
            stranded.append(label)
    if stranded:
        raise RuntimeError(
            f"{len(stranded)} pair(s) hold legs that could not be auto-flattened "
            f"on/after expiry: {', '.join(stranded)}. State+EOD are persisted; "
            "runner exits non-zero so notify-failure@ alerts the operator (H18).")


def _calendar_days_until_next_trading_day(today: date, holidays: set) -> int:
    """Calendar days until the next NSE trading day (today excluded), capped at 10
    (parity with run_paper_pairs)."""
    d = today
    for step in range(1, 11):
        d = d + timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            return step
    return 10


def warn_if_long_break(strategies, today: date, holidays: set,
                       log: logging.Logger) -> None:
    """M-R1 parity: this runner does NOT flatten non-expiring positions at session
    end, so an open book sits unmonitored across a long weekend / holiday block.
    Warn when the next trading day is ≥3 calendar days away and any pair is open,
    so the operator can square off manually before the break."""
    if not holidays:
        return
    gap = _calendar_days_until_next_trading_day(today, holidays)
    open_pairs = [f"{s.symbol_a}/{s.symbol_b}" for s in strategies
                  if s.state.position != "FLAT"]
    if gap >= 3 and open_pairs:
        log.warning("M-R1: next trading day is %d calendar days away and %d "
                    "pair(s) hold open positions: %s — they will sit unmonitored "
                    "across the break; consider squaring off manually.",
                    gap, len(open_pairs), ", ".join(open_pairs))


# ──────────────────────────────────────────────────────────────────
# Entry suppression near expiry (issue #70)
# ──────────────────────────────────────────────────────────────────
# Don't OPEN a new position when the front-month future is within `cutoff_days`
# of expiry: a trade opened that close to expiry has almost no room to revert
# before the contract dies and gets expiry-flattened — churn/cost for no edge.
# NOTE this is a near-expiry guard, NOT a "the trade can complete its max-hold"
# guarantee: max_holding_days (default 7 TRADING days ≈ 9-11 calendar days) far
# exceeds the default 3-calendar-day cutoff, so a trade opened 4-10 days out can
# still be cut short by expiry. Guaranteeing max-hold would need cutoff ≈
# max_holding_days in calendar days; the small default is a deliberately light
# touch (operator-tunable via --entry-cutoff-days).
# We suppress the ENTRY rather than roll the contract — the signal (γ / z-window)
# is trained on the FRONT-month STF panel (screen_pairs.load_front_month_panel),
# so trading the next month would measure a next-month quote against a front-month
# mean/std (calendar-basis contamination). Held positions are untouched:
# suppression rides the existing `halt_new` path, which blocks scan_and_propose
# (entries) but still runs check_and_rehedge (exits/rehedge), so an open
# near-expiry pair keeps exiting and is squared by flatten_expiring_legs.
def entry_suppressed(strategies, nfo: List[dict], today: date,
                     cutoff_days: int) -> set:
    """Set of strategies whose front-month future (the contract they'd enter on)
    expires within `cutoff_days` calendar days of today — NEW entries suppressed.
    cutoff_days <= 0 disables. A leg whose contract isn't on the chain is ignored
    (can't date it); the expiry-flatten path handles already-expired contracts."""
    if cutoff_days <= 0:
        return set()
    expiry_by_ts = _expiry_by_tradingsymbol(nfo)
    out = set()
    for s in strategies:
        exps = [expiry_by_ts.get(s.tradingsymbol_a), expiry_by_ts.get(s.tradingsymbol_b)]
        exps = [e for e in exps if e is not None]
        if exps and (min(exps) - today).days <= cutoff_days:
            out.add(s)
    return out


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
        "max_net_exposure_pct": str(args.max_net_exposure_pct),
    }
    out = DATA_CACHE / "config_kalman_derived.ini"
    DATA_CACHE.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        cfg.write(f)
    return str(out)


def build_parser() -> argparse.ArgumentParser:
    """The runner's argument parser. Exposed (not inlined in main) so the
    argparse defaults — the values that ACTUALLY govern the live paper runner,
    since the deploy unit passes no threshold flags and _write_config writes
    these into the derived config — are inspectable by the sync test that pins
    them to the strategy/backtest/config-template defaults (they had drifted
    silently as five independent literals)."""
    p = argparse.ArgumentParser(description="Kalman pair-trading paper runner")
    p.add_argument("--top", type=int, default=10)
    # Defaults re-based on Palomar Ch.15 (thresholded strategy, exit at the mean,
    # 6-month z-lookback) + the ADF regime gate — see tasks/kalman-pairs-rebase-
    # plan.md. Was entry 2.0 / exit 0.75 / lookback 60. Entry raised 1.0→1.5 by
    # the 2026-07-04 5-min revalidation (book s₀=1 churns on intraday noise;
    # 1.5 won both half-windows — see plan's 5-MIN REVALIDATION section).
    p.add_argument("--entry-z", type=float, default=1.5)
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
    p.add_argument("--entry-cutoff-days", type=int, default=3,
                   help="suppress NEW entries when the front-month future is "
                        "within this many calendar days of expiry (issue #70); "
                        "0 disables. Held positions still exit/flatten normally.")
    p.add_argument("--max-net-exposure-pct", type=float, default=1.0,
                   help="Refuse an entry whose |net notional| / gross notional "
                        "exceeds this. A same-side (γ<0) structure scores 1.0 — "
                        "it is a directional basket, not a hedge — while opposed "
                        "pairs measured 0.03-0.31 on the real book. Default 1.0 "
                        "= OFF; net exposure is logged on every entry either way.")
    p.add_argument("--candidates", default=str(CANDIDATES_PATH))
    p.add_argument("--force", action="store_true",
                   help="run even on a weekend/holiday (testing)")
    return p


def main() -> int:
    args = build_parser().parse_args()

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

    from core.broker import get_trading_client
    from core.kite_throttle import KiteRateLimiter, throttle_kite
    kite = throttle_kite(
        get_trading_client(CONFIG_PATH), KiteRateLimiter(rate_per_sec=8.0, burst=8),
    )
    prof = kite.profile()
    log.info("Authenticated as %s (%s)", prof["user_name"], prof["user_id"])
    nfo = kite.instruments("NFO") or []
    log.info("Prefetched %d NFO rows", len(nfo))

    prior = load_prior_state(log)
    # The panel must also span pairs that hold an OPEN position but have fallen
    # out of today's universe — without their columns carry_open_positions can't
    # rebuild them and they'd all orphan. load_front_month_panel drops symbols
    # under min_coverage before its dropna, so a genuinely gappy carry-over is
    # excluded rather than truncating the panel for everyone.
    panel_symbols = sorted(set(pairs["symbol_a"]) | set(pairs["symbol_b"])
                           | open_position_symbols(prior))
    panel = load_front_month_panel(panel_symbols, min_coverage=0.5)
    strategies = build_strategies(pairs, panel, nfo, kite, config_path, today, log)
    restore_matching(strategies, prior, log)
    # A pair that dropped out of today's ranked universe while still holding a
    # position must keep being managed to an exit — persisting only the top-N is
    # what erased three open books on 2026-08-28. Carried pairs are exit-only.
    carried, orphan_blobs = carry_open_positions(
        strategies, prior, panel, nfo, kite, config_path, today, log)
    strategies.extend(carried)
    catch_up_filters(strategies, panel, today, log)
    warn_if_gate_stale(strategies, log)   # #65: loud once-per-restart stale signal

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

    # Entry suppression near front-month expiry (issue #70). Expiries are static
    # for the session, so resolve the suppressed set once. Held pairs still exit.
    entry_block = entry_suppressed(strategies, nfo, today, args.entry_cutoff_days)
    # Carried-over pairs are managed to EXIT only: they are not in today's
    # universe on merit, so they must never open a NEW position.
    entry_block |= set(carried)
    if args.entry_cutoff_days > 0:
        log.info("Entry cutoff armed: %dd before front-month expiry; suppressed "
                 "today: %s", args.entry_cutoff_days,
                 ", ".join(f"{s.symbol_a}/{s.symbol_b}" for s in entry_block) or "none")

    heartbeat = HeartbeatTracker(SILENT_FAIL_THRESHOLD, SILENT_FAIL_PATH, log)
    log.info("Entering tick loop (%d pairs) until %s", len(strategies),
             end_ts.strftime("%H:%M"))
    while datetime.now() < end_ts:
        halt_all = HALT_ALL_PATH.exists()
        halt_new = halt_all or HALT_NEW_ENTRIES_PATH.exists()
        errored = 0
        if not halt_all:
            for s in strategies:
                if tick_one(s, log, halt_new=halt_new or s in entry_block):
                    errored += 1
        ran = 0 if halt_all else len(strategies)
        if heartbeat.record_tick(ran, errored):
            log.critical("Silent-fail threshold hit — exiting non-zero.")
            return 1
        write_state_file(strategies, log, archive=False,
                         extra_blobs=orphan_blobs)  # crash-safe intraday persist
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
    warn_if_long_break(strategies, today, holidays, log)
    step_filters_on_close(strategies, today, log)
    write_state_file(strategies, log, extra_blobs=orphan_blobs)
    write_eod_sidecar(strategies, today, log)
    if expiry_error is not None:
        raise expiry_error
    log.info("Kalman paper session complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
