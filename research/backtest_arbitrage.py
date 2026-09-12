"""
Arbitrage Backtester
====================
Replays cached F&O bhavcopy through ArbitrageStrategy via a MockKiteArb
adapter and reports per-symbol calendar-spread P&L plus a summary of
cash-futures basis signals.

Each "tick" is one trading day. The bhavcopy provides:
  - UndrlygPric per row → spot S
  - All STF rows for that ticker → multiple expiries (near, next, far)
  - NewBrdLotQty → lot size

EOD-only is a real limitation: most basis trades close intraday. We use
this backtester to:
  - Characterize entry-threshold sensitivity for the calendar spread
  - Track convergence-to-expiry P&L (pin convergence is real signal)
  - Count cash-futures basis events without sizing them

Open positions at the end of the replay are force-closed at the last bar.

Usage:
    python -m research.backtest_arbitrage
    python -m research.backtest_arbitrage --calendar-entry 0.015 --max-hold 10
    python -m research.backtest_arbitrage --universe RELIANCE,INFY,HDFCBANK
"""
from __future__ import annotations

import argparse
import configparser
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


from strategies.arbitrage import ArbitrageState, ArbitrageStrategy
from core.backtest_timeframe import warn_coarse_timeframe
from core.data_cache_io import find_tables, read_table, table_columns
from core import universe as _universe

# Trailing sessions used to judge whether a universe symbol is still on the
# tape. Matches core.screen_pairs._TAIL_DAYS — a symbol reported gone here and
# a symbol dropped for tail coverage there should be judged over one window.
_UNRESOLVED_TAIL_DAYS = 30

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_DIR = CACHE_DIR / "bhavcopy_raw"


# ──────────────────────────────────────────────────────────
# Bhavcopy ingestion → per-day STF panel
# ──────────────────────────────────────────────────────────

def load_stf_panel(
    raw_dir: Path = RAW_DIR,
    universe: Optional[List[str]] = None,
    instrument_types: Tuple[str, ...] = ("STF",),
) -> pd.DataFrame:
    """
    Build a long-form DataFrame of futures rows across the bhavcopy archive:
        date, symbol, expiry, tradingsymbol, close, spot, lot_size, volume

    One row per (date, symbol, expiry). `volume` is `TtlTradgVol` from the
    bhavcopy and is consumed by the mean-reversion calendar strategy's
    liquidity filter. If a particular CSV in the archive lacks the column
    (older NSE schemas), that file's rows still load with `volume = NaN`
    and downstream filters treat NaN as passing.

    `instrument_types` defaults to single-stock-futures only (back-compat).
    Pass `("IDF",)` for index futures (NIFTY, BANKNIFTY, FINNIFTY, …) or
    `("STF", "IDF")` for both. IDF rows have no underlying spot in the bhavcopy
    sense — `UndrlygPric` carries the spot index level, which is fine for the
    mean-reversion calendar (which only needs F_curr and F_next). The basis
    arm in `[arbitrage]` would be meaningless on indices anyway because the
    cash-and-carry trade isn't replicable on an index.
    """
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    if not files:
        raise RuntimeError(f"No bhavcopy tables in {raw_dir}")

    base_cols = ["TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "FinInstrmNm",
                 "ClsPric", "UndrlygPric", "NewBrdLotQty"]
    optional_cols = ["TtlTradgVol"]
    frames = []
    for f in files:
        # Probe header so we don't blow up on archives missing TtlTradgVol.
        try:
            header = table_columns(f)
        except Exception as e:
            logger.warning("skip %s: %s", f.name, e)
            continue
        cols = base_cols + [c for c in optional_cols if c in header]
        try:
            df = read_table(f, usecols=cols, dtype={"TckrSymb": str, "FinInstrmNm": str})
        except Exception as e:
            logger.warning("skip %s: %s", f.name, e)
            continue
        df = df[df["FinInstrmTp"].isin(instrument_types)]
        if "TtlTradgVol" not in df.columns:
            df = df.assign(TtlTradgVol=pd.NA)
        if universe:
            # Retired tickers admitted too: a renamed symbol's history lives
            # under its OLD name in the archive and apply_long() relabels it
            # onto the current ticker below (issue #226).
            wanted = set(universe) | {
                old_sym for old_sym, new_sym in _universe.SYMBOL_ALIASES.items()
                if new_sym in set(universe)
            }
            df = df[df["TckrSymb"].isin(wanted)]
        frames.append(df)

    if not frames:
        raise RuntimeError(
            f"No rows of types {instrument_types} found in bhavcopy archive"
        )
    out = pd.concat(frames, ignore_index=True)
    out = out.rename(columns={
        "TradDt": "date", "TckrSymb": "symbol", "XpryDt": "expiry",
        "FinInstrmNm": "tradingsymbol", "ClsPric": "close",
        "UndrlygPric": "spot", "NewBrdLotQty": "lot_size",
        "TtlTradgVol": "volume",
    })
    out["date"] = pd.to_datetime(out["date"]).dt.date
    out["expiry"] = pd.to_datetime(out["expiry"]).dt.date
    out["lot_size"] = out["lot_size"].astype(int)
    out["close"] = out["close"].astype(float)
    out["spot"] = out["spot"].astype(float)
    # `volume` may be NaN for archives lacking the column; coerce numeric.
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce")
    # Corporate actions (issue #226). One call here covers BOTH the research
    # backtests and calendar_meanreversion._seed_spread_history_from_bhavcopy,
    # which seeds the paper/live runner off this same loader — a demerger's
    # pre-cutoff rows must not reach a rolling spread statistic either.
    out = _universe.apply_long(out)
    if universe and not out.empty:
        # Resolve against the RECENT TAIL of the archive, not every symbol
        # ever seen in it: a name that traded for two years and then stopped
        # is still in `out["symbol"].unique()`, so a full-window check stays
        # silent on exactly the decay it is meant to catch (review of PR #227).
        _recent = sorted(out["date"].unique())[-_UNRESOLVED_TAIL_DAYS:]
        _universe.report_unresolved(
            universe, out.loc[out["date"].isin(_recent), "symbol"].unique(),
            f"bhavcopy STF panel (last {len(_recent)} sessions)", log=logger)
    out = out.sort_values(["date", "symbol", "expiry"]).reset_index(drop=True)
    return out


