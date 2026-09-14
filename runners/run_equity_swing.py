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

Note: pending-entry fills (``equity_pending_entries``) only drain in
``--mode paper``. A ``--mode signals`` dry-run leaves PENDING rows
untouched; they age out to SKIPPED_STALE after the max-age window.

Per-day logfile under ``logs/equity-YYYY-MM-DD.log``. Exits 0 on a
clean run, 1 on errors that didn't bring the process down.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent.parent

CONFIG_PATH = str(HERE / "config.ini")
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"

# Shared pre-flight gates (audit 2.1). The equity scan runner kept its own
# holiday helpers (left in place — private, different signatures) but
# lacked the tz/disk pre-flights and a single-instance lock; add those.
from core.runner_common import (  # noqa: E402
    HOLIDAYS_PATH,
    acquire_lock,
    assert_disk_space_ok,
    assert_timezone_ist,
)


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


# EQ-FU-2: constants moved to strategies/varsity_equity_swing.py so the
# backtester shares the same filter (autoresearch trade-count would
# otherwise overshoot live by the count of would-be-gap-skipped signals).
from strategies.varsity_equity_swing import (
    PENDING_GAP_ATR_THRESHOLD as _PENDING_GAP_ATR_THRESHOLD,
    PENDING_MAX_AGE_DAYS as _PENDING_MAX_AGE_DAYS,
)


def _scalar_open_from_panel(f: pd.DataFrame, today_ts: pd.Timestamp) -> float:
    """Look up today's open price as a scalar; raise if the panel is malformed.

    Defends against the duplicate-date pathology where ``f.loc[today_ts, "open"]``
    returns a Series (multiple rows with same index value) — that's a panel bug,
    not a market event, and we surface it loudly instead of crashing on
    ``float(Series)``.
    """
    val = f.loc[today_ts, "open"]
    if isinstance(val, pd.Series):
        if len(val) == 1:
            val = val.iloc[0]
        else:
            raise ValueError(
                f"panel has {len(val)} rows for {today_ts.date()} — "
                "duplicate-date ingest, refusing to fill"
            )
    return float(val)


