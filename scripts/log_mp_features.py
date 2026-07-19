#!/usr/bin/env python3
"""Nightly, read-only logger for Dalton market-generated indicators.

For every instrument with intraday bars in ``dashboard.db`` this computes one
``DayIndicators`` row per trading day (open type, day shape, balance-vs-prior,
range extension, excess, one-timeframing — see
``docs/market-profile-book-analysis.md``) and appends it to an ``mp_features``
table in the same DB. It touches **no order path** — it exists so
``research/mp_edge_report.py`` can measure whether any Market-Profile bucket actually
predicts forward returns on our own tape before we ever trade it (Phase 2 of the
plan).

Data reality on the deploy host (2026-07-13): the intraday ``bars`` table holds
**30-min equity F&O bars**, not NIFTY/BANKNIFTY. This logger processes whatever
30-min instruments are present in ``bars_universe``, so it will pick up the
indices automatically once they are backfilled — but today it runs on equities.
That is the honest, book-faithful intraday path we have (Rule 12).

The optional ``--source daily`` pass logs the *balance-state only* subset from
daily bhavcopy bars (the one Layer-3 read that survives at daily resolution),
loudly flagged as coarse via ``warn_coarse_timeframe``.

Idempotent: ``INSERT OR REPLACE`` on ``UNIQUE(source, instrument, day)``, so a
re-run overwrites the same rows rather than duplicating.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from typing import List, Optional

from core.market_profile import (
    Bar,
    DayProfile,
    compute_day_profile,
    market_generated_indicators,
    split_by_day,
)

logger = logging.getLogger("log_mp_features")

MP_FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS mp_features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,              -- 'intraday_30m' | 'daily'
    instrument TEXT NOT NULL,          -- symbol (or index name once backfilled)
    day TEXT NOT NULL,                 -- ISO date
    open_type TEXT,
    day_shape TEXT,
    profile_skew TEXT,
    balance_state TEXT,
    in_balance INTEGER,
    range_ext_up INTEGER,
    range_ext_down INTEGER,
    range_ext_first TEXT,
    excess_high INTEGER,
    excess_low INTEGER,
    poor_high INTEGER,
    poor_low INTEGER,
    single_print_count INTEGER,
    one_timeframing TEXT,
    one_timeframing_run INTEGER,
    open REAL, high REAL, low REAL, close REAL,
    poc REAL, vah REAL, val REAL, ib_high REAL, ib_low REAL,
    n_periods INTEGER,
    total_volume INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(source, instrument, day)
);
CREATE INDEX IF NOT EXISTS idx_mp_features_instr_day
    ON mp_features (instrument, day);
"""

_COLS = [
    "source", "instrument", "day", "open_type", "day_shape", "profile_skew",
    "balance_state", "in_balance", "range_ext_up", "range_ext_down",
    "range_ext_first", "excess_high", "excess_low", "poor_high", "poor_low",
    "single_print_count", "one_timeframing", "one_timeframing_run",
    "open", "high", "low", "close", "poc", "vah", "val", "ib_high", "ib_low",
    "n_periods", "total_volume", "created_at",
]


def _to_bars(rows: List[dict]) -> List[Bar]:
    out: List[Bar] = []
    for r in rows:
        out.append(Bar(
            ts=datetime.fromisoformat(r["ts"]),
            open=float(r["open"]), high=float(r["high"]),
            low=float(r["low"]), close=float(r["close"]),
            volume=int(r["volume"] or 0),
        ))
    return out


def _row_from_indicators(ind, *, source: str, instrument: str,
                         n_periods: int, total_volume: int) -> tuple:
    now = datetime.now().isoformat(timespec="seconds")
    return (
        source, instrument, ind.day.isoformat(),
        ind.open_type, ind.day_shape, ind.profile_skew,
        ind.balance_state, int(ind.in_balance),
        int(ind.range_ext_up), int(ind.range_ext_down), ind.range_ext_first,
        int(ind.excess_high), int(ind.excess_low),
        int(ind.poor_high), int(ind.poor_low),
        ind.single_print_count, ind.one_timeframing, ind.one_timeframing_run,
        ind.open, ind.high, ind.low, ind.close,
        ind.poc, ind.vah, ind.val, ind.ib_high, ind.ib_low,
        n_periods, total_volume, now,
    )


def _write_rows(conn, rows: List[tuple]) -> None:
    if not rows:
        return
    placeholders = ",".join("?" * len(_COLS))
    conn.executemany(
        f"INSERT OR REPLACE INTO mp_features ({','.join(_COLS)}) "
        f"VALUES ({placeholders})",
        rows,
    )


