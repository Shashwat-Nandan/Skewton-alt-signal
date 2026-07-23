#!/usr/bin/env python3
"""
Delivery-accumulation scan runner — invoked twice daily by systemd timers.

Clone of ``runners/run_equity_swing.py`` for
``strategies/delivery_accumulation.py`` (see that module's PHASE-D
PRECONDITION note: this runner implements the next-day-OPEN pending-entry
queue with gap-skip / max-age filters, so the paper fill model equals the
backtested one — proposals are never driven straight into
``execute_proposals``).

Two scan kinds, both run by the same script with ``--scan {open|close}``:

  open  -- 09:35 IST. Exits-only against the panel's most recent bar.
  close -- 18:45 IST, after fetch-bhavcopy-eq (18:00). Fills yesterday's
           queued entries at today's official open, runs exits, scans for
           new entries and queues them for the next session's open.

Delivery-data timing: the strategy lags delivery features by
``deliv_lag_days`` (default 1), so the 18:45 close scan needs day D−1
delivery — fetched by fetch-deliv.timer at 19:45 the previous evening.
A stale delivery cache is surfaced LOUDLY but does not block the run:
entry signals degrade to none (NaN percentile can't create a position),
while exits — the risk-reducing side — must still execute.

Mode selection: ``--mode signals`` (JSONL only) | ``--mode paper``
(persist to delivery_positions / delivery_scans / delivery_pending_entries).
Live mode is intentionally absent; the strategy raises if asked.

Per-day logfile under ``logs/delivery-YYYY-MM-DD.log``.
"""
from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent.parent

CONFIG_PATH = str(HERE / "config.ini")
LOG_DIR = HERE / "logs"
DATA_CACHE = HERE / "data_cache"
DELIV_CACHE = DATA_CACHE / "equity_delivery"

from core.runner_common import (  # noqa: E402
    HOLIDAYS_PATH,
    acquire_lock,
    assert_disk_space_ok,
    assert_holiday_data_fresh,
    assert_timezone_ist,
    is_trading_day,
    load_holidays,
)

# EQ-FU-2: shared fill-filter constants (re-exported by the strategy module
# so runner and backtester consume one truth).
from strategies.delivery_accumulation import (  # noqa: E402
    PENDING_GAP_ATR_THRESHOLD as _PENDING_GAP_ATR_THRESHOLD,
    PENDING_MAX_AGE_DAYS as _PENDING_MAX_AGE_DAYS,
)


def _setup_logging(today: date) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"delivery-{today.isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(logfile)],
        force=True,
    )
    return logging.getLogger("run_delivery_accum")


RAW_DELIV_DIR = DATA_CACHE / "deliv_raw"


def _warn_if_delivery_stale(today: date, log: logging.Logger) -> None:
    """LOUD (not fatal) staleness check on the delivery data.

    A stale cache makes the strategy silently inert (the BANKNIFTY-Taleb
    lesson: zero trades for 9 sessions before anyone noticed), so surface
    it every close scan; but never block the run — exits must still fire.

    Reads fetch_deliv's raw day cache FILENAMES (deliv_YYYYMMDD.parquet) —
    deterministic and free, unlike sampling per-symbol parquets whose glob
    order is arbitrary (code-review 2026-07-22). The whole body is inside
    try/except: this guard must never be able to abort the run.
    """
    try:
        latest: date | None = None
        for p in RAW_DELIV_DIR.glob("deliv_*.parquet"):
            try:
                d = datetime.strptime(p.stem.split("_")[1], "%Y%m%d").date()
            except (IndexError, ValueError):
                continue
            if latest is None or d > latest:
                latest = d
        if latest is None:
            log.critical("delivery raw cache EMPTY (%s) — strategy is inert; "
                         "run market_data.fetch_deliv", RAW_DELIV_DIR)
            return
        behind = (today - latest).days
        if behind > 4:
            log.critical(
                "delivery data latest day %s is %dd behind today (%s) — "
                "fetch-deliv.timer likely failing; deliv_pctile is NaN and "
                "the strategy is INERT (no entries) until refreshed",
                latest, behind, today,
            )
    except Exception as e:
        log.critical("delivery staleness check itself failed (%s) — treat "
                     "the delivery cache as suspect; run continues", e)


