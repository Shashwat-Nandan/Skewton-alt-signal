"""
Backtest Harness — Replay Historical Data Through the Hedger
=============================================================
Provides a mock Kite interface backed by historical OHLCV + options chain data,
allowing the full hedging engine to run without a live connection.

Usage:
  python backtest.py --data historical_data.csv --days 30

Data format (CSV):
  timestamp, underlying_price, symbol, strike, option_type, expiry,
  last_price, bid, ask, lot_size, iv

If no data file is provided, generates synthetic data for a smoke test.
"""

import argparse
import logging
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from greeks_engine import GreeksEngine, OptionContract, implied_volatility_bisect
from strategies.taleb_karpathy import (
    TalebKarpathyStrategy, HedgeState, _INDEX_SPOT_SYMBOLS,
    estimate_transaction_cost,
)

logger = logging.getLogger(__name__)


class MockKite:
    """
    Kite-compatible interface backed by a time-indexed DataFrame.
    Advances through historical ticks when quote() is called.
    """

    VARIETY_REGULAR = "regular"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, data: pd.DataFrame, underlying: str = "NIFTY"):
        self.data = data.sort_values("timestamp").reset_index(drop=True)
        self.underlying = underlying
        self._tick_idx = 0
        self._timestamps = self.data["timestamp"].unique()
        self._current_ts = self._timestamps[0] if len(self._timestamps) > 0 else None
        self._orders = []

    def advance_tick(self):
        """Move to next timestamp in the data."""
        if self._tick_idx < len(self._timestamps) - 1:
            self._tick_idx += 1
            self._current_ts = self._timestamps[self._tick_idx]
            return True
        return False

    @property
    def current_timestamp(self):
        return self._current_ts

    def quote(self, symbols: List[str]) -> Dict:
        """Return quotes for symbols at current tick."""
        result = {}
        tick_data = self.data[self.data["timestamp"] == self._current_ts]

        # Accept both the legacy "NSE:<UNDERLYING>" form and Kite's real
        # index display key ("NSE:NIFTY 50", "NSE:NIFTY BANK"). The strategy
        # now uses the latter for indices; historical CSVs key spot rows
        # by the bare underlying name.
        spot_aliases = {self.underlying}
        mapped = _INDEX_SPOT_SYMBOLS.get(self.underlying)
        if mapped:
            spot_aliases.add(mapped.split(":", 1)[1])
        for sym in symbols:
            # Parse symbol: "NSE:NIFTY 50" or "NFO:NIFTY26403CE22000"
            exchange, tsym = sym.split(":", 1) if ":" in sym else ("NFO", sym)

            if exchange == "NSE" and tsym in spot_aliases:
                # Return underlying spot price
                spot_rows = tick_data[tick_data["symbol"] == self.underlying]
                if not spot_rows.empty:
                    price = float(spot_rows.iloc[0]["last_price"])
                    result[sym] = {
                        "last_price": price,
                        "depth": {
                            "buy": [{"price": price * 0.999}],
                            "sell": [{"price": price * 1.001}],
                        },
                    }
                continue

            # Option/futures quote.
            # Historical CSVs store high/low in the "bid"/"ask" columns,
            # which is an intrabar range — far wider than a tick-level
            # bid/ask and unusable as a liquidity proxy. Synthesize a
            # ~0.3% spread around last_price instead.
            row = tick_data[tick_data["symbol"] == tsym]
            if not row.empty:
                r = row.iloc[0]
                last = float(r["last_price"])
                result[sym] = {
                    "last_price": last,
                    "depth": {
                        "buy": [{"price": last * 0.9985}],
                        "sell": [{"price": last * 1.0015}],
                    },
                }

        return result

    def instruments(self, exchange: str) -> List[Dict]:
        """Return instrument master from data at the current tick."""
        if exchange != "NFO":
            return []
        # Only return symbols available at the current tick (mirrors real broker behavior)
        tick_data = self.data[self.data["timestamp"] == self._current_ts]
        options = tick_data[tick_data["option_type"].isin(["CE", "PE"])].drop_duplicates("symbol")
        instruments = []
        for _, row in options.iterrows():
            instruments.append({
                "tradingsymbol": row["symbol"],
                "instrument_token": hash(row["symbol"]) % 1000000,
                "name": self.underlying,
                "strike": float(row["strike"]),
                "expiry": row["expiry"],
                "instrument_type": row["option_type"],
                "lot_size": int(row.get("lot_size", 25)),
            })
        # Add futures
        instruments.append({
            "tradingsymbol": f"{self.underlying}FUTMOCK",
            "instrument_token": 999999,
            "name": self.underlying,
            "strike": 0,
            "expiry": options["expiry"].iloc[0] if not options.empty else "",
            "instrument_type": "FUT",
            "lot_size": int(options.iloc[0].get("lot_size", 25)) if not options.empty else 25,
        })
        return instruments

    def place_order(self, **kwargs):
        """Record order (mock execution)."""
        order_id = f"BT-{len(self._orders)}-{self._tick_idx}"
        self._orders.append({**kwargs, "order_id": order_id, "timestamp": self._current_ts})
        return order_id

    def profile(self):
        return {"user_name": "Backtest", "user_id": "BT0000", "exchanges": ["NSE", "NFO"], "products": ["NRML"]}


