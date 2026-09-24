"""
Backtest harness for the delivery-accumulation strategy.

Replay model, cost model, and fill filters are identical to
``research/backtest_varsity_equity.py`` (Rule 7: same strategy methods,
same ``core.costs.estimate_equity_cost`` DELIVERY charges, same
EQ-FU-2 next-day-open fill queue with gap-skip and max-age). The only
additions are the delivery panel injection and delivery-specific CLI
overrides.

The strategy's ``deliv_lag_days`` default (1) is left untouched here so
the tested signal equals the deployable one: day-D scans act on day-D−1
delivery, matching a 19:45 IST fetch timer vs an 18:30 close scan.

CLI::

    python -m research.backtest_delivery_accum --source cache --start 2023-01-01 --end 2025-05-31
    python -m research.backtest_delivery_accum --entry-pctile 0.95 --min-hits 3
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.backtest_timeframe import warn_coarse_timeframe
from strategies._delivery import load_delivery_panel
from strategies._eq_data import load_equity_panel, load_universe
from strategies.delivery_accumulation import (
    DeliveryAccumulationStrategy,
    EquityPosition,
    PENDING_GAP_ATR_THRESHOLD,
    PENDING_MAX_AGE_DAYS,
)

logger = logging.getLogger(__name__)

ZERO_TRADE_PENALTY = -1e6

DEFAULT_SLIPPAGE_BPS = 5.0  # per side; statutory charges from core.costs


@dataclass
class TradeRecord:
    symbol: str
    entry_dt: pd.Timestamp
    entry_px: float
    qty: int
    exit_dt: pd.Timestamp
    exit_px: float
    exit_reason: str
    gross_pnl: float
    costs: float
    net_pnl: float
    holding_days: int
    R_multiple: float  # net_pnl / risk_at_entry


class DeliveryBacktester:
    """Walk-forward replay of DeliveryAccumulationStrategy over an OHLCV panel."""

    def __init__(
        self,
        panel: pd.DataFrame,
        deliv_panel: Optional[pd.DataFrame] = None,
        params_overrides: Optional[Dict] = None,
        slippage_bps: Optional[float] = None,
    ):
        self.panel = panel

        class _NullKite:
            pass
        self.strategy = DeliveryAccumulationStrategy(
            client=_NullKite(), config_path="/dev/null", mode="paper",
        )
        if params_overrides:
            self.strategy.params.update(params_overrides)
        if slippage_bps is not None:
            self.strategy.params["slippage_bps"] = slippage_bps
        self.strategy.set_panel(panel, sorted(panel["symbol"].unique().tolist()))
        if deliv_panel is not None:
            self.strategy.set_delivery_panel(deliv_panel)
        self.strategy._ensure_features()

        self.trade_log: List[TradeRecord] = []
        self.equity_curve: List[Tuple[pd.Timestamp, float]] = []
        self.daily_returns: List[float] = []
        self.n_skipped_gap: int = 0
        self.n_skipped_stale: int = 0

    def _trading_dates(self) -> List[pd.Timestamp]:
        return sorted(self.panel["date"].unique().tolist())

    def _open_price(self, sym: str, dt: pd.Timestamp) -> Optional[float]:
        f = self.strategy._features.get(sym)
        if f is None or dt not in f.index:
            return None
        v = f.loc[dt, "open"]
        return None if pd.isna(v) else float(v)

    def _fill_queued(self, dt: pd.Timestamp,
                     queued: List[Tuple], cash: float) -> float:
        """Next-open fill loop — same filters as the swing harness (EQ-FU-2)."""
        for proposal, signal_dt in queued:
            open_px = self._open_price(proposal.tradingsymbol, dt)
            if open_px is None or open_px <= 0:
                self.n_skipped_stale += 1
                continue
            age_days = (dt - signal_dt).days
            if age_days > PENDING_MAX_AGE_DAYS:
                self.n_skipped_stale += 1
                continue
            snap = proposal.greeks_snapshot or {}
            atr_v = float(snap.get("atr", 0.0))
            signal_close = float(snap.get("entry", 0.0))
            if atr_v > 0 and signal_close > 0:
                gap_atr = abs(open_px - signal_close) / atr_v
                if gap_atr > PENDING_GAP_ATR_THRESHOLD:
                    self.n_skipped_gap += 1
                    continue
            notional = open_px * proposal.quantity
            entry_cost = self.strategy._cost(open_px, proposal.quantity, "BUY")
            cash -= notional + entry_cost
            k_sl = self.strategy.params["atr_stop_multiplier"]
            rr = self.strategy.params["risk_reward"]
            sl = open_px - k_sl * atr_v
            target = open_px + rr * k_sl * atr_v
            pos = EquityPosition(
                symbol=proposal.tradingsymbol, side="LONG",
                entry_dt=dt, entry_px=open_px, qty=proposal.quantity,
                initial_sl=sl, target=target, atr_at_entry=atr_v,
                rationale=proposal.rationale,
            )
            self.strategy.positions[proposal.tradingsymbol] = pos
        return cash

    def run(self) -> Dict:
        dates = self._trading_dates()
        if not dates:
            raise RuntimeError("empty panel — cannot backtest")
        capital = self.strategy.params["total_capital"]
        cash = capital
        queued: List[Tuple] = []

        for i, dt in enumerate(dates):
            self.strategy.set_current_date(dt)

            cash = self._fill_queued(dt, queued, cash)
            queued = []

            exits = self.strategy.check_and_rehedge()
            for ex in exits:
                pos = self.strategy.positions.pop(ex.tradingsymbol, None)
                if pos is None:
                    continue
                exit_px = ex.price
                notional_exit = exit_px * pos.qty
                exit_cost = self.strategy._cost(exit_px, pos.qty, "SELL")
                cash += notional_exit - exit_cost
                gross_pnl = (exit_px - pos.entry_px) * pos.qty
                entry_cost = self.strategy._cost(pos.entry_px, pos.qty, "BUY")
                costs = entry_cost + exit_cost
                net_pnl = gross_pnl - costs
                holding = self.strategy._trading_days_between(pos.entry_dt, dt)
                risk_at_entry = (pos.entry_px - pos.initial_sl) * pos.qty
                rmult = (net_pnl / risk_at_entry) if risk_at_entry > 0 else 0.0
                pos.exit_dt = dt
                pos.exit_px = exit_px
                pos.exit_reason = (ex.greeks_snapshot or {}).get("exit_reason", "MANUAL")
                pos.pnl = net_pnl
                pos.costs = costs
                pos.status = "CLOSED"
                self.strategy.closed_positions.append(pos)
                self.trade_log.append(TradeRecord(
                    symbol=pos.symbol, entry_dt=pos.entry_dt, entry_px=pos.entry_px,
                    qty=pos.qty, exit_dt=dt, exit_px=exit_px,
                    exit_reason=pos.exit_reason,
                    gross_pnl=gross_pnl, costs=costs, net_pnl=net_pnl,
                    holding_days=holding, R_multiple=rmult,
                ))

            proposals = self.strategy.scan_and_propose()
            queued = [(p, dt) for p in proposals]

            mtm = sum(self._mtm_value(p, dt) for p in self.strategy.positions.values())
            equity = cash + mtm
            self.equity_curve.append((dt, equity))
            if i > 0:
                prev_eq = self.equity_curve[i-1][1]
                if prev_eq > 0:
                    self.daily_returns.append((equity - prev_eq) / prev_eq)

        return self.summary()

    def _mtm_value(self, pos: EquityPosition, dt: pd.Timestamp) -> float:
        # Fallback is the LAST KNOWN close (last_mtm_px, maintained by
        # check_and_rehedge; == entry_px only before the first mark), NOT
        # entry value: an entry-value fallback silently erased the open loss
        # of any position whose symbol stopped printing bars mid-backtest —
        # inflating equity/Sharpe exactly for the distressed names this
        # strategy buys (code-review 2026-07-22).
        f = self.strategy._features.get(pos.symbol)
        if f is None or dt not in f.index:
            return pos.last_mtm_px * pos.qty
        close = f.loc[dt, "close"]
        if pd.isna(close):
            return pos.last_mtm_px * pos.qty
        return float(close) * pos.qty

    # ── Reporting ──────────────────────────────────────────────────────────

    def summary(self) -> Dict:
        n = len(self.trade_log)
        if n == 0:
            return {
                "total_trades": 0,
                "score": ZERO_TRADE_PENALTY,
                "note": "no trades fired — see lessons.md flat-fitness rule",
                "n_skipped_gap": self.n_skipped_gap,
                "n_skipped_stale": self.n_skipped_stale,
            }
        wins = [t for t in self.trade_log if t.net_pnl > 0]
        losses = [t for t in self.trade_log if t.net_pnl <= 0]
        gross = sum(t.gross_pnl for t in self.trade_log)
        net = sum(t.net_pnl for t in self.trade_log)
        avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
        avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0
        win_rate = len(wins) / n
        profit_factor = (
            sum(t.net_pnl for t in wins) / abs(sum(t.net_pnl for t in losses))
            if losses and sum(t.net_pnl for t in losses) != 0 else float("inf")
        )
        avg_hold = sum(t.holding_days for t in self.trade_log) / n
        avg_R = sum(t.R_multiple for t in self.trade_log) / n
        rets = np.array(self.daily_returns) if self.daily_returns else np.array([0.0])
        ann = math.sqrt(252)
        sharpe = (rets.mean() / rets.std() * ann) if rets.std() > 1e-9 else 0.0
        equity = np.array([eq for _, eq in self.equity_curve])
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        max_dd = float(dd.min()) if len(dd) else 0.0
        n_days = len(self.equity_curve)
        cagr = ((equity[-1] / equity[0]) ** (252.0 / n_days) - 1.0) if n_days > 1 else 0.0
        calmar = (cagr / abs(max_dd)) if max_dd < 0 else float("inf")

        return {
            "total_trades": n,
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else None,
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_R": round(avg_R, 3),
            "avg_holding_days": round(avg_hold, 2),
            "gross_pnl": round(gross, 2),
            "net_pnl": round(net, 2),
            "transaction_costs": round(sum(t.costs for t in self.trade_log), 2),
            "sharpe": round(float(sharpe), 3),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "calmar": round(float(calmar), 3) if math.isfinite(calmar) else None,
            "starting_equity": round(self.equity_curve[0][1], 2) if self.equity_curve else None,
            "ending_equity": round(self.equity_curve[-1][1], 2) if self.equity_curve else None,
            "n_dates": len(self.equity_curve),
            "score": float(sharpe),
            "n_skipped_gap": self.n_skipped_gap,
            "n_skipped_stale": self.n_skipped_stale,
        }

    def per_symbol_breakdown(self) -> pd.DataFrame:
        if not self.trade_log:
            return pd.DataFrame()
        rows = []
        for t in self.trade_log:
            rows.append({"symbol": t.symbol, "net_pnl": t.net_pnl,
                         "R": t.R_multiple, "hold": t.holding_days,
                         "exit": t.exit_reason})
        df = pd.DataFrame(rows)
        agg = df.groupby("symbol").agg(
            trades=("net_pnl", "count"),
            wins=("net_pnl", lambda s: int((s > 0).sum())),
            net_pnl=("net_pnl", "sum"),
            avg_R=("R", "mean"),
            avg_hold=("hold", "mean"),
        ).sort_values("net_pnl", ascending=False)
        return agg


def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(message)s")

    p = argparse.ArgumentParser(description="Delivery-accumulation backtest")
    p.add_argument("--universe", default="data_cache/nifty200.csv")
    p.add_argument("--source", default="cache", choices=["auto", "cache", "stf"],
                   help="OHLCV source (default cache: delivery data is EQ-series, "
                        "so the STF proxy would mismatch the signal's instrument)")
    p.add_argument("--start", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--end", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--capital", type=float, default=1_000_000.0)
    p.add_argument("--risk-pct", type=float, default=1.0)
    p.add_argument("--entry-pctile", type=float, default=None,
                   help="deliv_entry_pctile override (fraction 0-1)")
    p.add_argument("--min-hits", type=int, default=None,
                   help="deliv_min_hits override")
    p.add_argument("--range-pos-max", type=float, default=None,
                   help="range_pos_max override (fraction 0-1)")
    p.add_argument("--atr-stop", type=float, default=None)
    p.add_argument("--rr", type=float, default=None)
    p.add_argument("--time-stop", type=int, default=None)
    p.add_argument("--turnover-cr", type=float, default=None,
                   help="min_avg_turnover_cr override")
    p.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    p.add_argument("--ledger-out", default="data_cache/delivery_accum_trades.tsv")
    p.add_argument("--report-json", default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    warn_coarse_timeframe("daily", backtest="backtest_delivery_accum",
                          reason="delivery percentage only exists at daily "
                          "resolution (EOD sec_bhavdata); holds are multi-week")

    universe = load_universe(Path(args.universe))
    panel = load_equity_panel(universe=universe, source=args.source)
    if args.start:
        panel = panel[panel["date"] >= pd.Timestamp(args.start)]
    if args.end:
        panel = panel[panel["date"] <= pd.Timestamp(args.end)]
    if panel.empty:
        print("ERROR: panel empty after date filter", file=sys.stderr)
        return 2

    deliv_panel = load_delivery_panel(universe)
    if deliv_panel.empty:
        print("ERROR: delivery cache empty — run "
              "`python -m market_data.fetch_deliv` first "
              "(data_cache/equity_delivery/)", file=sys.stderr)
        return 2
    # NOTE: the delivery panel is NOT date-filtered — the rolling percentile
    # needs the pre-window history; anti-lookahead is the rolling window +
    # deliv_lag_days, not a data cutoff.

    overrides = {"total_capital": args.capital, "risk_per_trade_pct": args.risk_pct}
    if args.entry_pctile is not None:  overrides["deliv_entry_pctile"] = args.entry_pctile
    if args.min_hits is not None:      overrides["deliv_min_hits"] = args.min_hits
    if args.range_pos_max is not None: overrides["range_pos_max"] = args.range_pos_max
    if args.atr_stop is not None:      overrides["atr_stop_multiplier"] = args.atr_stop
    if args.rr is not None:            overrides["risk_reward"] = args.rr
    if args.time_stop is not None:     overrides["time_stop_days"] = args.time_stop
    if args.turnover_cr is not None:   overrides["min_avg_turnover_cr"] = args.turnover_cr

    bt = DeliveryBacktester(panel, deliv_panel=deliv_panel,
                            params_overrides=overrides, slippage_bps=args.slippage_bps)
    summary = bt.run()

    print("=" * 78)
    print("Delivery Accumulation — Backtest Summary")
    print("=" * 78)
    print(f"  Universe          : {len(universe)} symbols")
    print(f"  Date range        : {panel['date'].min().date()} → {panel['date'].max().date()}")
    print(f"  Trading days      : {summary.get('n_dates', 0)}")
    print(f"  Entry pctile      : {bt.strategy.params['deliv_entry_pctile']:.2f} "
          f"(min hits {bt.strategy.params['deliv_min_hits']:.0f}/5, lag "
          f"{bt.strategy.params['deliv_lag_days']:.0f}d)")
    print(f"  Range-pos max     : {bt.strategy.params['range_pos_max']:.2f}")
    print(f"  ATR stop / RR     : {bt.strategy.params['atr_stop_multiplier']:.1f}× / "
          f"{bt.strategy.params['risk_reward']:.1f}")
    print(f"  Time stop         : {bt.strategy.params['time_stop_days']:.0f} days")
    print(f"  Slippage/side     : {args.slippage_bps:.1f} bps  (+ statutory delivery charges)")
    print("-" * 78)
    if summary["total_trades"] == 0:
        print(f"  No trades fired. Score sentinel: {summary['score']}")
        print("  Likely cause: percentile warm-up (need "
              f"{bt.strategy.params['pctile_min_periods']:.0f}+ delivery bars/symbol) "
              "or gates too tight.")
        return 1
    print(f"  Total trades      : {summary['total_trades']}")
    print(f"  Win rate          : {summary['win_rate']*100:.1f} %")
    print(f"  Profit factor     : {summary['profit_factor']}")
    print(f"  Avg R-multiple    : {summary['avg_R']}")
    print(f"  Avg holding days  : {summary['avg_holding_days']}")
    print(f"  Net P&L           : ₹{summary['net_pnl']:>14,.0f}")
    print(f"  Avg win / loss    : ₹{summary['avg_win']:>10,.0f}  /  ₹{summary['avg_loss']:>10,.0f}")
    print(f"  Sharpe (ann.)     : {summary['sharpe']}")
    print(f"  Max drawdown      : {summary['max_drawdown_pct']} %")
    print(f"  CAGR              : {summary['cagr_pct']} %")
    print(f"  Calmar            : {summary['calmar']}")
    print(f"  Equity            : ₹{summary['starting_equity']:>14,.0f}  →  "
          f"₹{summary['ending_equity']:>14,.0f}")
    print("-" * 78)

    if not args.quiet and bt.trade_log:
        from collections import Counter
        c = Counter(t.exit_reason for t in bt.trade_log)
        print("  Exit reasons      : " + ", ".join(f"{k}={v}" for k, v in c.most_common()))

    if not args.quiet:
        per = bt.per_symbol_breakdown()
        if not per.empty:
            print("-" * 78)
            print("  Top 10 winners (net ₹):")
            print(per.head(10).to_string())
            if len(per) > 10:
                print("\n  Bottom 5 losers (net ₹):")
                print(per.tail(5).to_string())

    if bt.trade_log:
        ledger = pd.DataFrame([t.__dict__ for t in bt.trade_log])
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
