#!/usr/bin/env python3
"""
Paper runner — intraday Kalman-vs-MA trend A/B (forward parity test).
====================================================================
Runs TWO `IntradayTrendStrategy` books per instrument (one Kalman, one MA) side
by side on identical live 5-min bars and sizing, on NIFTY + BANKNIFTY front-month
futures. The backtests show no robust Kalman>MA edge daily OR intraday
(tasks/kalman-trend-findings.md); this measures the one thing backtests can't —
live forward fills — at zero risk (paper only).

Mirrors the other paper runners via runner_common (TOTP auth, holiday/weekend
gate, 09:15→15:25 loop, shared HALT_* kill switches, atomic crash-safe state,
hourly heartbeat). Own system, no shared mutable state:
  - state : data_cache/kalman_trend_runner_state.json
  - EOD   : data_cache/kalman_trend_eod_<date>.json
  - log   : logs/paper-kalman-trend-<date>.log

The PURE core (bar aggregation, two-book stepping, EOD A/B report) is unit-
tested; the Kite-wired main() is host-smoke-test-only (no Kite session in CI).

NOTE: warmup fits the reduced-Kalman + MA params on recent 5-min history at
startup. Reuse the cached Kite session — never fresh-login while a live runner is
active. paper/signals only; no live order path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

import optimize_kalman_trend as opt
from strategies.kalman_trend_following import IntradayTrendStrategy

DATA_CACHE = Path("data_cache")
STATE_PATH = DATA_CACHE / "kalman_trend_runner_state.json"
SILENT_FAIL_PATH = DATA_CACHE / "SILENT_FAIL_kalman_trend"
SYMBOLS = ["NIFTY", "BANKNIFTY"]
LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 15}   # ₹/point/lot (front-month future)
BAR_SECONDS = 300                            # 5-min signal bars
# Per-side transaction cost in price POINTS, charged round-trip (`_close`
# subtracts 2×). Mirrors backtest_kalman_trend.py's `--cost` default (2.5) so the
# forward paper A/B is cost-consistent with the backtest that graded the strategy
# NO-GO — without it the ~30-trade/day Kalman book is flattered vs the 1-4-trade
# MA book by omitting costs entirely (issue #77). Flat across instruments to match
# the backtest; per-instrument realism is a possible follow-up.
COST_PER_UNIT_POINTS = 2.5
# Experiment sunset (docs/strategy-efficiency-review-2026-07-05.md §2.7). The
# backtest is NO-GO, the loop checker REJECTs every session, and the forward
# A/B pays ~14x the MA control's trading for a net wash across instruments —
# the experiment has answered its question. On/after this date the runner
# refuses to trade and exits 0 without an EOD sidecar, which the loop
# orchestrator already records as "no_session". Extending the runway is a
# deliberate act: move the date in a commit, don't delete the gate.
KILL_DATE = date(2026, 8, 30)

logger = logging.getLogger("paper-kalman-trend")


def experiment_expired(today: date, kill_date: date = KILL_DATE) -> bool:
    """True once the A/B experiment is on/after its sunset date."""
    return today >= kill_date


# ──────────────────────────────────────────────────────────────────────────
# PURE CORE (unit-tested)
# ──────────────────────────────────────────────────────────────────────────
class BarAggregator:
    """Aggregate a stream of (epoch_seconds, price) ticks into fixed-width bars.
    `add` returns the just-completed bar's close when a bar boundary rolls over,
    else None. Bar = floor(ts / width)."""

    def __init__(self, width_seconds: int = BAR_SECONDS):
        self.width = int(width_seconds)
        self._bucket: Optional[int] = None
        self._last_price: Optional[float] = None

    def add(self, ts: float, price: float) -> Optional[float]:
        if not np.isfinite(price):
            return None
        bucket = int(ts // self.width)
        completed = None
        if self._bucket is not None and bucket != self._bucket:
            completed = self._last_price          # close of the prior bar
        self._bucket = bucket
        self._last_price = float(price)
        return completed


@dataclass
class InstrumentBooks:
    """The Kalman + MA books for one instrument, stepped together on the same
    prices so the A/B is apples-to-apples."""
    symbol: str
    kalman: IntradayTrendStrategy
    ma: IntradayTrendStrategy

    def on_price(self, price: float) -> None:
        """Intraday tick: let either book hit its stop/target between bars."""
        self.kalman.check_exit(price)
        self.ma.check_exit(price)

    def on_bar(self, close: float, *, allow_entry: bool = True) -> None:
        self.kalman.on_bar(close, allow_entry=allow_entry)
        self.ma.on_bar(close, allow_entry=allow_entry)

    def on_session_start(self) -> None:
        self.kalman.on_session_start()
        self.ma.on_session_start()

    def set_cost(self, cost_per_unit: float) -> None:
        """Apply the current per-side cost to both books. Books RESTORED from
        prior state carry whatever `cost_per_unit` was serialized — including the
        stale 0.0 written before issue #77 — so the runner re-asserts the current
        cost after restore, else a persisted book would keep booking cost-free
        fills indefinitely (build_books already sets it for fresh warmups)."""
        self.kalman.cost_per_unit = cost_per_unit
        self.ma.cost_per_unit = cost_per_unit

    def eod_close(self, price: float) -> None:
        self.kalman.force_close(price)
        self.ma.force_close(price)

    def summary(self) -> dict:
        return {"symbol": self.symbol,
                "kalman": self.kalman.book_summary(),
                "ma": self.ma.book_summary()}

    def serialize(self) -> dict:
        return {"symbol": self.symbol, "kalman": self.kalman.serialize(),
                "ma": self.ma.serialize()}

    @classmethod
    def restore(cls, blob: dict) -> "InstrumentBooks":
        return cls(symbol=blob["symbol"],
                   kalman=IntradayTrendStrategy.restore(blob["kalman"]),
                   ma=IntradayTrendStrategy.restore(blob["ma"]))


def build_books(symbol: str, kal_params: dict, ma_params: dict) -> InstrumentBooks:
    """Construct the two books for `symbol` from fitted params."""
    lot = LOT_SIZE.get(symbol, 1)
    kal = IntradayTrendStrategy(
        signal_kind="kalman", filter_params=kal_params["filter_params"],
        model=kal_params.get("model", 2), mu=kal_params["mu"],
        stop_ticks=kal_params["stop_ticks"], target_ticks=kal_params["target_ticks"],
        tick_size=1.0, lot_size=lot, cost_per_unit=COST_PER_UNIT_POINTS)
    ma = IntradayTrendStrategy(
        signal_kind="ma", short=ma_params["short"], long=ma_params["long"],
        offset=ma_params["offset"], stop_ticks=ma_params["stop_ticks"],
        target_ticks=ma_params["target_ticks"], tick_size=1.0, lot_size=lot,
        cost_per_unit=COST_PER_UNIT_POINTS)
    return InstrumentBooks(symbol=symbol, kalman=kal, ma=ma)


def eod_report(books: List[InstrumentBooks], today: date) -> dict:
    """A/B comparison sidecar: per-instrument Kalman vs MA realized ₹ + totals."""
    per = [b.summary() for b in books]
    tot_k = sum(b.kalman.realized_rupees() for b in books)
    tot_m = sum(b.ma.realized_rupees() for b in books)
    return {
        "date": today.isoformat(),
        "system": "kalman_trend_ab",
        "instruments": per,
        "total_kalman_rupees": round(tot_k, 2),
        "total_ma_rupees": round(tot_m, 2),
        "kalman_minus_ma_rupees": round(tot_k - tot_m, 2),
    }


def fit_params(prices: np.ndarray, *, n_gen: int = 25, seed: int = 0) -> tuple[dict, dict]:
    """Fit reduced-Kalman + MA params on a recent intraday window (warmup).

    The fit MUST charge the same per-side cost the book charges
    (COST_PER_UNIT_POINTS): at optimize's cost_per_unit=0.0 default, CMA-ES
    prefers hyper-tight stops whose churn the live book then pays for
    (2026-07-07 walk-forward: zero-cost fit −769 pts/seed OOS on NIFTY vs
    +702 costed, at half the trade count)."""
    kal = opt.fit_kalman_reduced(prices, tick_size=1.0,
                                 cost_per_unit=COST_PER_UNIT_POINTS,
                                 n_gen=n_gen, seed=seed)
    ma = opt.fit_ma_crossover(prices, tick_size=1.0,
                              cost_per_unit=COST_PER_UNIT_POINTS,
                              n_gen=n_gen, seed=seed)
    return kal, ma


# ──────────────────────────────────────────────────────────────────────────
# State persistence (atomic, mirrors the other runners)
# ──────────────────────────────────────────────────────────────────────────
def write_state(books: List[InstrumentBooks]) -> None:
    payload = {"updated": datetime.now().isoformat(),
               "instruments": [b.serialize() for b in books]}
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, default=str, indent=2))
    tmp.replace(STATE_PATH)            # atomic crash-safe swap


def load_state() -> Dict[str, dict]:
    if not STATE_PATH.exists():
        return {}
    blob = json.loads(STATE_PATH.read_text())
    return {b["symbol"]: b for b in blob.get("instruments", [])}


def write_eod(books: List[InstrumentBooks], today: date) -> Path:
    path = DATA_CACHE / f"kalman_trend_eod_{today.isoformat()}.json"
    path.write_text(json.dumps(eod_report(books, today), default=str, indent=2))
    return path


# ──────────────────────────────────────────────────────────────────────────
# Kite-wired entrypoint (host smoke-test only — no Kite session in CI)
# ──────────────────────────────────────────────────────────────────────────
def main() -> int:  # pragma: no cover
    import time

    from dotenv import load_dotenv

    from kite_auth import KiteAuthManager
    from runner_common import (
        SILENT_FAIL_THRESHOLD,
        HeartbeatTracker,
        acquire_lock,
        assert_timezone_ist,
        install_signal_handlers,
        is_trading_day,
        load_holidays,
        sleep_until,
    )

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    assert_timezone_ist(logger)
    today = date.today()
    holidays = load_holidays(Path("holidays.csv"))
    ok, why = is_trading_day(today, holidays)
    if not ok:
        logger.info("Not a trading day (%s) — exiting.", why)
        return 0

    if experiment_expired(today):
        logger.critical(
            "kalman_trend A/B is past its kill date (%s) — refusing to trade. "
            "Verdict: backtest NO-GO + checker REJECT + ~14x MA churn for a "
            "net wash (docs/strategy-efficiency-review-2026-07-05.md §2.7). "
            "Operator: disable kalman-trend-paper/loop-kalman-trend timers; "
            "to extend the runway, move KILL_DATE in a commit.", KILL_DATE)
        return 0

    install_signal_handlers(logger)
    lock = acquire_lock(DATA_CACHE / ".kalman_trend.lock", logger)
    if lock is None:
        logger.error("Another instance holds the lock — exiting.")
        return 1

    load_dotenv(Path(__file__).resolve().parent / ".env")
    kite = KiteAuthManager("config.ini").get_kite()          # reuse cached session
    nfo = kite.instruments("NFO")

    from runner_common import HALT_ALL_PATH, HALT_NEW_ENTRIES_PATH

    # Resolve front-month future per symbol + warmup-fit on recent 5-min history
    # OF THE FUTURE WE TRADE (not the spot index — the future carries a basis and
    # a different move scale, and the fitted stop/target/µ + filter noise are all
    # scaled to the series they were fit on).
    prior = load_state()
    books: List[InstrumentBooks] = []
    tradesym: Dict[str, str] = {}
    for sym in SYMBOLS:
        fut = _resolve_front_month_future(nfo, sym, today)
        if fut is None:
            logger.warning("%s: no front-month future — skipping", sym)
            continue
        tradesym[sym] = fut["tradingsymbol"]
        if sym in prior:
            b = InstrumentBooks.restore(prior[sym])
            # Re-assert cost: a book serialized before #77 carries cost_per_unit=0.0
            # and restore() faithfully preserves it, so override to the current cost.
            b.set_cost(COST_PER_UNIT_POINTS)
            # The prior state is from yesterday's close → today's first bar spans
            # the overnight gap. Inflate now so the gap is absorbed as a level
            # jump, not one bar of velocity (the daily-restart path is the ONLY
            # real day boundary — the loop's midnight branch never fires in a
            # single-session process).
            b.on_session_start()
            books.append(b)
            logger.info("%s: restored books from prior state (session-start "
                        "inflate applied for the overnight gap)", sym)
            continue
        hist = kite.historical_data(int(fut["instrument_token"]),
                                    _days_ago(40), today, "5minute")
        prices = np.array([float(c["close"]) for c in hist], float)
        if len(prices) < 300:
            logger.warning("%s: only %d warmup bars on the future — skipping",
                           sym, len(prices))
            continue
        kal_p, ma_p = fit_params(prices[-1500:])
        books.append(build_books(sym, kal_p, ma_p))
        logger.info("%s: warmup-fit on %d future bars (kal stop/tgt %.0f/%.0f, "
                    "ma %d/%d)", sym, len(prices), kal_p["stop_ticks"],
                    kal_p["target_ticks"], ma_p["short"], ma_p["long"])

    if not books:
        logger.error("No tradeable instruments — exiting.")
        return 1

    agg = {b.symbol: BarAggregator(BAR_SECONDS) for b in books}
    heartbeat = HeartbeatTracker(SILENT_FAIL_THRESHOLD, SILENT_FAIL_PATH, logger)
    # Per-symbol consecutive quote failures: the global heartbeat only trips when
    # EVERY book fails (n_errored == n_ran), so one persistently-dead symbol would
    # otherwise be a silent no-trade book while the other keeps the session alive.
    quote_fails = {b.symbol: 0 for b in books}
    PER_SYMBOL_FAIL_WARN = 10
    session_day = today        # for overnight-gap detection across midnight runs
    open_t = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = datetime.now().replace(hour=15, minute=25, second=0, microsecond=0)
    sleep_until(open_t, logger)

    silent_dead = False
    while datetime.now() < close_t:
        if HALT_ALL_PATH.exists():
            logger.warning("HALT_ALL — force-closing and exiting.")
            break
        # new trading day across a long-lived process → inflate filter uncertainty
        # so the overnight gap isn't read as one bar of velocity.
        d_now = date.today()
        if d_now != session_day:
            for b in books:
                b.on_session_start()
            session_day = d_now
        allow_entry = not HALT_NEW_ENTRIES_PATH.exists()
        now = time.time()
        ran = errored = 0
        for b in books:
            ran += 1
            px = _last_price(kite, tradesym[b.symbol])
            if px is None:
                errored += 1            # feeds the all-books heartbeat below
                quote_fails[b.symbol] += 1
                if quote_fails[b.symbol] == PER_SYMBOL_FAIL_WARN:
                    logger.warning("%s: %d consecutive quote failures — this book "
                                   "is dead while others run (check %s)",
                                   b.symbol, PER_SYMBOL_FAIL_WARN, tradesym[b.symbol])
                continue
            quote_fails[b.symbol] = 0
            b.on_price(px)                       # intraday stop/target
            closed = agg[b.symbol].add(now, px)  # roll a 5-min bar?
            if closed is not None:
                b.on_bar(closed, allow_entry=allow_entry)
        write_state(books)
        # Loud-failure detector: if every book's quote fails for SILENT_FAIL
        # consecutive ticks (token expiry / API down), break and exit non-zero
        # instead of a silent no-trade session reporting success.
        if heartbeat.record_tick(ran, errored):
            logger.error("Silent-fail threshold hit (all quotes failing) — exiting.")
            silent_dead = True
            break
        time.sleep(30)

    # EOD: force-close both books at the last price and write the A/B sidecar.
    for b in books:
        px = _last_price(kite, tradesym[b.symbol])
        if px is not None:
            b.eod_close(px)
    write_state(books)
    path = write_eod(books, today)
    rep = eod_report(books, today)
    logger.info("EOD A/B: kalman ₹%.0f vs ma ₹%.0f (Δ %.0f) → %s",
                rep["total_kalman_rupees"], rep["total_ma_rupees"],
                rep["kalman_minus_ma_rupees"], path.name)
    return 1 if silent_dead else 0


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


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