def generate_synthetic_data(
    underlying: str = "NIFTY",
    spot_start: float = 22000,
    days: int = 30,
    ticks_per_day: int = 12,
    daily_vol: float = 0.012,
    lot_size: int = 25,
) -> pd.DataFrame:
    """
    Generate synthetic historical data for backtesting.
    Creates a spot path + ATM ± 5 strikes of CE/PE options with synthetic prices.
    """
    engine = GreeksEngine(risk_free_rate=0.065)
    rows = []
    spot = spot_start
    start_date = datetime(2026, 3, 1, 9, 15)
    expiry_date = start_date + timedelta(days=days + 7)
    expiry_str = expiry_date.strftime("%Y-%m-%d")

    strike_interval = 50 if underlying == "NIFTY" else 100
    base_iv = 0.15

    for day in range(days):
        for tick in range(ticks_per_day):
            ts = start_date + timedelta(days=day, minutes=tick * 30)

            # Random walk for spot
            ret = np.random.normal(0, daily_vol / math.sqrt(ticks_per_day))
            spot *= (1 + ret)
            spot = round(spot, 2)

            # Spot row
            rows.append({
                "timestamp": ts, "symbol": underlying,
                "underlying_price": spot, "strike": 0,
                "option_type": "IDX", "expiry": expiry_str,
                "last_price": spot, "bid": spot * 0.999,
                "ask": spot * 1.001, "lot_size": lot_size, "iv": 0,
            })

            # Options: ATM ± 5 strikes
            atm = round(spot / strike_interval) * strike_interval
            strikes = [atm + i * strike_interval for i in range(-5, 6)]
            T = max((expiry_date - ts).total_seconds() / (365.25 * 86400), 1 / 365)

            # Per-tick IV regime shift so ATM percentile varies across days
            tick_iv_shift = np.random.normal(0, 0.02)
            tick_base_iv = base_iv + tick_iv_shift

            for K in strikes:
                for otype in ["CE", "PE"]:
                    # Realistic smile: quadratic skew + per-strike noise
                    moneyness = (K - spot) / spot
                    smile_iv = tick_base_iv + 0.04 * moneyness ** 2 + np.random.normal(0, 0.005)
                    smile_iv = max(smile_iv, 0.05)

                    price = engine.bs_price(spot, K, T, smile_iv, otype)
                    price = max(price, 0.05)  # Floor

                    symbol = f"{underlying}{expiry_date.strftime('%y%m%d')}{otype}{int(K)}"
                    rows.append({
                        "timestamp": ts, "symbol": symbol,
                        "underlying_price": spot, "strike": K,
                        "option_type": otype, "expiry": expiry_str,
                        "last_price": round(price, 2),
                        "bid": round(price * 0.998, 2),
                        "ask": round(price * 1.002, 2),
                        "lot_size": lot_size,
                        "iv": round(smile_iv, 4),
                    })

    return pd.DataFrame(rows)


