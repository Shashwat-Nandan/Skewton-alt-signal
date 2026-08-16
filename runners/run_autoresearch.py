"""
Run the autoresearch optimization loop for a fixed number of experiments.

Usage:
  # Synthetic data (default):
  python -m runners.run_autoresearch --experiments 50

  # Real historical data from Kite:
  python -m runners.run_autoresearch --data data_cache/NIFTY_20260301_20260330.csv --experiments 100

  # Different metric:
  python -m runners.run_autoresearch --metric net_pnl --data historical.csv
"""
import argparse
import json
import logging
import os
import random
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure project root is on path

from research.backtest import generate_synthetic_data, MockKite, run_backtest
from core.data_cache_io import read_table
from strategies import TalebKarpathyStrategy
from runners.autoresearch_loop import HedgeResearchLoop, VETO_FITNESS, ZERO_TRADE_PENALTY

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
logger = logging.getLogger(__name__)

# Metrics run_backtest actually emits. Also validates the config-derived
# metric (argparse `choices` only checks CLI values): a typo'd
# [autoresearch] metric would otherwise score every cycle via
# metrics.get(typo, 0) == 0.0 — an hours-long flat sweep whose root cause
# is never named.
VALID_METRICS = ("sharpe_ratio", "net_pnl", "calmar_ratio",
                 "sortino_ratio", "gamma_theta_ratio", "convexity_edge")


def _split_data_into_windows(
    data: pd.DataFrame, window_days: int = 5, holdout_days: int = 0,
) -> tuple:
    """
    Split a historical DataFrame into non-overlapping training windows
    and a held-out validation set.

    Training windows are non-overlapping (step = window_days) to prevent
    data leakage between experiments. The last `holdout_days` trading days
    are reserved exclusively for validation and never appear in any
    training window.

    Returns:
        (train_windows, holdout_df)  — holdout_df is None if holdout_days == 0
    """
    data = data.sort_values("timestamp").reset_index(drop=True)
    timestamps = pd.to_datetime(data["timestamp"])
    dates = sorted(timestamps.dt.date.unique())

    # Reserve hold-out
    holdout_df = None
    if holdout_days > 0 and len(dates) > holdout_days + window_days:
        holdout_dates = dates[-holdout_days:]
        train_dates = dates[:-holdout_days]
        holdout_mask = timestamps.dt.date.isin(holdout_dates)
        holdout_df = data[holdout_mask].reset_index(drop=True)
    else:
        train_dates = dates

    if len(train_dates) <= window_days:
        train_mask = timestamps.dt.date.isin(train_dates)
        return [data[train_mask].reset_index(drop=True)], holdout_df

    # Non-overlapping windows (step = window_days)
    windows = []
    for i in range(0, len(train_dates) - window_days + 1, window_days):
        window_dates = train_dates[i : i + window_days]
        mask = timestamps.dt.date.isin(window_dates)
        window = data[mask].reset_index(drop=True)
        if len(window) > 0:
            windows.append(window)

    return windows, holdout_df