def _fill_pending_entries(strategy, today: date, scan_kind: str, log: logging.Logger) -> tuple[int, int, int, int]:
    """Materialize PENDING entry rows into open positions at today's open.

    Re-anchors SL/target to the actual fill price using the signal's stored
    sl_distance / target_distance. Each row resolves to one of FILLED /
    SKIPPED_GAP / SKIPPED_STALE / SKIPPED_OPEN in the DB. The per-row
    try/except ensures a single bad row never aborts the whole batch
    (CLAUDE.md Rule 12). Returns ``(filled, skipped_gap, skipped_stale,
    skipped_open)``.
    """
    from backend import db
    from strategies.varsity_equity_swing import EquityPosition

    pending = db.list_equity_pending_entries(status="PENDING")
    if not pending:
        return 0, 0, 0, 0

    today_ts = pd.Timestamp(today)
    filled = skipped_gap = skipped_stale = skipped_open = 0
    # Bind the attribute up front so partial-progress mutations propagate
    # even if a later row raises — prevents _persist_proposals from
    # double-inserting an equity_positions row we already created.
    db_ids = getattr(strategy, "_db_id_by_symbol", None)
    if db_ids is None:
        db_ids = {}
        strategy._db_id_by_symbol = db_ids

    for row in pending:
        sym = row["symbol"]
        try:
            signal_dt = date.fromisoformat(row["signal_dt"])
            age_days = (today - signal_dt).days

            if age_days > _PENDING_MAX_AGE_DAYS:
                note = f"signal aged {age_days}d > {_PENDING_MAX_AGE_DAYS}d max"
                db.update_equity_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            if sym in strategy.positions:
                note = "symbol already has an open position; dropping pending"
                db.update_equity_pending_entry_status(row["id"], "SKIPPED_OPEN", note=note)
                log.info("[PENDING SKIP] %s — %s", sym, note)
                skipped_open += 1
                continue

            f = strategy._features.get(sym)
            if f is None or today_ts not in f.index:
                note = f"no panel row for {today} (symbol dropped from universe or bhavcopy gap)"
                db.update_equity_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            open_px = _scalar_open_from_panel(f, today_ts)
            if not math.isfinite(open_px) or open_px <= 0:
                note = f"non-positive or non-finite open price ({open_px}) — corrupt bar"
                db.update_equity_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            atr = float(row["atr"])
            signal_close = float(row["signal_close"])
            sl_distance = float(row["sl_distance"])
            target_distance = float(row["target_distance"])

            # Defense in depth: NaN/inf at any of these inputs means a poison
            # row got past _queue_pending_entries' validation (or was hand-
            # INSERTed). Fail loud rather than open a position with NaN SL.
            if not all(math.isfinite(x) and x > 0
                       for x in (atr, signal_close, sl_distance, target_distance)):
                note = (f"non-finite stored fields "
                        f"(atr={atr}, close={signal_close}, "
                        f"sl_d={sl_distance}, tgt_d={target_distance})")
                db.update_equity_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            gap_atr = abs(open_px - signal_close) / atr

            if gap_atr > _PENDING_GAP_ATR_THRESHOLD:
                note = (f"gap {gap_atr:.2f}×ATR exceeds {_PENDING_GAP_ATR_THRESHOLD}×; "
                        f"open=₹{open_px:.2f} signal_close=₹{signal_close:.2f}")
                db.update_equity_pending_entry_status(row["id"], "SKIPPED_GAP", note=note)
                log.info("[PENDING SKIP] %s — %s", sym, note)
                skipped_gap += 1
                continue

            sl = open_px - sl_distance
            target = open_px + target_distance
            rationale = (row["rationale"] or "")
            if rationale:
                rationale += "; "
            rationale += (f"filled at next-day open ₹{open_px:.2f} "
                          f"(signal {signal_dt} close ₹{signal_close:.2f}, "
                          f"gap {gap_atr:.2f}×ATR)")

            pos = EquityPosition(
                symbol=sym, side=row["side"],
                entry_dt=today_ts,
                entry_px=open_px,
                qty=int(row["qty"]),
                initial_sl=sl, target=target,
                atr_at_entry=atr,
                rationale=rationale,
            )
            # Seed last_mtm_dt to today's bar date — otherwise _persist_proposals
            # would later write wall-clock now() into a column meant to hold the
            # bar date, breaking staleness dashboards.
            pos.last_mtm_dt = today_ts
            strategy.positions[sym] = pos

            pid = db.fill_pending_entry(
                {
                    "symbol": pos.symbol, "side": pos.side,
                    "entry_dt": pos.entry_dt.isoformat(),
                    "entry_px": pos.entry_px, "qty": pos.qty,
                    "initial_sl": pos.initial_sl, "current_sl": pos.current_sl,
                    "target": pos.target, "atr_at_entry": pos.atr_at_entry,
                    "rationale": pos.rationale,
                    "last_mtm_dt": pos.last_mtm_dt.isoformat(),
                    "last_mtm_px": pos.last_mtm_px,
                    "high_watermark": pos.high_watermark,
                },
                opened_by_scan=scan_kind,
                pending_id=row["id"],
                fill_px=open_px,
            )
            db_ids[sym] = pid
            log.info("[PENDING FILL] id=%d %s qty=%d @ ₹%.2f SL=₹%.2f TGT=₹%.2f "
                     "(signal %s close ₹%.2f, gap %.2f×ATR)",
                     pid, sym, pos.qty, open_px, sl, target,
                     signal_dt, signal_close, gap_atr)
            filled += 1
        except Exception as e:
            # Per-row isolation: log + mark SKIPPED_STALE with the error so the
            # row doesn't get retried tomorrow with the same fault.
            note = f"unhandled error: {type(e).__name__}: {e}"
            log.exception("[PENDING ERROR] %s — %s", sym, note)
            try:
                db.update_equity_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
            except Exception:
                log.exception("[PENDING ERROR] %s — also failed to mark row", sym)
            skipped_stale += 1

    return filled, skipped_gap, skipped_stale, skipped_open


_REQUIRED_SNAPSHOT_KEYS = ("atr", "entry", "sl", "target")


