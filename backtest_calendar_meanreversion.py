"""
Calendar Mean-Reversion Backtester
==================================
Bhavcopy replay for the Varsity-style statistical calendar spread. Drives
`strategies.calendar_meanreversion.CalendarMeanReversionStrategy` through
`backtest_arbitrage.MockKiteArb`, which we reuse unchanged.

What's different from `backtest_arbitrage.py`:

  * We pre-build a per-symbol spread + volume panel from the bhavcopy and
    inject it as the strategy's initial history. The strategy still appends
    today's observation at scan time so live and replay paths agree.
  * The `--compare-vs-arbitrage` flag runs the (post-fix) ArbitrageStrategy
    on the same panel and prints both summaries side by side.

Each tick = one trading day. EOD limitation matches the parent harness; for
this strategy it's actually appropriate — Varsity's signal originates on the
close and is intended to be acted on at the next open.

Usage:
    python backtest_calendar_meanreversion.py
    python backtest_calendar_meanreversion.py --universe RELIANCE,SBIN,ITC
    python backtest_calendar_meanreversion.py --entry-n-sd 1.5 --max-hold 5
    python backtest_calendar_meanreversion.py --compare-vs-arbitrage
"""
from __future__ import annotations

import argparse
import configparser
import logging
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest_arbitrage import (
    MockKiteArb,
    load_stf_panel,
    run_backtest as run_arbitrage_backtest,
)
from strategies.arbitrage import ArbitrageState
from strategies.calendar_meanreversion import (
    CalendarMeanReversionStrategy,
    SpreadHistory,
    VolumeHistory,
)
from backtest_timeframe import warn_coarse_timeframe

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# Spread + volume panel preparation
# ──────────────────────────────────────────────────────────

def build_spread_history(panel: pd.DataFrame) -> Tuple[SpreadHistory, VolumeHistory]:
    """
    Per (date, symbol) build front-month + next-month rows; emit:
      spread_history[sym]  = [(date, F_next - F_curr), ...]   sorted asc
      volume_history[sym]  = [(date, vol_curr, vol_next), ...] sorted asc
    """
    spread_h: SpreadHistory = {}
    vol_h: VolumeHistory = {}

    # groupby(date,symbol) and pick the two earliest expiries (sorted asc).
    grouped = panel.sort_values(["date", "symbol", "expiry"]).groupby(["date", "symbol"])
    for (d, sym), grp in grouped:
        if len(grp) < 2:
            continue
        front = grp.iloc[0]
        nxt = grp.iloc[1]
        spread = float(nxt["close"]) - float(front["close"])
        spread_h.setdefault(sym, []).append((d, spread))

        v_curr = front.get("volume")
        v_next = nxt.get("volume")
        try:
            v_curr_i = int(v_curr) if v_curr is not None and not _isnan(v_curr) else 0
            v_next_i = int(v_next) if v_next is not None and not _isnan(v_next) else 0
        except (TypeError, ValueError):
            v_curr_i = v_next_i = 0
        vol_h.setdefault(sym, []).append((d, v_curr_i, v_next_i))

    # Sort each per-symbol list by date (groupby already does in order, but
    # explicit is safer if the panel was filtered with a different sort).
    for sym in spread_h:
        spread_h[sym].sort(key=lambda r: r[0])
    for sym in vol_h:
        vol_h[sym].sort(key=lambda r: r[0])
    return spread_h, vol_h


def slice_history_before(
    full: SpreadHistory, before: object,
) -> SpreadHistory:
    """Return a copy of `full` containing only entries with date < `before`.

    Used to seed the strategy with only the warm-up portion of history before
    the first replay date — so the strategy's first scan tick has a real
    rolling baseline rather than an empty buffer that would silently fail
    the `min_history` gate forever.
    """
    out: SpreadHistory = {}
    for sym, rows in full.items():
        prefix = [r for r in rows if r[0] < before]
        if prefix:
            out[sym] = prefix
    return out


def slice_volume_before(
    full: VolumeHistory, before: object,
) -> VolumeHistory:
    out: VolumeHistory = {}
    for sym, rows in full.items():
        prefix = [r for r in rows if r[0] < before]
        if prefix:
            out[sym] = prefix
    return out


