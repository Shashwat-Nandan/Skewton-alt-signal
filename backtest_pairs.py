"""
Pair-Trading Backtester
=======================

Replays cached F&O bhavcopy STF closes through PairTradingStrategy via a
MockKite-style adapter and reports per-pair P&L.

Each "tick" is one trading day (the bhavcopy is EOD only). The first
~lookback_days produce no trades because the rolling z-score isn't
computable yet — the strategy itself returns None until the spread
history has enough samples. After that, entries fire on |z| >= entry_z,
exits on mean-revert / stop / max-holding.

Open positions at the end of the replay are force-closed at the last
available close so the reported P&L is fully realized.

Usage:
    python backtest_pairs.py                 # top 5 from pair_candidates.csv
    python backtest_pairs.py --top 10        # top 10
    python backtest_pairs.py --entry-z 1.5 --exit-z 0.3 --lookback 45
"""
from __future__ import annotations

import argparse
import configparser
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from screen_pairs import NIFTY_50, load_front_month_panel, screen_pairs
from strategies.pair_trading import PairState, PairTradingStrategy

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_DIR = CACHE_DIR / "bhavcopy_raw"
CANDIDATES_PATH = CACHE_DIR / "pair_candidates.csv"


# ──────────────────────────────────────────────────────────
# MockKite for stock-futures pair backtests
# ──────────────────────────────────────────────────────────

class MockKitePair:
    """
    Minimal Kite stand-in. Each `quote()` returns the close price of the
    *current* tick (date). `instruments("NFO")` returns synthetic non-
    expiring FUT rows so PairTradingStrategy._resolve_futures resolves
    once and caches.
    """

    VARIETY_REGULAR = "regular"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_DAY = "DAY"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, panel: pd.DataFrame, lot_sizes: Dict[str, int]):
        # panel: DataFrame indexed by trading date, columns are symbols, values are close prices.
        self.panel = panel
        self.lot_sizes = lot_sizes
        self._date_idx = 0
        self._orders: List[dict] = []

    @property
    def current_date(self) -> pd.Timestamp:
        return self.panel.index[self._date_idx]

    def advance(self) -> bool:
        if self._date_idx + 1 < len(self.panel):
            self._date_idx += 1
            return True
        return False

    def quote(self, symbols: List[str]) -> Dict[str, dict]:
        out = {}
        for sym in symbols:
            base = sym.split(":", 1)[-1]              # "NFO:RELIANCE_BTFUT" → "RELIANCE_BTFUT"
            underlying = base[: -len("_BTFUT")] if base.endswith("_BTFUT") else base
            if underlying in self.panel.columns:
                px = float(self.panel.iloc[self._date_idx][underlying])
                out[sym] = {
                    "last_price": px,
                    "depth": {
                        "buy": [{"price": px * 0.9985}],
                        "sell": [{"price": px * 1.0015}],
                    },
                }
        return out

    def instruments(self, exchange: str) -> List[dict]:
        if exchange != "NFO":
            return []
        rows = []
        for sym in self.panel.columns:
            rows.append({
                "name": sym,
                "tradingsymbol": f"{sym}_BTFUT",
                "instrument_type": "FUT",
                "lot_size": int(self.lot_sizes.get(sym, 1)),
                "expiry": "2099-12-31",   # never rolls during a backtest
                "instrument_token": abs(hash(sym)) % 1_000_000,
            })
        return rows

    def place_order(self, **kwargs):
        order_id = f"BT-{len(self._orders)}-{self._date_idx}"
        self._orders.append({**kwargs, "order_id": order_id, "date": str(self.current_date)})
        return order_id

    def profile(self):
        return {"user_name": "Backtest", "user_id": "BT0", "exchanges": ["NFO"], "products": ["NRML"]}


# ──────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────