def run_backtest(
    data: pd.DataFrame,
    underlying: str = "NIFTY",
    config_path: str = "config.ini",
    tunable_params: Optional[Dict] = None,
) -> Dict:
    """
    Run the full hedging engine over historical data.

    Args:
        tunable_params: If provided, override the hedger's tunable parameters
                        (used by autoresearch to test candidate param sets).

    Returns a dict with P/L curve, metrics, and trade log.
    """
    # Strip timezone info if present — greeks_engine uses naive datetimes
    data = data.copy()
    if hasattr(data["timestamp"].dt, "tz") and data["timestamp"].dt.tz is not None:
        data["timestamp"] = data["timestamp"].dt.tz_localize(None)

    mock_kite = MockKite(data, underlying)
    hedger = TalebKarpathyStrategy(mock_kite, config_path=config_path, mode="paper")
    # Backtests should not leak IV state between experiments; each run
    # builds its own rolling history from the replay ticks.
    hedger._persist_iv_history = False
    hedger._atm_iv_history = []
    hedger._cached_lot_size = int(data[data["option_type"].isin(["CE", "PE"])].iloc[0]["lot_size"])

    # Apply candidate tunable params if provided (autoresearch optimization)
    if tunable_params is not None:
        hedger.tunable_params.update(tunable_params)

    # Inject replay clock so _pre_trade_checks and time_to_expiry use historical timestamps
    # Strip timezone info to keep everything naive (greeks_engine uses naive datetimes)
    def replay_clock():
        ts = pd.Timestamp(mock_kite.current_timestamp).to_pydatetime()
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    hedger._clock = replay_clock
    hedger.proposer._clock = replay_clock

    pnl_curve = []
    trade_log = []
    tick_count = 0

    logger.info("Starting backtest: %d ticks", len(mock_kite._timestamps))

    while True:
        tick_count += 1
        ts = mock_kite.current_timestamp

        # Attempt entry whenever the book is flat
        if not hedger.state.positions:
            proposals = hedger.scan_and_propose()
            if proposals:
                hedger.execute_proposals(proposals)
                for p in proposals:
                    trade_log.append({
                        "timestamp": ts, "action": "ENTRY",
                        "symbol": p.tradingsymbol, "type": p.transaction_type,
                        "qty": p.quantity, "price": p.price,
                    })

        # Rehedge check
        if hedger.state.positions:
            rehedge = hedger.check_and_rehedge()
            if rehedge:
                hedger.execute_proposals(rehedge)
                for p in rehedge:
                    trade_log.append({
                        "timestamp": ts, "action": "REHEDGE",
                        "symbol": p.tradingsymbol, "type": p.transaction_type,
                        "qty": p.quantity, "price": p.price,
                    })

        pnl_curve.append({
            "timestamp": ts,
            "total_pnl": hedger.state.total_pnl,
            "unrealized_pnl": hedger.state.unrealized_pnl,
            "realized_pnl": hedger.state.realized_pnl,
            "transaction_costs": hedger.state.total_transaction_costs,
            "positions": len(hedger.state.positions),
            "rehedge_count": hedger.state.rehedge_count,
        })

        if not mock_kite.advance_tick():
            break

    # End-of-data flattening: report PnL on a fully realized book.
    # Without this the final metrics show unrealized_pnl ≠ 0 and reflect a
    # mark-to-market snapshot rather than a closed position.
    if hedger.state.positions:
        final_ts = mock_kite.current_timestamp
        spot = hedger._get_spot_price()
        hedger._update_positions_prices(spot)
        eod_close = hedger._generate_close_all_proposals()
        if eod_close:
            hedger.execute_proposals(eod_close)
            for p in eod_close:
                trade_log.append({
                    "timestamp": final_ts, "action": "EOD_CLOSE",
                    "symbol": p.tradingsymbol, "type": p.transaction_type,
                    "qty": p.quantity, "price": p.price,
                })
            pnl_curve.append({
                "timestamp": final_ts,
                "total_pnl": hedger.state.total_pnl,
                "unrealized_pnl": hedger.state.unrealized_pnl,
                "realized_pnl": hedger.state.realized_pnl,
                "transaction_costs": hedger.state.total_transaction_costs,
                "positions": len(hedger.state.positions),
                "rehedge_count": hedger.state.rehedge_count,
            })

    # Final metrics
    metrics = hedger.get_strategy_metrics()
    metrics["total_ticks"] = tick_count
    metrics["total_trades"] = len(trade_log)

    return {
        "pnl_curve": pd.DataFrame(pnl_curve),
        "trade_log": pd.DataFrame(trade_log) if trade_log else pd.DataFrame(),
        "metrics": metrics,
        "orders": mock_kite._orders,
        "closed_trades": pd.DataFrame(hedger.state.closed_trades) if hedger.state.closed_trades else pd.DataFrame(),
    }