# ──────────────────────────────────────────────────────────
# Strategy bootstrap (bypass __init__ to avoid kite_auth)
# ──────────────────────────────────────────────────────────

def make_strategy(
    kite: MockKiteArb,
    universe: List[str],
    *,
    spread_history: SpreadHistory,
    volume_history: VolumeHistory,
    risk_free_rate: float = 0.07,
    dividend_yield: float = 0.0,
    lookback_days: int = 200,
    entry_n_sd: float = 1.0,
    exit_n_sd: float = 0.25,
    stop_loss_n_sd: float = 0.5,
    max_hold_days: int = 3,
    require_dte_near_le: int = 7,
    min_history: int = 60,
    min_avg_volume: int = 100_000,
    max_open: int = 5,
    lots_per_leg: int = 1,
    max_leg_notional: Optional[float] = 500_000,
    allow_long: bool = True,
    allow_short: bool = True,
    dividend_yields: Optional[Dict[str, float]] = None,
) -> CalendarMeanReversionStrategy:
    s = CalendarMeanReversionStrategy.__new__(CalendarMeanReversionStrategy)
    # Parent (ArbitrageStrategy) attributes — keep in sync with backtest_arbitrage.make_strategy.
    s.kite = kite
    s.config = configparser.ConfigParser()
    s.config_path = "config.ini"
    s.mode = "paper"
    s.universe = list(universe)
    s.risk_free_rate = risk_free_rate
    s.dividend_yield = dividend_yield
    s.dividend_yields = dict(dividend_yields or {})
    s.basis_entry_annual = 9.99   # silence basis arm in backtest output
    s.basis_min_dte = 99
    s.calendar_entry_annual = 9.99
    s.calendar_exit_annual = 0.0
    s.calendar_max_holding_days = 999
    s.calendar_min_dte_near = 999
    s.calendar_max_leg_basis = 0.0
    s.disable_calendar = True
    s.lots_per_leg = 1
    s.max_open_calendars = 5
    s.max_leg_notional = None
    s.total_capital = 500_000
    s.state = ArbitrageState()
    s._instrument_cache = None
    s._ts_to_name = {}
    s._load_instruments = lambda: kite.instruments("NFO")
    s._clock = lambda: datetime.combine(kite.current_date, datetime.min.time())

    # Mean-rev attributes.
    s.lookback_days = lookback_days
    s.entry_n_sd = entry_n_sd
    s.exit_n_sd = exit_n_sd
    s.stop_loss_n_sd = stop_loss_n_sd
    s.mr_max_hold_days = max_hold_days
    s.require_dte_near_le = require_dte_near_le
    s.min_history = min_history
    s.min_avg_volume = min_avg_volume
    s.mr_max_open = max_open
    s.mr_lots_per_leg = lots_per_leg
    s.mr_max_leg_notional = max_leg_notional
    s.allow_long = allow_long
    s.allow_short = allow_short
    s._spread_history = dict(spread_history)
    s._volume_history = dict(volume_history)
    s._last_history_date = {}
    s._entry_context = {}
    return s


# ──────────────────────────────────────────────────────────
# Backtest core
# ──────────────────────────────────────────────────────────