def load_lot_sizes(symbols: List[str], raw_dir: Path = RAW_DIR) -> Dict[str, int]:
    """Read STF lot sizes from the most recent bhavcopy CSV (lots are stable)."""
    files = sorted(raw_dir.glob("bhavcopy_fo_*.csv"))
    if not files:
        raise RuntimeError(f"No bhavcopy CSVs in {raw_dir}")
    latest = files[-1]
    df = pd.read_csv(
        latest,
        usecols=["FinInstrmTp", "TckrSymb", "NewBrdLotQty"],
        dtype={"TckrSymb": str, "FinInstrmTp": str},
    )
    df = df[(df["FinInstrmTp"] == "STF") & (df["TckrSymb"].isin(symbols))]
    return df.drop_duplicates("TckrSymb").set_index("TckrSymb")["NewBrdLotQty"].astype(int).to_dict()


def load_top_pairs(n: int, path: Path = CANDIDATES_PATH) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `python screen_pairs.py` first."
        )
    df = pd.read_csv(path).sort_values("rank_score").head(n).reset_index(drop=True)
    return df


# ──────────────────────────────────────────────────────────
# Strategy bootstrap (bypass __init__ to skip bhavcopy seed)
# ──────────────────────────────────────────────────────────

def make_strategy(
    symbol_a: str, symbol_b: str, hedge_ratio: float, kite: MockKitePair,
    *, entry_z: float, exit_z: float, stop_z: float,
    lookback_days: int, max_holding_days: int, lots_per_leg: int,
    max_leg_notional: Optional[float] = None,
    seed_spreads: Optional[List[float]] = None,
) -> PairTradingStrategy:
    s = PairTradingStrategy.__new__(PairTradingStrategy)
    s.kite = kite
    s.config = configparser.ConfigParser()
    s.config_path = "config.ini"
    s.mode = "paper"
    s.symbol_a = symbol_a
    s.symbol_b = symbol_b
    s.hedge_ratio = hedge_ratio
    s.entry_z = entry_z
    s.exit_z = exit_z
    s.stop_z = stop_z
    s.lookback_days = lookback_days
    s.lots_per_leg = lots_per_leg
    s.max_holding_days = max_holding_days
    s.max_leg_notional = max_leg_notional
    s.total_capital = 500_000
    s.state = PairState()
    # Out-of-sample mode pre-loads the rolling-window history with
    # train-period spreads so z-scores are immediately computable on
    # tick 1 of the test slice — no warm-up bleeding into the test data.
    s._spread_history = list(seed_spreads) if seed_spreads is not None else []
    s._cached_futures = {}
    # Strategy uses _clock() in: max_holding check, entry_time stamp.
    # Bind it to the mock's current backtest date so time-stop fires at
    # the right (replay) bar.
    s._clock = lambda: datetime.combine(kite.current_date.date(), datetime.min.time())
    return s


# ──────────────────────────────────────────────────────────
# Single-pair backtest
# ──────────────────────────────────────────────────────────