def _queue_pending_entries(proposals, today: date, log: logging.Logger) -> int:
    """Persist today's entry proposals to ``equity_pending_entries``.

    Replaces direct ``execute_proposals`` for entries — fills happen at the
    *next* close-scan run using that day's official open. Dedupes against
    existing PENDING rows so a same-day re-run doesn't double-queue.

    Required snapshot keys (atr/entry/sl/target) are validated loudly;
    a missing key raises KeyError so a strategy-side refactor that drops
    one becomes a visible crash, not silently-dropped signals.
    """
    from backend import db

    n = 0
    for prop in proposals:
        sym = prop.tradingsymbol
        if db.has_pending_entry_for_symbol(sym):
            log.info("[PENDING DEDUPE] %s already pending — skipping new signal", sym)
            continue

        snap = prop.greeks_snapshot or {}
        missing = [k for k in _REQUIRED_SNAPSHOT_KEYS if k not in snap]
        if missing:
            raise KeyError(
                f"proposal for {sym} missing required greeks_snapshot keys "
                f"{missing}; strategy contract violated — refusing to queue"
            )

        atr = float(snap["atr"])
        signal_close = float(snap["entry"])
        sl_distance = signal_close - float(snap["sl"])
        target_distance = float(snap["target"]) - signal_close

        # Reject NaN/inf as well as non-positive — NaN <= 0 is False so the
        # naive guard would let it through and produce a position with NaN
        # SL/target that check_and_rehedge can never exit (NaN comparisons
        # are always False).
        if not all(math.isfinite(x) and x > 0
                   for x in (atr, sl_distance, target_distance)):
            log.warning("[PENDING SKIP] %s — invalid metrics "
                        "(atr=%s sl_d=%s tgt_d=%s); proposal dropped",
                        sym, atr, sl_distance, target_distance)
            continue

        pid = db.insert_equity_pending_entry(
            signal_dt=today.isoformat(),
            symbol=sym,
            side="LONG",
            signal_close=signal_close,
            sl_distance=sl_distance,
            target_distance=target_distance,
            atr=atr,
            qty=int(prop.quantity),
            rationale=prop.rationale,
        )
        log.info("[PENDING QUEUE] id=%d %s qty=%d signal_close=₹%.2f "
                 "sl_d=₹%.2f tgt_d=₹%.2f — fills at next session open",
                 pid, sym, prop.quantity, signal_close, sl_distance, target_distance)
        n += 1
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
        log.info("[DB CLOSE] id=%d %s @ ₹%.2f reason=%s pnl=₹%s",
                 pid, pos.symbol, pos.exit_px or 0, pos.exit_reason, f"{pos.pnl:+,.0f}")
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

    # Pre-flight gates (audit 2.1 — protections this runner lacked). TZ
    # first (a wrong-TZ run misquotes the trading-day boundary; the unit
    # sets TZ=Asia/Kolkata); disk next (a full partition corrupts DB
    # writes). Both fail loud.
    assert_timezone_ist(log)
    assert_disk_space_ok([LOG_DIR, DATA_CACHE], log)

    _assert_holiday_data_fresh(today, log)
    ok, reason = _is_trading_day(today)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    # Single-instance lock, keyed by scan kind: two concurrent same-kind
    # scans would double-drain equity_pending_entries (double bookings).
    # open + close are different kinds and never overlap, so they don't
    # block each other. Held for the process lifetime via _lock_fd.
    _lock_fd = acquire_lock(  # noqa: F841
        DATA_CACHE / f".equity_swing_{args.scan}.lock", log,
        label=f"equity-swing runner (--scan={args.scan})",
    )

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
        from core.broker import get_trading_client
        kite = get_trading_client(CONFIG_PATH)
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
    strategy.log_effective_params()

    # Anchor the scan at today (close) or the most recent prior trading day
    # (open). Then verify the panel actually contains that date — otherwise
    # the strategy silently scans whatever stale date max() finds, hiding a
    # broken fetch cron. CLAUDE.md Rule 12: surface the failure.
    strategy.set_current_date(pd.Timestamp(today))
    strategy._ensure_features()
    if not strategy._features:
        log.error("EQ panel loaded zero symbols — equity_ohlcv/ cache is empty. "
                  "Run market_data/fetch_bhavcopy_eq.py first.")
        return 1
    panel_max = max(f.index.max() for f in strategy._features.values())
    panel_max_date = panel_max.date() if hasattr(panel_max, "date") else panel_max
    if args.scan == "close":
        # Close scan anchors to today's bar — fetch-bhavcopy-eq must have run.
        if panel_max_date < today:
            log.error(
                "EQ panel latest date is %s, expected today (%s). "
                "fetch-bhavcopy-eq.service likely failed or didn't run. "
                "Refusing to scan stale data — fix the panel and re-run.",
                panel_max_date, today,
            )
            return 1
    else:
        # Open scan runs before today's bhavcopy lands; anchor at the
        # panel's max date (= most recent prior trading day) and warn if
        # that's more than 4 calendar days behind (covers long weekends
        # and Diwali). 4 days = Fri→Tue worst case.
        days_behind = (today - panel_max_date).days
        if days_behind > 4:
            log.error(
                "EQ panel latest date is %s, more than 4 days behind today (%s). "
                "fetch-bhavcopy-eq.service has likely been failing. "
                "Refusing to scan — fix the panel and re-run.",
                panel_max_date, today,
            )
            return 1
        if days_behind > 1:
            log.warning("EQ panel is %d days behind today — proceeding with stale anchor",
                        days_behind)
        strategy.set_current_date(panel_max)

    # Resume open paper positions from the DB so SL/target/time-stop logic
    # has full state across cron runs.
    if args.mode == "paper":
        n_resumed = _load_open_positions_into_strategy(strategy, log)
        log.info("Resumed %d open paper positions from DB", n_resumed)

    n_closed_before = len(strategy.closed_positions)
    n_open_before_fill = len(strategy.positions)

    # Fill yesterday's queued entry signals at TODAY'S OPEN (from bhavcopy
    # panel). Close-scan only — open-scan doesn't have today's official
    # bhavcopy yet, so pending fills wait until evening.
    if args.scan == "close" and args.mode == "paper":
        f_filled, f_skip_gap, f_skip_stale, f_skip_open = _fill_pending_entries(
            strategy, today, args.scan, log)
        if f_filled or f_skip_gap or f_skip_stale or f_skip_open:
            log.info("Pending entries: %d filled, %d skipped (gap), "
                     "%d skipped (stale), %d skipped (already-open)",
                     f_filled, f_skip_gap, f_skip_stale, f_skip_open)

    # Run rehedge first (exit triggers), then scan (new entries). Same-day
    # SL/target hits on positions just filled at today's open are caught
    # here because check_and_rehedge walks today's bar's [low, high].
    try:
        exits = strategy.check_and_rehedge()
        if exits:
            log.info("rehedge produced %d exit proposal(s)", len(exits))
            strategy.execute_proposals(exits)
    except Exception as e:
        log.exception("check_and_rehedge failed: %s", e)

    n_signals = 0
    n_queued = 0
    if args.scan == "close":
        try:
            entries = strategy.scan_and_propose()
            n_signals = len(entries)
            if entries:
                log.info("scan produced %d entry proposal(s) — queueing for next-day open",
                         len(entries))
                if args.mode == "paper":
                    n_queued = _queue_pending_entries(entries, today, log)
                else:
                    # signals mode: still write JSONL via strategy
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
        # Net open delta = fills - same-day-closes. Use this for n_trades so a
        # position filled at today's open that exits same-day in rehedge
        # counts as ONE round-trip, not two events (n_filled + n_closed both
        # incremented). The naive sum was an over-counting bug.
        n_opens_net = max(0, len(strategy.positions) - n_open_before_fill)
        notes = None
        if n_queued:
            notes = f"queued {n_queued} pending entry(ies) for next session"
        db.insert_equity_scan(
            scan_dt=datetime.now().isoformat(),
            scan_kind=args.scan, mode=args.mode,
            n_signals=n_signals,
            n_trades=n_closed_today + n_opens_net,
            n_open_positions=len(strategy.positions),
            n_closed_today=n_closed_today,
            notes=notes,
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