def log_intraday(conn, *, interval: int, min_periods: int,
                 value_area_pct: float, ib_periods: int) -> int:
    """Full DayIndicators per (symbol, day) from intraday bars. Returns rows written."""
    from backend import bars as bdb_bars

    universe = bdb_bars.list_universe()
    written = 0
    skipped_thin = 0
    instruments_with_data = 0

    for u in universe:
        token = u["instrument_token"]
        symbol = u["symbol"]
        if bdb_bars.count_bars(token, interval) == 0:
            continue
        instruments_with_data += 1
        bars = _to_bars(bdb_bars.get_bars(token, interval))
        days = split_by_day(bars)

        prior: Optional[DayProfile] = None
        out_rows: List[tuple] = []
        for day_bars in days:
            if len(day_bars) < min_periods:
                skipped_thin += 1
                # Still advance `prior` with whatever profile we can build, so a
                # thin day doesn't silently break the next day's balance_state.
                prior = compute_day_profile(
                    day_bars, value_area_pct=value_area_pct, ib_periods=ib_periods,
                ) or prior
                continue
            ind = market_generated_indicators(
                day_bars, prior=prior,
                value_area_pct=value_area_pct, ib_periods=ib_periods,
            )
            if ind is None:
                continue
            out_rows.append(_row_from_indicators(
                ind, source=f"intraday_{interval}m", instrument=symbol,
                n_periods=len(day_bars),
                total_volume=sum(b.volume for b in day_bars),
            ))
            prior = compute_day_profile(
                day_bars, value_area_pct=value_area_pct, ib_periods=ib_periods,
            )
        _write_rows(conn, out_rows)
        written += len(out_rows)

    if instruments_with_data == 0:
        logger.warning(
            "\n" + "=" * 72 + "\n"
            "  NO intraday bars found in dashboard.db (interval=%dm).\n"
            "  The bars table needs a Kite backfill before intraday MP features\n"
            "  can be logged. Nothing written for source=intraday.\n"
            + "=" * 72, interval,
        )
    else:
        logger.info(
            "intraday: %d instruments with data, %d rows written, %d thin days skipped",
            instruments_with_data, written, skipped_thin,
        )
    return written


def log_daily(conn, *, universe_path, source: str, lookback: int,
              value_area_pct: float) -> int:
    """Balance-state-only rows from daily bhavcopy bars (coarse — flagged loud)."""
    from pathlib import Path

    import pandas as pd  # noqa: F401  (panel is a DataFrame)

    from core.backtest_timeframe import warn_coarse_timeframe
    from core.market_profile import _classify_balance
    from strategies._eq_data import load_equity_panel, load_universe
    from strategies._market_profile_eq import rolling_value_area

    warn_coarse_timeframe(
        "daily", backtest="log_mp_features(daily)",
        reason="daily bhavcopy has one bar/day — only the balance-state (VA vs "
               "prior VA) subset of Dalton's indicators is computable at this "
               "resolution; open type / day shape / excess need intraday bars.",
        logger=logger,
    )

    universe = (load_universe(Path(universe_path)) if universe_path
                else load_universe())
    panel = load_equity_panel(universe=universe, source=source)
    if panel.empty:
        logger.warning("daily: panel empty — nothing written")
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    for sym, g in panel.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        va = rolling_value_area(g, lookback=lookback, value_area_pct=value_area_pct)
        # va is indexed by date with mp_vah/mp_poc/mp_val; shift(1) = prior VA.
        prev = va.shift(1)
        out_rows: List[tuple] = []
        for i, (dt, row) in enumerate(va.iterrows()):
            if row.isna().any() or prev.iloc[i].isna().any():
                continue
            state = _classify_balance(
                row["mp_val"], row["mp_vah"],
                prev.iloc[i]["mp_val"], prev.iloc[i]["mp_vah"],
            )
            in_bal = state in {"inside", "overlapping_higher", "overlapping_lower"}
            gr = g.iloc[i]
            out_rows.append((
                "daily", sym, pd_ts_to_iso(dt),
                None, None, None, state, int(in_bal),
                None, None, None, None, None, None, None, None, None, None,
                float(gr["open"]), float(gr["high"]), float(gr["low"]),
                float(gr["close"]),
                row["mp_poc"], row["mp_vah"], row["mp_val"], None, None,
                1, int(gr.get("volume", 0) or 0), now,
            ))
        _write_rows(conn, out_rows)
        written += len(out_rows)
    logger.info("daily: %d balance-state rows written", written)
    return written


def pd_ts_to_iso(dt) -> str:
    """Format a pandas/py date as an ISO date string."""
    try:
        return dt.date().isoformat()
    except AttributeError:
        return str(dt)[:10]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["intraday", "daily", "both"],
                    default="intraday",
                    help="which feature source(s) to log (default: intraday)")
    ap.add_argument("--interval", type=int, default=30,
                    help="intraday bar interval in minutes (default: 30)")
    ap.add_argument("--min-periods", type=int, default=6,
                    help="min intraday periods to classify a day (default: 6)")
    ap.add_argument("--value-area-pct", type=float, default=0.70)
    ap.add_argument("--ib-periods", type=int, default=2)
    ap.add_argument("--daily-source", default="auto",
                    help="load_equity_panel source for --source daily")
    ap.add_argument("--daily-lookback", type=int, default=20)
    ap.add_argument("--universe", default=None,
                    help="universe CSV path for the daily pass")
    ap.add_argument("--db", default=None,
                    help="override dashboard.db path (default: settings)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from backend import db as backend_db
    if args.db:
        from pathlib import Path
        backend_db.reset_for_tests(Path(args.db))
    conn = backend_db.get_conn()
    conn.executescript(MP_FEATURES_DDL)

    total = 0
    if args.source in {"intraday", "both"}:
        total += log_intraday(
            conn, interval=args.interval, min_periods=args.min_periods,
            value_area_pct=args.value_area_pct, ib_periods=args.ib_periods,
        )
    if args.source in {"daily", "both"}:
        total += log_daily(
            conn, universe_path=args.universe, source=args.daily_source,
            lookback=args.daily_lookback, value_area_pct=args.value_area_pct * 100.0,
        )
    logger.info("done: %d mp_features rows written/updated", total)


if __name__ == "__main__":
    main()
