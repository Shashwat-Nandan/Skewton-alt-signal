#!/usr/bin/env python3
"""
Paper runner — MA-momentum on NIFTY/BANKNIFTY index futures (§6.3).
==================================================================
Standalone paper book for the Kalman-trend A/B's MA *control*, with the
windows FROZEN. It does not call CMA-ES. It has no live path.

Historical replay of these windows was NO-GO (OOS-prior Sharpe negative,
net below 2× round-trip × n — ``python -m research.backtest_ma_momentum``).
This runner is the pre-registered 60-session paper holdout the operator
asked for anyway. Kill is the standing decay rule; do not then retune.

Shape (mirrors run_paper_kalman_trend, MA-only):

  1. Pre-flight (TZ / disk / holiday) + auth + restore.
  2. Seed each book's SMA window from recent 5-min history of the *future*
     we trade (warmup of the signal, not a fit).
  3. 09:15–15:25 IST: quote every 30s, 5-min bars, stop between bars,
     flatten at 15:25.
  4. Own lock / state / EOD / scoped halt. Never the shared
     HALT_NEW_ENTRIES (2026-07-15 fleet-freeze).

Assumes wall-clock IST. LIVE MODE IS NOT SUPPORTED — ``build_book`` raises.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from core.runner_common import (
    HALT_ALL_PATH,
    HALT_NEW_ENTRIES_PATH,
    HOLIDAYS_PATH,
    SESSION_END_AT,
    SILENT_FAIL_THRESHOLD,
    HeartbeatTracker,
    acquire_lock,
    assert_disk_space_ok,
    assert_holiday_data_fresh,
    assert_timezone_ist,
    install_signal_handlers,
    is_trading_day,
    load_holidays,
    scoped_halt_new_entries_path,
    sleep_until,
)
from runners.run_paper_kalman_trend import BarAggregator
from strategies import ma_momentum as mm
from strategies.kalman_trend_following import IntradayTrendStrategy

HERE = Path(__file__).resolve().parent.parent
DATA_CACHE = HERE / "data_cache"
LOG_DIR = HERE / "logs"
STATE_PATH = DATA_CACHE / "ma_momentum_runner_state.json"
SILENT_FAIL_PATH = DATA_CACHE / "SILENT_FAIL_ma_momentum"
LOCK_FILE = DATA_CACHE / ".ma_momentum.lock"
HALT_DAILY_LOSS_PATH = DATA_CACHE / "HALT_MA_MOMENTUM_DAILY_LOSS"
HALT_ENTRIES_PATH = scoped_halt_new_entries_path("ma_momentum")
BAR_SECONDS = 300
DEFAULT_MAX_DAILY_LOSS_INR = 25_000.0

logger = logging.getLogger("paper-ma-momentum")


@dataclass
class InstrumentBook:
    symbol: str
    book: IntradayTrendStrategy
    tradingsymbol: str = ""
    # Last real mark of THIS tradingsymbol. Persisted so a position that
    # survives a crash is squared at the price of the contract it was opened
    # on, not at whatever the next session's first quote happens to be.
    last_px: Optional[float] = None
    # check_exit books the stop at the LEVEL, but we only poll every 30s, so a
    # fill can be far through it — on BANKNIFTY the frozen stop is 14.38 pts,
    # smaller than a routine 30s excursion. Recording the overshoot does not
    # change the fill (that would be a different strategy); it makes the
    # holdout's optimism measurable instead of invisible.
    stop_overshoot_points: float = 0.0
    n_stop_fills: int = 0

    def on_price(self, price: float) -> None:
        level = self.book.stop_price if self.book.pos != 0 else None
        rec = self.book.check_exit(price)
        self.last_px = price
        if rec is not None and rec.reason == "stop" and level is not None:
            over = abs(price - level)
            self.stop_overshoot_points += over
            self.n_stop_fills += 1
            if over > 0:
                logger.warning(
                    "%s: stop booked at level %.2f but the quote that "
                    "triggered it was %.2f (overshoot %.2f pts = \u20b9%.0f) \u2014 "
                    "30s poll, fill is optimistic by that much",
                    self.symbol, level, price, over, over * self.book.lot_size)

    def on_bar(self, close: float, *, allow_entry: bool = True) -> None:
        self.book.on_bar(close, allow_entry=allow_entry)

    def on_session_start(self) -> None:
        self.book.on_session_start()

    def eod_close(self, price: float) -> None:
        self.book.force_close(price)

    def serialize(self) -> dict:
        return {"symbol": self.symbol, "book": self.book.serialize(),
                "tradingsymbol": self.tradingsymbol,
                "last_px": self.last_px,
                "stop_overshoot_points": round(self.stop_overshoot_points, 4),
                "n_stop_fills": self.n_stop_fills}

    @classmethod
    def restore(cls, blob: dict) -> "InstrumentBook":
        mm.assert_paper_only("paper")
        restored = IntradayTrendStrategy.restore(blob["book"])
        # Restore may have carried drifted params from a hand-edit / old
        # experiment. Re-assert frozen windows; refuse stop-swap if open.
        mm.reassert_frozen(restored, blob["symbol"])
        if restored.pos != 0 and restored.stop_ticks != mm.FROZEN_PARAMS[blob["symbol"]]["stop_ticks"]:
            raise RuntimeError(
                f"{blob['symbol']}: restored an OPEN position whose stop_ticks "
                f"{restored.stop_ticks} ≠ frozen "
                f"{mm.FROZEN_PARAMS[blob['symbol']]['stop_ticks']}. "
                "Not swapping a stop under a live paper position — flatten "
                "by hand or wait for 15:25, then restart."
            )
        inst = cls(symbol=blob["symbol"], book=restored,
                   tradingsymbol=blob.get("tradingsymbol") or "")
        lp = blob.get("last_px")
        inst.last_px = None if lp is None else float(lp)
        inst.stop_overshoot_points = float(blob.get("stop_overshoot_points") or 0.0)
        inst.n_stop_fills = int(blob.get("n_stop_fills") or 0)
        return inst


def eod_report(books: List[InstrumentBook], today: date) -> dict:
    per = []
    total = 0.0
    n_trades = 0
    overshoot_rupees = 0.0
    n_stop_fills = 0
    for b in books:
        s = b.book.book_summary()
        over_inr = b.stop_overshoot_points * b.book.lot_size
        per.append({"symbol": b.symbol, "tradingsymbol": b.tradingsymbol,
                    "ma": s,
                    "stop_overshoot_points": round(b.stop_overshoot_points, 2),
                    "stop_overshoot_rupees": round(over_inr, 2),
                    "n_stop_fills": b.n_stop_fills})
        total += s["realized_rupees"]
        n_trades += s["n_trades"]
        overshoot_rupees += over_inr
        n_stop_fills += b.n_stop_fills
    return {
        "date": today.isoformat(),
        "system": "ma_momentum",
        "total_rupees": round(total, 2),
        "n_trades": n_trades,
        # Cumulative \u20b9 by which stop fills are optimistic: check_exit books at
        # the stop LEVEL, the 30s poll means price was already through it.
        # total_rupees is NOT adjusted (that would silently restate the
        # pre-registered number) \u2014 subtract this when reading the holdout.
        "stop_overshoot_rupees": round(overshoot_rupees, 2),
        "n_stop_fills": n_stop_fills,
        "instruments": per,
        "note": ("PAPER ONLY. Historical OOS prior was NO-GO "
                 "(research.backtest_ma_momentum). This sidecar is the "
                 "§6.3 60-session holdout; decay scores it."),
    }


def write_state(books: List[InstrumentBook],
                carry: Optional[Dict[str, dict]] = None) -> None:
    """Persist the loaded books, PRESERVING the stored blob of any symbol we
    could not load today.

    Writing only `books` would delete a symbol whose front-month future did not
    resolve, or whose restore failed — and since eod_report sums the
    *cumulative* realized_rupees that the scoreboard reads as a cumulative
    series, losing a book's history prints as a monthly LOSS the size of its
    whole lifetime P&L. Two of those would trip the decay machine on an
    artifact instead of on P&L.
    """
    merged: Dict[str, dict] = dict(carry or {})
    for b in books:
        merged[b.symbol] = b.serialize()
    ordered = [merged[s] for s in mm.SYMBOLS if s in merged]
    ordered += [v for k, v in merged.items() if k not in mm.SYMBOLS]
    payload = {"updated": datetime.now().isoformat(), "instruments": ordered}
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2))
    tmp.replace(STATE_PATH)            # atomic crash-safe swap


def load_state() -> Dict[str, dict]:
    if not STATE_PATH.exists():
        return {}
    blob = json.loads(STATE_PATH.read_text())
    return {b["symbol"]: b for b in blob.get("instruments", [])}


def state_session_date() -> Optional[date]:
    """Trading date the stored state was last written on, or None if unknown.
    A state file from an EARLIER date means any open position survived a
    session boundary — this book flattens at 15:25, so that is always a fault.
    """
    if not STATE_PATH.exists():
        return None
    try:
        blob = json.loads(STATE_PATH.read_text())
        return datetime.fromisoformat(blob["updated"]).date()
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def write_eod(books: List[InstrumentBook], today: date) -> Path:
    path = DATA_CACHE / f"ma_momentum_eod_{today.isoformat()}.json"
    path.write_text(json.dumps(eod_report(books, today), default=str, indent=2))
    return path


def session_pnl_rupees(books: List[InstrumentBook],
                       last_px: Dict[str, float]) -> float:
    """Today's closed P&L plus open MTM. Daily-loss cap reads this."""
    total = 0.0
    for b in books:
        s = b.book.book_summary()
        total += s["session_realized_rupees"]
        px = last_px.get(b.symbol)
        if b.book.pos != 0 and px is not None:
            total += b.book.pos * (px - b.book.entry_price) * b.book.lot_size
    return total


