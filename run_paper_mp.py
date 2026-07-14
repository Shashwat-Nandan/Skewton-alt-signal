#!/usr/bin/env python3
"""Paper runner — Market-Profile `trend_up` overnight-continuation (Phase 3b).

EOD cadence: once per day after the close it (1) exits yesterday's longs at
today's close, (2) classifies every universe name's day from its 30-min bars,
and (3) if it is a broad-momentum day (≥K names `trend_up`) and the kill switch
has not tripped, opens equal-weight longs at today's close to exit tomorrow.

**Paper only.** No Kite, no order path — it reads already-stored 30-min bars from
dashboard.db (kept current by `fetch_bars.py --update`) and books fills at the
official close. Its job is to accumulate the out-of-sample days the backtest
could not (the edge is consistent-but-underpowered — single regime, t<1.4), and
to HALT itself the moment forward paper turns against it (`check_kill`).

Modes:
  (default)      process the latest available bar date only (the nightly job);
                 idempotent — a date already in mp_trend_runs is skipped.
  --replay       reset the book and walk every available date in order (seeds
                 the paper book from history + is the parity check vs
                 backtest_mp_trend.py).
  --date D       process one specific date.
"""
from __future__ import annotations

import argparse
import bisect
import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from market_profile import Bar, DayProfile, compute_day_profile, split_by_day
from strategies.market_profile_intraday import (
    MPTrendConfig,
    check_kill,
    classify_day_longs,
    position_size,
    trade_pnl,
)

