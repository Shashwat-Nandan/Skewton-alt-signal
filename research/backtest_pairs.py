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
    python -m research.backtest_pairs                 # top 5 from pair_candidates.csv
    python -m research.backtest_pairs --top 10        # top 10
    python -m research.backtest_pairs --entry-z 1.5 --exit-z 0.3 --lookback 45
"""
from __future__ import annotations

import argparse
import configparser
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.data_cache_io import find_tables, read_table


from core.screen_pairs import (
    NIFTY_50,
    classify_pair_candidates,
    load_front_month_panel,
    screen_pairs,
)
from research.engine import MockBroker
from strategies.pair_trading import (
    DEFAULT_MARGIN_HEADROOM,
    PairState,
    PairTradingStrategy,
)

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
RAW_DIR = CACHE_DIR / "bhavcopy_raw"
CANDIDATES_PATH = CACHE_DIR / "pair_candidates.csv"


# Mock broker: shared research/engine.MockBroker (moved from a local
# MockKitePair 2026-07-21; defaults are byte-identical for this harness —
# parity in tests/test_mock_broker.py).


# ──────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────

def load_lot_sizes(symbols: List[str], raw_dir: Path = RAW_DIR) -> Dict[str, int]:
    """Read STF lot sizes from the most recent bhavcopy day (lots are stable)."""
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    if not files:
        raise RuntimeError(f"No bhavcopy tables in {raw_dir}")
    latest = files[-1]
    df = read_table(
        latest,
        usecols=["FinInstrmTp", "TckrSymb", "NewBrdLotQty"],
        dtype={"TckrSymb": str, "FinInstrmTp": str},
    )
    df = df[(df["FinInstrmTp"] == "STF") & (df["TckrSymb"].isin(symbols))]
    return df.drop_duplicates("TckrSymb").set_index("TckrSymb")["NewBrdLotQty"].astype(int).to_dict()


def load_stf_expiries(raw_dir: Path = RAW_DIR) -> List[date]:
    """Every distinct single-stock-futures expiry date on the bhavcopy tape.

    Feeds MockBroker so replays force-flatten on real expiry days instead of
    holding a contract that, in the mock, never expired. ~8s over a 2-year
    cache; called once per backtest run, not per pair.
    """
    files = find_tables(raw_dir, "bhavcopy_fo_*")
    if not files:
        raise RuntimeError(f"No bhavcopy tables in {raw_dir}")
    out: set = set()
    for f in files:
        df = read_table(
            f,
            usecols=["FinInstrmTp", "XpryDt"],
            dtype={"FinInstrmTp": str},
        )
        df = df[df["FinInstrmTp"] == "STF"]
        if df.empty:
            continue
        out |= set(pd.to_datetime(df["XpryDt"], errors="coerce").dropna().dt.date)
    if not out:
        raise RuntimeError(f"No STF expiry dates found in {raw_dir}")
    return sorted(out)


def select_top_pairs(df: pd.DataFrame, n: int,
                     max_pvalue: Optional[float] = None) -> pd.DataFrame:
    """Pick the `n` pairs the live runner would trade, in its admit order.

    2026-08-07: this harness used to `sort_values("rank_score").head(n)`,
    which is NOT what runners/run_paper_pairs.select_pairs does — it skipped
    the |β| band, the corr/half-life/p-value quality floor and the
    leg-concentration cap. The gap is not cosmetic: on the same OOS split and
    identical strategy params, the raw-rank universe reported ₹-5.63M against
    ₹+213k for the live-selected one, because raw rank admits |β|≈0.1 pairs
    and stacks six of eight legs on one symbol. A backtest that trades a
    universe the runner would refuse cannot validate the runner (Rule 9).

    Selection is delegated to core.screen_pairs.classify_pair_candidates so
    there is exactly one implementation of the rules (Rule 7); `select_pairs`
    layers only the runner's stale-CSV age check on top, which is meaningless
    on a replay.

    `max_pvalue` mirrors the runner's --quality-max-pvalue. It must be
    threaded, not defaulted: the persistent (real-money) system passes 0.05
    because its CSV already cleared the persistence screen, and re-testing at
    QUALITY_MAX_PVALUE=0.025 is double jeopardy. Hardcoding 0.025 here made
    the harness *refuse pairs the live runner trades* — the same
    universe-mismatch defect this function exists to fix, inverted.
    """
    annotated = classify_pair_candidates(df, n, logger, max_pvalue=max_pvalue)
    admitted = annotated[annotated["skip_reason"] == ""]
    if "processing_rank" in admitted.columns:
        admitted = admitted.sort_values("processing_rank")
    return admitted.head(n).reset_index(drop=True)


def load_top_pairs(n: int, path: Path = CANDIDATES_PATH,
                   max_pvalue: Optional[float] = None) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run `python -m core.screen_pairs` first."
        )
    return select_top_pairs(pd.read_csv(path), n, max_pvalue)


# ──────────────────────────────────────────────────────────
# Strategy bootstrap (bypass __init__ to skip bhavcopy seed)
# ──────────────────────────────────────────────────────────

def make_strategy(
    symbol_a: str, symbol_b: str, hedge_ratio: float, kite: MockBroker,
    *, entry_z: float, exit_z: float, stop_z: float,
    lookback_days: int, max_holding_days: int, lots_per_leg: int,
    max_leg_notional: Optional[float] = None,
    min_edge_multiplier: float = 1.5,
    max_entry_z: float = 3.25,
    safety_buffer: float = 0.75,
    entry_dte_buffer_days: int = 1,
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
    s.max_entry_z = max_entry_z
    s.safety_buffer = safety_buffer
    s.min_edge_multiplier = min_edge_multiplier
    s.lookback_days = lookback_days
    s.lots_per_leg = lots_per_leg
    s.max_holding_days = max_holding_days
    s.entry_dte_buffer_days = entry_dte_buffer_days
    s.max_leg_notional = max_leg_notional
    s.total_capital = 500_000
    # __init__ establishes these but this __new__ bootstrap bypasses it; the
    # tick/entry/fill paths read them, so without them every tick raises
    # AttributeError and the backtest silently zeroes out. (The list drifted as
    # features landed — H5 cooldown, book-notional cap, place-order backoff,
    # exit debounce — so set the full set, not just the one that throws first.)
    #
    # Most mirror the live __init__ defaults AND are inert at daily resolution:
    # cooldown is in MINUTES so 60 ≪ one daily bar (1440 min); the place-order
    # backoff never trips with the always-succeeding mock; the book cap stays
    # disabled (None); slippage matches the live 5 bps default.
    #
    # exit_debounce_ticks is the EXCEPTION: live uses 2, but a "tick" is a
    # daily bar here, so 2 would impose a 2-DAY mean-revert exit debounce where
    # live has ~2 minutes (2×60s) — over-holding reverted spreads and biasing
    # P&L. The debounce is an intraday-noise filter; a daily close is already
    # settled, so use 1 (exit on first in-band bar) for a faithful daily replay.
    s.stop_cooldown_minutes = 60
    # Live-only precheck knob (backtest is paper, never calls it), but mirror
    # the __init__ default so the bootstrap stays complete.
    s._margin_headroom = DEFAULT_MARGIN_HEADROOM
    s.paper_slippage_bps = 5.0
    s.exit_debounce_ticks = 1
    s.max_book_notional = None
    s._book_notional_fn = None
    s._kite_refresh = None
    s._nfo_instruments_cache = None
    s._holidays_cache = None
    s._pending_exit_reason = None
    # Issue #90: no signal publishing in backtests — replays are not master
    # decisions and must never land on the bus.
    s._signal_publisher = None
    s.signal_system_tag = None
    s._pending_entry_z = None
    s._place_order_fail_streak = 0
    s._place_order_skip_ticks_left = 0
    s._place_order_skip_window = 5
    s.limit_protection_pct = 0.25
    s._spread_panel = None
    s._session_start_realized = 0.0
    s._session_start_unrealized = 0.0
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

def _legs_expired_by(s: PairTradingStrategy, today: date) -> bool:
    """True once any held leg's contract has reached or passed its expiry.

    The `>=` is the point — see the call site. Legs restored from a
    pre-expiry-field state file carry expiry="" and are skipped; in a replay
    every leg is stamped at entry from _resolve_futures, so that only affects
    hand-built fixtures.
    """
    for leg in s.state.legs:
        if not leg.expiry:
            continue
        try:
            exp = datetime.strptime(str(leg.expiry)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if today >= exp:
            return True
    return False


def backtest_one(
    pair_row: pd.Series, replay_panel: pd.DataFrame, lot_sizes: Dict[str, int],
    *, entry_z: float, exit_z: float, stop_z: float,
    lookback_days: int, max_holding_days: int, lots_per_leg: int,
    max_leg_notional: Optional[float] = None,
    min_edge_multiplier: float = 1.5,
    max_entry_z: float = 3.25,
    safety_buffer: float = 0.75,
    entry_dte_buffer_days: int = 1,
    seed_panel: Optional[pd.DataFrame] = None,
    expiries: Optional[List[date]] = None,
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

    mock = MockBroker(pair_panel, lot_sizes, expiries=expiries)
    s = make_strategy(
        a, b, hedge, mock,
        entry_z=entry_z, exit_z=exit_z, stop_z=stop_z,
        lookback_days=lookback_days, max_holding_days=max_holding_days,
        lots_per_leg=lots_per_leg, max_leg_notional=max_leg_notional,
        min_edge_multiplier=min_edge_multiplier,
        max_entry_z=max_entry_z,
        safety_buffer=safety_buffer,
        entry_dte_buffer_days=entry_dte_buffer_days,
        seed_spreads=seed_spreads,
    )

    pnl_curve: List[dict] = []
    while True:
        # Both instrument caches are sticky for a strategy's lifetime, which
        # live is one session. Here one instance spans months, so unless they
        # are dropped per bar every expiry lookup — the EXPIRY flatten below
        # and the entry DTE gate in scan_and_propose — answers with bar 1's
        # contract and silently never fires.
        if expiries is not None:
            s._nfo_instruments_cache = None
            s._cached_futures = {}
        try:
            entries = s.scan_and_propose()
            if entries:
                s.execute_proposals(entries)
            rehedges = s.check_and_rehedge()
            if rehedges:
                s.execute_proposals(rehedges)
        except Exception as e:
            logger.warning("%s/%s tick %s failed: %s", a, b, mock.current_date.date(), e)

        # Contract expiry, mirroring run_paper_pairs' session-end check
        # (it flattens at 15:25 on expiry day, so the flatten belongs after
        # the bar's scan/rehedge, not before).
        #
        # Deliberately `today >= expiry`, not legs_expire_on's `== today`.
        # The runner ticks every calendar trading day so equality always
        # lands; a replay does not. pair_panel is `replay_panel[[a, b]]
        # .dropna()` over a panel built with min_coverage=0.50, so either leg
        # missing on the expiry date removes that bar, equality never holds,
        # and by the next bar the front month has already rolled — the
        # position then carries across the roll on a continuous price series,
        # free of charge. That is latent on today's gap-free NIFTY-50 cache
        # and live for exactly the thin symbols min_coverage=0.50 admits.
        if expiries is not None and s.state.position != "FLAT":
            today = mock.current_date.date()
            if _legs_expired_by(s, today):
                _, prices = s._observe_spread()
                if prices:
                    s._update_unrealized(prices)
                    exp_props = s._build_exit_proposals("EXPIRY", 0.0, prices)
                    if exp_props:
                        s.execute_proposals(exp_props)

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
          f"entry={args.entry_z} exit={args.exit_z} stop={args.stop_z} "
          f"max_entry={args.max_entry_z} buf={args.safety_buffer} "
          f"min_edge={args.min_edge_multiplier}×, "
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
    p.add_argument("--max-entry-z", type=float, default=3.25, dest="max_entry_z",
                   help="Hard ceiling for entries; past |z|>=max_entry_z the "
                        "spread is treated as a regime break and refused.")
    p.add_argument("--safety-buffer", type=float, default=0.75, dest="safety_buffer",
                   help="Per-trade widening of stop_z: effective_stop = "
                        "max(stop_z, |entry_z| + safety_buffer). Keeps deep "
                        "entries from being insta-stopped by sub-σ jitter.")
    p.add_argument("--min-edge-multiplier", type=float, default=1.5,
                   dest="min_edge_multiplier",
                   help="Refuse entries whose expected ₹ move from current z "
                        "back to exit band is below this multiple of round-trip "
                        "cost. 0 disables the hurdle.")
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
    p.add_argument("--quality-max-pvalue", type=float, default=None,
                   help="Override the cointegration p-value ceiling used by "
                        "pair selection (core.screen_pairs.QUALITY_MAX_PVALUE "
                        "= 0.025). Mirror the runner: the persistent system "
                        "runs --quality-max-pvalue 0.05, so validating that "
                        "book requires passing 0.05 here too.")
    p.add_argument("--no-expiry", action="store_true",
                   help="Disable contract-expiry force-flatten (pre-2026-08-07 "
                        "behaviour). Diagnostic only: it makes held positions "
                        "free to carry across expiry, which the live runner "
                        "cannot do.")
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
        pairs = select_top_pairs(screened, args.top, args.quality_max_pvalue)
        if pairs.empty:
            logger.error("No train-screened pair cleared the live selection "
                         "filters (|β| band / corr / half-life / p-value); aborting")
            return 1
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
        pairs = load_top_pairs(args.top, Path(args.candidates),
                               args.quality_max_pvalue)
        if pairs.empty:
            # load_top_pairs could not return empty before it applied the
            # selection filters. Without this guard `universe` is [] and
            # load_front_month_panel dies with "No STF rows for the requested
            # universe" — an error that blames the bhavcopy cache for what is
            # actually a filter outcome, sending the operator to re-fetch
            # market data. run_paper_pairs.select_pairs raises here too.
            logger.error(
                "No candidate in %s cleared the live selection filters "
                "(|β| band / corr / half-life / p-value ≤ %s). Nothing to "
                "backtest — loosen --quality-max-pvalue or re-screen.",
                args.candidates, args.quality_max_pvalue or "default 0.025",
            )
            return 1
        universe = sorted(set(pairs["symbol_a"]) | set(pairs["symbol_b"]))
        logger.info("Loading bhavcopy panel for %d unique symbols", len(universe))
        replay_panel = load_front_month_panel(universe, min_coverage=0.50)
        lot_sizes = load_lot_sizes(universe)
        seed_panel = None

    missing_lots = [s for s in universe if s not in lot_sizes]
    if missing_lots:
        logger.warning("Missing lot sizes for: %s", missing_lots)

    expiries = None if args.no_expiry else load_stf_expiries()
    if expiries is None:
        logger.warning("--no-expiry: contracts never expire in this replay, so "
                       "held positions are never force-flattened. P&L will be "
                       "optimistic against the live runner (Rule 12).")
    else:
        logger.info("Modelling %d STF expiry dates (%s → %s)",
                    len(expiries), expiries[0], expiries[-1])

    results = []
    for _, row in pairs.iterrows():
        logger.info("Backtesting %s/%s (β=%.3f)…", row["symbol_a"], row["symbol_b"], row["hedge_ratio"])
        r = backtest_one(
            row, replay_panel, lot_sizes,
            entry_z=args.entry_z, exit_z=args.exit_z, stop_z=args.stop_z,
            lookback_days=args.lookback_days, max_holding_days=args.max_holding_days,
            lots_per_leg=args.lots_per_leg,
            max_leg_notional=args.max_leg_notional,
            min_edge_multiplier=args.min_edge_multiplier,
            max_entry_z=args.max_entry_z,
            safety_buffer=args.safety_buffer,
            seed_panel=seed_panel,
            expiries=expiries,
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