def backtest_one(
    pair_row: pd.Series, replay_panel: pd.DataFrame, lot_sizes: Dict[str, int],
    *, entry_z: float, exit_z: float, stop_z: float,
    lookback_days: int, max_holding_days: int, lots_per_leg: int,
    max_leg_notional: Optional[float] = None,
    seed_panel: Optional[pd.DataFrame] = None,
) -> Optional[dict]:
    a, b = pair_row["symbol_a"], pair_row["symbol_b"]
    hedge = float(pair_row["hedge_ratio"])

    if a not in replay_panel.columns or b not in replay_panel.columns:
        logger.warning("%s/%s — missing from replay panel; skipping", a, b)
        return None

    pair_panel = replay_panel[[a, b]].dropna()
    # Without a seed panel, we need at least lookback_days of in-replay
    # warmup before the first computable z-score. With a seed panel,
    # we only need a couple of bars to drive the scan loop.
    min_required = 5 if seed_panel is not None else lookback_days + 5
    if len(pair_panel) < min_required:
        logger.warning("%s/%s — only %d days; need %d. skipping",
                       a, b, len(pair_panel), min_required)
        return None

    seed_spreads: Optional[List[float]] = None
    if seed_panel is not None and a in seed_panel.columns and b in seed_panel.columns:
        seed_pair = seed_panel[[a, b]].dropna()
        seed_spreads = (seed_pair[a] - hedge * seed_pair[b]).tolist()
        # Cap to lookback_days * 3 so the buffer doesn't grow unbounded.
        seed_spreads = seed_spreads[-(lookback_days * 3):]

    mock = MockKitePair(pair_panel, lot_sizes)
    s = make_strategy(
        a, b, hedge, mock,
        entry_z=entry_z, exit_z=exit_z, stop_z=stop_z,
        lookback_days=lookback_days, max_holding_days=max_holding_days,
        lots_per_leg=lots_per_leg, max_leg_notional=max_leg_notional,
        seed_spreads=seed_spreads,
    )

    pnl_curve: List[dict] = []
    while True:
        try:
            entries = s.scan_and_propose()
            if entries:
                s.execute_proposals(entries)
            rehedges = s.check_and_rehedge()
            if rehedges:
                s.execute_proposals(rehedges)
        except Exception as e:
            logger.warning("%s/%s tick %s failed: %s", a, b, mock.current_date.date(), e)

        pnl_curve.append({
            "date": mock.current_date,
            "realized": s.state.realized_pnl,
            "unrealized": s.state.unrealized_pnl,
            "total": s.state.realized_pnl + s.state.unrealized_pnl,
            "position": s.state.position,
        })

        if not mock.advance():
            break

    # Force-close any open position at the last bar so the reported P&L
    # is fully realized — otherwise unrealized leakage masks true
    # round-trip economics.
    if s.state.position != "FLAT":
        last_prices = {a: float(pair_panel[a].iloc[-1]),
                       b: float(pair_panel[b].iloc[-1])}
        # Update marks first so the final unrealized line is correct,
        # then build/execute exit proposals.
        s._update_unrealized(last_prices)
        close_props = s._build_exit_proposals("EOD_CLOSE", 0.0, last_prices)
        if close_props:
            s.execute_proposals(close_props)
        pnl_curve.append({
            "date": pair_panel.index[-1],
            "realized": s.state.realized_pnl,
            "unrealized": s.state.unrealized_pnl,
            "total": s.state.realized_pnl + s.state.unrealized_pnl,
            "position": "FORCE_CLOSED",
        })

    pnl_df = pd.DataFrame(pnl_curve).set_index("date")
    return {
        "pair": f"{a}/{b}",
        "symbol_a": a,
        "symbol_b": b,
        "hedge_ratio": hedge,
        "n_days": len(pair_panel),
        "n_round_trips": len(s.state.closed_trades),
        "n_orders": len(mock._orders),
        "realized_pnl": s.state.realized_pnl,
        "unrealized_pnl": s.state.unrealized_pnl,
        "total_pnl": s.state.realized_pnl + s.state.unrealized_pnl,
        "transaction_costs": s.state.total_transaction_costs,
        "win_rate_pct": _win_rate(s.state.closed_trades),
        "max_drawdown": _max_drawdown(pnl_df["total"].values),
        "pnl_curve": pnl_df,
        "closed_trades": s.state.closed_trades,
    }


def _win_rate(closed_trades: List[dict]) -> Optional[float]:
    if not closed_trades:
        return None
    # Each closed_trade carries the cumulative realized_pnl AT close — diff
    # them to get per-trade P&L.
    diffs = []
    prev = 0.0
    for t in closed_trades:
        rp = t.get("realized_pnl", 0.0)
        diffs.append(rp - prev)
        prev = rp
    if not diffs:
        return None
    wins = sum(1 for d in diffs if d > 0)
    return wins / len(diffs) * 100


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    return float(dd.min())