def print_report(results: Dict):
    """Print a human-readable backtest report."""
    metrics = results["metrics"]
    pnl = results["pnl_curve"]
    trades = results["trade_log"]

    print("\n" + "=" * 60)
    print("BACKTEST REPORT")
    print("=" * 60)
    print(f"  Ticks processed:     {metrics['total_ticks']}")
    print(f"  Total trades:        {metrics['total_trades']}")
    print(f"  Rehedge count:       {metrics['rehedge_count']}")
    print(f"  Position count:      {metrics['position_count']}")
    print()
    print("  P/L Summary:")
    print(f"    Net P/L:           {metrics['net_pnl']:>12,.2f}")
    print(f"    Realized P/L:      {metrics['realized_pnl']:>12,.2f}")
    print(f"    Unrealized P/L:    {metrics['unrealized_pnl']:>12,.2f}")
    print(f"    Transaction costs: {metrics['total_transaction_costs']:>12,.2f}")
    print(f"    Gamma scalp P/L:   {metrics['gamma_scalp_pnl']:>12,.2f}")
    print(f"    Theta decay paid:  {metrics['theta_decay_paid']:>12,.2f}")
    print()
    print("  Risk Metrics:")
    print(f"    Max drawdown:      {metrics['max_drawdown']:>12,.2f} ({metrics['max_drawdown_pct']:.2f}%)")
    print(f"    Sharpe ratio:      {metrics['sharpe_ratio']:>12.4f}")
    print(f"    Calmar ratio:      {metrics['calmar_ratio']:>12.4f}")
    print(f"    Sortino ratio:     {metrics['sortino_ratio']:>12.4f}")

    if not pnl.empty:
        print()
        print("  P/L Curve (first/last 5 ticks):")
        print(f"    {'Timestamp':<22} {'Total P/L':>12} {'Positions':>10}")
        for _, row in pnl.head(5).iterrows():
            print(f"    {str(row['timestamp']):<22} {row['total_pnl']:>12,.2f} {int(row['positions']):>10}")
        if len(pnl) > 10:
            print(f"    {'...':<22}")
        for _, row in pnl.tail(5).iterrows():
            print(f"    {str(row['timestamp']):<22} {row['total_pnl']:>12,.2f} {int(row['positions']):>10}")

    if not trades.empty:
        print()
        print("  Trade Log:")
        print(f"    {'Timestamp':<22} {'Action':<10} {'Symbol':<30} {'Type':<6} {'Qty':>5} {'Price':>10}")
        for _, row in trades.iterrows():
            print(f"    {str(row['timestamp']):<22} {row['action']:<10} {row['symbol']:<30} {row['type']:<6} {row['qty']:>5} {row['price']:>10.2f}")

    closed = results.get("closed_trades")
    if closed is not None and not closed.empty:
        print()
        print("  Per-Trade PnL Attribution:")
        print(f"    {'Entry':<19} {'Hold(h)':>8} {'Rehedges':>9} {'IV%':>6} {'Gross':>10} {'Costs':>9} {'Scalp':>10} {'Residual':>10}")
        for _, row in closed.iterrows():
            print(
                f"    {str(row['entry_time']):<19} "
                f"{row['holding_minutes']/60:>8.1f} {int(row['n_rehedges']):>9} "
                f"{row['entry_atm_iv']*100:>5.1f}% {row['gross_pnl']:>10,.0f} "
                f"{row['costs']:>9,.0f} {row['gamma_scalp']:>10,.0f} "
                f"{row['residual']:>10,.0f}"
            )
        print(f"    {'─'*99}")
        print(
            f"    {'TOTAL':<19} {closed['holding_minutes'].sum()/60:>8.1f} "
            f"{int(closed['n_rehedges'].sum()):>9} "
            f"{'':>6} {closed['gross_pnl'].sum():>10,.0f} "
            f"{closed['costs'].sum():>9,.0f} {closed['gamma_scalp'].sum():>10,.0f} "
            f"{closed['residual'].sum():>10,.0f}"
        )
        n = len(closed)
        wins = (closed['gross_pnl'] > 0).sum()
        print(f"    Trades: {n}  Win rate: {wins/n*100:.1f}%  "
              f"Avg gross: {closed['gross_pnl'].mean():,.0f}  "
              f"Median gross: {closed['gross_pnl'].median():,.0f}")

    print("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    parser = argparse.ArgumentParser(description="Backtest the Taleb Dynamic Hedger")
    parser.add_argument("--data", type=str, help="Path to historical data CSV")
    parser.add_argument("--days", type=int, default=30, help="Days of synthetic data to generate")
    parser.add_argument("--underlying", type=str, default="NIFTY", help="Underlying to trade")
    parser.add_argument("--config", type=str, default="config.ini", help="Config file path")
    parser.add_argument("--save-pnl", type=str, help="Save P/L curve to CSV")
    args = parser.parse_args()

    if args.data:
        logger.info("Loading historical data from %s", args.data)
        data = pd.read_csv(args.data, parse_dates=["timestamp"])
        # Strip timezone info — greeks_engine uses naive datetimes
        if data["timestamp"].dt.tz is not None:
            data["timestamp"] = data["timestamp"].dt.tz_localize(None)
    else:
        logger.info("Generating %d days of synthetic data for %s", args.days, args.underlying)
        data = generate_synthetic_data(underlying=args.underlying, days=args.days)

    results = run_backtest(data, underlying=args.underlying, config_path=args.config)
    print_report(results)

    if args.save_pnl:
        results["pnl_curve"].to_csv(args.save_pnl, index=False)
        logger.info("P/L curve saved to %s", args.save_pnl)