logger = logging.getLogger("run_paper_mp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mp_trend_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    entry_px REAL NOT NULL,
    qty INTEGER NOT NULL,
    exit_date TEXT,
    exit_px REAL,
    gross REAL, cost REAL, net REAL,
    status TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN | CLOSED
    created_at TEXT NOT NULL,
    UNIQUE(symbol, entry_date)
);
CREATE INDEX IF NOT EXISTS idx_mptp_status ON mp_trend_positions(status);
CREATE TABLE IF NOT EXISTS mp_trend_runs (
    run_date TEXT PRIMARY KEY,
    n_universe INTEGER, n_trend_up INTEGER,
    n_opened INTEGER, n_closed INTEGER,
    day_net REAL, cum_net REAL,
    halted INTEGER, reason TEXT,
    created_at TEXT NOT NULL
);
"""


def _bars_from_rows(rows) -> List[Bar]:
    return [Bar(ts=datetime.fromisoformat(r["ts"]), open=float(r["open"]),
               high=float(r["high"]), low=float(r["low"]), close=float(r["close"]),
               volume=int(r["volume"] or 0)) for r in rows]


def load_universe_days(
    interval: int,
) -> tuple[Dict[str, Dict[str, list]], Dict[str, List[str]], List[str]]:
    """Return ({symbol: {date_iso: day_bars}}, {symbol: sorted_dates}, all_dates).

    The per-symbol sorted date list is built ONCE here so per-date processing can
    bisect it instead of re-sorting the dict on every call.
    """
    from backend import bars as bdb
    per_symbol: Dict[str, Dict[str, list]] = {}
    sorted_days: Dict[str, List[str]] = {}
    all_dates: set[str] = set()
    for u in bdb.list_universe():
        token, sym = u["instrument_token"], u["symbol"]
        if bdb.count_bars(token, interval) == 0:
            continue
        days = split_by_day(_bars_from_rows(bdb.get_bars(token, interval)))
        d_map = {d[0].ts.date().isoformat(): d for d in days}
        per_symbol[sym] = d_map
        sorted_days[sym] = sorted(d_map)
        all_dates.update(d_map)
    return per_symbol, sorted_days, sorted(all_dates)


def _close_price(day_bars) -> float:
    return day_bars[-1].close


def _exit_date_for(sorted_days: List[str], entry: str, D: str) -> Optional[str]:
    """Most recent trading date for a symbol with entry < date <= D, or None."""
    hi = bisect.bisect_right(sorted_days, D)
    if hi == 0:
        return None
    cand = sorted_days[hi - 1]
    return cand if cand > entry else None


def _prior_date(sorted_days: List[str], D: str) -> Optional[str]:
    """Largest trading date strictly before D, or None."""
    lo = bisect.bisect_left(sorted_days, D)
    return sorted_days[lo - 1] if lo > 0 else None


def _calendar_gap(entry: str, D: str) -> int:
    return (date.fromisoformat(D) - date.fromisoformat(entry)).days


def _close_position(conn, pos, exit_date: str, exit_px: float, cfg) -> float:
    """Close one position at exit_px; return the net P&L booked."""
    pnl = trade_pnl(pos["entry_px"], exit_px, pos["qty"], cfg.cost_bps)
    conn.execute(
        "UPDATE mp_trend_positions SET exit_date=?, exit_px=?, gross=?, cost=?, "
        "net=?, status='CLOSED' WHERE id=?",
        (exit_date, exit_px, pnl["gross"], pnl["cost"], pnl["net"], pos["id"]),
    )
    return pnl["net"]


def _realized_series(conn) -> List[float]:
    rows = conn.execute(
        "SELECT net FROM mp_trend_positions WHERE status='CLOSED' "
        "ORDER BY exit_date, id"
    ).fetchall()
    return [float(r["net"]) for r in rows]


def process_date(conn, cfg: MPTrendConfig, per_symbol, sorted_days, D: str) -> None:
    """Exit due longs at their next available close, then (kill-permitting) open D's."""
    now = datetime.now().isoformat(timespec="seconds")

    # 1) Exit open positions at the symbol's most recent close in (entry, D].
    n_closed = 0
    n_carried = 0
    day_net = 0.0
    for pos in conn.execute("SELECT * FROM mp_trend_positions WHERE status='OPEN'").fetchall():
        sym = pos["symbol"]
        sdays = sorted_days.get(sym, [])
        exit_d = _exit_date_for(sdays, pos["entry_date"], D)
        if exit_d is not None:
            day_net += _close_position(
                conn, pos, exit_d, _close_price(per_symbol[sym][exit_d]), cfg)
            n_closed += 1
            continue
        # No bar for this symbol since entry (halt/suspension). Carry — but bound
        # it: a symbol that never trades again (delisting) would otherwise leave
        # the position open forever. Force-close at the last known close past the
        # calendar cap. Either way, do NOT stay silent (Rule 12).
        n_carried += 1
        age = _calendar_gap(pos["entry_date"], D)
        if age > cfg.max_hold_days:
            last_d = sdays[bisect.bisect_right(sdays, D) - 1] if sdays else None
            px = _close_price(per_symbol[sym][last_d]) if last_d else pos["entry_px"]
            day_net += _close_position(conn, pos, D, px, cfg)
            n_closed += 1
            logger.warning("%s: force-closed %s at last known close (held %dd, "
                           "no bar since entry %s)", D, sym, age, pos["entry_date"])
        else:
            logger.warning("%s: carrying %s — no bar since entry %s (held %dd)",
                           D, sym, pos["entry_date"], age)

    # 2) Kill check on the realized series (post-exit), LATCHED: once any prior
    #    run halted, stay halted — a later recovery must not silently resume
    #    entries (the switch is one-way for this paper harness).
    realized = _realized_series(conn)
    kill = check_kill(realized, cfg)
    cum_net = sum(realized)
    latched = conn.execute(
        "SELECT reason FROM mp_trend_runs WHERE halted=1 ORDER BY run_date LIMIT 1"
    ).fetchone()
    if latched is not None:
        halted, reason = True, (latched["reason"] or "latched (prior halt)")
    else:
        halted, reason = kill.halted, kill.reason

    # 3) Classify D and (if broad-momentum + not halted) open longs at D close.
    bars_by_symbol = {s: dmap[D] for s, dmap in per_symbol.items() if D in dmap}
    priors: Dict[str, Optional[DayProfile]] = {}
    for s in bars_by_symbol:
        pd_ = _prior_date(sorted_days[s], D)
        priors[s] = compute_day_profile(per_symbol[s][pd_]) if pd_ else None

    n_trend_up, longs = classify_day_longs(bars_by_symbol, cfg, priors)
    n_opened = 0
    if longs and not halted:
        for sym in longs:
            entry_px = _close_price(bars_by_symbol[sym])
            qty = position_size(cfg.capital, len(longs), entry_px)
            if qty <= 0:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO mp_trend_positions "
                "(symbol, entry_date, entry_px, qty, created_at) VALUES (?,?,?,?,?)",
                (sym, D, entry_px, qty, now),
            )
            n_opened += 1

    conn.execute(
        "INSERT OR REPLACE INTO mp_trend_runs (run_date, n_universe, n_trend_up, "
        "n_opened, n_closed, day_net, cum_net, halted, reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (D, len(bars_by_symbol), n_trend_up, n_opened, n_closed, day_net, cum_net,
         int(halted), reason, now),
    )
    logger.info(
        "%s: universe=%d trend_up=%d opened=%d closed=%d carried=%d "
        "day_net=%.0f cum_net=%.0f%s",
        D, len(bars_by_symbol), n_trend_up, n_opened, n_closed, n_carried,
        day_net, cum_net, f" HALTED({reason})" if halted else "",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--min-signals", type=int, default=3)
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--cost-bps", type=float, default=25.0)
    ap.add_argument("--kill-drawdown", type=float, default=0.06,
                    help="halt on this peak-to-trough fraction of capital (default 0.06)")
    ap.add_argument("--kill-cum-loss", type=float, default=None,
                    help="halt on this absolute ₹ cumulative net loss "
                         "(default: 4%% of --capital, so it scales with size)")
    ap.add_argument("--replay", action="store_true",
                    help="reset the book and walk every available date")
    ap.add_argument("--date", default=None, help="process one specific date (ISO)")
    ap.add_argument("--db", default=None, help="override dashboard.db path")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Default the cum-loss floor to a fraction of capital so both kill triggers
    # scale together — a fixed ₹ floor would trip near-instantly at large size.
    kill_cum_loss = (args.kill_cum_loss if args.kill_cum_loss is not None
                     else 0.04 * args.capital)
    cfg = MPTrendConfig(min_signals=args.min_signals, capital=args.capital,
                        cost_bps=args.cost_bps, kill_max_drawdown=args.kill_drawdown,
                        kill_cum_loss=kill_cum_loss)

    from backend import db as backend_db
    if args.db:
        from pathlib import Path
        backend_db.reset_for_tests(Path(args.db))
    conn = backend_db.get_conn()
    conn.executescript(SCHEMA)

    per_symbol, sorted_days, all_dates = load_universe_days(args.interval)
    if not all_dates:
        logger.warning("No %dm bars in dashboard.db — run fetch_bars.py first.", args.interval)
        return

    if args.replay:
        conn.execute("DELETE FROM mp_trend_positions")
        conn.execute("DELETE FROM mp_trend_runs")
        logger.info("replay: reset book, walking %d dates", len(all_dates))
        for D in all_dates:
            process_date(conn, cfg, per_symbol, sorted_days, D)
    elif args.date:
        process_date(conn, cfg, per_symbol, sorted_days, args.date)
    else:
        D = all_dates[-1]
        done = conn.execute("SELECT 1 FROM mp_trend_runs WHERE run_date=?", (D,)).fetchone()
        if done:
            logger.info("%s already processed — nothing to do.", D)
        else:
            process_date(conn, cfg, per_symbol, sorted_days, D)

    # Summary
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(net),0) net FROM mp_trend_positions "
        "WHERE status='CLOSED'"
    ).fetchone()
    logger.info("book: %d closed trades, cumulative net ₹%.0f", row["n"], row["net"])


if __name__ == "__main__":
    main()