def _setup_logging(level: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(
                LOG_DIR / f"paper-ma-momentum-{date.today().isoformat()}.log"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def _days_ago(n: int) -> date:  # pragma: no cover
    from datetime import timedelta
    return date.today() - timedelta(days=n)


def _resolve_front_month_future(nfo, symbol, today):  # pragma: no cover
    futs = [i for i in nfo if i.get("name") == symbol
            and i.get("instrument_type") == "FUT"
            and i.get("expiry") and i["expiry"] >= today]
    return min(futs, key=lambda i: i["expiry"]) if futs else None


def _last_price(kite, tradingsymbol):  # pragma: no cover
    try:
        key = f"NFO:{tradingsymbol}"
        return float(kite.quote([key])[key]["last_price"])
    except Exception as e:
        logger.debug("quote failed for %s: %s", tradingsymbol, e)
        return None


def _seed_from_history(kite, token, book: IntradayTrendStrategy) -> int:  # pragma: no cover
    """Pull recent 5-min closes of the traded future and fill the SMA window."""
    hist = kite.historical_data(int(token), _days_ago(10), date.today(), "5minute")
    px = [float(c["close"]) for c in hist]
    return mm.seed_closes(book, px)


def _window_len(book: IntradayTrendStrategy) -> int:
    return 0 if book._closes is None else len(book._closes)


def _safe_seed(kite, token, book: IntradayTrendStrategy,
               sym: str) -> int:  # pragma: no cover
    """Seed the SMA window; a history failure must not take the runner down.
    An unseeded book is mute, not wrong — but say so at CRITICAL."""
    try:
        return _seed_from_history(kite, token, book)
    except Exception as e:
        logger.critical(
            "%s: could not seed the SMA window from history (%s) — the book "
            "will emit no signal until %d live bars have accumulated",
            sym, e, int(book.long) - _window_len(book))
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="MA-momentum PAPER runner (§6.3)")
    p.add_argument("--force", action="store_true",
                   help="run on a non-trading day / skip sleep-until-open. "
                        "Does NOT flatten on a holiday.")
    p.add_argument("--once", action="store_true",
                   help="single quote+manage pass, then exit (no 15:25 flatten "
                        "unless we are already at session end)")
    p.add_argument("--max-daily-loss-inr", type=float,
                   default=DEFAULT_MAX_DAILY_LOSS_INR,
                   help="session ΔP&L (closed+open MTM) at which new entries "
                        "are halted (0 disables). Scoped flag, not shared.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    _setup_logging(args.log_level)
    load_dotenv(HERE / ".env")
    assert_timezone_ist(logger)
    assert_disk_space_ok([DATA_CACHE, LOG_DIR], logger)
    today = date.today()
    holidays = load_holidays(HOLIDAYS_PATH)
    assert_holiday_data_fresh(holidays, today, logger)
    ok, why = is_trading_day(today, holidays)
    if not ok and not args.force:
        logger.info("Not a trading day (%s) — exiting.", why)
        return 0
    if HALT_ALL_PATH.exists():
        logger.warning("HALT_ALL present — exiting")
        return 0

    install_signal_handlers(logger)
    try:
        lock = acquire_lock(LOCK_FILE, logger, label="ma-momentum-paper runner")
    except RuntimeError as e:
        logger.error("%s", e)
        return 1
    _ = lock  # keep FD alive

    from core.kite_auth import KiteAuthManager
    kite = KiteAuthManager("config.ini").get_kite()
    nfo = kite.instruments("NFO")

    prior = load_state()
    prior_date = state_session_date()
    carried = prior_date is not None and prior_date < today
    books: List[InstrumentBook] = []
    for sym in mm.SYMBOLS:
        fut = _resolve_front_month_future(nfo, sym, today)
        if fut is None:
            # State for this symbol is preserved by write_state's carry arg.
            logger.error("%s: no front-month future — skipping today; its "
                         "stored book is preserved, not reset", sym)
            continue
        if sym in prior:
            try:
                inst = InstrumentBook.restore(prior[sym])
            except Exception as e:
                # Fail loud, but per symbol: raising out of main() here killed
                # the healthy leg too, and every systemd restart re-read the
                # same state and crashed again until StartLimitBurst gave up —
                # so the advice to "wait for 15:25" could never happen.
                logger.critical(
                    "%s: cannot restore its book (%s). Skipping this symbol "
                    "for the whole session; its stored state is left intact "
                    "for an operator. The other symbols still trade.", sym, e)
                continue
            prev_ts = prior[sym].get("tradingsymbol") or ""
            rolled = bool(prev_ts) and prev_ts != fut["tradingsymbol"]
            inst.on_session_start()
            # A position may only be open here if we crashed. Square it at the
            # last real mark of the contract it was opened on, before any new
            # quote can fire check_exit against a stale stop_price (that books
            # at the LEVEL and would swallow the whole overnight gap).
            if inst.book.pos != 0 and (carried or rolled):
                mark = inst.last_px if inst.last_px is not None else inst.book.entry_price
                rec = inst.book.force_close(mark)
                logger.critical(
                    "%s: found an OPEN position from %s (%s) — this book "
                    "flattens at 15:25, so it survived a crash. Squared at "
                    "the last stored mark %.2f%s for %.2f pts. Not carrying a "
                    "stale stop into a new session.",
                    sym, prev_ts or "an earlier session",
                    prior_date.isoformat() if prior_date else "unknown date",
                    mark,
                    "" if inst.last_px is not None else " (NO stored mark — "
                    "used the entry price, so this fill is unmarked)",
                    rec.pnl_points if rec else 0.0)
                inst.last_px = None
            if rolled:
                # The window holds closes of the EXPIRED contract. The roll
                # basis (~150 pts NIFTY / ~330 BANKNIFTY) is wider than the
                # frozen dead-band (61.4 / 201.2), so a mixed window does not
                # merely delay a signal — it fabricates one for `long` bars.
                logger.warning(
                    "%s: contract rolled %s → %s — discarding the SMA window "
                    "and re-seeding from the new contract",
                    sym, prev_ts, fut["tradingsymbol"])
                mm.reset_window(inst.book)
            inst.tradingsymbol = fut["tradingsymbol"]
            n = 0
            if mm.window_is_short(inst.book):
                n = _safe_seed(kite, fut["instrument_token"], inst.book, sym)
            logger.info("%s: restored %s (short=%s long=%s stop=%.1f) "
                        "window=%d/%s%s",
                        sym, inst.tradingsymbol, inst.book.short, inst.book.long,
                        inst.book.stop_ticks, _window_len(inst.book),
                        inst.book.long, f" reseeded={n}" if n else "")
        else:
            book = mm.build_book(sym)
            n = _safe_seed(kite, fut["instrument_token"], book, sym)
            inst = InstrumentBook(symbol=sym, book=book,
                                  tradingsymbol=fut["tradingsymbol"])
            logger.info("%s: new book %s seeded %d closes (short=%s long=%s)",
                        sym, inst.tradingsymbol, n, book.short, book.long)
        books.append(inst)

    if not books:
        logger.error("No tradeable instruments — exiting.")
        return 1

    agg = {b.symbol: BarAggregator(BAR_SECONDS) for b in books}
    heartbeat = HeartbeatTracker(SILENT_FAIL_THRESHOLD, SILENT_FAIL_PATH, logger)
    quote_fails = {b.symbol: 0 for b in books}
    PER_SYMBOL_FAIL_WARN = 10
    last_px: Dict[str, float] = {}
    open_t = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = datetime.now().replace(
        hour=SESSION_END_AT[0], minute=SESSION_END_AT[1], second=0, microsecond=0)
    if not args.force and not args.once:
        sleep_until(open_t, logger)

    def _allow_entry() -> bool:
        if HALT_NEW_ENTRIES_PATH.exists() or HALT_ENTRIES_PATH.exists():
            return False
        if HALT_DAILY_LOSS_PATH.exists():
            return False
        if args.max_daily_loss_inr > 0:
            pnl = session_pnl_rupees(books, last_px)
            if pnl <= -args.max_daily_loss_inr:
                HALT_DAILY_LOSS_PATH.write_text(
                    f"{datetime.now().isoformat()} session_pnl={pnl:.2f}\n")
                # Nothing clears this flag automatically — not this runner, not
                # an ExecStartPre. Entries stay halted on EVERY later session
                # until an operator removes it, while EOD sidecars keep being
                # written, so the scoreboard would read "flat", not "dead".
                logger.critical(
                    "DAILY LOSS LIMIT BREACHED: session ΔP&L=₹%.0f vs ₹%.0f. "
                    "Touching %s — entries suspended; open positions still "
                    "exit and still flatten at 15:25. This flag PERSISTS "
                    "across sessions and halts the rest of the 60-session "
                    "holdout. Operator: `rm %s` to resume.",
                    pnl, -args.max_daily_loss_inr,
                    HALT_DAILY_LOSS_PATH, HALT_DAILY_LOSS_PATH)
                return False
        return True

    if HALT_DAILY_LOSS_PATH.exists():
        logger.critical(
            "%s is present from an earlier session — NO entries will be taken "
            "today (exits and the 15:25 flatten still run). The holdout is "
            "paused, not flat. Operator: `rm %s` to resume.",
            HALT_DAILY_LOSS_PATH, HALT_DAILY_LOSS_PATH)

    silent_dead = False

    def _tick() -> None:
        nonlocal silent_dead
        ran = errored = 0
        allow = _allow_entry()
        now = time.time()
        for b in books:
            ran += 1
            px = _last_price(kite, b.tradingsymbol)
            if px is None:
                errored += 1
                quote_fails[b.symbol] += 1
                if quote_fails[b.symbol] == PER_SYMBOL_FAIL_WARN:
                    logger.warning("%s: %d consecutive quote failures (%s)",
                                   b.symbol, PER_SYMBOL_FAIL_WARN,
                                   b.tradingsymbol)
                continue
            quote_fails[b.symbol] = 0
            last_px[b.symbol] = px
            b.on_price(px)
            closed = agg[b.symbol].add(now, px)
            if closed is not None:
                b.on_bar(closed, allow_entry=allow)
        write_state(books, prior)
        if heartbeat.record_tick(ran, errored):
            logger.error("Silent-fail threshold hit — exiting.")
            silent_dead = True

    try:
        if args.once or (args.force and datetime.now() >= close_t):
            _tick()
        else:
            while datetime.now() < close_t:
                if HALT_ALL_PATH.exists():
                    logger.warning("HALT_ALL — flattening and exiting.")
                    break
                _tick()
                if silent_dead:
                    break
                time.sleep(30)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — writing state and EOD")

    # Flatten at session end / SIGTERM, never on a holiday --force, and
    # never on --once mid-session (that would close a hold the strategy
    # is supposed to carry to 15:25).
    at_close = datetime.now() >= close_t
    if ok and (at_close or not args.once):
        for b in books:
            if b.book.pos == 0:
                continue
            px = _last_price(kite, b.tradingsymbol) or last_px.get(b.symbol)
            if px is not None:
                b.eod_close(px)
            else:
                # The fallback quote is the very call that fails during a
                # quote outage. Leave the position open rather than invent a
                # mark; the next session's restore squares it at the stored
                # mark before any stale stop_price can fire at its LEVEL.
                logger.critical(
                    "%s: could not mark %s to flatten — position pos=%+d left "
                    "OPEN in state. It will be squared at the last stored "
                    "mark on the next start. Do NOT read today's EOD as final.",
                    b.symbol, b.tradingsymbol, b.book.pos)
    write_state(books, prior)
    if ok:
        path = write_eod(books, today)
        rep = eod_report(books, today)
        logger.info("EOD MA-momentum ₹%.0f n=%d → %s",
                    rep["total_rupees"], rep["n_trades"], path.name)
    return 1 if silent_dead else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