# ──────────────────────────────────────────────────────────
# MockKiteArb — feeds one-day-at-a-time slices to the strategy
# ──────────────────────────────────────────────────────────

class MockKiteArb:
    """
    Minimal Kite stand-in for the arbitrage strategy.

    `instruments("NFO")` returns the STF rows for the *current* date so
    `_symbol_futures_sorted` resolves only to expiries trading that day.

    `quote(["NFO:SYMBOL26APRFUT"])` returns that day's close. `quote(["NSE:RELIANCE"])`
    returns the underlying spot from the same row.
    """

    VARIETY_REGULAR = "regular"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, panel: pd.DataFrame):
        self.panel = panel
        self._dates = sorted(panel["date"].unique())
        self._idx = 0
        self._orders: List[dict] = []
        self._day_cache: Dict[Tuple, dict] = {}

    @property
    def current_date(self):
        return self._dates[self._idx]

    def advance(self) -> bool:
        if self._idx + 1 < len(self._dates):
            self._idx += 1
            self._day_cache.clear()
            return True
        return False

    def _today_rows(self) -> pd.DataFrame:
        d = self.current_date
        return self.panel[self.panel["date"] == d]

    def instruments(self, exchange: str) -> List[dict]:
        if exchange != "NFO":
            return []
        cache_key = ("instr", self.current_date)
        if cache_key in self._day_cache:
            return self._day_cache[cache_key]
        rows = []
        for _, r in self._today_rows().iterrows():
            rows.append({
                "name": r["symbol"],
                "tradingsymbol": r["tradingsymbol"],
                "instrument_type": "FUT",
                "lot_size": int(r["lot_size"]),
                "expiry": r["expiry"].isoformat(),
                "instrument_token": abs(hash(r["tradingsymbol"])) % 1_000_000,
            })
        self._day_cache[cache_key] = rows
        return rows

    def _today_indexes(self) -> Tuple[Dict[str, float], Dict[str, float]]:
        cache_key = ("idx", self.current_date)
        cached = self._day_cache.get(cache_key)
        if cached is not None:
            return cached
        rows = self._today_rows()
        ts_to_close = dict(zip(rows["tradingsymbol"], rows["close"]))
        sym_to_spot = (
            rows.drop_duplicates("symbol")
                .set_index("symbol")["spot"]
                .to_dict()
        )
        result = (ts_to_close, sym_to_spot)
        self._day_cache[cache_key] = result
        return result

    def quote(self, symbols: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        ts_to_close, sym_to_spot = self._today_indexes()
        for s in symbols:
            ex, base = s.split(":", 1)
            if ex == "NFO":
                px = ts_to_close.get(base)
                if px is not None:
                    out[s] = {
                        "last_price": float(px),
                        "depth": {
                            "buy":  [{"price": float(px) * 0.9995}],
                            "sell": [{"price": float(px) * 1.0005}],
                        },
                    }
            elif ex == "NSE":
                spot = sym_to_spot.get(base)
                if spot is not None:
                    out[s] = {"last_price": float(spot)}
        return out

    def place_order(self, **kwargs):
        order_id = f"BT-{len(self._orders)}-{self._idx}"
        self._orders.append({**kwargs, "order_id": order_id, "date": str(self.current_date)})
        return order_id

    def profile(self):
        return {"user_name": "Backtest", "user_id": "BT0", "exchanges": ["NFO"], "products": ["NRML"]}


# ──────────────────────────────────────────────────────────
# Strategy bootstrap (bypass __init__ to avoid touching kite_auth)
# ──────────────────────────────────────────────────────────

def make_strategy(
    kite: MockKiteArb,
    universe: List[str],
    *,
    risk_free_rate: float,
    dividend_yield: float,
    basis_entry_annual: float,
    calendar_entry_annual: float,
    calendar_exit_annual: float,
    calendar_max_holding_days: int,
    calendar_min_dte_near: int,
    calendar_max_leg_basis: float,
    basis_min_dte: int,
    lots_per_leg: int,
    max_open_calendars: int,
    max_leg_notional: Optional[float],
    dividend_yields: Optional[Dict[str, float]] = None,
) -> ArbitrageStrategy:
    s = ArbitrageStrategy.__new__(ArbitrageStrategy)
    s.kite = kite
    s.config = configparser.ConfigParser()
    s.config_path = "config.ini"
    s.mode = "paper"
    s.universe = list(universe)
    s.risk_free_rate = risk_free_rate
    s.dividend_yield = dividend_yield
    s.dividend_yields = dict(dividend_yields or {})
    s.basis_entry_annual = basis_entry_annual
    s.basis_min_dte = basis_min_dte
    s.calendar_entry_annual = calendar_entry_annual
    s.calendar_exit_annual = calendar_exit_annual
    s.calendar_max_holding_days = calendar_max_holding_days
    s.calendar_min_dte_near = calendar_min_dte_near
    s.calendar_max_leg_basis = calendar_max_leg_basis
    # Production default for the rupee cost hurdle so the backtest grades the
    # same gate that trades. The exit debounce is 1 (= off) here: this replay
    # is DAILY-cadence (one check_and_rehedge per bhavcopy day), so N ticks
    # would mean N DAYS — not the ~N-minute intraday noise filter it is live.
    s.calendar_cost_hurdle_mult = 2.0
    s.calendar_exit_debounce_ticks = 1
    # This builder bypasses __init__ via __new__, so EVERY attribute the
    # strategy dereferences MUST be set here — a missing one kills every
    # backtest tick with a swallowed AttributeError (code-review 2026-07-11:
    # calendar_stop_loss_mult was missing from day one, and
    # calendar_margin_pct had been missing since 2026-06-17, i.e. the
    # backtest had been silently dead for weeks). test_arbitrage.py's
    # builder-parity test now sweeps __init__'s attribute list via AST, so
    # the next omission fails CI instead of returning flat P&L.
    # calendar_entry_min_dte needs nothing: it is a @property derived from
    # calendar_min_dte_near / calendar_max_holding_days above.
    s.calendar_stop_loss_mult = 1.0     # production default (thesis stop)
    s.calendar_margin_pct = 0.06        # production default (2026-06-17 fix)
    # Mirror of the __init__ default (#233). Measure-only, and inert here in
    # any case: the backtest feed has no depth, so _expected_crossing_cost
    # returns 0.0 regardless.
    s.calendar_crossing_mult = 0.0
    # Session-delta baselines (read by generate_eod_report; latent here only
    # because the backtest never called it — set for parity anyway).
    s._session_start_realized = 0.0
    s._session_start_unrealized = 0.0
    s.disable_calendar = False
    s.lots_per_leg = lots_per_leg
    s.max_open_calendars = max_open_calendars
    s.max_leg_notional = max_leg_notional
    s.total_capital = 500_000
    s.state = ArbitrageState()
    s._ts_to_name = {}
    # Crucial: the instrument cache must NOT persist across days because
    # each bhavcopy day publishes a different set of expiring contracts.
    # We override _load_instruments to bypass the per-instance cache.
    s._instrument_cache = None
    s._load_instruments = lambda: kite.instruments("NFO")
    s._clock = lambda: datetime.combine(kite.current_date, datetime.min.time())
    return s


# ──────────────────────────────────────────────────────────
# Backtest core
# ──────────────────────────────────────────────────────────

def run_backtest(
    panel: pd.DataFrame,
    *,
    risk_free_rate: float = 0.07,
    dividend_yield: float = 0.0,
    basis_entry_annual: float = 0.015,
    basis_min_dte: int = 3,
    calendar_entry_annual: float = 0.020,
    calendar_exit_annual: float = 0.005,
    calendar_max_holding_days: int = 15,
    calendar_min_dte_near: int = 4,
    calendar_max_leg_basis: float = 0.10,
    lots_per_leg: int = 1,
    max_open_calendars: int = 5,
    max_leg_notional: Optional[float] = None,
    dividend_yields: Optional[Dict[str, float]] = None,
) -> dict:
    universe = sorted(panel["symbol"].unique().tolist())
    mock = MockKiteArb(panel)
    s = make_strategy(
        mock, universe,
        risk_free_rate=risk_free_rate, dividend_yield=dividend_yield,
        basis_entry_annual=basis_entry_annual,
        calendar_entry_annual=calendar_entry_annual,
        calendar_exit_annual=calendar_exit_annual,
        calendar_max_holding_days=calendar_max_holding_days,
        calendar_min_dte_near=calendar_min_dte_near,
        calendar_max_leg_basis=calendar_max_leg_basis,
        basis_min_dte=basis_min_dte,
        lots_per_leg=lots_per_leg,
        max_open_calendars=max_open_calendars,
        max_leg_notional=max_leg_notional,
        dividend_yields=dividend_yields,
    )

    pnl_curve: List[dict] = []
    basis_events: List[dict] = []
    n_ticks = 0
    n_tick_errors = 0

    while True:
        try:
            # Capture basis events out of band — paper mode by default does
            # not emit basis signals. Sample them directly from the snapshot.
            snapshots = s._observe_universe()
            for snap in snapshots:
                if snap["dte_near"] >= basis_min_dte and abs(snap["basis_annual"]) >= basis_entry_annual:
                    basis_events.append({
                        "date": mock.current_date,
                        "symbol": snap["symbol"],
                        "spot": snap["spot"],
                        "fut": snap["near_price"],
                        "dte": snap["dte_near"],
                        "basis_annual": snap["basis_annual"],
                    })

            entries = s.scan_and_propose()
            if entries:
                s.execute_proposals(entries)
            exits = s.check_and_rehedge()
            if exits:
                s.execute_proposals(exits)
        except Exception as e:
            logger.warning("tick %s failed: %s", mock.current_date, e)
            n_tick_errors += 1
        n_ticks += 1

        pnl_curve.append({
            "date": mock.current_date,
            "realized": s.state.realized_pnl,
            "unrealized": s.state.unrealized_pnl,
            "total": s.state.realized_pnl + s.state.unrealized_pnl,
            "n_open": len(s.state.open_calendars),
        })

        if not mock.advance():
            break

    # Force-close any open calendars at last bar.
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

    # Fail LOUD when every tick errored (Rule 12; code-review 2026-07-11): a
    # systematic bug (e.g. an attribute missing from the __new__ bootstrap)
    # used to be swallowed per-tick above and returned as a clean 0-trade,
    # flat-P&L result that sweeps then treated as a real measurement.
    if n_ticks > 0 and n_tick_errors == n_ticks:
        raise RuntimeError(
            f"ALL {n_ticks} backtest ticks raised — this is a harness/strategy "
            "bug, not a no-trade market; see the per-tick warnings above.")

    pnl_df = pd.DataFrame(pnl_curve).set_index("date")
    return {
        "n_days": len(pnl_df),
        "n_round_trips": len(s.state.closed_trades),
        "n_orders": len(mock._orders),
        "n_basis_events": len(basis_events),
        "realized_pnl": s.state.realized_pnl,
        "unrealized_pnl": s.state.unrealized_pnl,
        "total_pnl": s.state.realized_pnl + s.state.unrealized_pnl,
        "transaction_costs": s.state.total_transaction_costs,
        "max_drawdown": _max_drawdown(pnl_df["total"].values),
        "pnl_curve": pnl_df,
        "closed_trades": s.state.closed_trades,
        "basis_events": basis_events,
    }


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float((equity - peak).min())


# ──────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────

def print_report(result: dict, args) -> None:
    print()
    print("Arbitrage backtest — calendar spread (tradable) + basis (signals only)")
    print(f"  universe={args.universe or 'NIFTY 50 (or whatever bhavcopy yields)'}")
    print(f"  carry: r={args.risk_free_rate} q={args.dividend_yield}")
    print(f"  calendar entry={args.calendar_entry} exit={args.calendar_exit} "
          f"max-hold={args.max_hold}d min-dte-near={args.min_dte_near}d "
          f"max-leg-basis={args.max_leg_basis} "
          f"lots={args.lots_per_leg} cap={args.max_leg_notional}")
    print(f"  basis entry={args.basis_entry} (signals-only)")
    print("=" * 90)
    print(f"  Days replayed:        {result['n_days']}")
    print(f"  Calendars opened:     {result['n_round_trips']}  (orders placed: {result['n_orders']})")
    print(f"  Basis events logged:  {result['n_basis_events']}")
    print("-" * 90)
    print(f"  Realized P&L:        ₹{result['realized_pnl']:>12,.0f}")
    print(f"  Unrealized P&L:      ₹{result['unrealized_pnl']:>12,.0f}")
    print(f"  Net P&L:             ₹{result['total_pnl']:>12,.0f}  "
          f"(after costs ₹{result['transaction_costs']:,.0f})")
    print(f"  Gross P&L:           ₹{(result['total_pnl'] + result['transaction_costs']):>12,.0f}")
    print(f"  Max drawdown:        ₹{result['max_drawdown']:>12,.0f}")
    print("=" * 90)

    if result["closed_trades"]:
        # closed_trades.realized_pnl is now a per-trade delta (not the running
        # total). Sort by it directly and report the worst+best so the tail
        # of the distribution is visible — Varsity flags that calendar P&L
        # is small per-trade so the spread of outcomes matters.
        sorted_trades = sorted(
            result["closed_trades"], key=lambda t: t["realized_pnl"], reverse=True,
        )
        head = sorted_trades[:5]
        tail = sorted_trades[-5:] if len(sorted_trades) > 5 else []
        print("\nTop 5 closed calendars by per-trade realized P&L:")
        for t in head:
            print(f"  {t['symbol']:<14} {t['position']:<16} entry_diff={t['entry_carry_diff']:+.3f} "
                  f"realized=₹{t['realized_pnl']:>+10,.0f} costs=₹{t['transaction_costs']:>7,.0f}")
        if tail:
            print("Bottom 5:")
            for t in tail:
                print(f"  {t['symbol']:<14} {t['position']:<16} entry_diff={t['entry_carry_diff']:+.3f} "
                      f"realized=₹{t['realized_pnl']:>+10,.0f} costs=₹{t['transaction_costs']:>7,.0f}")

    if result["basis_events"]:
        # Top by absolute annualized basis
        df = pd.DataFrame(result["basis_events"])
        df["abs_basis"] = df["basis_annual"].abs()
        top = df.sort_values("abs_basis", ascending=False).head(10)
        print("\nTop 10 cash-futures basis dislocations (signals-only):")
        for _, r in top.iterrows():
            sign = "RICH" if r["basis_annual"] > 0 else "CHEAP"
            print(f"  {r['date']}  {r['symbol']:<14} {sign:<5} "
                  f"basis={r['basis_annual']*100:+.2f}% ann.  "
                  f"S={r['spot']:.2f} F={r['fut']:.2f} dte={r['dte']}d")
    print()


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    p = argparse.ArgumentParser(description="EOD bhavcopy backtest for the arbitrage strategy")
    p.add_argument("--universe", type=str, default=None,
                   help="Comma-separated underlyings. Default: every STF in the bhavcopy archive.")
    p.add_argument("--risk-free-rate", type=float, default=0.07, dest="risk_free_rate")
    p.add_argument("--dividend-yield", type=float, default=0.0, dest="dividend_yield")
    p.add_argument("--basis-entry", type=float, default=0.015, dest="basis_entry")
    p.add_argument("--basis-min-dte", type=int, default=3, dest="basis_min_dte")
    p.add_argument("--calendar-entry", type=float, default=0.020, dest="calendar_entry")
    p.add_argument("--calendar-exit", type=float, default=0.005, dest="calendar_exit")
    p.add_argument("--max-hold", type=int, default=15, dest="max_hold")
    p.add_argument("--min-dte-near", type=int, default=4, dest="min_dte_near")
    p.add_argument("--max-leg-basis", type=float, default=0.10, dest="max_leg_basis",
                   help="Cleanliness gate: skip calendars where either leg's "
                        "annualized basis exceeds this. Set very high (e.g. 9.99) "
                        "to disable.")
    p.add_argument("--lots-per-leg", type=int, default=1, dest="lots_per_leg")
    p.add_argument("--max-open-calendars", type=int, default=5, dest="max_open_calendars")
    p.add_argument("--max-leg-notional", type=float, default=500_000, dest="max_leg_notional")
    p.add_argument("--dividend-yields", type=str, default="", dest="dividend_yields",
                   help="Comma-separated per-symbol dividend yields, e.g. "
                        "'ITC=0.04,COALINDIA=0.06,HUL=0.025'. Symbols not "
                        "listed fall back to --dividend-yield.")
    p.add_argument("--from", type=str, default=None, dest="date_from",
                   help="Start date YYYY-MM-DD (inclusive)")
    p.add_argument("--to", type=str, default=None, dest="date_to",
                   help="End date YYYY-MM-DD (inclusive)")
    p.add_argument("--save-curve", type=str, default=None,
                   help="Optional: write daily P&L curve to this CSV")
    args = p.parse_args()

    warn_coarse_timeframe("daily", backtest="backtest_arbitrage", logger=logger,
                          reason="no 5-min single-stock-futures data — daily "
                          "bhavcopy only; run market_data/fetch_5min_stf.py on the host to "
                          "build a 5-min STF corpus (issue #63)")

    universe = (
        [s.strip().upper() for s in args.universe.split(",") if s.strip()]
        if args.universe else None
    )
    logger.info("Loading STF panel from bhavcopy archive…")
    panel = load_stf_panel(universe=universe)

    if args.date_from:
        d0 = datetime.strptime(args.date_from, "%Y-%m-%d").date()
        panel = panel[panel["date"] >= d0]
    if args.date_to:
        d1 = datetime.strptime(args.date_to, "%Y-%m-%d").date()
        panel = panel[panel["date"] <= d1]
    if panel.empty:
        logger.error("Panel is empty after date filtering")
        return 1

    logger.info("Panel: %d rows over %d trading days, %d underlyings",
                len(panel), panel["date"].nunique(), panel["symbol"].nunique())

    # Reuse the strategy's parser so live + replay never disagree on the format.
    div_yields = ArbitrageStrategy._parse_yield_map(args.dividend_yields)

    result = run_backtest(
        panel,
        risk_free_rate=args.risk_free_rate, dividend_yield=args.dividend_yield,
        basis_entry_annual=args.basis_entry, basis_min_dte=args.basis_min_dte,
        calendar_entry_annual=args.calendar_entry,
        calendar_exit_annual=args.calendar_exit,
        calendar_max_holding_days=args.max_hold,
        calendar_min_dte_near=args.min_dte_near,
        calendar_max_leg_basis=args.max_leg_basis,
        lots_per_leg=args.lots_per_leg,
        max_open_calendars=args.max_open_calendars,
        max_leg_notional=args.max_leg_notional,
        dividend_yields=div_yields,
    )

    print_report(result, args)

    if args.save_curve:
        result["pnl_curve"].to_csv(args.save_curve)
        logger.info("Wrote daily P&L curve to %s", args.save_curve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
