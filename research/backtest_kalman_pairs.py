"""
Kalman Pair-Trading Backtester — the Phase-2 go/no-go
=====================================================
Replays cached F&O bhavcopy STF closes through KalmanPairStrategy and compares
three hedge-ratio trackers on the SAME pairs, window, sizing, costs, and z-band:

  • static    — Kalman with α≈0, i.e. γ frozen at the training-window OLS value
                (the incumbent static-β behaviour, run through the identical
                code path so the comparison is apples-to-apples — no duplicated
                decision logic);
  • basic     — basic Kalman, Eq.(15.3), α=1e-5 (book default);
  • momentum  — momentum Kalman, Eq.(15.4), α=1e-6 (book default).

Each "tick" is one trading day (bhavcopy is EOD). The pair universe comes from
data_cache/pair_candidates.csv (discovery is unchanged — decision D6); the
Kalman system IGNORES the static screener β and re-fits γ itself on the log-
price training window. Decisions on day t use the predicted state α_{t|t-1}
(causal) and the day-t close; step_daily_close then folds day t in.

Out-of-sample by construction: the filter is seeded on the train slice; P&L is
booked only on the test slice. Open positions are force-closed at the last test
bar so reported P&L is fully realized.

The go/no-go question (CLAUDE.md Rule 12 — be explicit if the answer is "no"):
does Kalman produce a more stationary spread AND better net-of-cost P&L than
static β on Indian F&O pairs? The book's edge is on US ETFs; it may not transfer.

Timeframe (issue #63 — all backtests on 5-min): --timeframe defaults to "5min",
which replays entry/exit on 5-min bars and needs data_cache/stf_5min/ (populated
by market_data/fetch_5min_stf.py on the host). The legacy daily go/no-go report is still
available via --timeframe daily and needs no extra data.

Usage:
    python -m research.backtest_kalman_pairs --timeframe daily              # daily report (no host data needed)
    python -m research.backtest_kalman_pairs --timeframe daily --top 15 --train-fraction 0.5
    python -m research.backtest_kalman_pairs                                # 5-min (needs data_cache/stf_5min/)
    python -m research.backtest_kalman_pairs --csv-out data_cache/kalman_bt.csv
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd


from research.backtest_pairs import load_lot_sizes
from core.data_cache_io import read_table
from core import universe as _universe
from core.screen_pairs import (
    NIFTY_50, _half_life, load_front_month_panel, screen_pairs, screen_pairs_book,
)
from strategies.kalman_pair_trading import KalmanPairStrategy

logger = logging.getLogger(__name__)

CACHE_DIR = Path("./data_cache")
CANDIDATES_PATH = CACHE_DIR / "pair_candidates.csv"
STF_5MIN_DIR = CACHE_DIR / "stf_5min"   # per-symbol 5-min CSVs (market_data/fetch_5min_stf.py)

# (label, model, alpha). α≈0 is the frozen-γ (static-β) limit.
CONFIGS = [
    ("static", "basic", 1e-12),
    ("basic", "basic", 1e-5),
    ("momentum", "momentum", 1e-6),
]


def _write_temp_config(knobs: dict) -> str:
    """KalmanPairStrategy reads [kalman_pair_trading] from its config_path and
    refuses paper mode without max_leg_notional. Write a throwaway ini with the
    backtest knobs so we construct it normally — no __init__ bypass (Rule 3)."""
    fd = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
    fd.write("[strategy]\ntotal_capital = 500000\n\n[kalman_pair_trading]\n")
    for k, v in knobs.items():
        fd.write(f"{k} = {v}\n")
    fd.close()
    return fd.name


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float((equity - peak).min())


def _win_rate(closed_trades: List[dict]) -> Optional[float]:
    if not closed_trades:
        return None
    wins = sum(1 for t in closed_trades if t.get("realized_pnl", 0.0) > 0)
    return wins / len(closed_trades) * 100


def _force_close(strat, symbol_a, symbol_b, price_a, price_b, equity: list) -> None:
    """Force-close an open position at the last test price and append the final
    realized+unrealized equity point (so reported P&L is fully realized). No-op
    when already flat. Shared by the daily and 5-min replays."""
    if strat.state.position == "FLAT":
        return
    prices = {symbol_a: price_a, symbol_b: price_b}
    strat._update_unrealized(prices)
    props = strat._build_exit_proposals("EOD_CLOSE", 0.0, prices)
    if props:
        strat.execute_proposals(props)
    equity.append(strat.state.realized_pnl + strat.state.unrealized_pnl)


def _replay_metrics(strat, symbol_a, symbol_b, label, spreads: list,
                    equity: list, n_days: int) -> dict:
    """The 12-key per-pair result row shared by run_replay and run_replay_5min.
    Single source of truth for the report schema print_report consumes — a new
    field or an accounting fix now lands in both replays at once."""
    sp = np.asarray(spreads, dtype=float)
    return {
        "pair": f"{symbol_a}/{symbol_b}", "config": label,
        "n_days": n_days,
        "n_round_trips": len(strat.state.closed_trades),
        "net_pnl": strat.state.realized_pnl + strat.state.unrealized_pnl,
        "costs": strat.state.total_transaction_costs,
        "gross_pnl": (strat.state.realized_pnl + strat.state.unrealized_pnl
                      + strat.state.total_transaction_costs),
        "win_rate_pct": _win_rate(strat.state.closed_trades),
        "max_drawdown": _max_drawdown(np.asarray(equity)),
        "spread_var": float(np.var(sp)) if sp.size else float("nan"),
        "half_life": _half_life(sp) if sp.size > 10 else float("nan"),
        "final_gamma": strat._gamma_today,
    }


def run_replay(symbol_a, symbol_b, lot_a, lot_b,
               train_a, train_b, test_a, test_b, test_dates,
               *, label, model, alpha, config_path) -> Optional[dict]:
    """One pair, one config. Returns metrics or None (skipped, with a reason
    logged) when the filter can't be seeded (degenerate/out-of-band γ).

    `label` (static/basic/momentum) — NOT `model` — is the config key, because
    the static and basic configs share model='basic' (they differ only in α)."""
    ts_a, ts_b = f"{symbol_a}_FUT", f"{symbol_b}_FUT"
    quote = {ts_a: None, ts_b: None}
    cur = [test_dates[0]]
    try:
        strat = KalmanPairStrategy(
            client=None, config_path=config_path, mode="paper",
            symbol_a=symbol_a, symbol_b=symbol_b,
            tradingsymbol_a=ts_a, tradingsymbol_b=ts_b,
            lot_size_a=lot_a, lot_size_b=lot_b,
            training_a=train_a, training_b=train_b,
            model=model, alpha=alpha,
            quote_fn=lambda t: quote.get(t),
            clock=lambda: datetime.combine(cur[0], datetime.min.time()),
        )
    except ValueError as e:
        logger.warning("%s/%s [%s] skipped: %s", symbol_a, symbol_b, label, e)
        return None

    spreads, equity = [], []
    for ca, cb, d in zip(test_a, test_b, test_dates):
        quote[ts_a], quote[ts_b] = float(ca), float(cb)
        cur[0] = d
        try:
            entries = strat.scan_and_propose()
            if entries:
                strat.execute_proposals(entries)
            rehedges = strat.check_and_rehedge()
            if rehedges:
                strat.execute_proposals(rehedges)
        except Exception as e:  # a bad bar must not abort the whole replay
            logger.warning("%s/%s [%s] tick %s failed: %s",
                           symbol_a, symbol_b, label, d, e)
        spreads.append(strat.step_daily_close(float(ca), float(cb)))
        equity.append(strat.state.realized_pnl + strat.state.unrealized_pnl)

    # Force-close any open position at the last test bar.
    _force_close(strat, symbol_a, symbol_b,
                 float(test_a[-1]), float(test_b[-1]), equity)
    return _replay_metrics(strat, symbol_a, symbol_b, label, spreads, equity,
                           len(test_dates))


def load_5min_panel(symbols, directory: Path = STF_5MIN_DIR) -> "pd.DataFrame":
    """Wide close panel (index=5-min datetime, columns=symbol) from the per-symbol
    tables written by market_data/fetch_5min_stf.py. Sorted; per-pair alignment is via dropna."""
    frames = {}
    for s in symbols:
        # Follow renames: the 5-min corpus is written per symbol, so a table
        # for a retired ticker still holds that security's history (#226).
        for candidate in dict.fromkeys([_universe.canonical(s), s]):
            try:
                df = read_table(directory / f"{candidate}.parquet",
                                parse_dates=["date"])
            except FileNotFoundError:
                continue
            frames[_universe.canonical(s)] = df.set_index("date")["close"]
            break
    # Rule 12: a missing 5-min table used to be a bare `continue`, so a symbol
    # simply vanished from every Kalman study with no message — the same
    # silent-skip that let two dead tickers survive months in the universe
    # (review of PR #227).
    _universe.report_unresolved(symbols, frames, f"5-min panel ({directory})",
                                log=logger)
    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames).sort_index()


def run_replay_5min(symbol_a, symbol_b, lot_a, lot_b, train_a, train_b, bars,
                    *, label, model, alpha, config_path) -> Optional[dict]:
    """5-minute replay (timeframe rule, issue #63). Seeds the filter on the DAILY
    training window (D1 — the filter still updates once per day), then makes
    entry/exit decisions on EACH 5-min bar and advances the filter once per day on
    that day's last bar's close. This mirrors the live runner (intraday decisions
    against live quotes, daily filter update) — the path the daily backtest never
    exercised. `bars` is a DataFrame indexed by 5-min datetime, columns
    [symbol_a, symbol_b]."""
    ts_a, ts_b = f"{symbol_a}_FUT", f"{symbol_b}_FUT"
    quote = {ts_a: None, ts_b: None}
    cur = [bars.index[0].to_pydatetime()]
    try:
        strat = KalmanPairStrategy(
            client=None, config_path=config_path, mode="paper",
            symbol_a=symbol_a, symbol_b=symbol_b,
            tradingsymbol_a=ts_a, tradingsymbol_b=ts_b,
            lot_size_a=lot_a, lot_size_b=lot_b,
            training_a=train_a, training_b=train_b, model=model, alpha=alpha,
            quote_fn=lambda t: quote.get(t),
            clock=lambda: cur[0],            # the current 5-min bar's timestamp
        )
    except ValueError as e:
        logger.warning("%s/%s [%s] skipped: %s", symbol_a, symbol_b, label, e)
        return None

    spreads, equity = [], []
    n_days = 0
    last_ca = last_cb = None
    for _day, day_bars in bars.groupby(bars.index.date):
        n_days += 1
        day_ca = day_cb = None        # day's last VALID (positive) close
        for tsx, row in day_bars.iterrows():
            ca, cb = float(row[symbol_a]), float(row[symbol_b])
            quote[ts_a], quote[ts_b] = ca, cb
            cur[0] = tsx.to_pydatetime()
            try:
                e = strat.scan_and_propose()
                if e:
                    strat.execute_proposals(e)
                r = strat.check_and_rehedge()
                if r:
                    strat.execute_proposals(r)
            except Exception as ex:        # a bad bar must not abort the replay
                logger.warning("%s/%s [%s] bar %s failed: %s",
                               symbol_a, symbol_b, label, tsx, ex)
            equity.append(strat.state.realized_pnl + strat.state.unrealized_pnl)
            # Only positive closes are valid (Kite returns 0.0 for halted/illiquid
            # futures; dropna keeps a 0.0). A 0.0 last bar must not reach
            # step_daily_close, which raises on non-positive prices (would abort
            # the whole replay — the "a bad bar must not abort" guarantee).
            if ca > 0 and cb > 0:
                day_ca, day_cb = ca, cb
                last_ca, last_cb = ca, cb
        # End of day: advance the filter on the day's last VALID close (D1). Skip
        # days with no valid bar (fully halted) rather than crash.
        if day_ca is not None:
            spreads.append(strat.step_daily_close(day_ca, day_cb))

    # Force-close at the day's last VALID close (None only if every bar halted).
    if last_ca is not None:
        _force_close(strat, symbol_a, symbol_b, last_ca, last_cb, equity)
    return _replay_metrics(strat, symbol_a, symbol_b, label, spreads, equity,
                           n_days)


def backtest_pair(a, b, train_panel, test_panel, lot_sizes, *, configs,
                  config_path) -> List[dict]:
    """Seed the filter on the TRAIN-slice prices, book P&L only on the
    TEST slice. Pair SELECTION happens upstream by screening the train panel,
    so both selection and the filter seed are out-of-sample (no look-ahead)."""
    if a not in lot_sizes or b not in lot_sizes:
        logger.warning("%s/%s missing lot size; skipping", a, b)
        return []
    train = train_panel[[a, b]].dropna()
    test = test_panel[[a, b]].dropna()
    if len(train) < 60:
        logger.warning("%s/%s train slice too short (%d); skipping", a, b, len(train))
        return []
    if len(test) < 30:
        logger.warning("%s/%s test slice too short (%d); skipping", a, b, len(test))
        return []
    train_a, train_b = train[a].values, train[b].values
    test_a, test_b = test[a].values, test[b].values
    test_dates = [d.date() for d in test.index]

    out = []
    for label, model, alpha in configs:
        r = run_replay(a, b, int(lot_sizes[a]), int(lot_sizes[b]),
                       train_a, train_b, test_a, test_b, test_dates,
                       label=label, model=model, alpha=alpha,
                       config_path=config_path)
        if r:
            out.append(r)
    return out


def print_report(rows: List[dict], args):
    df = pd.DataFrame(rows)
    if df.empty:
        print("No results — no pair could be backtested.")
        return
    print(f"\nKalman pair backtest — top={args.top}, "
          f"train_fraction={args.train_fraction} (OUT-OF-SAMPLE), "
          f"entry={args.entry_z} exit={args.exit_z} stop={args.stop_z} "
          f"lookback={args.lookback_days}d max-hold={args.max_holding_days}d "
          f"adf-gate={'off' if args.adf_gate_p <= 0 else f'p<{args.adf_gate_p}/{args.adf_gate_window}d'}")
    print("=" * 96)

    # Per-config aggregate — the headline go/no-go comparison. Spread-var is
    # reported as the MEDIAN: a pair whose γ drifts toward −1 makes the
    # normalized spread (÷(1+γ)) blow up, so the mean is outlier-dominated.
    print(f"\n{'config':<10} {'pairs':>5} {'trips':>6} {'net P&L':>14} "
          f"{'gross P&L':>14} {'costs':>12} {'avg win%':>9} "
          f"{'med spread-var':>15} {'med half-life':>14}")
    print("-" * 96)
    order = {c[0]: i for i, c in enumerate(CONFIGS)}
    for cfg in sorted(df["config"].unique(), key=lambda c: order.get(c, 99)):
        g = df[df["config"] == cfg]
        wins = g["win_rate_pct"].dropna()
        hl = g["half_life"].replace([np.inf, -np.inf], np.nan)
        print(f"{cfg:<10} {len(g):>5} {int(g['n_round_trips'].sum()):>6} "
              f"{g['net_pnl'].sum():>14,.0f} {g['gross_pnl'].sum():>14,.0f} "
              f"{g['costs'].sum():>12,.0f} "
              f"{(wins.mean() if not wins.empty else float('nan')):>9.1f} "
              f"{g['spread_var'].median():>15.5f} "
              f"{hl.median():>14.2f}")

    # Spread-stationarity: does Kalman lower the spread variance vs static,
    # pair by pair? Report the fraction of pairs where it does.
    piv = df.pivot_table(index="pair", columns="config", values="spread_var")
    if {"static", "basic", "momentum"}.issubset(piv.columns):
        print("\nSpread variance vs static (lower = more stationary):")
        for cfg in ("basic", "momentum"):
            better = (piv[cfg] < piv["static"]).sum()
            print(f"  {cfg:<9}: more stationary than static on "
                  f"{better}/{len(piv)} pairs")
    if args.csv_out:
        df.to_csv(args.csv_out, index=False)
        print(f"\nPer-pair rows written to {args.csv_out}")


def build_parser():
    """The backtester's argument parser. Exposed (not inlined in main) so its
    argparse defaults are inspectable by the sync test that pins entry_z to the
    runner/strategy/config-template copies (they had drifted as independent
    literals — see tasks/kalman-pairs-rebase-plan.md)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--train-fraction", type=float, default=0.5)
    # Defaults re-based on Palomar Ch.15 (exit at mean, 6-mo lookback) + the ADF
    # regime gate. Was entry 2.0 / exit 0.75 / lookback 60. Entry 1.0→1.5 per
    # the 2026-07-04 5-min revalidation (parity with strategy/runner defaults).
    ap.add_argument("--entry-z", dest="entry_z", type=float, default=1.5)
    ap.add_argument("--exit-z", dest="exit_z", type=float, default=0.0)
    ap.add_argument("--stop-z", dest="stop_z", type=float, default=4.0)
    ap.add_argument("--lookback-days", dest="lookback_days", type=int, default=126)
    ap.add_argument("--max-holding-days", dest="max_holding_days", type=int, default=7)
    ap.add_argument("--lots-per-leg", dest="lots_per_leg", type=int, default=1)
    ap.add_argument("--max-leg-notional", dest="max_leg_notional", type=float,
                    default=2_000_000)
    ap.add_argument("--min-edge-multiplier", dest="min_edge_multiplier",
                    type=float, default=1.5)
    ap.add_argument("--adf-gate-p", dest="adf_gate_p", type=float, default=0.05)
    ap.add_argument("--adf-gate-window", dest="adf_gate_window", type=int, default=60)
    # Pair selection: "book" = NPD prescreen + cointegration gate (Palomar §15.4,
    # the Kalman system's selection); "composite" = the static system's
    # p-value/half-life/vol rank. A/B them on the holdout.
    # "book" = NPD prescreen + NPD rank; "book-composite" = NPD prescreen +
    # composite rank (the hybrid); "composite" = correlation prescreen + composite.
    ap.add_argument("--selection", choices=("book", "book-composite", "composite"),
                    default="composite")
    # Timeframe (issue #63): "5min" replays entry/exit on 5-min bars (filter still
    # daily, D1) using data_cache/stf_5min/ from market_data/fetch_5min_stf.py; "daily" uses
    # bhavcopy closes (one decision/day) and is the legacy path.
    ap.add_argument("--timeframe", choices=("daily", "5min"), default="5min")
    ap.add_argument("--csv-out", dest="csv_out", default=None)
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    if not (0.1 < args.train_fraction < 0.95):
        ap.error("--train-fraction must be in (0.1, 0.95)")

    config_path = _write_temp_config({
        "entry_z": args.entry_z, "exit_z": args.exit_z, "stop_z": args.stop_z,
        "lookback_days": args.lookback_days,
        "max_holding_days": args.max_holding_days,
        "lots_per_leg": args.lots_per_leg,
        "max_leg_notional": args.max_leg_notional,
        "min_edge_multiplier": args.min_edge_multiplier,
        "adf_gate_p": args.adf_gate_p, "adf_gate_window": args.adf_gate_window,
        # Daily: one bar = one tick → exit on the first in-band bar. 5-min: keep
        # the 2-tick debounce (it's an intraday-noise filter, its actual purpose).
        "exit_debounce_ticks": 1 if args.timeframe == "daily" else 2,
    })
    lot_sizes = load_lot_sizes(NIFTY_50)

    if args.timeframe == "5min":
        return _main_5min(args, config_path, lot_sizes)

    # ── daily path (legacy) ──
    # Screen on the TRAIN slice, backtest on the holdout — pair selection is
    # out-of-sample (using the pre-screened pair_candidates.csv, which was
    # screened on RECENT data, would leak future cointegration into the test).
    panel = load_front_month_panel(NIFTY_50, min_coverage=0.50)
    cut = int(len(panel) * args.train_fraction)
    train_panel, test_panel = panel.iloc[:cut], panel.iloc[cut:]
    print(f"Screening {len(train_panel)} train days for cointegrated pairs "
          f"(selection={args.selection}, holdout = {len(test_panel)} days)...")
    screened = _screen(args.selection, train_panel)
    if screened.empty:
        print("No cointegrated pairs found on the train slice.")
        return 0
    screened = screened.head(args.top)

    rows: List[dict] = []
    for _, row in screened.iterrows():
        rows.extend(backtest_pair(
            row["symbol_a"], row["symbol_b"], train_panel, test_panel,
            lot_sizes, configs=CONFIGS, config_path=config_path,
        ))
    print_report(rows, args)
    return 0


def _screen(selection: str, train_panel):
    if selection == "book":
        return screen_pairs_book(train_panel, p_threshold=0.05, rank_by="npd")
    if selection == "book-composite":
        return screen_pairs_book(train_panel, p_threshold=0.05, rank_by="composite")
    return screen_pairs(train_panel, p_threshold=0.05, min_correlation=0.5)


def _main_5min(args, config_path, lot_sizes) -> int:
    """5-min replay: seed/screen on DAILY bhavcopy BEFORE the 5-min window, replay
    entry/exit on the 5-min bars. Selection stays out-of-sample (screened on the
    pre-window daily slice). Needs data_cache/stf_5min/ (market_data/fetch_5min_stf.py)."""
    panel5 = load_5min_panel(NIFTY_50)
    if panel5.empty:
        print("No 5-min data in data_cache/stf_5min/ — run market_data/fetch_5min_stf.py on "
              "the host (live Kite session) first. See issue #63.")
        return 1
    t0 = panel5.index.min().date()
    daily = load_front_month_panel(NIFTY_50, min_coverage=0.50)
    train_panel = daily[daily.index.date < t0]
    print(f"5-min replay: {len(panel5)} bars over "
          f"{panel5.index.min().date()}→{panel5.index.max().date()}; "
          f"screening {len(train_panel)} daily train days before the window "
          f"(selection={args.selection})...")
    if len(train_panel) < 60:
        print(f"Daily training slice before {t0} too short ({len(train_panel)}).")
        return 1
    screened = _screen(args.selection, train_panel)
    if screened.empty:
        print("No cointegrated pairs on the pre-window daily slice.")
        return 0

    rows: List[dict] = []
    n_pairs = 0
    for _, row in screened.iterrows():
        a, b = row["symbol_a"], row["symbol_b"]
        if a not in panel5.columns or b not in panel5.columns:
            continue
        if a not in lot_sizes or b not in lot_sizes:
            continue
        bars = panel5[[a, b]].dropna()
        tr = train_panel[[a, b]].dropna()
        if len(bars) < 100 or len(tr) < 60:
            continue
        n_pairs += 1
        if n_pairs > args.top:
            break
        for label, model, alpha in CONFIGS:
            r = run_replay_5min(a, b, int(lot_sizes[a]), int(lot_sizes[b]),
                                tr[a].values, tr[b].values, bars,
                                label=label, model=model, alpha=alpha,
                                config_path=config_path)
            if r:
                rows.append(r)
    if not rows:
        print("No pairs had both legs in the 5-min data + a daily train slice.")
        return 0
    print_report(rows, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
