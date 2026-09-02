"""
Time-series momentum on NIFTY/BANKNIFTY futures — MA crossover.
==============================================================
``docs/research/strategy-finetuning-profitability-2026-08-30.md`` §6.3.

This is the Kalman-trend A/B's MA *control*, extracted as its own paper
book. Params are the windows that printed the scoreboard +₹40,310
(refit 2026-07-15), **frozen**. Jointly refitting them with CMA-ES is how
Kalman-trend got an in-sample Sharpe of 5 and an OOS of luck.

Historical 5-min replay of these exact windows was **NO-GO**
(``research.backtest_ma_momentum``): OOS-prior Sharpe −0.07 / −0.34 and
net missed 2× round-trip × n on both legs. The scoreboard +₹40k is the
27-day in-sample warmup. This module exists because the operator asked
for the pre-registered **60-session paper holdout** anyway. It is not a
belief that the historical kill was wrong.

PAPER ONLY. ``mode="live"`` raises and will keep raising. 1 lot per
index. Kill is the standing decay rule (two consecutive losing complete
months → PARK); do not then CMA-ES the windows.

Does **not** subclass ``BaseStrategy``: the scan/propose interface is the
wrong shape for a 5-min bar + 15:25 flatten book. The engine is
``IntradayTrendStrategy(signal_kind="ma")``, same as the A/B control.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, Iterable, List

import numpy as np

from strategies.kalman_trend_following import IntradayTrendStrategy

logger = logging.getLogger(__name__)

COST_PER_UNIT_POINTS = 2.5
TICK_SIZE = 1.0
# ₹/point/lot of the front-month future. 1 lot, matching the paper A/B.
LOT_SIZE = {"NIFTY": 75, "BANKNIFTY": 15}
SYMBOLS = ("NIFTY", "BANKNIFTY")

# Frozen from data_cache/kalman_trend_runner_state.json, refit_at 2026-07-15.
# Not config.ini, not argparse: §6.3 forbids retuning.
FROZEN_PARAMS: Dict[str, dict] = {
    "NIFTY": {
        "short": 34, "long": 53,
        "offset": 61.41627897339698,
        "stop_ticks": 219.99811484001893,
        "target_ticks": None,
    },
    "BANKNIFTY": {
        "short": 27, "long": 109,
        "offset": 201.22275897343027,
        "stop_ticks": 14.380470607494061,
        "target_ticks": None,
    },
}

REFIT_DATE = date(2026, 7, 15)
WARMUP_CALENDAR_DAYS = 40
FIT_START = REFIT_DATE - timedelta(days=WARMUP_CALENDAR_DAYS)  # 2026-06-05
# The fit window is CLOSED at the refit. Sessions on/after this date were never
# seen by the 2026-07-15 fit, so they are post-refit OOS — not in-sample. The
# harness must not absorb them into the IS slice when the tape is extended.
FIT_END = REFIT_DATE


class LiveModeForbidden(RuntimeError):
    """ma_momentum has no live path. The historical OOS prior was NO-GO."""


def assert_paper_only(mode: str) -> None:
    if mode == "live":
        raise LiveModeForbidden(
            "ma_momentum is PAPER ONLY. Historical OOS-prior Sharpe was "
            "negative on both legs (research.backtest_ma_momentum); this "
            "exists as the §6.3 60-session paper holdout. Live raises "
            "permanently — do not add a live branch to 'make it run'."
        )


def build_book(symbol: str, *, mode: str = "paper") -> IntradayTrendStrategy:
    """One frozen-MA book. ``mode='live'`` raises before any state is built."""
    assert_paper_only(mode)
    if symbol not in FROZEN_PARAMS:
        raise KeyError(
            f"no frozen MA params for {symbol}; known: {sorted(FROZEN_PARAMS)}"
        )
    p = FROZEN_PARAMS[symbol]
    return IntradayTrendStrategy(
        signal_kind="ma",
        short=p["short"], long=p["long"], offset=p["offset"],
        stop_ticks=p["stop_ticks"], target_ticks=p["target_ticks"],
        tick_size=TICK_SIZE,
        lot_size=LOT_SIZE[symbol],
        cost_per_unit=COST_PER_UNIT_POINTS,
    )


def seed_closes(book: IntradayTrendStrategy, closes: Iterable[float]) -> int:
    """Fill the causal SMA window from history. Does NOT change params.

    Without this, ``long=109`` on BANKNIFTY is ~7 hours of 5-min bars and
    the first session is almost entirely silent. Seeding is warmup of the
    *signal*, not a fit.
    """
    if book.signal_kind != "ma" or book._closes is None:
        return 0
    raw = [float(x) for x in closes]
    # `x == x` only drops NaN: ±inf survived it and never reaches on_bar's
    # np.isfinite guard, so one bad candle poisons both SMAs (mean of a window
    # containing inf is non-finite → every comparison False) and the book goes
    # silently mute for `long` bars. Reject on finiteness and say how many.
    seq: List[float] = [x for x in raw if np.isfinite(x)]
    dropped = len(raw) - len(seq)
    if dropped:
        logger.warning(
            "seed_closes dropped %d non-finite close(s) of %d from history; "
            "the SMA window is seeded from the remainder", dropped, len(raw))
    if not seq:
        return 0
    window = int(book.long)
    for px in seq[-window:]:
        book._closes.append(px)
    book.n_bars = max(book.n_bars, len(book._closes))
    return len(book._closes)


def reset_window(book: IntradayTrendStrategy) -> None:
    """Drop every seeded close. Used when the prices in the window came from a
    DIFFERENT instrument than the one we are about to quote (a futures roll):
    the roll basis is larger than the frozen dead-band, so a mixed window
    fabricates a signal rather than merely delaying one."""
    if book.signal_kind == "ma" and book._closes is not None:
        book._closes.clear()


def window_is_short(book: IntradayTrendStrategy) -> bool:
    """True when the SMA window cannot yet produce a signal, so the caller must
    re-seed. Fires on a fresh book, after reset_window(), and after
    reassert_frozen() has widened `long` past what the restored state held."""
    if book.signal_kind != "ma" or book._closes is None:
        return True
    return len(book._closes) < int(book.long)


def reassert_frozen(book: IntradayTrendStrategy, symbol: str) -> bool:
    """Force frozen windows onto a restored book. Returns True if anything
    changed. Refuses to swap stop/target under an OPEN position — those
    levels were derived from the entry, not from the SMA lengths, but the
    stop *distance* is a param, so an open book with a drifted stop_ticks
    is a fail-loud for the caller (we still fix short/long/offset)."""
    p = FROZEN_PARAMS[symbol]
    changed = False
    for attr in ("short", "long", "offset", "stop_ticks", "target_ticks"):
        if getattr(book, attr) != p[attr]:
            if attr in ("stop_ticks", "target_ticks") and book.pos != 0:
                continue
            setattr(book, attr, p[attr])
            changed = True
    book.cost_per_unit = COST_PER_UNIT_POINTS
    book.lot_size = LOT_SIZE[symbol]
    if book._closes is not None:
        book._closes = type(book._closes)(book._closes, maxlen=int(book.long))
        if len(book._closes) < int(book.long):
            # Widening `long` (e.g. a restored 8 → the frozen 53) leaves the
            # window under-filled, and the restore path does not seed. Without
            # this the book is inert for up to `long` bars with nothing said.
            logger.warning(
                "%s: SMA window holds %d/%d closes after reasserting frozen "
                "params — caller must re-seed or the book is mute for %d bars",
                symbol, len(book._closes), int(book.long),
                int(book.long) - len(book._closes))
    return changed