# ──────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────

def print_report(results: List[dict], args):
    print()
    mode_label = (
        f"OUT-OF-SAMPLE (train_fraction={args.train_fraction}, screened on train, "
        f"backtest on test)"
        if args.train_fraction is not None
        else "IN-SAMPLE (lookahead bias — see warning above)"
    )
    cap_label = f"max-leg-notional=₹{args.max_leg_notional:,.0f}" if args.max_leg_notional else "no cap"
    print(f"Pair-trading backtest — {mode_label}")
    print(f"  top={args.top}, lookback={args.lookback_days}d, "
          f"entry={args.entry_z} exit={args.exit_z} stop={args.stop_z}, "
          f"max-hold={args.max_holding_days}d, lots-per-leg={args.lots_per_leg}, {cap_label}")
    print("=" * 110)
    print(f"{'#':<3} {'Pair':<22} {'β':>9} {'Days':>5} {'Trips':>6} {'Net P&L':>13} "
          f"{'Costs':>11} {'Gross P&L':>13} {'Win%':>6} {'MaxDD':>13}")
    print("-" * 110)
    total_net = total_costs = total_gross = 0.0
    n = len(results)
    for i, r in enumerate(results, 1):
        if r is None:
            continue
        gross = r["total_pnl"] + r["transaction_costs"]
        win = f"{r['win_rate_pct']:.1f}" if r["win_rate_pct"] is not None else "  —"
        print(f"{i:<3} {r['pair']:<22} {r['hedge_ratio']:>9.3f} {r['n_days']:>5d} "
              f"{r['n_round_trips']:>6d} {r['total_pnl']:>13,.0f} "
              f"{r['transaction_costs']:>11,.0f} {gross:>13,.0f} "
              f"{win:>6} {r['max_drawdown']:>13,.0f}")
        total_net += r["total_pnl"]
        total_costs += r["transaction_costs"]
        total_gross += gross
    print("-" * 110)
    print(f"{'':<3} {'TOTAL':<22} {'':>9} {'':>5} {'':>6} {total_net:>13,.0f} "
          f"{total_costs:>11,.0f} {total_gross:>13,.0f} {'':>6} {'':>13}")
    print("=" * 110)
    print(f"  Aggregate net P&L (lots_per_leg={args.lots_per_leg}): ₹{total_net:,.0f} "
          f"on {n} pairs over ~{results[0]['n_days'] if results else '?'} trading days each")
    print()