def run_backtest(
    panel: pd.DataFrame,
    *,
    entry_n_sd: float = 1.0,
    exit_n_sd: float = 0.25,
    stop_loss_n_sd: float = 0.5,
    max_hold_days: int = 3,
    require_dte_near_le: int = 7,
    min_history: int = 60,
    min_avg_volume: int = 100_000,
    lookback_days: int = 200,
    max_open: int = 5,
    lots_per_leg: int = 1,
    max_leg_notional: Optional[float] = 500_000,
    allow_long: bool = True,
    allow_short: bool = True,
) -> dict:
    universe = sorted(panel["symbol"].unique().tolist())
    full_spread_h, full_vol_h = build_spread_history(panel)

    mock = MockKiteArb(panel)
    first_date = mock.current_date
    seed_spread = slice_history_before(full_spread_h, first_date)
    seed_vol = slice_volume_before(full_vol_h, first_date)

    s = make_strategy(
        mock, universe,
        spread_history=seed_spread, volume_history=seed_vol,
        entry_n_sd=entry_n_sd, exit_n_sd=exit_n_sd,
        stop_loss_n_sd=stop_loss_n_sd, max_hold_days=max_hold_days,
        require_dte_near_le=require_dte_near_le,
        min_history=min_history, min_avg_volume=min_avg_volume,
        lookback_days=lookback_days, max_open=max_open,
        lots_per_leg=lots_per_leg, max_leg_notional=max_leg_notional,
        allow_long=allow_long, allow_short=allow_short,
    )

    pnl_curve: List[dict] = []
    while True:
        try:
            # Append the day's volume snapshot before scan, so the liquidity
            # filter sees today's data. Spread is appended inside scan via
            # _record_history (idempotent).
            today = mock.current_date
            for sym, rows in full_vol_h.items():
                today_rows = [r for r in rows if r[0] == today]
                if not today_rows:
                    continue
                hist = s._volume_history.setdefault(sym, [])
                if hist and hist[-1][0] == today:
                    hist[-1] = today_rows[-1]
                else:
                    hist.append(today_rows[-1])
                cap = max(s.lookback_days * 2, 500)
                if len(hist) > cap:
                    del hist[: len(hist) - cap]

            entries = s.scan_and_propose()
            if entries:
                s.execute_proposals(entries)
            exits = s.check_and_rehedge()
            if exits:
                s.execute_proposals(exits)
        except Exception as e:
            logger.warning("tick %s failed: %s", mock.current_date, e)

        pnl_curve.append({
            "date": mock.current_date,
            "realized": s.state.realized_pnl,
            "unrealized": s.state.unrealized_pnl,
            "total": s.state.realized_pnl + s.state.unrealized_pnl,
            "n_open": len(s.state.open_calendars),
        })

        if not mock.advance():
            break

    # Force-close any open calendars at the last bar.
    if s.state.open_calendars:
        last_snaps = {snap["symbol"]: snap for snap in s._observe_universe()}
        s._update_unrealized(last_snaps)
        for symbol, trade in list(s.state.open_calendars.items()):
            snap = last_snaps.get(symbol)
            if not snap:
                continue
            close_props = s._build_calendar_exit(trade, snap, "EOD_CLOSE")
            if close_props:
                s.execute_proposals(close_props)
        pnl_curve.append({
            "date": mock.current_date,
            "realized": s.state.realized_pnl,
            "unrealized": s.state.unrealized_pnl,
            "total": s.state.realized_pnl + s.state.unrealized_pnl,
            "n_open": len(s.state.open_calendars),
        })

    pnl_df = pd.DataFrame(pnl_curve).set_index("date")
    return {
        "n_days": len(pnl_df),
        "n_round_trips": len(s.state.closed_trades),
        "n_orders": len(mock._orders),
        "realized_pnl": s.state.realized_pnl,
        "unrealized_pnl": s.state.unrealized_pnl,
        "total_pnl": s.state.realized_pnl + s.state.unrealized_pnl,
        "transaction_costs": s.state.total_transaction_costs,
        "max_drawdown": _max_drawdown(pnl_df["total"].values),
        "pnl_curve": pnl_df,
        "closed_trades": s.state.closed_trades,
        "history_sizes": {
            sym: len(h) for sym, h in s._spread_history.items()
        },
    }


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float((equity - peak).min())


def _isnan(x) -> bool:
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False


# ──────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────