def _load_open_positions_into_strategy(strategy, log: logging.Logger) -> tuple[int, int]:
    """Resume in-memory position state from dashboard.db on startup.

    Returns ``(n_resumed, n_bad)``. A row that fails to parse is a paging
    condition, not a shrug (code-review 2026-07-22): the row stays OPEN in
    the DB forever (phantom P&L) while its symbol's slot frees up in memory
    — a new fill for the same symbol would double the exposure. The caller
    turns ``n_bad > 0`` into a non-zero exit so OnFailure notifies."""
    from backend import db
    from strategies.varsity_equity_swing import EquityPosition
    rows = db.list_delivery_positions(status="OPEN")
    n = n_bad = 0
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
            n_bad += 1
            log.error(
                "OPEN delivery_positions row id=%s (%s) failed to parse: %s — "
                "position is UNMANAGED (no exits/MTM) and its symbol slot is "
                "free for double-entry. Repair or close the row in the DB.",
                r.get("id"), r.get("symbol"), e,
            )
    return n, n_bad


def _scalar_open_from_panel(f: pd.DataFrame, today_ts: pd.Timestamp) -> float:
    """Today's open as a scalar; loud on the duplicate-date pathology."""
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


def _fill_pending_entries(strategy, today: date, scan_kind: str,
                          log: logging.Logger) -> tuple[int, int, int, int]:
    """Materialize PENDING rows into open positions at today's open.

    Same filters and per-row isolation as the equity-swing runner
    (EQ-FU-2): FILLED / SKIPPED_GAP / SKIPPED_STALE / SKIPPED_OPEN.
    """
    from backend import db
    from strategies.varsity_equity_swing import EquityPosition

    pending = db.list_delivery_pending_entries(status="PENDING")
    if not pending:
        return 0, 0, 0, 0

    today_ts = pd.Timestamp(today)
    filled = skipped_gap = skipped_stale = skipped_open = 0
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
                db.update_delivery_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            if sym in strategy.positions:
                note = "symbol already has an open position; dropping pending"
                db.update_delivery_pending_entry_status(row["id"], "SKIPPED_OPEN", note=note)
                log.info("[PENDING SKIP] %s — %s", sym, note)
                skipped_open += 1
                continue

            f = strategy._features.get(sym)
            if f is None or today_ts not in f.index:
                note = f"no panel row for {today} (symbol dropped or bhavcopy gap)"
                db.update_delivery_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            open_px = _scalar_open_from_panel(f, today_ts)
            if not math.isfinite(open_px) or open_px <= 0:
                note = f"non-positive or non-finite open price ({open_px}) — corrupt bar"
                db.update_delivery_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            atr = float(row["atr"])
            signal_close = float(row["signal_close"])
            sl_distance = float(row["sl_distance"])
            target_distance = float(row["target_distance"])

            if not all(math.isfinite(x) and x > 0
                       for x in (atr, signal_close, sl_distance, target_distance)):
                note = (f"non-finite stored fields (atr={atr}, close={signal_close}, "
                        f"sl_d={sl_distance}, tgt_d={target_distance})")
                db.update_delivery_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
                log.warning("[PENDING SKIP] %s — %s", sym, note)
                skipped_stale += 1
                continue

            gap_atr = abs(open_px - signal_close) / atr
            if gap_atr > _PENDING_GAP_ATR_THRESHOLD:
                note = (f"gap {gap_atr:.2f}×ATR exceeds {_PENDING_GAP_ATR_THRESHOLD}×; "
                        f"open=₹{open_px:.2f} signal_close=₹{signal_close:.2f}")
                db.update_delivery_pending_entry_status(row["id"], "SKIPPED_GAP", note=note)
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
            # NOTE: pos.last_mtm_dt is deliberately NOT seeded in memory —
            # check_and_rehedge only marks it after adjudicating a bar, and
            # the double-adjudication guard keys off it (code-review
            # 2026-07-22): a fill-time seed would suppress the same-day
            # SL/target check the backtest performs on the entry bar. The
            # DB row gets entry_dt (the bar date) for staleness dashboards.
            #
            # DB fill FIRST, in-memory book only after the transaction
            # commits — the reverse order left an in-memory position alive
            # after a DB error, which _persist_proposals then inserted as
            # OPEN while the audit row said SKIPPED (code-review 2026-07-22).
            pid = db.fill_delivery_pending_entry(
                {
                    "symbol": pos.symbol, "side": pos.side,
                    "entry_dt": pos.entry_dt.isoformat(),
                    "entry_px": pos.entry_px, "qty": pos.qty,
                    "initial_sl": pos.initial_sl, "current_sl": pos.current_sl,
                    "target": pos.target, "atr_at_entry": pos.atr_at_entry,
                    "rationale": pos.rationale,
                    "last_mtm_dt": pos.entry_dt.isoformat(),
                    "last_mtm_px": pos.last_mtm_px,
                    "high_watermark": pos.high_watermark,
                },
                opened_by_scan=scan_kind,
                pending_id=row["id"],
                fill_px=open_px,
            )
            strategy.positions[sym] = pos
            db_ids[sym] = pid
            log.info("[PENDING FILL] id=%d %s qty=%d @ ₹%.2f SL=₹%.2f TGT=₹%.2f "
                     "(signal %s close ₹%.2f, gap %.2f×ATR)",
                     pid, sym, pos.qty, open_px, sl, target,
                     signal_dt, signal_close, gap_atr)
            filled += 1
        except Exception as e:
            note = f"unhandled error: {type(e).__name__}: {e}"
            log.exception("[PENDING ERROR] %s — %s", sym, note)
            try:
                db.update_delivery_pending_entry_status(row["id"], "SKIPPED_STALE", note=note)
            except Exception:
                log.exception("[PENDING ERROR] %s — also failed to mark row", sym)
            skipped_stale += 1

    return filled, skipped_gap, skipped_stale, skipped_open


