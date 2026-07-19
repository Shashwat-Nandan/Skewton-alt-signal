#!/usr/bin/env python3
"""Issue #122 — variant D: trailing stop vs the 15:25 EOD flatten (MA arm).

Spec: tasks/kalman-trend-trailing-stop-experiment.md. Runs **variant D only**
(trailing stop + KEEP the daily flatten). D is the spec's "run first" case: it
isolates trail-vs-fixed-stop while never holding overnight, so the two blockers
that gate variant C — gap-aware fills (`check_exit` books at the LEVEL) and NRML
margin (~3x MIS) — do not apply. Same intraday risk profile as the incumbent, so
Sharpe is a fair comparison here (no capital-blindness).

Protocol (per the spec):
  - Walk-forward folds; per fold fit the MA params on TRAIN with the #121
    flatten-aware fit, giving variant A (short/long/offset/stop/target).
  - Variant D reuses A's short/long/offset UNCHANGED and fits ONLY the trail
    distance T on TRAIN (grid, train-Sharpe argmax) — isolating the exit rule.
    Spec non-goal: do not re-fit short/long/offset (that is a fish).
  - Score both on the immediately-following OOS test slice. Pool OOS P&L per
    seed, median across seeds (matches backtest_kalman_trend's basis).
  - >=5 seeds, both instruments, costs at 2.5 AND 8 pts/side-equivalent.
  - Report the T-sensitivity curve at FIXED T (the plateau check): a sharp peak
    is fragility -> REJECT even if the peak is high.

Reuses optimize_kalman_trend's engine (Rule 7) — no bespoke simulator.
Measurement only: changes no runner, no params, no order path.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from research import optimize_kalman_trend as o

TICK = 1.0
T_GRID = [25, 50, 75, 100, 150, 200, 250, 300]


def load(symbol: str):
    """(closes, session_ends, OHLC). Fails loud on a close-only tape: booking at
    the stop LEVEL is what made the first D run fiction (#122)."""
    p = Path("data_cache") / f"{symbol}_5minute.parquet"
    if not p.exists():
        raise FileNotFoundError(p)
    df = pd.read_parquet(p)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return (df["close"].to_numpy(float),
            o.session_ends_from_timestamps(df["datetime"].tolist()),
            o.OHLC.from_frame(df))


def _pooled_sharpe(pnl: np.ndarray) -> float:
    sd = float(np.std(pnl))
    return float(np.mean(pnl) / sd * np.sqrt(o.TRADING_DAYS)) if sd > 0 else float("nan")


def _fit_trail_on_train(train, train_ends, mp, cost, train_ohlc=None):
    """Fit T on TRAIN ONLY (grid, train-Sharpe argmax). No holdout peeking.

    Returns None when NO T yields a finite train Sharpe (nothing traded / zero
    variance). Returning the grid's first entry there — the previous behaviour —
    silently reported T=25 (the floor) for a fold that fitted nothing, which is
    indistinguishable from a genuine boundary pin and would fabricate exactly the
    'T pins at the floor' evidence this experiment is judged on. The caller drops
    such folds from BOTH arms (paired) and reports the count.
    """
    d = o.ma_direction(train, short=mp["short"], long=mp["long"], offset=mp["offset"])
    best_T, best_s = None, -np.inf
    for T in T_GRID:
        r = o.simulate(train, d, stop_ticks=mp["stop_ticks"], target_ticks=mp["target_ticks"],
                       tick_size=TICK, cost_per_unit=cost, session_ends=train_ends,
                       trail_ticks=T, **o._ohlc_kwargs(train_ohlc))
        s = r.sharpe
        if np.isfinite(s) and s > best_s:
            best_T, best_s = T, s
    return best_T


def run(symbol: str, *, train_len: int, test_len: int, step: int, seeds: list[int],
        n_gen: int, cost: float) -> dict:
    closes, ends, ohlc = load(symbol)
    n = len(closes)
    starts = list(range(0, n - train_len - test_len + 1, step))
    seed_A, seed_D = [], []
    # Per-SEED pooled Sharpe then median — the SAME basis as A/D (and as
    # backtest_kalman_trend, which warns that pooling across seeds "shrinks std
    # ~1/sqrt(N) and inflates Sharpe ~sqrt(N)"). Pooling the plateau across seeds
    # while A/D used per-seed-median made the two incomparable, and the plateau
    # is the pre-registered reject criterion.
    fixedT_seed = {T: [] for T in T_GRID}
    chosen_T, n_tr_A, n_tr_D, n_unfittable = [], 0, 0, 0

    for seed in seeds:
        A_pnl, D_pnl = [], []
        fixedT_pnl = {T: [] for T in T_GRID}
        for a in starts:
            b, c = a + train_len, a + train_len + test_len
            train, train_ends = closes[a:b], ends[a:b]
            # FIT and EVAL share the fill model (#121-class mismatch otherwise);
            # OHLC.slice moves all three arrays together so a fold cannot
            # half-slice them.
            train_ohlc = ohlc.slice(a, b)
            mp = o.fit_ma_crossover(train, tick_size=TICK, cost_per_unit=cost,
                                    n_gen=n_gen, seed=seed, session_ends=train_ends,
                                    fit_target=False,   # #125: cannot bind intraday
                                    **o._ohlc_kwargs(train_ohlc))
            T = _fit_trail_on_train(train, train_ends, mp, cost, train_ohlc)
            if T is None:
                # Nothing fitted on this train fold — drop it from BOTH arms so
                # the comparison stays paired, and surface the count (Rule 12).
                n_unfittable += 1
                continue
            # causal signal: warm up from the train start, score only [b:c]
            seg_dir = o.ma_direction(closes[a:c], short=mp["short"], long=mp["long"],
                                     offset=mp["offset"])[b - a:]
            test, test_ends = closes[b:c], ends[b:c]
            test_kw = o._ohlc_kwargs(ohlc, b, c)

            rA = o.simulate(test, seg_dir, stop_ticks=mp["stop_ticks"],
                            target_ticks=mp["target_ticks"], tick_size=TICK,
                            cost_per_unit=cost, session_ends=test_ends, **test_kw)
            rD = o.simulate(test, seg_dir, stop_ticks=mp["stop_ticks"],
                            target_ticks=mp["target_ticks"], tick_size=TICK,
                            cost_per_unit=cost, session_ends=test_ends,
                            trail_ticks=T, **test_kw)
            A_pnl.append(rA.daily_pnl); D_pnl.append(rD.daily_pnl)
            chosen_T.append(T); n_tr_A += rA.n_trades; n_tr_D += rD.n_trades

            # plateau check: same OOS slice at every FIXED T (no fitting)
            for Tf in T_GRID:
                fixedT_pnl[Tf].append(
                    o.simulate(test, seg_dir, stop_ticks=mp["stop_ticks"],
                               target_ticks=mp["target_ticks"], tick_size=TICK,
                               cost_per_unit=cost, session_ends=test_ends,
                               trail_ticks=Tf, **test_kw).daily_pnl)
        if not A_pnl:
            continue
        seed_A.append(_pooled_sharpe(np.concatenate(A_pnl)))
        seed_D.append(_pooled_sharpe(np.concatenate(D_pnl)))
        for T in T_GRID:
            fixedT_seed[T].append(_pooled_sharpe(np.concatenate(fixedT_pnl[T])))

    def med(xs):
        return float(np.nanmedian(xs)) if np.any(np.isfinite(xs)) else float("nan")
    ns = max(len(seeds), 1)
    return {
        "symbol": symbol, "folds": len(starts), "seeds": len(seeds),
        "A_sharpe": med(seed_A), "D_sharpe": med(seed_D),
        # PER-SEED trade counts: the typical single strategy's churn, not the sum
        # over N strategies (mirrors backtest_kalman_trend's *_per_seed metrics).
        "A_trades_per_seed": round(n_tr_A / ns, 1),
        "D_trades_per_seed": round(n_tr_D / ns, 1),
        "T_chosen": chosen_T, "n_unfittable": n_unfittable,
        "plateau": {T: med(fixedT_seed[T]) for T in T_GRID},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="NIFTY,BANKNIFTY")
    ap.add_argument("--train-len", type=int, default=1500)   # ~20 sessions of 5-min
    ap.add_argument("--test-len", type=int, default=375)     # ~5 sessions
    ap.add_argument("--step", type=int, default=375)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--n-gen", type=int, default=20)
    ap.add_argument("--costs", default="2.5,8.0")
    args = ap.parse_args()

    print("\nIssue #122 variant D — trailing stop + KEEP the 15:25 flatten (MA arm)")
    print("HONEST OHLC FILLS (touch detection + gap-aware); fit and eval share them.")
    print("Walk-forward, T fit on TRAIN only, short/long/offset held at incumbent.\n")
    for cost in [float(x) for x in args.costs.split(",")]:
        print(f"{'='*76}\nCOST {cost} pts/side\n{'='*76}")
        for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            r = run(sym, train_len=args.train_len, test_len=args.test_len,
                    step=args.step, seeds=list(range(args.seeds)),
                    n_gen=args.n_gen, cost=cost)
            ts = r["T_chosen"]
            print(f"\n{r['symbol']}  ({r['folds']} folds x {r['seeds']} seeds; "
                  f"per-seed pooled OOS Sharpe, median across seeds)")
            print(f"  A (fixed stop/target + flatten) : Sharpe {r['A_sharpe']:>6.2f}  "
                  f"({r['A_trades_per_seed']} trades/seed)")
            print(f"  D (trailing stop  + flatten)    : Sharpe {r['D_sharpe']:>6.2f}  "
                  f"({r['D_trades_per_seed']} trades/seed)")
            if r["n_unfittable"]:
                print(f"  !! {r['n_unfittable']} fold(s) dropped — no finite train "
                      f"Sharpe at any T (paired: dropped from BOTH arms)")
            if ts:
                print(f"  T chosen on train: median {int(np.median(ts))}, "
                      f"range {min(ts)}-{max(ts)}")
            else:
                print("  T chosen on train: NONE fitted")
            print("  plateau (median per-seed OOS Sharpe at FIXED T — want a plateau, not a spike):")
            print("    " + "  ".join(f"T{T}:{r['plateau'][T]:>6.2f}" for T in T_GRID))
    print(f"\n{'='*76}")
    print("READ: D only advances if it beats A on BOTH instruments at BOTH costs")
    print("AND the plateau is flat. A spike = fragility = REJECT (spec 3.4.4).")
    print("A PASS earns a forward paper A/B slot only — never a live path.")
    print("=" * 76)


if __name__ == "__main__":
    main()
