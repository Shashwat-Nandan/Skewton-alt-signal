#!/usr/bin/env python3
"""Backtest the Market-Profile `trend_up` overnight-continuation strategy — the
GATE before any paper runner is built (Phase 3a of the plan).

Signal (known only at the close, from the full-day 30-min profile):
    day_shape == 'trend_up'   →  go long AT TODAY'S CLOSE, exit at NEXT CLOSE.

This is an EOD, one-day-hold strategy: the only decisions happen at closes, so a
close-to-close simulation is *exact* — there is no intraday entry/exit path to
misrepresent (this is why the 5-min backtest convention doesn't bite here; the
signal is built from 30-min bars but the trade never touches an intraday quote).

Portfolio: each day, equal-weight all `trend_up` names, hold one day, net of a
round-trip cost (default 25 bps — a realistic overnight *delivery* hold: STT
~20 bps round-trip + brokerage/exchange/GST/stamp). Days with no signal sit flat.

The go/no-go is the **holdout** block (last 30% of dates, out-of-sample) *and*
per-month behaviour: a single-regime, cost-fragile edge does not graduate
(Rule 12; `feedback_no_promote_if_zero_trade_holdout`).
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import pandas as pd


def load_signals(db_path: str, source: str, bucket_col: str,
                 bucket_val: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            f"SELECT instrument, day, {bucket_col} AS bucket, close "
            "FROM mp_features WHERE source = ? ORDER BY instrument, day",
            conn, params=(source,),
        )
    finally:
        conn.close()
    df["next_close"] = df.groupby("instrument")["close"].shift(-1)
    df["ret"] = (df["next_close"] - df["close"]) / df["close"]
    sig = df[(df["bucket"] == bucket_val)].dropna(subset=["ret"]).copy()
    return sig


def daily_portfolio(sig: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    """Equal-weight the day's signals; net return per calendar day."""
    sig = sig.copy()
    sig["net"] = sig["ret"] - cost_bps / 1e4
    g = sig.groupby("day")["net"]
    port = pd.DataFrame({"port_ret": g.mean(), "n_pos": g.size()})
    port.index = pd.to_datetime(port.index)
    return port.sort_index()


def metrics(port: pd.DataFrame, label: str) -> dict:
    r = port["port_ret"]
    n_days = len(r)
    if n_days == 0:
        return {"label": label, "n_days": 0}
    total = (1 + r).prod() - 1
    ann = (1 + r.mean()) ** 252 - 1
    sharpe = (r.mean() / r.std(ddof=1) * math.sqrt(252)) if r.std(ddof=1) > 0 else float("nan")
    eq = (1 + r).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    return {
        "label": label,
        "n_days": n_days,
        "n_trades": int(port["n_pos"].sum()),
        "avg_pos_per_day": round(port["n_pos"].mean(), 1),
        "total_ret_%": round(total * 100, 1),
        "ann_ret_%": round(ann * 100, 1),
        "sharpe": round(sharpe, 2),
        "max_dd_%": round(dd * 100, 1),
        "daily_mean_bps": round(r.mean() * 1e4, 1),
        "daily_t": round(r.mean() / (r.std(ddof=1) / math.sqrt(n_days)), 2)
                   if r.std(ddof=1) > 0 else float("nan"),
    }


def _print_metrics(m: dict) -> None:
    if m.get("n_days", 0) == 0:
        print(f"  [{m['label']}] no days")
        return
    print(f"  [{m['label']}] days={m['n_days']} trades={m['n_trades']} "
          f"({m['avg_pos_per_day']}/day)")
    print(f"      total {m['total_ret_%']}%  ann {m['ann_ret_%']}%  "
          f"Sharpe {m['sharpe']}  maxDD {m['max_dd_%']}%")
    print(f"      daily mean {m['daily_mean_bps']} bps  (t={m['daily_t']})")