def print_report(result: dict, args, label: str = "Mean-rev calendar") -> None:
    print()
    print(f"{label} backtest")
    print(f"  universe={args.universe or 'all STF in archive'}")
    print(f"  entry_n_sd={args.entry_n_sd}  exit_n_sd={args.exit_n_sd} "
          f"stop_n_sd={args.stop_loss_n_sd}  max_hold={args.max_hold}d  "
          f"dte_near≤{args.require_dte_near_le}d")
    print(f"  min_history={args.min_history}  min_avg_volume={args.min_avg_volume:,} "
          f"lookback={args.lookback_days}d  lots={args.lots_per_leg}  cap=₹{args.max_leg_notional:,.0f}")
    print(f"  allow_long={args.allow_long}  allow_short={args.allow_short}")
    print("=" * 95)
    print(f"  Days replayed:        {result['n_days']}")
    print(f"  Calendars opened:     {result['n_round_trips']}  (orders placed: {result['n_orders']})")
    print("-" * 95)
    print(f"  Realized P&L:        ₹{result['realized_pnl']:>12,.0f}")
    print(f"  Unrealized P&L:      ₹{result['unrealized_pnl']:>12,.0f}")
    print(f"  Net P&L:             ₹{result['total_pnl']:>12,.0f}  "
          f"(after costs ₹{result['transaction_costs']:,.0f})")
    print(f"  Gross P&L:           ₹{(result['total_pnl'] + result['transaction_costs']):>12,.0f}")
    print(f"  Max drawdown:        ₹{result['max_drawdown']:>12,.0f}")
    print("=" * 95)

    if result["closed_trades"]:
        sorted_trades = sorted(
            result["closed_trades"], key=lambda t: t["realized_pnl"], reverse=True,
        )
        # Per-symbol breakdown — Varsity flagged per-name asymmetry as a real
        # finding; surface it so the operator can flip allow_long / allow_short
        # per-deployment.
        by_symbol: Dict[str, dict] = {}
        for t in result["closed_trades"]:
            row = by_symbol.setdefault(
                t["symbol"], {"n": 0, "wins": 0, "pnl": 0.0, "long_n": 0, "short_n": 0,
                              "long_pnl": 0.0, "short_pnl": 0.0}
            )
            row["n"] += 1
            row["wins"] += 1 if t["realized_pnl"] > 0 else 0
            row["pnl"] += t["realized_pnl"]
            if t["position"] == "LONG_CALENDAR":
                row["long_n"] += 1
                row["long_pnl"] += t["realized_pnl"]
            else:
                row["short_n"] += 1
                row["short_pnl"] += t["realized_pnl"]
        print("\nPer-symbol breakdown (n / win-rate / total realized / by-direction):")
        print(f"  {'symbol':<14} {'n':>3} {'win%':>5} {'realized':>12} "
              f"{'long_n':>6} {'long_pnl':>10} {'short_n':>7} {'short_pnl':>10}")
        for sym in sorted(by_symbol, key=lambda s: by_symbol[s]["pnl"], reverse=True):
            r = by_symbol[sym]
            wr = (r["wins"] / r["n"]) * 100 if r["n"] else 0
            print(f"  {sym:<14} {r['n']:>3} {wr:>4.0f}% {r['pnl']:>+12,.0f} "
                  f"{r['long_n']:>6} {r['long_pnl']:>+10,.0f} "
                  f"{r['short_n']:>7} {r['short_pnl']:>+10,.0f}")

        head = sorted_trades[:5]
        tail = sorted_trades[-5:] if len(sorted_trades) > 5 else []
        print("\nTop 5 trades by realized P&L:")
        for t in head:
            print(f"  {t['symbol']:<14} {t['position']:<16} entry_z={t['entry_carry_diff']:+.2f}  "
                  f"realized=₹{t['realized_pnl']:>+10,.0f}  costs=₹{t['transaction_costs']:>7,.0f}")
        if tail:
            print("Bottom 5:")
            for t in tail:
                print(f"  {t['symbol']:<14} {t['position']:<16} entry_z={t['entry_carry_diff']:+.2f}  "
                      f"realized=₹{t['realized_pnl']:>+10,.0f}  costs=₹{t['transaction_costs']:>7,.0f}")
    print()


