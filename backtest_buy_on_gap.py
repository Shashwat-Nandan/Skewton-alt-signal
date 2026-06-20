"""
Backtest harness for the Buy-on-Gap intraday mean-reversion strategy.

Replay model
------------
Pure intraday, one trading day at a time. On each date:
  1. ``scan_and_propose`` runs at the OPEN — qualifying gap-downs (open below
     prev_close by > k·σ, above the long MA, liquid) are ranked most-oversold
     first and the top N are BOUGHT at today's open.
  2. ``check_and_rehedge`` (with ``_force_close=True``) runs to flatten every
     position the SAME DAY — at the catastrophic stop if the day's low breached
     it, else at today's close.

There is no overnight carry and no look-ahead: the gap is observable at the
open, all trailing stats (σ, MA, turnover, prev_close) are shifted one bar in
the strategy's feature builder, and the exit uses today's low/close which are
realised after entry. The catastrophic stop is approximated by the day's low
(the only intraday extreme in daily bars) — conservative: it assumes the worst
intraday print can fill the stop.

Costs
-----
Round-trip intraday cost (default 0.15 % of notional, applied half on entry,
half on exit) is booked inside the strategy's paper-execute path, so the
backtest P&L already nets costs — identical accounting to the live paper runner
(Rule 7). Override with ``--cost-pct``.

Scoring sentinel
----------------
``ZERO_TRADE_PENALTY = -1e6`` when no trades fire, so a sweep can't mistake
"no signal" for a neutral score (flat-fitness lesson).

CLI::

    python backtest_buy_on_gap.py
    python backtest_buy_on_gap.py --gap-std-mult 1.5 --max-positions 3
    python backtest_buy_on_gap.py --no-trend-filter --start 2025-01-01
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from strategies._eq_data import load_equity_panel, load_universe
from strategies.buy_on_gap import BuyOnGapStrategy

logger = logging.getLogger(__name__)

ZERO_TRADE_PENALTY = -1e6


class BuyOnGapBacktester:
    """Walk-forward replay of BuyOnGapStrategy over a daily OHLCV panel."""

    def __init__(self, panel: pd.DataFrame, params_overrides: Optional[Dict] = None):
        self.panel = panel

        class _NullKite:
            pass
        self.strategy = BuyOnGapStrategy(
            kite=_NullKite(), config_path="/dev/null", mode="paper",
        )
        if params_overrides:
            self.strategy.params.update(params_overrides)
        self.strategy.set_panel(panel, sorted(panel["symbol"].unique().tolist()))
        self.strategy.set_today_quotes(None)  # backtest reads the panel row
        self.strategy._ensure_features()

        self.equity_curve: List[Tuple[pd.Timestamp, float]] = []
        self.daily_returns: List[float] = []

    def _trading_dates(self) -> List[pd.Timestamp]:
        return sorted(self.panel["date"].unique().tolist())

    def run(self) -> Dict:
        dates = self._trading_dates()
        if not dates:
            raise RuntimeError("empty panel — cannot backtest")
        capital = self.strategy.params["total_capital"]
        self.strategy._force_close = True  # every position exits same day

        for i, dt in enumerate(dates):
            self.strategy.set_current_date(dt)
            # 1) enter at the open
            entries = self.strategy.scan_and_propose()
            if entries:
                self.strategy.execute_proposals(entries)
            # 2) exit the same day (stop via low, else close)
            exits = self.strategy.check_and_rehedge()
            if exits:
                self.strategy.execute_proposals(exits)
            # Any position that somehow survived (no bar) is force-marked flat at
            # entry so the book never carries overnight in the backtest.
            for sym in list(self.strategy.positions):
                logger.warning("%s @ %s: no bar to exit — dropping at entry px",
                               sym, dt.date())
                self.strategy.positions.pop(sym, None)

            # 3) mark equity (book is flat each EOD → equity = capital + realized)
            equity = capital + self.strategy.realized_pnl
            self.equity_curve.append((dt, equity))
            if i > 0:
                prev_eq = self.equity_curve[i - 1][1]
                if prev_eq > 0:
                    self.daily_returns.append((equity - prev_eq) / prev_eq)

        return self.summary()

    def summary(self) -> Dict:
        trades = self.strategy.closed_positions
        n = len(trades)
        if n == 0:
            return {"total_trades": 0, "score": ZERO_TRADE_PENALTY,
                    "note": "no trades fired — see lessons.md flat-fitness rule"}
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        net = sum(t.pnl for t in trades)
        avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0
        win_rate = len(wins) / n
        profit_factor = (
            sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))
            if losses and sum(t.pnl for t in losses) != 0 else float("inf")
        )
        rets = np.array(self.daily_returns) if self.daily_returns else np.array([0.0])
        ann = math.sqrt(252)
        sharpe = (rets.mean() / rets.std() * ann) if rets.std() > 1e-9 else 0.0
        equity = np.array([eq for _, eq in self.equity_curve])
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = float(dd.min()) if len(dd) else 0.0
        n_days = len(self.equity_curve)
        cagr = ((equity[-1] / equity[0]) ** (252.0 / n_days) - 1.0) if n_days > 1 and equity[0] > 0 else 0.0
        calmar = (cagr / abs(max_dd)) if max_dd < 0 else float("inf")
        n_stops = sum(1 for t in trades if t.exit_reason == "CATASTROPHIC_STOP")

        return {
            "total_trades": n,
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else None,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "net_pnl": round(net, 2),
            "transaction_costs": round(self.strategy.transaction_costs, 2),
            "sharpe": round(float(sharpe), 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "calmar": round(float(calmar), 3) if math.isfinite(calmar) else None,
            "starting_equity": round(self.equity_curve[0][1], 2) if self.equity_curve else None,
            "ending_equity": round(self.equity_curve[-1][1], 2) if self.equity_curve else None,
            "n_dates": n_days,
            "n_catastrophic_stops": n_stops,
            "trades_per_day": round(n / n_days, 2) if n_days else 0.0,
            "score": float(sharpe),
        }

    def per_symbol_breakdown(self) -> pd.DataFrame:
        if not self.strategy.closed_positions:
            return pd.DataFrame()
        rows = [{"symbol": t.symbol, "net_pnl": t.pnl, "gap_z": t.gap_z,
                 "exit": t.exit_reason} for t in self.strategy.closed_positions]
        df = pd.DataFrame(rows)
        return df.groupby("symbol").agg(
            trades=("net_pnl", "count"),
            wins=("net_pnl", lambda s: int((s > 0).sum())),
            net_pnl=("net_pnl", "sum"),
            avg_gap_z=("gap_z", "mean"),
        ).sort_values("net_pnl", ascending=False)


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(message)s")

    p = argparse.ArgumentParser(description="Buy-on-Gap intraday mean-reversion backtest")
    p.add_argument("--universe", default="data_cache/nifty200.csv")
    p.add_argument("--source", default="auto", choices=["auto", "cache", "stf"])
    p.add_argument("--start", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--end", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--max-positions", type=int, default=None)
    p.add_argument("--gap-std-mult", type=float, default=None,
                   help="k: require gap_ret <= -k*ret_std (default 1.0)")
    p.add_argument("--std-window", type=int, default=None)
    p.add_argument("--ma-window", type=int, default=None)
    p.add_argument("--no-trend-filter", action="store_true",
                   help="Disable the 'open above long MA' refinement")
    p.add_argument("--stop-loss-pct", type=float, default=None)
    p.add_argument("--cost-pct", type=float, default=None,
                   help="Round-trip cost %% of notional (default 0.15)")
    p.add_argument("--ledger-out", default="data_cache/buy_on_gap_trades.tsv")
    p.add_argument("--report-json", default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    universe = load_universe(Path(args.universe))
    panel = load_equity_panel(universe=universe, source=args.source)
    if args.start:
        panel = panel[panel["date"] >= pd.Timestamp(args.start)]
    if args.end:
        panel = panel[panel["date"] <= pd.Timestamp(args.end)]
    if panel.empty:
        print("ERROR: panel empty after date filter", file=sys.stderr)
        return 2

    overrides: Dict = {"total_capital": args.capital}
    if args.max_positions is not None: overrides["max_positions"] = args.max_positions
    if args.gap_std_mult is not None:  overrides["gap_std_mult"] = args.gap_std_mult
    if args.std_window is not None:    overrides["std_window"] = args.std_window
    if args.ma_window is not None:     overrides["ma_window"] = args.ma_window
    if args.no_trend_filter:           overrides["use_trend_filter"] = 0
    if args.stop_loss_pct is not None: overrides["stop_loss_pct"] = args.stop_loss_pct
    if args.cost_pct is not None:      overrides["cost_pct"] = args.cost_pct

    bt = BuyOnGapBacktester(panel, params_overrides=overrides)
    summary = bt.run()
    pr = bt.strategy.params

    print("=" * 78)
    print("Buy-on-Gap — Intraday Mean Reversion — Backtest Summary")
    print("=" * 78)
    print(f"  Universe          : {len(universe)} symbols ({panel['symbol'].nunique()} with data)")
    print(f"  Date range        : {panel['date'].min().date()} → {panel['date'].max().date()}")
    print(f"  Trading days      : {summary.get('n_dates', 0)}")
    print(f"  Gap threshold     : {pr['gap_std_mult']:.2f}σ over {int(pr['std_window'])}d returns")
    print(f"  Trend filter      : {'on (open > %d-MA)' % int(pr['ma_window']) if int(pr['use_trend_filter']) else 'off'}")
    print(f"  Max positions/day : {int(pr['max_positions'])}  (equal-weight, {pr['max_gross_exposure_pct']:.0f}% gross)")
    print(f"  Catastrophic stop : {pr['stop_loss_pct']:.1f}% below entry")
    print(f"  Round-trip cost   : {pr['cost_pct']:.2f} %")
    print("-" * 78)
    if summary["total_trades"] == 0:
        print(f"  No trades fired. Score sentinel: {summary['score']}")
        print(f"  (need {int(pr['ma_window'])}+ bars warm-up before the long-MA/σ evaluate.)")
        return 1
    print(f"  Total trades      : {summary['total_trades']}  ({summary['trades_per_day']}/day)")
    print(f"  Win rate          : {summary['win_rate']*100:.1f} %")
    print(f"  Profit factor     : {summary['profit_factor']}")
    print(f"  Net P&L           : ₹{summary['net_pnl']:>14,.0f}  (costs ₹{summary['transaction_costs']:,.0f})")
    print(f"  Avg win / loss    : ₹{summary['avg_win']:>10,.0f}  /  ₹{summary['avg_loss']:>10,.0f}")
    print(f"  Catastrophic stops: {summary['n_catastrophic_stops']}")
    print(f"  Sharpe (ann.)     : {summary['sharpe']}")
    print(f"  Max drawdown      : {summary['max_drawdown_pct']} %")
    print(f"  CAGR              : {summary['cagr_pct']} %")
    print(f"  Calmar            : {summary['calmar']}")
    print(f"  Equity            : ₹{summary['starting_equity']:>14,.0f}  →  ₹{summary['ending_equity']:>14,.0f}")
    print("-" * 78)

    if not args.quiet:
        from collections import Counter
        c = Counter(t.exit_reason for t in bt.strategy.closed_positions)
        print("  Exit reasons      : " + ", ".join(f"{k}={v}" for k, v in c.most_common()))
        per = bt.per_symbol_breakdown()
        if not per.empty:
            print("-" * 78)
            print("  Top 10 winners (net ₹):")
            print(per.head(10).to_string())
            if len(per) > 10:
                print("\n  Bottom 5 losers (net ₹):")
                print(per.tail(5).to_string())

    if bt.strategy.closed_positions:
        ledger = pd.DataFrame([t.to_dict() for t in bt.strategy.closed_positions])
        ledger.to_csv(args.ledger_out, sep="\t", index=False)
        print("-" * 78)
        print(f"  Per-trade ledger  : {args.ledger_out}")

    if args.report_json:
        Path(args.report_json).write_text(json.dumps(summary, indent=2, default=str))
        print(f"  Summary JSON      : {args.report_json}")

    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