# ──────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    p = argparse.ArgumentParser(description="Backtest the top N pair-trading pairs")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--candidates", type=str, default=str(CANDIDATES_PATH))
    p.add_argument("--entry-z", type=float, default=2.0)
    p.add_argument("--exit-z", type=float, default=0.75)
    p.add_argument("--stop-z", type=float, default=4.0)
    p.add_argument("--lookback", type=int, default=30, dest="lookback_days")
    p.add_argument("--max-hold", type=int, default=7, dest="max_holding_days")
    p.add_argument("--lots-per-leg", type=int, default=1)
    p.add_argument("--max-leg-notional", type=float, default=None,
                   help="Cap per-leg notional in ₹. Strategy scales BOTH legs "
                        "down (preserving the hedge ratio) to fit, or skips "
                        "the entry if even 1 lot of the larger leg busts the cap. "
                        "Tames high-β pairs that would otherwise auto-deploy "
                        "huge amounts.")
    p.add_argument("--train-fraction", type=float, default=None,
                   help="Out-of-sample mode: re-screen pairs on the first "
                        "N fraction of bhavcopy days, then backtest only on "
                        "the remaining test slice. Eliminates the lookahead "
                        "bias of using the cached pair_candidates.csv (which "
                        "was screened on the full panel). e.g. 0.7")
    p.add_argument("--universe", type=str, default=None,
                   help="When --train-fraction is set, screen this universe "
                        "(newline-separated symbol file). Default: NIFTY 50.")
    p.add_argument("--save-curves", type=str, default=None,
                   help="Optional: write per-pair P&L curves to this CSV")
    args = p.parse_args()

    if args.train_fraction is not None and not (0.1 < args.train_fraction < 0.95):
        logger.error("--train-fraction must be in (0.1, 0.95)")
        return 1

    if args.train_fraction is not None:
        # Out-of-sample: load the broader universe, split, screen on train,
        # backtest on test.
        if args.universe:
            universe = [s.strip() for s in Path(args.universe).read_text().splitlines() if s.strip()]
        else:
            universe = NIFTY_50
        logger.info("Out-of-sample mode: loading panel for %d-symbol universe", len(universe))
        full_panel = load_front_month_panel(universe, min_coverage=0.50)
        lot_sizes = load_lot_sizes(universe)

        n = len(full_panel)
        cutoff = int(n * args.train_fraction)
        train_panel = full_panel.iloc[:cutoff]
        test_panel = full_panel.iloc[cutoff:]
        logger.info("Split %d days: train %s → %s (%d), test %s → %s (%d)",
                    n,
                    train_panel.index[0].date(), train_panel.index[-1].date(), len(train_panel),
                    test_panel.index[0].date(), test_panel.index[-1].date(), len(test_panel))

        logger.info("Re-screening on train slice…")
        screened = screen_pairs(train_panel, p_threshold=0.05, min_correlation=0.5)
        if screened.empty:
            logger.error("No pairs passed cointegration on train slice; aborting")
            return 1
        pairs = screened.head(args.top).reset_index(drop=True)
        logger.info("Top %d train-screened pairs (these are NEW selections, not from "
                    "cached pair_candidates.csv):", args.top)
        for _, r in pairs.iterrows():
            logger.info("  %s/%s β=%.3f p=%.4f half-life=%.1fd",
                        r["symbol_a"], r["symbol_b"], r["hedge_ratio"],
                        r["coint_pvalue"], r["half_life_days"])

        replay_panel = test_panel
        seed_panel = train_panel
    else:
        logger.warning("IN-SAMPLE backtest — using cached pair_candidates.csv. "
                       "This has lookahead bias (the screener saw all the data "
                       "we're now testing on). Use --train-fraction for honest "
                       "out-of-sample results.")
        pairs = load_top_pairs(args.top, Path(args.candidates))
        universe = sorted(set(pairs["symbol_a"]) | set(pairs["symbol_b"]))
        logger.info("Loading bhavcopy panel for %d unique symbols", len(universe))
        replay_panel = load_front_month_panel(universe, min_coverage=0.50)
        lot_sizes = load_lot_sizes(universe)
        seed_panel = None

    missing_lots = [s for s in universe if s not in lot_sizes]
    if missing_lots:
        logger.warning("Missing lot sizes for: %s", missing_lots)

    results = []
    for _, row in pairs.iterrows():
        logger.info("Backtesting %s/%s (β=%.3f)…", row["symbol_a"], row["symbol_b"], row["hedge_ratio"])
        r = backtest_one(
            row, replay_panel, lot_sizes,
            entry_z=args.entry_z, exit_z=args.exit_z, stop_z=args.stop_z,
            lookback_days=args.lookback_days, max_holding_days=args.max_holding_days,
            lots_per_leg=args.lots_per_leg,
            max_leg_notional=args.max_leg_notional,
            seed_panel=seed_panel,
        )
        if r is not None:
            results.append(r)

    print_report(results, args)

    if args.save_curves and results:
        curves = pd.concat(
            [r["pnl_curve"].assign(pair=r["pair"]) for r in results],
            axis=0,
        )
        curves.to_csv(args.save_curves)
        logger.info("Wrote P&L curves to %s", args.save_curves)


if __name__ == "__main__":
    main()