def fit_min_signals(port: pd.DataFrame, split, cost_bps: float,
                    min_train_days: int = 20) -> None:
    """Rescue hypothesis: the edge concentrates on high-signal-count ('broad
    momentum') days. Fit the threshold K on TRAIN, confirm on HOLDOUT — K is
    chosen with NO sight of the holdout (leakage-free; n_pos is known at EOD).
    """
    train = port.loc[port.index < split]
    hold = port.loc[port.index >= split]
    k_max = int(port["n_pos"].max())

    print(f"\n{'=' * 68}\nRESCUE: fit min-signals/day K on TRAIN, confirm on HOLDOUT\n{'=' * 68}")
    print("  TRAIN sweep (days with n_pos >= K):")
    print(f"    {'K':>3} {'days':>5} {'mean_bps':>9} {'sharpe':>7}")
    best_k, best_sharpe = None, -1e9
    for k in range(1, k_max + 1):
        t = train[train["n_pos"] >= k]["port_ret"]
        if len(t) < min_train_days:
            print(f"    {k:>3} {len(t):>5}   (too few train days, stop sweep)")
            break
        sh = t.mean() / t.std(ddof=1) * math.sqrt(252) if t.std(ddof=1) > 0 else float("nan")
        print(f"    {k:>3} {len(t):>5} {t.mean()*1e4:>9.1f} {sh:>7.2f}")
        # Pick the best TRAIN Sharpe that is also net-positive in mean.
        if t.mean() > 0 and sh > best_sharpe:
            best_k, best_sharpe = k, sh

    if best_k is None:
        print("\n  → No K makes TRAIN net-positive at this cost. Rescue FAILS;")
        print("    the trend_up trade is abandoned (Rule 12 — do not fish the holdout).")
        return

    print(f"\n  Chosen K = {best_k} (best net-positive TRAIN Sharpe = {best_sharpe:.2f}).")
    print("  → HOLDOUT confirmation at that fixed K (the verdict):")
    for name, blk in (("TRAIN", train), ("HOLDOUT", hold)):
        sub = blk[blk["n_pos"] >= best_k]
        _print_metrics(metrics(sub, f"{name} K>={best_k}"))
    ho = hold[hold["n_pos"] >= best_k]["port_ret"]
    verdict = (len(ho) >= 8 and ho.mean() > 0
               and ho.mean() / ho.std(ddof=1) > 0) if len(ho) > 1 else False
    print(f"\n  RESCUE VERDICT: {'SURVIVES' if verdict else 'FAILS'} — "
          f"holdout net {'positive' if (len(ho)>0 and ho.mean()>0) else 'negative'} "
          f"(n={len(ho)} days).")
    if not verdict:
        print("  Abandon: a filter that clears train but not holdout is overfitting.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data_cache/dashboard.db")
    ap.add_argument("--source", default="intraday_30m")
    ap.add_argument("--bucket-col", default="day_shape")
    ap.add_argument("--bucket-val", default="trend_up")
    ap.add_argument("--cost-bps", type=float, default=25.0,
                    help="round-trip cost, bps (default 25 = overnight delivery)")
    ap.add_argument("--holdout-frac", type=float, default=0.30,
                    help="fraction of the date range held out-of-sample (default 0.30)")
    ap.add_argument("--fit-min-signals", action="store_true",
                    help="rescue: fit a min-signals/day filter on train, confirm on holdout")
    args = ap.parse_args()

    sig = load_signals(args.db, args.source, args.bucket_col, args.bucket_val)
    if sig.empty:
        print(f"No signals for {args.bucket_col}={args.bucket_val!r}.")
        return

    port = daily_portfolio(sig, args.cost_bps)
    dates = port.index
    split = dates[int(len(dates) * (1 - args.holdout_frac))]

    print(f"\nBacktest — long {args.bucket_col}={args.bucket_val} at close, "
          f"exit next close")
    print(f"  {len(sig)} signals, {sig['instrument'].nunique()} names, "
          f"{port.index.min().date()}..{port.index.max().date()}")
    print(f"  cost = {args.cost_bps} bps round-trip, equal-weight per day")
    print(f"  train/holdout split at {split.date()} "
          f"(holdout = last {int(args.holdout_frac*100)}% of dates)\n")

    _print_metrics(metrics(port, "ALL"))
    _print_metrics(metrics(port.loc[port.index < split], "TRAIN"))
    _print_metrics(metrics(port.loc[port.index >= split], "HOLDOUT"))

    print("\n  per-month (portfolio):")
    pm = port.copy()
    pm["mon"] = pm.index.strftime("%Y-%m")
    by = pm.groupby("mon").apply(
        lambda g: pd.Series({
            "days": len(g),
            "trades": int(g["n_pos"].sum()),
            "mean_bps": round(g["port_ret"].mean() * 1e4, 1),
            "total_%": round(((1 + g["port_ret"]).prod() - 1) * 100, 2),
        }), include_groups=False)
    print(by.to_string())

    # Cost sweep on the holdout — where does the out-of-sample edge die?
    print("\n  holdout daily-mean bps vs cost:")
    ho_sig = sig[pd.to_datetime(sig["day"]) >= split]
    for c in (0, 10, 15, 20, 25, 30, 35):
        p = daily_portfolio(ho_sig, c)
        print(f"    @ {c:>2} bps → {p['port_ret'].mean()*1e4:6.1f} bps/day")

    if args.fit_min_signals:
        fit_min_signals(port, split, args.cost_bps)

    print("\n" + "=" * 68)
    print("GRADUATION: HOLDOUT Sharpe > 0 with positive daily mean AND no month")
    print("that alone sinks it, at the realistic --cost-bps. A gross-only edge")
    print("that dies out-of-sample or net of cost does NOT become a paper runner.")
    print("=" * 68)


if __name__ == "__main__":
    main()