_REQUIRED_SNAPSHOT_KEYS = ("atr", "entry", "sl", "target")


def _queue_pending_entries(proposals, today: date, log: logging.Logger) -> tuple[int, int]:
    """Persist today's entry proposals to ``delivery_pending_entries``.

    Returns ``(n_queued, n_malformed)``. A contract-violating proposal is
    isolated per-row instead of raising (code-review 2026-07-22: one bad
    proposal used to abort the loop and silently drop every remaining
    valid entry for the day); the caller turns ``n_malformed > 0`` into a
    non-zero exit so the contract violation still pages.
    """
    from backend import db

    n = n_malformed = 0
    for prop in proposals:
        sym = prop.tradingsymbol
        if db.has_delivery_pending_entry_for_symbol(sym):
            log.info("[PENDING DEDUPE] %s already pending — skipping new signal", sym)
            continue

        snap = prop.greeks_snapshot or {}
        missing = [k for k in _REQUIRED_SNAPSHOT_KEYS if k not in snap]
        if missing:
            n_malformed += 1
            log.error(
                "proposal for %s missing required greeks_snapshot keys %s — "
                "strategy contract violated; proposal dropped, run will exit "
                "non-zero", sym, missing,
            )
            continue

        atr = float(snap["atr"])
        signal_close = float(snap["entry"])
        sl_distance = signal_close - float(snap["sl"])
        target_distance = float(snap["target"]) - signal_close

        if not all(math.isfinite(x) and x > 0
                   for x in (atr, sl_distance, target_distance)):
            log.warning("[PENDING SKIP] %s — invalid metrics "
                        "(atr=%s sl_d=%s tgt_d=%s); proposal dropped",
                        sym, atr, sl_distance, target_distance)
            continue

        pid = db.insert_delivery_pending_entry(
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
    return n, n_malformed


def _persist_proposals(strategy, scan_kind: str, log: logging.Logger) -> tuple[int, int]:
    """Mirror in-memory state into the delivery_* tables."""
    from backend import db
    db_ids = getattr(strategy, "_db_id_by_symbol", {})
    n_open = n_close = 0

    for sym, pos in strategy.positions.items():
        if sym in db_ids:
            db.update_delivery_position_mtm(
                position_id=db_ids[sym],
                last_mtm_dt=(pos.last_mtm_dt or datetime.now()).isoformat()
                            if pos.last_mtm_dt else datetime.now().isoformat(),
                last_mtm_px=float(pos.last_mtm_px),
                current_sl=float(pos.current_sl),
                high_watermark=float(pos.high_watermark),
            )
            continue
        pid = db.insert_delivery_position({
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

    for pos in strategy.closed_positions:
        pid = db_ids.pop(pos.symbol, None)
        if pid is None:
            continue
        db.close_delivery_position(
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
    p = argparse.ArgumentParser(description="Delivery-accumulation scan runner")
    p.add_argument("--scan", choices=["open", "close"], required=True)
    p.add_argument("--mode", choices=["signals", "paper"], default="paper")
    p.add_argument("--force", action="store_true",
                   help="Run even on weekends/holidays (testing only)")
    args = p.parse_args()

    today = datetime.now().date()
    log = _setup_logging(today)

    assert_timezone_ist(log)
    assert_disk_space_ok([LOG_DIR, DATA_CACHE], log)

    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, log)
    ok, reason = is_trading_day(today, holidays)
    if not ok and not args.force:
        log.info("No-op: %s. Exiting.", reason)
        return 0

    _lock_fd = acquire_lock(  # noqa: F841
        DATA_CACHE / f".delivery_accum_{args.scan}.lock", log,
        label=f"delivery-accum runner (--scan={args.scan})",
    )

    log.info("=" * 60)
    log.info("DELIVERY ACCUM SCAN — kind=%s  mode=%s  date=%s", args.scan, args.mode, today)
    log.info("=" * 60)

    from backend import db
    from strategies.delivery_accumulation import DeliveryAccumulationStrategy
    db.init_schema()

    # No Kite dependency: both scans consume EOD bhavcopy panels only.
    # Kept as a degraded-mode ctor arg for BaseStrategy parity.
    class _NullKite:
        pass
    strategy = DeliveryAccumulationStrategy(_NullKite(), config_path=CONFIG_PATH, mode=args.mode)
    log.info("Active params: pctile>=%.2f hits>=%d/5 lag=%dd rangePos<=%.2f "
             "atr×%.1f rr=%.1f timeStop=%dd",
             strategy.params["deliv_entry_pctile"], int(strategy.params["deliv_min_hits"]),
             int(strategy.params["deliv_lag_days"]), strategy.params["range_pos_max"],
             strategy.params["atr_stop_multiplier"], strategy.params["risk_reward"],
             int(strategy.params["time_stop_days"]))
    strategy.log_effective_params()

    if args.scan == "close":
        _warn_if_delivery_stale(today, log)

    strategy.set_current_date(pd.Timestamp(today))
    strategy._ensure_features()
    if not strategy._features:
        log.error("EQ panel loaded zero symbols — equity_ohlcv/ cache is empty. "
                  "Run market_data/fetch_bhavcopy_eq.py first.")
        return 1
    panel_max = max(f.index.max() for f in strategy._features.values())
    panel_max_date = panel_max.date() if hasattr(panel_max, "date") else panel_max
    if args.scan == "close":
        if panel_max_date < today:
            log.error(
                "EQ panel latest date is %s, expected today (%s). "
                "fetch-bhavcopy-eq.service likely failed or didn't run. "
                "Refusing to scan stale data — fix the panel and re-run.",
                panel_max_date, today,
            )
            return 1
    else:
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

    # Any failure below is surfaced via the exit code so the systemd
    # OnFailure= path pages (code-review 2026-07-22: log.exception alone
    # meant the exit engine could fail every scan while the oneshot
    # reported success). The run still completes its remaining work.
    run_failed = False

    if args.mode == "paper":
        n_resumed, n_bad_rows = _load_open_positions_into_strategy(strategy, log)
        log.info("Resumed %d open paper positions from DB", n_resumed)
        if n_bad_rows:
            run_failed = True

    n_closed_before = len(strategy.closed_positions)
    n_open_before_fill = len(strategy.positions)

    if args.scan == "close" and args.mode == "paper":
        f_filled, f_skip_gap, f_skip_stale, f_skip_open = _fill_pending_entries(
            strategy, today, args.scan, log)
        if f_filled or f_skip_gap or f_skip_stale or f_skip_open:
            log.info("Pending entries: %d filled, %d skipped (gap), "
                     "%d skipped (stale), %d skipped (already-open)",
                     f_filled, f_skip_gap, f_skip_stale, f_skip_open)

    try:
        exits = strategy.check_and_rehedge()
        if exits:
            log.info("rehedge produced %d exit proposal(s)", len(exits))
            strategy.execute_proposals(exits)
    except Exception as e:
        log.exception("check_and_rehedge failed: %s — exits NOT evaluated "
                      "this scan; run will exit non-zero", e)
        run_failed = True

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
                    n_queued, n_malformed = _queue_pending_entries(entries, today, log)
                    if n_malformed:
                        run_failed = True
                else:
                    strategy.execute_proposals(entries)
        except Exception as e:
            log.exception("scan_and_propose failed: %s — run will exit non-zero", e)
            run_failed = True
    else:
        log.info("open scan: skipping new entries (exits-only at open)")

    if args.mode == "paper":
        n_open_persisted, n_close_persisted = _persist_proposals(strategy, args.scan, log)
        log.info("DB writes: %d new opens, %d closes", n_open_persisted, n_close_persisted)

    if args.mode == "paper":
        n_closed_today = len(strategy.closed_positions) - n_closed_before
        n_opens_net = max(0, len(strategy.positions) - n_open_before_fill)
        notes = None
        if n_queued:
            notes = f"queued {n_queued} pending entry(ies) for next session"
        db.insert_delivery_scan(
            scan_dt=datetime.now().isoformat(),
            scan_kind=args.scan, mode=args.mode,
            n_signals=n_signals,
            n_trades=n_closed_today + n_opens_net,
            n_open_positions=len(strategy.positions),
            n_closed_today=n_closed_today,
            notes=notes,
        )

    try:
        report = strategy.generate_eod_report()
        log.info("Report: %s", report)
    except Exception as e:
        log.exception("generate_eod_report failed: %s", e)
        run_failed = True

    if run_failed:
        log.error("Session complete WITH FAILURES (see above) — exiting 1 "
                  "so OnFailure notifies.")
        return 1
    log.info("Session complete. Exiting cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