def _print_arbitrage_summary(arb_result: dict) -> None:
    print()
    print("Theoretical-carry arbitrage backtest (for comparison)")
    print("=" * 95)
    print(f"  Days replayed:        {arb_result['n_days']}")
    print(f"  Calendars opened:     {arb_result['n_round_trips']}")
    print(f"  Net P&L:             ₹{arb_result['total_pnl']:>12,.0f}  "
          f"(after costs ₹{arb_result['transaction_costs']:,.0f})")
    print(f"  Gross P&L:           ₹{(arb_result['total_pnl'] + arb_result['transaction_costs']):>12,.0f}")
    print(f"  Max drawdown:        ₹{arb_result['max_drawdown']:>12,.0f}")
    print("=" * 95)


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    p = argparse.ArgumentParser(description="EOD bhavcopy backtest for the Varsity-style mean-rev calendar")
    p.add_argument("--universe", type=str, default=None,
                   help="Comma-separated underlyings. Default: every STF in the bhavcopy archive.")
    p.add_argument("--entry-n-sd", type=float, default=1.0, dest="entry_n_sd")
    p.add_argument("--exit-n-sd", type=float, default=0.25, dest="exit_n_sd")
    p.add_argument("--stop-loss-n-sd", type=float, default=0.5, dest="stop_loss_n_sd")
    p.add_argument("--max-hold", type=int, default=3, dest="max_hold")
    p.add_argument("--require-dte-near-le", type=int, default=7, dest="require_dte_near_le")
    p.add_argument("--min-history", type=int, default=60, dest="min_history")
    p.add_argument("--min-avg-volume", type=int, default=100_000, dest="min_avg_volume")
    p.add_argument("--lookback-days", type=int, default=200, dest="lookback_days")
    p.add_argument("--lots-per-leg", type=int, default=1, dest="lots_per_leg")
    p.add_argument("--max-open", type=int, default=5, dest="max_open")
    p.add_argument("--max-leg-notional", type=float, default=500_000, dest="max_leg_notional")
    p.add_argument("--allow-long", type=lambda v: str(v).lower() in ("true", "1", "yes"),
                   default=True, dest="allow_long")
    p.add_argument("--allow-short", type=lambda v: str(v).lower() in ("true", "1", "yes"),
                   default=True, dest="allow_short")
    p.add_argument("--from", type=str, default=None, dest="date_from",
                   help="Start of replay window (YYYY-MM-DD). History before this is used as warm-up.")
    p.add_argument("--to", type=str, default=None, dest="date_to")
    p.add_argument("--save-curve", type=str, default=None)
    p.add_argument("--compare-vs-arbitrage", action="store_true",
                   help="Also run the theoretical-carry arb strategy on the same panel and print both summaries.")
    p.add_argument("--instrument-types", type=str, default="STF",
                   dest="instrument_types",
                   help="Comma-separated FinInstrmTp values: STF (single-stock futures, default), "
                        "IDF (index futures: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50), "
                        "or both (STF,IDF). For IDF you almost certainly want to raise "
                        "--max-leg-notional (NIFTY ≈ ₹1.6M/lot, BANKNIFTY ≈ ₹1.7M/lot).")
    args = p.parse_args()

    warn_coarse_timeframe("daily", backtest="backtest_calendar_meanreversion",
                          logger=logger,
                          reason="no 5-min single-stock-futures data — daily "
                          "bhavcopy only; run fetch_5min_stf.py on the host to "
                          "build a 5-min STF corpus (issue #63)")

    universe = (
        [s.strip().upper() for s in args.universe.split(",") if s.strip()]
        if args.universe else None
    )
    instrument_types = tuple(
        t.strip().upper() for t in args.instrument_types.split(",") if t.strip()
    )
    logger.info("Loading panel %s from bhavcopy archive…", instrument_types)
    full_panel = load_stf_panel(universe=universe, instrument_types=instrument_types)
    logger.info("Full panel: %d rows over %d trading days, %d underlyings",
                len(full_panel), full_panel["date"].nunique(), full_panel["symbol"].nunique())

    # If --from is given, use everything before it as warm-up history; the
    # backtest itself only replays from that date onward.
    if args.date_from:
        d0 = datetime.strptime(args.date_from, "%Y-%m-%d").date()
        warmup_panel = full_panel[full_panel["date"] < d0]
        replay_panel = full_panel[full_panel["date"] >= d0]
    else:
        warmup_panel = full_panel.iloc[:0]
        replay_panel = full_panel
    if args.date_to:
        d1 = datetime.strptime(args.date_to, "%Y-%m-%d").date()
        replay_panel = replay_panel[replay_panel["date"] <= d1]
    if replay_panel.empty:
        logger.error("Replay panel is empty after date filtering")
        return 1

    if not warmup_panel.empty:
        logger.info("Warm-up: %d rows over %d days; replay: %d rows over %d days",
                    len(warmup_panel), warmup_panel["date"].nunique(),
                    len(replay_panel), replay_panel["date"].nunique())

    # The strategy's internal seeder will read warmup via build_spread_history
    # over the COMBINED panel (warmup + replay), then slice by first replay date.
    combined_panel = pd.concat([warmup_panel, replay_panel], ignore_index=True)
    full_spread_h, full_vol_h = build_spread_history(combined_panel)

    mock = MockKiteArb(replay_panel)
    first_date = mock.current_date
    seed_spread = slice_history_before(full_spread_h, first_date)
    seed_vol = slice_volume_before(full_vol_h, first_date)

    universe_replay = sorted(replay_panel["symbol"].unique().tolist())
    s = make_strategy(
        mock, universe_replay,
        spread_history=seed_spread, volume_history=seed_vol,
        entry_n_sd=args.entry_n_sd, exit_n_sd=args.exit_n_sd,
        stop_loss_n_sd=args.stop_loss_n_sd, max_hold_days=args.max_hold,
        require_dte_near_le=args.require_dte_near_le,
        min_history=args.min_history, min_avg_volume=args.min_avg_volume,
        lookback_days=args.lookback_days, max_open=args.max_open,
        lots_per_leg=args.lots_per_leg, max_leg_notional=args.max_leg_notional,
        allow_long=args.allow_long, allow_short=args.allow_short,
    )

    pnl_curve: List[dict] = []
    while True:
        try:
            today = mock.current_date
            for sym, rows in full_vol_h.items():
                today_rows = [r for r in rows if r[0] == today]
                if not today_rows:
                    continue
                hist = s._volume_history.setdefault(sym, [])
                if hist and hist[-1][0] == today:
                    hist[-1] = today_rows[-1]
                else:
                    hist.append(today_rows[-1])
                cap = max(s.lookback_days * 2, 500)
                if len(hist) > cap:
                    del hist[: len(hist) - cap]

            entries = s.scan_and_propose()
            if entries:
                s.execute_proposals(entries)
            exits = s.check_and_rehedge()
            if exits:
                s.execute_proposals(exits)
        except Exception as e:
            logger.warning("tick %s failed: %s", mock.current_date, e)

        pnl_curve.append({
            "date": mock.current_date,
            "realized": s.state.realized_pnl,
            "unrealized": s.state.unrealized_pnl,
            "total": s.state.realized_pnl + s.state.unrealized_pnl,
            "n_open": len(s.state.open_calendars),
        })

        if not mock.advance():
            break

    if s.state.open_calendars:
        last_snaps = {snap["symbol"]: snap for snap in s._observe_universe()}
        s._update_unrealized(last_snaps)
        for symbol, trade in list(s.state.open_calendars.items()):
            snap = last_snaps.get(symbol)
            if not snap:
                continue
            close_props = s._build_calendar_exit(trade, snap, "EOD_CLOSE")
            if close_props:
                s.execute_proposals(close_props)

    pnl_df = pd.DataFrame(pnl_curve).set_index("date")
    result = {
        "n_days": len(pnl_df),
        "n_round_trips": len(s.state.closed_trades),
        "n_orders": len(mock._orders),
        "realized_pnl": s.state.realized_pnl,
        "unrealized_pnl": s.state.unrealized_pnl,
        "total_pnl": s.state.realized_pnl + s.state.unrealized_pnl,
        "transaction_costs": s.state.total_transaction_costs,
        "max_drawdown": _max_drawdown(pnl_df["total"].values),
        "pnl_curve": pnl_df,
        "closed_trades": s.state.closed_trades,
    }
    print_report(result, args, label="Calendar mean-reversion (Varsity-style)")

    if args.save_curve:
        result["pnl_curve"].to_csv(args.save_curve)
        logger.info("Wrote daily P&L curve to %s", args.save_curve)

    if args.compare_vs_arbitrage:
        logger.info("Running theoretical-carry arbitrage strategy on the same panel for comparison…")
        arb_result = run_arbitrage_backtest(replay_panel, max_leg_notional=args.max_leg_notional)
        _print_arbitrage_summary(arb_result)

    return 0


if __name__ == "__main__":
    sys.exit(main())
