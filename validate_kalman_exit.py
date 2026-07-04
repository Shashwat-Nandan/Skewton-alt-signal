#!/usr/bin/env python3
"""Kalman pairs — exit-band / debounce validation (issue #66).
================================================================
Quantifies the re-based exit (book exit-at-mean, exit_z=0.0, entry-side
zero-crossing, exit_debounce_ticks=2) against a small profit-take band on the
5-MINUTE intraday path — the path the daily backtest (one decision/day) can't
see. The concern: a position that reverts NEAR the mean but never crosses zero,
or overshoots for a single bar then reverses, is not closed and can decay to the
stop, turning a near-winner into a loss (PR #64 review #9).

Method (faithful to backtest_kalman_pairs.run_replay_5min — same per-day day_ca
step, same EOD close, same fills/costs), plus per-trade instrumentation the
production replay lacks:
  • closest approach to the mean while holding (min|z|), sampled at BAR START so
    the exit bar counts — this is the direct "stall-to-stop" signal;
  • exit-reason mix (MEAN_REVERT / STOP / MAX_HOLD / EOD_CLOSE);
  • both a CONTINUOUS full-window run (production-faithful) AND a MAY/JUN split
    (robustness) — the two can disagree, which is itself the finding.

Reads data_cache/stf_5min/ (fetch_5min_stf.py) + daily bhavcopy for the OOS
screen. Selection = composite (the shipped runner default). Writes per-trade rows
to data_cache/kalman_exit66_trades.csv so every reported number is reproducible.

Usage:
    python validate_kalman_exit.py                 # continuous + split, top-12
    python validate_kalman_exit.py --top 12 --debounce 2
"""
from __future__ import annotations

import argparse
from collections import Counter
from multiprocessing import Pool
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest_kalman_pairs import _screen, _write_temp_config, load_5min_panel
from backtest_pairs import load_lot_sizes
from screen_pairs import NIFTY_50, load_front_month_panel
from strategies.kalman_pair_trading import KalmanPairStrategy

CACHE = Path("data_cache")
EXITS = [0.0, 0.1, 0.25, 0.4, 0.6]
STALL_BANDS = (0.2, 0.3, 0.4, 0.5)   # "near the mean" is threshold-sensitive → report a range
SPLIT = pd.Timestamp("2026-06-01")


def _screened_jobs(bars_panel: pd.DataFrame, train_panel: pd.DataFrame,
                   top: int) -> List[tuple]:
    """Top-N composite pairs screened OOS on `train_panel`, replayed on the
    5-min `bars_panel`. Both selection and filter seed are pre-window."""
    screened = _screen("composite", train_panel)
    lot_sizes = load_lot_sizes(NIFTY_50)
    jobs, n = [], 0
    for _, row in screened.iterrows():
        a, b = row["symbol_a"], row["symbol_b"]
        if a not in bars_panel.columns or b not in bars_panel.columns:
            continue
        if a not in lot_sizes or b not in lot_sizes:
            continue
        bars = bars_panel[[a, b]].dropna()
        tr = train_panel[[a, b]].dropna()
        if len(bars) < 100 or len(tr) < 60:
            continue
        n += 1
        if n > top:
            break
        jobs.append((a, b, int(lot_sizes[a]), int(lot_sizes[b]),
                     tr[a].values, tr[b].values, bars))
    return jobs


def replay(a, b, la, lb, tra, trb, bars, exit_z, debounce
           ) -> Tuple[Optional[dict], list]:
    """One pair/config. Mirrors run_replay_5min (per-day day_ca step, skip
    fully-halted days, EOD close) and additionally records, per closed trade,
    its closest approach to the mean (min|z| while open) + exit reason + P&L."""
    ts_a, ts_b = f"{a}_FUT", f"{b}_FUT"
    quote = {ts_a: None, ts_b: None}
    cur = [bars.index[0].to_pydatetime()]
    cfg = _write_temp_config({
        "entry_z": 1.5, "exit_z": exit_z, "stop_z": 4.0, "lookback_days": 126,
        "max_holding_days": 7, "lots_per_leg": 1, "max_leg_notional": 2_000_000,
        "min_edge_multiplier": 1.5, "adf_gate_p": 0.05, "adf_gate_window": 60,
        "exit_debounce_ticks": debounce,
    })
    try:
        s = KalmanPairStrategy(
            kite=None, config_path=cfg, mode="paper", symbol_a=a, symbol_b=b,
            tradingsymbol_a=ts_a, tradingsymbol_b=ts_b, lot_size_a=la, lot_size_b=lb,
            training_a=tra, training_b=trb, model="momentum", alpha=1e-6,
            quote_fn=lambda t: quote.get(t), clock=lambda: cur[0],
        )
    except ValueError:
        return None, []

    per_trade: list = []
    n_closed, cur_min = 0, float("inf")
    last_ca = last_cb = None
    for _day, day_bars in bars.groupby(bars.index.date):
        day_ca = day_cb = None                 # per-day reset (production parity)
        for tsx, row in day_bars.iterrows():
            ca, cb = float(row[a]), float(row[b])
            quote[ts_a], quote[ts_b] = ca, cb
            cur[0] = tsx.to_pydatetime()
            # Closest approach sampled at BAR START while holding, so the exit
            # bar's z (a MAX_HOLD/stall that closes near the mean) is counted.
            if s.state.position != "FLAT":
                sp, _ = s._observe_spread()
                z = s._z_score(sp)
                if z is not None:
                    cur_min = min(cur_min, abs(z))
            try:
                e = s.scan_and_propose()
                if e:
                    s.execute_proposals(e)
                r = s.check_and_rehedge()
                if r:
                    s.execute_proposals(r)
            except Exception:                  # a bad bar must not abort the replay
                pass
            if len(s.state.closed_trades) > n_closed:
                for t in s.state.closed_trades[n_closed:]:
                    per_trade.append((t.get("exit_reason"),
                                      cur_min if cur_min != float("inf") else np.nan,
                                      t.get("realized_pnl", 0.0)))
                n_closed = len(s.state.closed_trades)
                cur_min = float("inf")
            if ca > 0 and cb > 0:
                day_ca, day_cb = ca, cb
                last_ca, last_cb = ca, cb
        if day_ca is not None:
            s.step_daily_close(day_ca, day_cb)

    if s.state.position != "FLAT" and last_ca is not None:
        s._update_unrealized({a: last_ca, b: last_cb})
        props = s._build_exit_proposals("EOD_CLOSE", 0.0, {a: last_ca, b: last_cb})
        if props:
            s.execute_proposals(props)
        for t in s.state.closed_trades[n_closed:]:
            per_trade.append((t.get("exit_reason"),
                              cur_min if cur_min != float("inf") else np.nan,
                              t.get("realized_pnl", 0.0)))

    m = {"net": s.state.realized_pnl + s.state.unrealized_pnl,
         "costs": s.state.total_transaction_costs,
         "trips": len(s.state.closed_trades)}
    return m, per_trade