def main():
    parser = argparse.ArgumentParser(description="Run autoresearch optimization")
    parser.add_argument("--experiments", type=int, default=50,
                        help="Number of experiments to run (default: 50)")
    parser.add_argument("--metric", type=str, default=None,
                        choices=list(VALID_METRICS),
                        help="Primary metric to optimize. Default: the "
                             "[autoresearch] metric from config.ini (net_pnl on "
                             "this host) — a hardcoded sharpe_ratio default here "
                             "used to silently override the config for manual "
                             "runs. gamma_theta_ratio is the Phase 2.4 Taleb-"
                             "framework efficiency metric, DECOUPLED from money "
                             "(2026-06-14) — don't optimize it alone.")
    parser.add_argument("--eval-cycles", type=int, default=None,
                        help="Backtest replays per experiment — on the "
                             "captured-tape path this IS the replay-window "
                             "size. Default: [autoresearch] "
                             "eval_cycles_per_experiment from config.ini "
                             "(a hardcoded default of 3 used to silently "
                             "clobber the config for manual runs).")
    parser.add_argument("--days", type=int, default=5,
                        help="Days per synthetic replay (default: 5)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed BOTH numpy and stdlib random. Reproduces "
                             "the mutation path (which knob, joint-vs-single, "
                             "step size) for a captured-tape sweep; a "
                             "synthetic-data run also regenerates its tape "
                             "per cycle, so only the tape path is bit-stable.")
    parser.add_argument("--data", type=str, default=None,
                        help="Path to historical data CSV (from market_data/fetch_historical_data.py). "
                             "If omitted, uses synthetic data.")
    parser.add_argument("--underlying", type=str, default="NIFTY",
                        help="Underlying symbol (default: NIFTY)")
    parser.add_argument("--window-days", type=int, default=5,
                        help="Window size in days for historical data cross-validation (default: 5)")
    parser.add_argument("--validation-data", type=str, default=None,
                        help="Path to separate validation CSV (overrides hold-out split)")
    parser.add_argument("--out", type=str, default="best_params.json",
                        help="Where to write the winning params (default: "
                             "best_params.json). The weekly regen points "
                             "this at a dated candidate so the canonical "
                             "file is never touched (audit 2.3).")
    args = parser.parse_args()

    if args.seed is not None:
        # BOTH generators (2026-08-10 review). numpy alone is not
        # reproducibility: `_propose_mutation_once` picks WHICH knob to move
        # with stdlib `random.choice` and decides joint-vs-single with
        # `random.random()`, so an unseeded stdlib stream gives a different
        # mutation path — and, because the no-op re-draw consumes a variable
        # number of `np.random.normal` draws per experiment, it desynchronizes
        # the numpy stream too. An operator re-running --seed to reproduce a
        # weekly candidate before promoting it must get that candidate back.
        np.random.seed(args.seed)
        random.seed(args.seed)

    # Load historical data if provided
    historical_data = None
    historical_windows = None
    holdout_data = None
    if args.data:
        logger.info("Loading historical data from %s", args.data)
        historical_data = read_table(args.data, parse_dates=["timestamp"])
        holdout_days = args.window_days  # Reserve 1 window worth of days for holdout
        historical_windows, holdout_data = _split_data_into_windows(
            historical_data, args.window_days, holdout_days=holdout_days,
        )
        logger.info("Split into %d non-overlapping %d-day training windows + %d-day hold-out",
                    len(historical_windows), args.window_days, holdout_days)
        if holdout_data is not None:
            logger.info("Hold-out: %s — %s (%d rows, never seen during training)",
                        holdout_data["timestamp"].min().date(),
                        holdout_data["timestamp"].max().date(),
                        len(holdout_data))

    # Bootstrap a hedger instance (autoresearch only uses it for param storage)
    dummy_data = generate_synthetic_data(days=1, ticks_per_day=1)
    dummy_kite = MockKite(dummy_data, args.underlying)
    hedger = TalebKarpathyStrategy(dummy_kite, config_path="config.ini", mode="paper")

    # Override autoresearch config for this run. --metric / --eval-cycles
    # omitted keep the loop's config-derived values ([autoresearch]
    # metric / eval_cycles_per_experiment).
    loop = HedgeResearchLoop(hedger, config_path="config.ini")
    if args.metric is not None:
        loop.primary_metric = args.metric
    args.metric = loop.primary_metric
    if args.metric not in VALID_METRICS:
        parser.error(
            f"[autoresearch] metric = {args.metric!r} in config.ini is not "
            f"one of {sorted(VALID_METRICS)} — fix the config or pass --metric."
        )
    if args.eval_cycles is not None:
        loop.eval_cycles = args.eval_cycles
    args.eval_cycles = loop.eval_cycles

    # Pre-screen training windows. A window where the seed params produce 0
    # trades gives the optimizer no gradient — every mutation will tie at the
    # zero-trade penalty floor, so the random walk can't escape. Drop those
    # windows up front; if none survive, fail loudly (the alternative is the
    # silent flat-fitness sweep that hid this bug for a week).
    if historical_windows:
        seed_params = hedger.tunable_params
        kept_windows, dropped_idx = [], []
        for i, w in enumerate(historical_windows):
            try:
                n_trades = run_backtest(
                    w, underlying=args.underlying, tunable_params=seed_params,
                )["metrics"].get("total_trades", 0)
            except Exception as e:
                logger.warning("Pre-screen window %d failed: %s — dropping", i, e)
                n_trades = 0
            if n_trades > 0:
                kept_windows.append(w)
            else:
                dropped_idx.append(i)
        if not kept_windows:
            raise RuntimeError(
                f"All {len(historical_windows)} training windows produced 0 trades "
                f"with seed params. The data likely contains no regime where the "
                f"entry gates trigger. Suggested fixes: widen --days (longer history "
                f"more likely to span a tradable regime), loosen min_rv_iv_ratio in "
                f"best_params.json, or inspect the data CSV for regime."
            )
        logger.info(
            "Pre-screen: kept %d/%d training windows (dropped indices %s for zero-trade)",
            len(kept_windows), len(historical_windows), dropped_idx,
        )
        historical_windows = kept_windows

        # Holdout: warn but never fail — validation is informational, not gating.
        if holdout_data is not None and len(holdout_data) > 0:
            try:
                holdout_trades = run_backtest(
                    holdout_data, underlying=args.underlying, tunable_params=seed_params,
                )["metrics"].get("total_trades", 0)
                if holdout_trades == 0:
                    logger.warning(
                        "Pre-screen: holdout produced 0 trades with seed params — "
                        "validation report at end will be uninformative."
                    )
            except Exception as e:
                logger.warning("Pre-screen holdout backtest failed: %s — proceeding", e)

    # When --data is not provided AND captured tape sessions exist,
    # use autoresearch_loop._run_experiment as-is — it prefers
    # captured tape over synthetic (Phase 2.3) and applies the
    # variance penalty (Phase 2.4) on cycle-averaged metrics. The
    # monkey-patch below was written before those landed and only
    # handles historical CSV / synthetic GBM; skipping it lets the
    # captured-tape path run.
    from research.backtest import list_captured_sessions
    if args.data is None and list_captured_sessions(args.underlying):
        logger.info(
            "No --data flag; using captured-tape replay path "
            "(autoresearch_loop._run_experiment Phase 2.3)."
        )
    else:
        # convexity_edge needs the Phase-1 component metrics, which only
        # accrue on real captured-tape replay (theoretical_scalp_pnl needs
        # per-tick greeks updates; middle_band_worst_pnl needs a real book).
        # On CSV/synthetic data the components sit at 0.0 and the composite
        # silently degenerates to mean(net_pnl) — fail loud instead
        # (Rule 12; same class of bug as the metrics.get(typo, 0) sweep).
        if loop.primary_metric == "convexity_edge":
            raise SystemExit(
                "--metric convexity_edge requires the captured-tape replay "
                "path (no --data flag, with captured sessions present for "
                f"{args.underlying}). CSV/synthetic replay would zero the "
                "component metrics and silently degrade the objective.")
        # Patch _run_experiment to use historical or synthetic data
        def patched_run(params):
            import copy as cp
            original_params = cp.deepcopy(loop.hedger.tunable_params)
            loop.hedger.tunable_params = cp.deepcopy(params)
            try:
                return _patched_run_inner(params)
            finally:
                # Mirror _run_experiment's try/finally: the -999999 early
                # returns below must not leak the mutation into the hedger.
                loop.hedger.tunable_params = original_params

        def _patched_run_inner(params):
            cycle_metrics = []

            if historical_windows:
                # Historical mode: sample random windows for each cycle
                for cycle in range(loop.eval_cycles):
                    try:
                        window = historical_windows[
                            np.random.randint(0, len(historical_windows))
                        ]
                        results = run_backtest(
                            window, underlying=args.underlying, tunable_params=params
                        )
                        m = results["metrics"]
                        if m.get("total_trades", 0) == 0:
                            logger.debug("  Cycle %d: 0 trades — penalty %.0f",
                                         cycle + 1, ZERO_TRADE_PENALTY)
                            m = {**m, loop.primary_metric: ZERO_TRADE_PENALTY}
                        cycle_metrics.append(m)
                    except Exception as e:
                        logger.warning("Cycle %d failed: %s", cycle + 1, e)
                        return VETO_FITNESS
            else:
                # Synthetic mode: generate fresh data per cycle
                for cycle in range(loop.eval_cycles):
                    try:
                        data = generate_synthetic_data(
                            underlying=args.underlying, days=args.days, ticks_per_day=12
                        )
                        results = run_backtest(
                            data, underlying=args.underlying, tunable_params=params
                        )
                        m = results["metrics"]
                        if m.get("total_trades", 0) == 0:
                            logger.debug("  Cycle %d: 0 trades — penalty %.0f",
                                         cycle + 1, ZERO_TRADE_PENALTY)
                            m = {**m, loop.primary_metric: ZERO_TRADE_PENALTY}
                        cycle_metrics.append(m)
                    except Exception as e:
                        logger.warning("Cycle %d failed: %s", cycle + 1, e)
                        return VETO_FITNESS

            if not cycle_metrics:
                return VETO_FITNESS
            # Phase-3: mirror _run_experiment's per-session P&L stash for the
            # validation bootstrap.
            loop._last_cycle_pnls = [
                float(m.get("net_pnl", 0.0)) for m in cycle_metrics]
            values = [m.get(loop.primary_metric, 0) for m in cycle_metrics]
            avg = np.mean(values)
            total_capital = loop.hedger.immutable_params.get("total_capital", 500000)
            max_dd = max(m.get("max_drawdown", 0) for m in cycle_metrics)
            max_dd_pct = (max_dd / total_capital) * 100 if total_capital > 0 else 0
            if max_dd_pct > loop.max_dd_threshold:
                logger.info("  DD %.2f%% exceeds threshold — penalizing", max_dd_pct)
                avg = VETO_FITNESS
            return avg
        loop._run_experiment = patched_run

    # ── Run ──
    # Describe the data the eval will ACTUALLY use. When --data is given the
    # loop replays that CSV; otherwise _run_experiment prefers captured tape
    # (the most recent eval_cycles sessions) and only falls back to synthetic
    # GBM when no tape exists. Mirror that selection here so the headline log
    # and the COMPLETE summary don't mislabel a tape run as "synthetic".
    if args.data:
        data_desc = f"historical ({args.data})"
    else:
        from research.backtest import list_captured_sessions
        captured = list_captured_sessions(args.underlying)
        replay = captured[-args.eval_cycles:] if captured else []
        if replay:
            data_desc = (f"captured tape ({len(replay)} sessions: "
                         f"{replay[0]}..{replay[-1]})")
        else:
            data_desc = f"synthetic ({args.days} days, no captured tape)"
    logger.info("=" * 60)
    logger.info("AUTORESEARCH: %d experiments, metric=%s, %d cycles, data=%s",
                args.experiments, args.metric, args.eval_cycles, data_desc)
    logger.info("Starting params: %s", json.dumps(hedger.tunable_params, indent=2))
    logger.info("=" * 60)

    # Baseline. `establish_baseline` is the ONE definition, shared with
    # HedgeResearchLoop.run() — this driver used to keep a byte-identical
    # copy, and that duplication is what let the report drift onto the
    # moving `loop.baseline_metric` while the loop kept the seed correctly.
    # The return value is the SEED's score and never moves.
    logger.info("[0/%d] Baseline...", args.experiments)
    seed_baseline = loop.establish_baseline()
    # Phase-3: per-session P&Ls of the CURRENT BEST config, refreshed on every
    # acceptance — feeds the validation bootstrap. Starts as the baseline's.
    best_cycle_pnls = list(getattr(loop, "_last_cycle_pnls", []) or [])

    # Experiments
    for i in range(1, args.experiments + 1):
        result = loop.run_single_experiment()
        if result["accepted"]:
            best_cycle_pnls = list(getattr(loop, "_last_cycle_pnls", []) or [])
        status = "ACCEPTED" if result["accepted"] else "rejected"
        logger.info("[%d/%d] %s %s=%.6f (mutated %s: %.4f -> %.4f) best=%.6f",
                    i, args.experiments, status,
                    args.metric, result["metric_value"],
                    result["param_mutated"], result["old_value"], result["new_value"],
                    result["best_so_far"])

    # ── Report ──
    print("\n" + "=" * 60)
    print("AUTORESEARCH COMPLETE")
    print("=" * 60)
    print(f"  Experiments:    {args.experiments}")
    print(f"  Metric:         {args.metric}")
    print(f"  Data:           {data_desc}")
    # `seed_baseline`, NOT loop.baseline_metric: the latter is the hill-
    # climber's *current* anchor and is overwritten on every acceptance
    # (autoresearch_loop.run_single_experiment), so printing it always
    # renders "Baseline == Best" — the 2026-08-08 run reported
    # "Baseline -188.77 / Best -188.77" for a sweep that actually started
    # at -2739.60, i.e. the report hid a 14x improvement and read as
    # "the sweep found nothing". The JSON was always right
    # (sweep_quality.seed_baseline); only this console line was wrong.
    print(f"  Baseline:       {seed_baseline:.6f}" if seed_baseline > VETO_FITNESS
          else f"  Baseline:       {seed_baseline}")
    print(f"  Best:           {loop.best_metric_value:.6f}")
    if VETO_FITNESS < loop.best_metric_value <= 0.0:
        # 2026-07-19 review: a negative best under a NON-vetoed seed is
        # relative improvement only — nothing else in the report says so.
        print("  ⚠️  Best fitness is ≤ 0 — the sweep found less-bad configs, "
              "not positive edge; do not promote on 'beats baseline' alone.")
        logger.warning("Best fitness %.6f is ≤ 0 — relative improvement "
                       "only, no positive-edge config found.",
                       loop.best_metric_value)
    print("\n  Best parameters:")
    for k, v in sorted(loop.best_params.items()):
        if k in loop.TUNABLE_RANGES:
            print(f"    {k:<30s} = {v}")
    print(f"\n  Results log:    {loop.results_file}")

    # ── Sweep quality (Rule 12: an uninformative sweep must say so) ──
    # Computed by the loop itself (HedgeResearchLoop.sweep_quality — see
    # its docstring for the 06-20/06-27 provenance) so this driver and the
    # LOOP-FOREVER path in runners/run.py share one definition.
    sweep_quality = loop.sweep_quality(seed_baseline)
    print("\n  Sweep quality:")
    print(f"    accepted:         {sweep_quality['accepted']}/{sweep_quality['experiments']}")
    print(f"    distinct fitness: {sweep_quality['distinct_fitness']}")
    print(f"    plateau share:    {sweep_quality['plateau_share']:.0%}")
    if sweep_quality["warnings"]:
        print("\n  ⚠️  SWEEP UNINFORMATIVE — do NOT promote this candidate:")
        for w in sweep_quality["warnings"]:
            print(f"      - {w}")
        logger.warning("SWEEP UNINFORMATIVE: %s",
                       "; ".join(sweep_quality["warnings"]))

    # Save best params
    loop._save_best_params(out_file=args.out, sweep_quality=sweep_quality)
    print(f"  Best params:    {args.out}")
    print("=" * 60)

    # Validation run on truly unseen data. seed_iv/skew default to None (IV
    # history wiped) and are only set for the captured-tape branch, which must
    # prime the rolling windows the same way the fitness eval does — otherwise
    # _compute_iv_percentile sees <30 obs, returns the neutral 50.0, and the
    # validation is as degenerate as the metric it's checking.
    #
    # The whole section is best-effort: the candidate is already saved above,
    # and an exception here (e.g. a corrupt .zst hold-out archive — always an
    # archive now that the hold-out sits outside the KEEP_RAW window) would
    # otherwise abort the wrapper's `set -euo pipefail` block AFTER the run's
    # real product succeeded, marking the ~4h oneshot FAILED and skipping
    # candidate pruning.
    try:
        val_seed_iv, val_seed_skew = None, None
        if args.validation_data:
            val_data = read_table(args.validation_data, parse_dates=["timestamp"])
            val_label = (f"separate validation set ({args.validation_data}, "
                         f"{val_data['timestamp'].min().date()} — "
                         f"{val_data['timestamp'].max().date()}, "
                         f"never seen during training)")
        elif holdout_data is not None and len(holdout_data) > 0:
            val_data = holdout_data
            val_label = (f"hold-out ({val_data['timestamp'].min().date()} — "
                         f"{val_data['timestamp'].max().date()}, "
                         f"never seen during training)")
        elif historical_data is not None:
            # Fallback: not enough data for holdout, use full dataset (warn user)
            val_data = historical_data
            val_label = "full dataset (WARNING: no true hold-out, insufficient data)"
        else:
            # No CSV: prefer real captured-tape sessions over synthetic GBM.
            # Phase-3 (2026-07-18): the hold-out is a SET — the most recent
            # outside-window session plus the largest-|move| outside-window
            # session (pick_holdout_sessions). A convexity strategy validated
            # only on the usually-quiet most-recent session can never be
            # falsified; the tail session is where the thesis lives.
            from runners.autoresearch_loop import (
                build_validation_verdict, load_daily_moves, pick_holdout_sessions,
            )
            from research.backtest import (
                list_captured_sessions, load_captured_tape, load_iv_skew_seed,
                load_daily_iv_seed,
            )
            captured = list_captured_sessions(args.underlying)
            window = set(loop._replay_sessions or captured[-loop.eval_cycles:])
            moves = load_daily_moves(args.underlying)
            holdout_dates = pick_holdout_sessions(captured, window, moves)
            if holdout_dates:
                # Reuse the seed the fitness eval built (same drop_recent); compute
                # it if the run never took the tape path. NOTE (Rule 12): the seed
                # is NOT timestamp-filtered against the hold-out dates, so it can
                # include later IV — adequate for a relative sanity check, not a
                # look-ahead-clean absolute claim. Same caveat as load_iv_skew_seed.
                val_seed_iv = getattr(loop, "_iv_seed", None)
                val_seed_skew = getattr(loop, "_skew_seed", None)
                if val_seed_iv is None:
                    drop = loop.config.getint("autoresearch", "iv_seed_drop_recent", fallback=0)
                    val_seed_iv, val_seed_skew = load_iv_skew_seed(args.underlying, drop_recent=drop)
                # A single hold-out session cannot rank itself, so the daily
                # pool must be supplied or the whole hold-out goes inert.
                # Dated, so _compute_iv_percentile drops the session under
                # test and anything later — look-ahead-clean, unlike the two
                # seeds above.
                val_seed_daily = getattr(loop, "_daily_iv_seed", None)
                if not val_seed_daily:
                    val_seed_daily = load_daily_iv_seed(args.underlying)
                if not moves:
                    print("\n  NOTE: daily-move data unavailable — hold-out "
                          "selection degraded to recency-only (no tail session).")
                session_results = []
                print(f"\n  Validation run ({len(holdout_dates)}-session "
                      f"captured-tape hold-out, outside the {len(window)}-"
                      f"session fitness window)...")
                for vd in holdout_dates:
                    vr = run_backtest(
                        load_captured_tape(vd, args.underlying),
                        underlying=args.underlying, tunable_params=loop.best_params,
                        seed_iv_history=val_seed_iv, seed_skew_history=val_seed_skew,
                        seed_daily_iv=val_seed_daily,
                    )["metrics"]
                    session_results.append({
                        "date": vd, "net_pnl": vr["net_pnl"],
                        "total_trades": vr["total_trades"],
                        "max_drawdown": vr["max_drawdown"],
                        "move_pct": moves.get(vd),
                    })
                    mv = moves.get(vd)
                    print(f"    {vd} ({f'{mv:+.2f}%' if mv is not None else 'move n/a'}): "
                          f"Net P/L {vr['net_pnl']:>10,.0f}  "
                          f"Trades {vr['total_trades']}  "
                          f"MaxDD {vr['max_drawdown']:,.0f}")

                capital = float(hedger.immutable_params.get("total_capital", 500000))
                verdict = build_validation_verdict(
                    session_results, capital, moves,
                    insample_pnls=best_cycle_pnls,
                )
                pn = verdict["checks"]["bootstrap_p_negative"]
                print("\n  Promotion checklist:")
                for name, ok in verdict["checks"].items():
                    if name == "bootstrap_p_negative":
                        print(f"    {name:<31} = "
                              f"{'n/a (too few sessions)' if pn is None else f'{pn:.2f} (coarse, n={len(best_cycle_pnls) + len(session_results)})'}")
                    elif name == "shuffle_null_p":
                        alpha = verdict["shuffle_alpha"]
                        n_ins = verdict["shuffle_sessions"]["insample"]
                        n_out = verdict["shuffle_sessions"]["holdout"]
                        # Disclose the in-sample dominance next to the p it
                        # biases (2026-07-19 review): the series is mostly
                        # the replay window the config was optimized on.
                        detail = (f"{ok:.3f} (alpha {alpha:g}; n={n_ins} "
                                  f"in-sample + {n_out} hold-out, "
                                  "in-sample-dominated)") if ok is not None \
                            else "n/a (too few sessions)"
                        print(f"    {name:<31} = {detail}")
                    else:
                        # None = untestable, mirror the p-value rows' label
                        # instead of printing a bare 'None' that reads as a
                        # failed check.
                        print(f"    {name:<31} = "
                              f"{'n/a (not tested)' if ok is None else ok}")
                if verdict.get("walkforward"):
                    wf = verdict["walkforward"]
                    print(f"    walk-forward window P&Ls        : "
                          f"{wf['window_pnls']} "
                          f"(consistency {wf['consistency_rate']:.0%})")
                if verdict["promote_ok"]:
                    gates_untested = (
                        verdict["checks"]["edge_beats_shuffle_null"] is None
                        or verdict["checks"]["walkforward_any_window_positive"]
                        is None)
                    if gates_untested:
                        # Thin data (cold-start host): promote_ok fell back
                        # to the pre-#159 trades/bleed/tail formula — say so
                        # at the verdict line, not only in the warnings.
                        print("    → checks PASS, but the absolute edge "
                              "gates were UNTESTED (thin data) — this is "
                              "the pre-#159 checklist only.")
                    else:
                        print("    → checks PASS — promotion remains an "
                              "operator decision (review warnings + sweep "
                              "quality).")
                else:
                    print("    → DO NOT PROMOTE:")
                for w in verdict["warnings"]:
                    print(f"      - {w}")
                    logger.warning("VALIDATION: %s", w)

                # Persist the verdict into the candidate file (atomic rewrite,
                # best-effort — the candidate itself was already saved above).
                try:
                    cand_path = Path(args.out)
                    cand = json.loads(cand_path.read_text())
                    cand["validation"] = verdict
                    tmp = cand_path.with_suffix(cand_path.suffix + ".tmp")
                    tmp.write_text(json.dumps(cand, indent=2, default=str))
                    os.replace(tmp, cand_path)
                    print(f"  Verdict embedded in {args.out}")
                except Exception as e:
                    logger.warning("Could not embed verdict in %s: %s", args.out, e)
                val_data = None   # handled here; skip the single-run path below
                val_label = None
            else:
                np.random.seed(42)
                # 40 days, not 10: the IV percentile now ranks against one
                # observation per SESSION and needs 30 prior sessions to
                # leave warmup, so a 10-day synthetic tape can never fire an
                # entry. Deliberately NOT seeded from the EOD archive —
                # synthetic vol is not calibrated to real NIFTY vol, so it
                # must rank against its own distribution (daily_iv_from_frame).
                val_data = generate_synthetic_data(days=40, ticks_per_day=12)
                val_label = (f"synthetic (seed=42, 40 days) — all {len(captured)} "
                             f"tape session(s) are inside the fitness window, "
                             f"no hold-out exists")

        if val_data is not None:
            print(f"\n  Validation run ({val_label})...")
            val_results = run_backtest(
                val_data, underlying=args.underlying, tunable_params=loop.best_params,
                seed_iv_history=val_seed_iv, seed_skew_history=val_seed_skew,
            )
            vm = val_results["metrics"]
            print(f"    Net P/L:      {vm['net_pnl']:>12,.2f}")
            print(f"    Sharpe:       {vm['sharpe_ratio']:>12.4f}")
            print(f"    Calmar:       {vm['calmar_ratio']:>12.4f}")
            print(f"    Sortino:      {vm['sortino_ratio']:>12.4f}")
            print(f"    Max DD:       {vm['max_drawdown']:>12,.2f}")
            print(f"    Trades:       {vm['total_trades']}")
    except Exception as e:
        logger.warning("Validation run failed: %s", e)
        print(f"\n  WARNING: validation run failed ({e}) — skipping. The "
              f"candidate at {args.out} and its sweep_quality verdict are "
              "unaffected.")
    print("=" * 60)


if __name__ == "__main__":
    main()
