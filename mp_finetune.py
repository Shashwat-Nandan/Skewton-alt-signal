#!/usr/bin/env python3
"""Pre-registered fine-tune experiments for the MP trend_up paper strategy.

Goal (operator request 2026-07-14): raise net profit PER TRADE and optimise the
NUMBER of trades — i.e. trade less, better — without falling into a parameter
fish (the buy-on-gap lesson: do NOT re-sweep signal params; test few,
economically-motivated hypotheses with train/holdout discipline).

PRE-REGISTERED HYPOTHESES (stated before results were seen):

  H1  Breadth threshold K in {3,4,5,6}. Already observed monotone on holdout in
      the Phase-2 edge report; formalise the per-trade vs trade-count trade-off.
      Economic story: the broader the momentum day, the stronger the herding
      continuation.
  H2  Top-N concentration, N in {3,5}: on qualifying days take only the N
      strongest trend_up names ranked by one_timeframing_run (length of the
      one-way auction). Economic story: the most persistent single-name
      auctions carry the most unfinished business; fewer+larger positions also
      cut flat per-order brokerage drag.
  H3  Poor-high filter: only take trend_up names whose day left a POOR high
      (no excess tail). Dalton: a poor high is revisited — unfinished business
      above → continuation. Excess at the high = the auction finished → fade
      risk.
  H5  Hold horizon h in {1,2,3} days (exit at close of D+h). If continuation
      persists past day 1, per-trade profit rises while round-trips (and cost
      events) fall. Portfolio-level check uses NON-OVERLAPPING entries (only
      enter when flat) so the result is tradeable as-is.

Discipline:
  - Same train/holdout split as backtest_mp_trend.py: last 30% of dates OOS.
  - All results net of 25 bps round-trip (overnight delivery reality).
  - HONESTY NOTE: this holdout has now been read several times (K
    monotonicity, the rescue). It is semi-worn — treat "confirmed" here as
    "promoted to forward-paper test", not as fresh OOS proof. The paper runner
    (kill-switched) remains the true out-of-sample arbiter.

Measurement only — this script trades nothing and changes no runner config.
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import pandas as pd


def load_features(db_path: str, source: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT instrument, day, day_shape, one_timeframing_run, poor_high, "
            "excess_high, close FROM mp_features WHERE source = ? "
            "ORDER BY instrument, day",
            conn, params=(source,),
        )
    finally:
        conn.close()
    for h in (1, 2, 3):
        df[f"c{h}"] = df.groupby("instrument")["close"].shift(-h)
        df[f"r{h}"] = (df[f"c{h}"] - df["close"]) / df["close"] * 1e4  # gross bps
    # Day-level breadth: how many names printed trend_up that day.
    breadth = (df[df["day_shape"] == "trend_up"].groupby("day").size()
               .rename("n_trend_up"))
    df = df.merge(breadth, on="day", how="left")
    df["n_trend_up"] = df["n_trend_up"].fillna(0).astype(int)
    return df


def _tstat(x: pd.Series) -> float:
    n = len(x)
    if n < 2 or x.std(ddof=1) == 0:
        return float("nan")
    return x.mean() / (x.std(ddof=1) / math.sqrt(n))


def per_trade_stats(trades: pd.DataFrame, ret_col: str, cost_bps: float,
                    split: str) -> dict:
    """Per-trade net stats, train vs holdout."""
    out = {}
    for name, blk in (("train", trades[trades["day"] < split]),
                      ("hold", trades[trades["day"] >= split])):
        net = blk[ret_col].dropna() - cost_bps
        days = blk["day"].nunique()
        out[name] = {
            "trades": len(net),
            "days": days,
            "tr/day": round(len(net) / days, 1) if days else 0.0,
            "net_bps": round(net.mean(), 1) if len(net) else float("nan"),
            "median": round(net.median(), 1) if len(net) else float("nan"),
            "win%": round((net > 0).mean() * 100, 1) if len(net) else float("nan"),
            "t": round(_tstat(net), 2) if len(net) else float("nan"),
        }
    return out


def portfolio_daily(trades: pd.DataFrame, ret_col: str, cost_bps: float,
                    split: str) -> dict:
    """Equal-weight per-day portfolio (h=1 only), train vs holdout."""
    d = trades.dropna(subset=[ret_col]).copy()
    d["net"] = d[ret_col] - cost_bps
    daily = d.groupby("day")["net"].mean()
    out = {}
    for name, blk in (("train", daily[daily.index < split]),
                      ("hold", daily[daily.index >= split])):
        sh = (blk.mean() / blk.std(ddof=1) * math.sqrt(252)
              if len(blk) > 1 and blk.std(ddof=1) > 0 else float("nan"))
        out[name] = {"days": len(blk),
                     "bps/day": round(blk.mean(), 1) if len(blk) else float("nan"),
                     "sharpe": round(sh, 2)}
    return out


def nonoverlap_horizon(trades: pd.DataFrame, h: int, cost_bps: float,
                       split: str) -> dict:
    """H5 tradeable check: enter a qualifying day only when FLAT, hold h days.

    Walk qualifying days in order; skip any day that falls inside an open hold.
    Returns per-trade net stats on the non-overlapping cohort set.
    """
    ret_col = f"r{h}"
    days = sorted(trades["day"].unique())
    # exit day of each entry = the h-th next TRADING day for that instrument;
    # approximate the blackout with the day-grid: entry day index + h.
    day_idx = {d: i for i, d in enumerate(days)}
    chosen: list[str] = []
    next_free = -1
    for d in days:
        i = day_idx[d]
        if i >= next_free:
            chosen.append(d)
            next_free = i + h
    sub = trades[trades["day"].isin(chosen)]
    return per_trade_stats(sub, ret_col, cost_bps, split), len(chosen)


def fmt(tag: str, pt: dict, port: dict | None = None) -> str:
    t, hh = pt["train"], pt["hold"]
    line = (f"  {tag:<34} "
            f"TRAIN n={t['trades']:>3} ({t['tr/day']}/d) net={t['net_bps']:>6} t={t['t']:>5}  |  "
            f"HOLD n={hh['trades']:>3} ({hh['tr/day']}/d) net={hh['net_bps']:>6} "
            f"win={hh['win%']}% t={hh['t']:>5}")
    if port:
        line += (f"  |  HOLD port {port['hold']['bps/day']} bps/d "
                 f"Sh {port['hold']['sharpe']}")
    return line


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data_cache/dashboard.db")
    ap.add_argument("--source", default="intraday_30m")
    ap.add_argument("--cost-bps", type=float, default=25.0)
    ap.add_argument("--holdout-frac", type=float, default=0.30)
    args = ap.parse_args()

    df = load_features(args.db, args.source)
    all_days = sorted(df["day"].unique())
    split = all_days[int(len(all_days) * (1 - args.holdout_frac))]
    tu = df[df["day_shape"] == "trend_up"].copy()

    print(f"\nMP trend_up fine-tune — {len(df)} rows, split at {split}, "
          f"cost {args.cost_bps} bps, per-trade returns NET")
    print("=" * 110)

    # ── H1: breadth threshold K ──────────────────────────────────────
    print("\nH1 — breadth threshold K (all trend_up names on days with n_trend_up >= K), h=1:")
    for k in (3, 4, 5, 6):
        sub = tu[tu["n_trend_up"] >= k]
        pt = per_trade_stats(sub, "r1", args.cost_bps, split)
        port = portfolio_daily(sub, "r1", args.cost_bps, split)
        print(fmt(f"K>={k}", pt, port))

    # ── H2: top-N by one_timeframing_run on K>=3 days ────────────────
    print("\nH2 — top-N strongest (one_timeframing_run) on K>=3 days, h=1:")
    base = tu[tu["n_trend_up"] >= 3]
    for n in (3, 5):
        sub = (base.sort_values(["day", "one_timeframing_run"],
                                ascending=[True, False])
               .groupby("day").head(n))
        pt = per_trade_stats(sub, "r1", args.cost_bps, split)
        port = portfolio_daily(sub, "r1", args.cost_bps, split)
        print(fmt(f"top-{n} by 1TF run", pt, port))

    # ── H3: poor-high filter on K>=3 days ────────────────────────────
    print("\nH3 — auction-finish filter on K>=3 days, h=1:")
    for tag, cond in (("poor_high=1 (unfinished above)", base["poor_high"] == 1),
                      ("excess_high=1 (finished)", base["excess_high"] == 1)):
        sub = base[cond]
        pt = per_trade_stats(sub, "r1", args.cost_bps, split)
        port = portfolio_daily(sub, "r1", args.cost_bps, split)
        print(fmt(tag, pt, port))

    # ── H5: hold horizon (per-trade, overlapping cohorts) ────────────
    print("\nH5 — hold horizon h (K>=3, ALL qualifying days; one cost per trade):")
    for h in (1, 2, 3):
        pt = per_trade_stats(base, f"r{h}", args.cost_bps, split)
        print(fmt(f"h={h} close->close+{h}", pt))

    print("\nH5 — NON-OVERLAPPING entries (tradeable as-is: enter only when flat):")
    for h in (2, 3):
        pt, n_entry_days = nonoverlap_horizon(base, h, args.cost_bps, split)
        print(fmt(f"h={h} non-overlap ({n_entry_days} entry days)", pt))

    print("\n" + "=" * 110)
    print("READ: a lever is promotable to the FORWARD paper runner only if HOLD")
    print("per-trade net rises vs the K>=3 baseline WITHOUT the trade count")
    print("collapsing (feedback_no_promote_if_zero_trade_holdout), and the")
    print("portfolio Sharpe does not degrade. This holdout is semi-worn — any")
    print("winner is a candidate for the forward paper book, not proven.")
    print("=" * 110)


if __name__ == "__main__":
    main()