def _one(task):
    (a, b, la, lb, tra, trb, bars), (label, ex, db) = task
    m, per = replay(a, b, la, lb, tra, trb, bars, ex, db)
    if m is None:
        return None, None
    m.update({"label": label, "exit_z": ex, "debounce": db})
    rows = [{"label": label, "pair": f"{a}/{b}", "exit_z": ex, "debounce": db,
             "reason": r, "min_abs_z": mz, "pnl": pnl} for r, mz, pnl in per]
    return m, rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalman pairs exit-band validation (#66)")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--debounce", type=int, default=2,
                    help="debounce for the exit-reason/stall detail table")
    args = ap.parse_args()

    panel5 = load_5min_panel(NIFTY_50)
    if panel5.empty:
        print("No 5-min data in data_cache/stf_5min/ — run fetch_5min_stf.py first.")
        return 1
    daily = load_front_month_panel(NIFTY_50, min_coverage=0.50)
    t0 = panel5.index.min().date()

    # Build the three window job-sets: CONTINUOUS (whole window) + MAY + JUN.
    cont = _screened_jobs(panel5, daily[daily.index.date < t0], args.top)
    dm = panel5.index.date
    may = _screened_jobs(panel5[dm < SPLIT.date()], daily[daily.index.date < t0], args.top)
    jun = _screened_jobs(panel5[dm >= SPLIT.date()],
                         daily[daily.index.date < SPLIT.date()], args.top)
    windows = {"CONT": cont, "MAY": may, "JUN": jun}
    print(f"5-min exit validation: {panel5.index.min().date()}→"
          f"{panel5.index.max().date()}; top={args.top}; "
          f"CONT={len(cont)} MAY={len(may)} JUN={len(jun)} pairs")

    tasks = [(j, (label, ex, db)) for label, jobs in windows.items()
             for ex in EXITS for db in (1, 2) for j in jobs]
    with Pool(8) as pool:
        res = pool.map(_one, tasks, chunksize=3)
    aggs = pd.DataFrame([m for m, _ in res if m])
    trades = pd.DataFrame([r for _, rows in res if rows for r in rows])
    trades.to_csv(CACHE / "kalman_exit66_trades.csv", index=False)

    pd.set_option("display.width", 200)

    def net_table(label):
        g = (aggs[aggs.label == label].groupby(["exit_z", "debounce"])
             .agg(net=("net", "sum"), trips=("trips", "sum")).reset_index())
        return g.pivot_table(index="exit_z", columns="debounce", values="net")

    print("\n=== net P&L by exit_z (CONTINUOUS full-window, production-faithful) ===")
    print(net_table("CONT").to_string(float_format=lambda x: f"{x:,.0f}"))
    print("\n=== net P&L by exit_z — split-half robustness (debounce=%d) ===" % args.debounce)
    sh = (aggs[(aggs.label.isin(("MAY", "JUN"))) & (aggs.debounce == args.debounce)]
          .groupby(["exit_z", "label"]).net.sum().unstack())
    print(sh.to_string(float_format=lambda x: f"{x:,.0f}"))

    print("\n=== exit-reason mix + stall-to-stop sensitivity "
          "(CONTINUOUS, debounce=%d) ===" % args.debounce)
    ct = trades[(trades.label == "CONT") & (trades.debounce == args.debounce)]
    for ex in EXITS:
        sub = ct[ct.exit_z == ex]
        rc = Counter(sub.reason)
        adv = sub[sub.reason.isin(["STOP", "MAX_HOLD"])].min_abs_z.dropna()
        stalls = "/".join(str(int((adv <= b).sum())) for b in STALL_BANDS)
        closest = f"{adv.min():.3f}" if len(adv) else "n/a"
        print(f" exit_z={ex}: MR={rc.get('MEAN_REVERT',0)} STOP={rc.get('STOP',0)} "
              f"MH={rc.get('MAX_HOLD',0)} EOD={rc.get('EOD_CLOSE',0)} | adverse "
              f"closest|z|={closest} stall@{'/'.join(map(str, STALL_BANDS))}={stalls}")
    print(f"\nPer-trade rows → {CACHE / 'kalman_exit66_trades.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
