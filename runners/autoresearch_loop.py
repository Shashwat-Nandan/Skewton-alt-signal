"""
Autoresearch Loop — Karpathy-Style Autonomous Strategy Optimization
====================================================================
Adapted from karpathy/autoresearch pattern:
  - Human writes program.md (strategy config) → agent modifies parameters
  - Each "experiment" = run the hedging strategy with mutated params for N cycles
  - Metric (Sharpe, PnL, etc.) determines keep/discard
  - Results logged to results.tsv for analysis
  - LOOP FOREVER until human interrupts

Key adaptation from ML to trading:
  - train.py → tunable_params dict (rehedge threshold, position size, etc.)
  - val_bpb → Sharpe ratio / net PnL / Calmar ratio
  - 5-min training budget → N hedging cycles (configurable)
  - Git branches → parameter snapshots in results.tsv

Safety: Immutable params (max loss, no naked shorts, etc.) are NEVER mutated.

References:
  - Karpathy, "autoresearch" (2026) — github.com/karpathy/autoresearch
  - program.md pattern: human writes intent, agent executes experiments
"""

import copy
import json
import time
import random
import logging
import configparser
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import numpy as np

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)


# Sentinel returned from a single backtest cycle that produced zero trades.
# Distinct from the -999999 used for hard failures (cycle exception, DD blow-up):
# zero-trades is "no signal", failures are "actively bad". Both are far below
# any plausible sharpe so the optimizer rejects them. The split exists so logs
# distinguish a flat-fitness regime from a strategy that's blowing up.
ZERO_TRADE_PENALTY = -1e6

# Objectives measured in rupees of P&L. For these a zero-trade session is a
# LEGITIMATE ₹0 outcome (and on edgeless tape, better than a losing config),
# so it must NOT get ZERO_TRADE_PENALTY — that would push the optimizer to
# overtrade rather than let it choose to trade less. For ratio objectives
# (gamma_theta_ratio, sharpe_ratio) a no-trade session is undefined, so the
# penalty still applies. See _run_experiment.
# convexity_edge is rupee-denominated too: a no-trade session contributes
# all-zero components, which is its true outcome.
PNL_METRICS = frozenset({"net_pnl", "realized_pnl", "convexity_edge"})

# The hard-veto sentinel (bleed cap, squandered edge, DD blow-up, cycle
# failure). Any fitness at or below this is "disqualified", not a score —
# issue #159: comparing against it ("keep iff > baseline") accepts anything
# non-vetoed, so acceptance must fall back to an absolute floor instead.
# ZERO_TRADE_PENALTY (-1e6) sits below this on purpose: a baseline whose
# cycles averaged to the zero-trade penalty is equally not a baseline.
VETO_FITNESS = -999999.0


# ── Phase-3 validation/promotion helpers (2026-07-18 redesign) ──
# Module-level so run_autoresearch's validation stage and tests share one
# definition. The candidate JSON gets the verdict these produce; promotion
# itself stays an OPERATOR decision (standing rule) — these inform it.

def load_daily_moves(underlying: str) -> Dict[str, float]:
    """Per-session close-to-close spot move (%) keyed by ISO date, from the
    newest {underlying}_*_eod table — the same source (and recency-by-filename
    convention) the strategy's spot-history seed uses. The NIFTY_daily.csv/
    parquet series is NOT used: it went stale at 2026-06-25 while the EOD
    options snapshots stay fresh. Returns {} on any failure — callers degrade
    to recency-only hold-out selection and say so."""
    import pandas as pd
    from core.data_cache_io import find_tables, read_table
    try:
        candidates = find_tables(Path("data_cache"), f"{underlying}_*_eod")
        if not candidates:
            return {}
        df = read_table(candidates[-1], usecols=["timestamp", "underlying_price"])
        df["date"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d")
        spot = (df.drop_duplicates(subset="date", keep="last")
                  .sort_values("date").set_index("date")["underlying_price"])
        moves = spot.pct_change() * 100.0
        return {d: float(v) for d, v in moves.items() if v == v}  # drop NaN
    except Exception as e:
        logger.warning("load_daily_moves(%s) failed: %s — hold-out selection "
                       "degrades to recency-only", underlying, e)
        return {}


def pick_holdout_sessions(captured: list, window: set,
                          moves: Dict[str, float]) -> list:
    """Hold-out set for candidate validation: the most recent session outside
    the fitness window (the pre-Phase-3 behaviour) PLUS the largest-|move|
    outside-window session (the tail hold-out). A convexity strategy's
    payoff lives in the tail sessions; validating only on the most recent —
    usually quiet — session could never falsify a candidate (Phase-0 F2)."""
    outside = [d for d in captured if d not in window]
    if not outside:
        return []
    picks = [outside[-1]]
    scored = [d for d in outside if d in moves]
    if scored:
        tail = max(scored, key=lambda d: abs(moves[d]))
        if tail not in picks:
            picks.append(tail)
    return picks


def build_validation_verdict(session_results: list, capital: float,
                             moves: Dict[str, float],
                             insample_pnls: Optional[list] = None,
                             tail_threshold_pct: float = 1.0,
                             bleed_cap_pct: float = 1.5,
                             rng_seed: int = 7,
                             shuffle_alpha: float = 0.10) -> Dict:
    """Machine-readable promotion verdict from per-hold-out-session results
    (each: {'date','net_pnl','total_trades','max_drawdown'}).

    Checks (each failure appends a warning; promote_ok = all pass):
      holdout_trades_nonzero — the standing no-promote rule: a candidate that
          cannot trade the hold-out is 'improving' by trading less.
      tail_day_nonnegative — on hold-out sessions with |move| ≥
          tail_threshold_pct the candidate must not lose money: tails are the
          product this strategy sells. None (not a failure) when the hold-out
          contains no tail session — but that absence is itself warned, since
          the validation then couldn't test the thesis.
      bleed_bounded — no hold-out session loses more than bleed_cap_pct of
          capital (mirrors the convexity_edge veto and the live daily-loss
          guard).
      bootstrap_p_negative — share of 10k bootstrap resamples (deterministic
          rng_seed) of the combined in-sample + hold-out per-session P&Ls
          whose mean is ≤ 0. Reported, not gated: with ~15 points it is a
          coarse stability signal, not significance (Rule 12: labeled so).
      edge_beats_shuffle_null — sign-flip permutation test on the combined
          per-session P&Ls. Null: no edge (each session's P&L equally likely
          + or −); p = share of 10k sign-flipped resamples whose mean ≥ the
          observed mean (+1 correction). Gates at shuffle_alpha. This is the
          issue-#159 ABSOLUTE bar in the narrow sense that it never
          references the seed, so it stays meaningful when the seed is
          vetoed and 'beats seed' is degenerate. It is NOT an out-of-sample
          claim (2026-07-19 review): the series is dominated by the
          IN-SAMPLE replay sessions the config was optimized on (hold-out
          contributes only 1-2 picks — too few to test alone), so a pass is
          a necessary floor of evidence, not proof of forward edge; the
          split is disclosed in `shuffle_sessions`. Two more labeled
          properties: alpha defaults to 0.10, not 0.05 — with ~15-17
          sessions the test is coarse (p reported so the margin is
          visible) — and a positive mean concentrated in k large sessions
          bottoms out near p≈2^-k, so ~4+ independent tail wins are needed
          to clear alpha 0.10; a fail with positive mean is warned as
          insufficient evidence, distinct from a bleeding book. Location
          test, unlike the order-shuffle path test it was adapted from
          (HKUDS/Vibe-Trading backtest/validation.py): mean−½σ fitness is
          order-invariant, only location can gate promotion here.
      walkforward_any_window_positive — chronological windows (3 if ≥9
          in-sample sessions, else 2) over the IN-SAMPLE per-session P&Ls
          (the replay window; hold-out picks are non-contiguous). Gated only
          on the degenerate case: NO window net-positive = the config
          demonstrated edge nowhere (the −1.7k..−3.6k candidate shape from
          the 2026-07-19 re-score). 'Most windows profitable' would be the
          WRONG gate for a convexity book — a tail-harvester legitimately
          bleeds small in quiet windows and earns everything in the window
          holding the tail — so consistency_rate is reported ungated.
    """
    pnls = [float(r["net_pnl"]) for r in session_results]
    trades = sum(int(r.get("total_trades", 0)) for r in session_results)
    bleed_floor = -capital * bleed_cap_pct / 100.0
    warnings = []

    trades_ok = trades > 0
    if not trades_ok:
        warnings.append(
            f"0 trades across the {len(session_results)}-session hold-out — "
            "candidate 'improves' by not trading (standing no-promote rule)")

    tail_sessions = [r for r in session_results
                     if abs(moves.get(r["date"], 0.0)) >= tail_threshold_pct]
    if tail_sessions:
        tail_ok = all(float(r["net_pnl"]) >= 0 for r in tail_sessions)
        if not tail_ok:
            worst = min(tail_sessions, key=lambda r: float(r["net_pnl"]))
            warnings.append(
                f"lost ₹{-float(worst['net_pnl']):,.0f} on tail session "
                f"{worst['date']} ({moves.get(worst['date'], 0.0):+.2f}%) — "
                "tails are the product; a config that loses them is not edge")
    else:
        tail_ok = None
        warnings.append(
            f"hold-out contains no session with |move| ≥ "
            f"{tail_threshold_pct}% — the convexity thesis was NOT tested")

    bleed_ok = all(p > bleed_floor for p in pnls) if pnls else True
    if not bleed_ok:
        warnings.append(
            f"a hold-out session lost more than {bleed_cap_pct}% of capital "
            f"(floor ₹{bleed_floor:,.0f}) — unmanaged bleed")

    p_neg = None
    shuffle_p = None
    shuffle_ok = None
    combined = list(insample_pnls or []) + pnls
    # One guard + one array for both resampling tests: a threshold or
    # construction edited in one and not the other would let the two tests
    # silently disagree about when they run on identical data. Each test
    # keeps its own default_rng(rng_seed) stream.
    if len(combined) >= 5:
        arr = np.asarray(combined, dtype=float)

        rng = np.random.default_rng(rng_seed)
        means = rng.choice(arr, size=(10_000, len(arr)), replace=True).mean(axis=1)
        p_neg = float((means <= 0).mean())

        observed = float(arr.mean())
        rng = np.random.default_rng(rng_seed)
        flips = rng.choice(np.array([-1.0, 1.0]), size=(10_000, len(arr)))
        null_means = (flips * arr).mean(axis=1)
        shuffle_p = float((int((null_means >= observed).sum()) + 1) / (10_000 + 1))
        shuffle_ok = shuffle_p <= shuffle_alpha
        if not shuffle_ok and observed > 0:
            # Distinguish "underpowered" from "bleeding" (2026-07-19 review):
            # a positive mean concentrated in k large sessions bottoms out
            # near p≈2^-k regardless of how positive the mean is — the gate
            # cannot pass ~fewer than 4 independent tail wins at alpha 0.10.
            # That is insufficient EVIDENCE, not a negative book; say which.
            warnings.append(
                f"mean session P&L is positive (₹{observed:,.0f}) but fails "
                f"the shuffle null (p={shuffle_p:.3f} > alpha "
                f"{shuffle_alpha:g}) — edge is concentrated in too few "
                "sessions to rule out luck; needs more tail evidence, "
                "not a bleeding config")
        elif not shuffle_ok:
            warnings.append(
                f"mean session P&L does not beat the shuffle null "
                f"(p={shuffle_p:.3f} > alpha {shuffle_alpha:g}) — no absolute "
                "evidence of edge, only relative-to-seed")
    else:
        warnings.append(
            f"only {len(combined)} session P&Ls — shuffle-null edge test "
            "could NOT run (absolute edge untested)")

    walkforward = None
    wf_ok = None
    ins = [float(p) for p in (insample_pnls or [])]
    if len(ins) >= 4:
        k = 3 if len(ins) >= 9 else 2
        size = len(ins) // k
        sums = []
        for i in range(k):
            hi = (i + 1) * size if i < k - 1 else len(ins)
            sums.append(float(sum(ins[i * size:hi])))
        wf_ok = any(s > 0 for s in sums)
        walkforward = {
            "n_windows": k,
            "window_pnls": [round(s, 2) for s in sums],
            "consistency_rate": round(sum(1 for s in sums if s > 0) / k, 3),
        }
        if not wf_ok:
            warnings.append(
                f"no walk-forward window is net-positive ({k} windows over "
                f"{len(ins)} in-sample sessions) — the config demonstrated "
                "edge nowhere on the window")
    else:
        warnings.append(
            f"in-sample series too short ({len(ins)} sessions) for "
            "walk-forward windows — consistency untested")

    promote_ok = bool(trades_ok and bleed_ok and tail_ok is not False
                      and shuffle_ok is not False and wf_ok is not False)
    return {
        "sessions": session_results,
        "checks": {
            "holdout_trades_nonzero": trades_ok,
            "tail_day_nonnegative": tail_ok,
            "bleed_bounded": bleed_ok,
            "bootstrap_p_negative": p_neg,
            "shuffle_null_p": shuffle_p,
            "edge_beats_shuffle_null": shuffle_ok,
            "walkforward_any_window_positive": wf_ok,
        },
        "shuffle_alpha": shuffle_alpha,
        # Composition of the shuffle/bootstrap series (Rule 12): the tests
        # are in-sample-dominated, and the reader must be able to see by
        # how much without re-deriving it.
        "shuffle_sessions": {"insample": len(insample_pnls or []),
                             "holdout": len(pnls)},
        "walkforward": walkforward,
        "promote_ok": promote_ok,
        "warnings": warnings,
    }


class HedgeResearchLoop:
    """
    Autonomous parameter optimization loop for the Taleb Dynamic Hedger.

    Pattern (from Karpathy's autoresearch):
    ┌──────────────────────────────────────────────┐
    │  1. Snapshot current params as "baseline"     │
    │  2. Mutate ONE parameter (random walk)        │
    │  3. Run hedger for N evaluation cycles        │
    │  4. Measure: Sharpe, PnL, MaxDD, etc.         │
    │  5. If better AND within risk limits → KEEP    │
    │     Else → DISCARD, revert to baseline        │
    │  6. Log experiment to results.tsv              │
    │  7. GOTO 1 (LOOP FOREVER)                     │
    └──────────────────────────────────────────────┘
    """

    # Parameters the loop is allowed to tune and their valid ranges
    TUNABLE_RANGES = {
        "rehedge_delta_threshold": (0.5, 1.5),
        "gamma_scalp_band_pct": (0.5, 3.0),
        "position_size_pct": (5.0, 25.0),
        "vega_limit": (1000.0, 8000.0),  # per-lot; scales with position size
        "max_holding_period_hours": (4.0, 168.0),  # 4 hours to 1 week
        "max_entry_alpha": (5000.0, 150000.0),
        "mc_worst_path_loss_pct": (1.0, 10.0),
        "cost_hurdle_factor": (1.0, 8.0),  # raised: cube-root scaling in
        # Phase 1.2 means hurdle=8 demands only 2× scalp/cost, not 8×
        "rv_window_days": (2.0, 15.0),
        # Dropped 2026-06-07: min_rv_iv_ratio and skew_pct_max are LEGACY-path
        # hard gates, bypassed when enable_regime_dispatch=True (the live config)
        # — see taleb_karpathy.py `not regime_enabled` guards. Sweeping them
        # under regime dispatch wasted experiments on no-op params. They remain
        # config-settable for the non-regime path; their regime-path equivalents
        # are the regime_* thresholds below.
        # Dropped 2026-08-09 for the same reason, once the same demotion was
        # applied to it: entry_iv_percentile_min / entry_iv_percentile_max.
        # Until then the band was the ONE surviving hard gate under dispatch,
        # and it shadowed the classifier — 100% of blocked ticks on the
        # 15-session window hit the upper bound and 36.7% sat at IV pct >= 70,
        # i.e. above regime_calendar_iv_pct_min, so CALENDAR_SHORT_FRONT could
        # never be reached. Sweeping the band now moves nothing under dispatch;
        # the regime_* IV cutoffs below are its live equivalents.
        # Phase 5: T-0 (expiry day) band tightening factor. 1.0 = disabled,
        # 0.33 = aggressive sticky-strike harvest. Tighter values produce
        # more rehedges on expiry day; the cost gate still filters sub-EV.
        "t0_band_factor": (0.33, 1.0),
        # Phase 3.1 regime-classifier routing cutoffs. These are what the
        # classifier actually decides on under regime dispatch; previously
        # hardcoded (call site passed no Thresholds), so structure routing was
        # untunable. Ranges span each cutoff's plausible NIFTY band.
        "regime_straddle_iv_pct_max": (40.0, 80.0),
        "regime_straddle_rv_iv_ratio_min": (0.6, 1.5),
        "regime_straddle_skew_pct_max": (50.0, 90.0),
        "regime_calendar_iv_pct_min": (50.0, 90.0),
        "regime_calendar_skew_pct_max": (40.0, 80.0),
        "regime_risk_reversal_skew_pct_min": (60.0, 95.0),
        "regime_backspread_vvol_min": (0.05, 0.40),
        "regime_asymmetric_strangle_rv_iv_min": (1.0, 2.5),
        "regime_asymmetric_strangle_skew_pct_min": (50.0, 90.0),
    }

    def __init__(self, hedger, config_path: str = "config.ini"):
        """
        Args:
            hedger: BaseStrategy instance (the "train.py" equivalent)
            config_path: Path to configuration file
        """
        self.hedger = hedger
        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        self.eval_cycles = self.config.getint("autoresearch", "eval_cycles_per_experiment")
        self.primary_metric = self.config.get("autoresearch", "metric")
        self.max_dd_threshold = self.config.getfloat("autoresearch", "max_drawdown_threshold")
        self.results_file = self.config.get("autoresearch", "results_file")
        self.log_file = self.config.get("autoresearch", "log_file")
        self.mutation_step = self.config.getfloat("autoresearch", "mutation_step_size")
        # Issue #159: acceptance bar when the baseline is vetoed (see
        # _evaluate_experiment). 0.0 = require strictly positive fitness.
        self.vetoed_baseline_abs_floor = self.config.getfloat(
            "autoresearch", "vetoed_baseline_abs_floor", fallback=0.0)
        # Negative floors are unsupported (2026-07-19 review): monotonic
        # acceptance keeps every accepted config above the FIRST accepted
        # value, so any floor is technically preserved — but a negative
        # floor means "accept losing configs", which contradicts the bar's
        # documented purpose. Fail loud at construction, before hours of
        # sweep compute, rather than run under murky semantics.
        if self.vetoed_baseline_abs_floor < 0.0:
            raise ValueError(
                f"[autoresearch] vetoed_baseline_abs_floor = "
                f"{self.vetoed_baseline_abs_floor} is negative — the "
                "absolute bar exists to require positive fitness under a "
                "vetoed seed; use 0.0 (default) or a positive floor.")

        self.experiment_number = 0
        self.baseline_params = copy.deepcopy(hedger.tunable_params)
        self.baseline_metric = None
        self.best_params = copy.deepcopy(hedger.tunable_params)
        self.best_metric_value = float("-inf")
        # One {"accepted", "metric_value"} record per experiment — the
        # input to sweep_quality(), shared by every driver of this loop.
        self._experiment_records = []
        # session_date -> parsed tape frame; filled in _run_experiment.
        self._tape_cache = {}
        # Replay window, pinned on first _run_experiment (None = not yet).
        self._replay_sessions = None

        self._init_results_file()
        self._setup_logging()

    def run(self):
        """
        Main loop. Runs forever until interrupted.

        Equivalent to Karpathy's "LOOP FOREVER" instruction in program.md.
        The human must manually stop this (Ctrl+C or kill process).
        """
        logger.info("=" * 60)
        logger.info("AUTORESEARCH LOOP STARTED")
        logger.info("Primary metric: %s", self.primary_metric)
        logger.info("Eval cycles per experiment: %d", self.eval_cycles)
        logger.info("Max drawdown threshold: %.1f%%", self.max_dd_threshold)
        logger.info("=" * 60)

        # ── Step 0: Establish baseline ──
        logger.info("[Experiment 0] Running baseline with current parameters...")
        self.baseline_metric = self._run_experiment(self.baseline_params)
        self.best_metric_value = self.baseline_metric
        self._log_experiment(
            experiment_id=0,
            mutated_param="BASELINE",
            old_value=0,
            new_value=0,
            metric_value=self.baseline_metric,
            accepted=True,
            params=self.baseline_params,
        )
        logger.info("[Experiment 0] Baseline %s: %.4f", self.primary_metric, self.baseline_metric)
        # baseline_metric drifts upward as mutations are accepted; keep the
        # seed's score for the sweep-quality verdict at save time.
        seed_baseline = self.baseline_metric

        # ── Main loop ──
        try:
            while True:
                self.experiment_number += 1
                logger.info("─" * 60)
                logger.info("[Experiment %d] Starting...", self.experiment_number)

                # 1. Propose mutation
                mutated_params, param_name, old_val, new_val = self._propose_mutation()
                logger.info(
                    "  Mutating '%s': %.4f → %.4f",
                    param_name, old_val, new_val
                )

                # 2. Run experiment with mutated params
                metric_value = self._run_experiment(mutated_params)
                logger.info(
                    "  Result: %s = %.4f (baseline: %.4f)",
                    self.primary_metric, metric_value, self.baseline_metric
                )

                # 3. Decide: keep or discard
                accepted = self._evaluate_experiment(metric_value)
                self._experiment_records.append(
                    {"accepted": accepted, "metric_value": metric_value},
                )

                if accepted:
                    logger.info("  ✅ ACCEPTED — new params are better!")
                    self.baseline_params = copy.deepcopy(mutated_params)
                    self.baseline_metric = metric_value
                    self.hedger.tunable_params = copy.deepcopy(mutated_params)

                    if metric_value > self.best_metric_value:
                        self.best_metric_value = metric_value
                        self.best_params = copy.deepcopy(mutated_params)
                        logger.info("  🏆 NEW BEST: %s = %.4f", self.primary_metric, metric_value)
                else:
                    logger.info("  ❌ DISCARDED — reverting to baseline.")
                    self.hedger.tunable_params = copy.deepcopy(self.baseline_params)

                # 4. Log
                self._log_experiment(
                    experiment_id=self.experiment_number,
                    mutated_param=param_name,
                    old_value=old_val,
                    new_value=new_val,
                    metric_value=metric_value,
                    accepted=accepted,
                    params=mutated_params,
                )

                # 5. Brief pause between experiments
                time.sleep(2)

        except KeyboardInterrupt:
            logger.info("\n" + "=" * 60)
            logger.info("AUTORESEARCH LOOP STOPPED BY USER")
            logger.info("Total experiments: %d", self.experiment_number)
            logger.info("Best %s: %.4f", self.primary_metric, self.best_metric_value)
            logger.info("Best params: %s", json.dumps(self.best_params, indent=2))
            logger.info("Results saved to: %s", self.results_file)
            logger.info("=" * 60)
            # Stamp the quality verdict here too — before this, the
            # LOOP-FOREVER path wrote the canonical best_params.json
            # permanently verdict-less (and the preservation exclusion in
            # _save_best_params strips any stale one), so 'no sweep_quality'
            # was ambiguous between pre-feature files and this path.
            self._save_best_params(
                sweep_quality=self.sweep_quality(seed_baseline),
            )

    def run_single_experiment(self) -> dict:
        """
        Run a single experiment cycle. Useful for testing or
        integration with external schedulers.

        Returns dict with experiment results.
        """
        self.experiment_number += 1
        mutated_params, param_name, old_val, new_val = self._propose_mutation()
        metric_value = self._run_experiment(mutated_params)
        accepted = self._evaluate_experiment(metric_value)
        self._experiment_records.append(
            {"accepted": accepted, "metric_value": metric_value},
        )

        if accepted:
            self.baseline_params = copy.deepcopy(mutated_params)
            self.baseline_metric = metric_value
            self.hedger.tunable_params = copy.deepcopy(mutated_params)
            if metric_value > self.best_metric_value:
                self.best_metric_value = metric_value
                self.best_params = copy.deepcopy(mutated_params)

        self._log_experiment(
            self.experiment_number, param_name, old_val, new_val,
            metric_value, accepted, mutated_params
        )

        return {
            "experiment_id": self.experiment_number,
            "param_mutated": param_name,
            "old_value": old_val,
            "new_value": new_val,
            "metric": self.primary_metric,
            "metric_value": metric_value,
            "accepted": accepted,
            "best_so_far": self.best_metric_value,
        }

    # ══════════════════════════════════════════════════════════════
    # MUTATION ENGINE
    # ══════════════════════════════════════════════════════════════

    # Phase 2.5: pairs of params that are mechanically correlated and
    # benefit from joint perturbation. The hill-climber's univariate
    # walk can't reach combinations where both knobs need to move in
    # lockstep — e.g. widening the rehedge band only helps if the
    # cost-hurdle stays consistent, otherwise the cost gate now blocks
    # what the band would have allowed. With probability
    # `joint_mutation_prob` the proposer mutates one pair instead of
    # one param.
    # Joint-mutation pairs (Phase 2.5). EVERY key here MUST also be in
    # TUNABLE_RANGES — _mutate_one indexes TUNABLE_RANGES[param], so a pair
    # naming a non-tunable raises KeyError when joint mutation selects it.
    # (min_rv_iv_ratio was dropped from TUNABLE_RANGES on 2026-06-07 but
    # left here, crashing the 2026-06-13 weekly run; _propose_mutation now
    # also filters defensively.) test_autoresearch_loop pins the invariant.
    JOINT_PAIRS = [
        ("rehedge_delta_threshold", "gamma_scalp_band_pct"),
        ("cost_hurdle_factor", "gamma_scalp_band_pct"),
        # (entry_iv_percentile_min, entry_iv_percentile_max) removed
        # 2026-08-09 alongside their TUNABLE_RANGES entries — leaving a pair
        # behind after dropping its range is precisely the 2026-06-13 crash.
    ]

    def _mutate_one(self, params: Dict, param_name: str) -> Tuple[float, float]:
        """Single-param Gaussian step + range clamp + rounding.
        Returns (old_value, new_value). Mutates `params` in place so
        callers can chain multiple mutations within one experiment."""
        low, high = self.TUNABLE_RANGES[param_name]
        old_value = params[param_name]
        param_range = high - low
        step = np.random.normal(0, self.mutation_step * param_range)
        new_value = old_value + step
        new_value = max(low, min(high, new_value))

        # Per-param rounding rules. Keep these consistent with the
        # tunable schema in strategies/taleb_karpathy.py.
        if param_name in ("entry_iv_percentile_min", "entry_iv_percentile_max",
                          "skew_pct_max"):
            new_value = round(new_value, 0)
        elif param_name == "vega_limit":
            new_value = round(new_value, 0)
        else:
            new_value = round(new_value, 4)

        # Cross-param invariants enforced at write-time.
        if param_name == "entry_iv_percentile_min":
            new_value = min(new_value, params.get("entry_iv_percentile_max", 95) - 5)
        elif param_name == "entry_iv_percentile_max":
            new_value = max(new_value, params.get("entry_iv_percentile_min", 10) + 5)

        params[param_name] = new_value
        return old_value, new_value

    # A mutation that lands back on the value it started from costs a full
    # replay (~5 min of a 25-experiment weekly budget) to re-measure a
    # config we have already scored, and inflates the plateau_share that
    # sweep_quality uses to judge whether the sweep was informative. Two
    # mechanisms produce one: a param already sitting on a TUNABLE_RANGES
    # bound (`entry_iv_percentile_min` = 5.0 = its low) clamps any outward
    # step straight back, and the integer rounding applied to the
    # percentile/vega params collapses any |step| < 0.5 to zero. The
    # 2026-08-08 sweep burned experiment 20 on
    # `entry_iv_percentile_min: 5.0000 -> 5.0000` exactly this way.
    # Re-draw instead — a fresh draw also re-picks the parameter, so a knob
    # pinned against a bound yields to one that can still move.
    _MUTATION_ATTEMPTS = 8

    def _propose_mutation(self) -> Tuple[Dict, str, float, float]:
        """Propose a mutation that actually changes the parameter set.

        Delegates to `_propose_mutation_once` and re-draws while the
        proposal is a no-op (see `_MUTATION_ATTEMPTS`). If every attempt
        is a no-op the last one is returned anyway and the caller still
        gets a well-formed experiment — but we log it, because a landscape
        where nothing can move is a finding, not a detail (Rule 12).
        """
        for attempt in range(self._MUTATION_ATTEMPTS):
            params, name, old_value, new_value = self._propose_mutation_once()
            if params != self.baseline_params:
                return params, name, old_value, new_value
        logger.warning(
            "Mutation proposer produced a no-op %d times in a row (last: %s "
            "%.4f -> %.4f) — every sampled parameter is pinned at a range "
            "bound or below its rounding granularity. Experiment %s will "
            "re-measure the current config.",
            self._MUTATION_ATTEMPTS, name, old_value, new_value,
            getattr(self, "experiment_number", "?"),
        )
        return params, name, old_value, new_value

    def _propose_mutation_once(self) -> Tuple[Dict, str, float, float]:
        """
        Propose a parameter mutation. Either single-param (default) or
        joint pair (Phase 2.5) with probability `joint_mutation_prob`.

        For single-param: Gaussian random walk with step size
        proportional to the parameter's range.

        For joint-pair: Both params in the pair receive independent
        Gaussian steps in the same experiment, exposing the hill-
        climber to ridges in the fitness landscape that univariate
        moves can't traverse.

        May return a no-op (new == old) when the sampled parameter is
        pinned at a bound; `_propose_mutation` is the caller that filters
        those out.
        """
        params = copy.deepcopy(self.baseline_params)
        joint_prob = self.config.getfloat(
            "autoresearch", "joint_mutation_prob", fallback=0.0,
        )
        # A joint pair is usable only if BOTH keys are in the params
        # snapshot AND still in TUNABLE_RANGES (a key dropped from the
        # ranges but left in JOINT_PAIRS / best_params.json would KeyError
        # in _mutate_one — the 2026-06-13 crash). Filter once, then decide.
        available = [
            p for p in self.JOINT_PAIRS
            if all(k in params and k in self.TUNABLE_RANGES for k in p)
        ]
        do_joint = (joint_prob > 0
                    and random.random() < joint_prob
                    and bool(available))

        if do_joint:
            pair = random.choice(available)
            primary, secondary = pair
            old_v1, new_v1 = self._mutate_one(params, primary)
            old_v2, new_v2 = self._mutate_one(params, secondary)
            # We log the primary in the canonical fields; the secondary
            # is appended to the descriptor so the TSV row reflects the
            # joint move.
            return (params, f"{primary}+{secondary}",
                    old_v1, new_v1)

        # Default: single-param walk.
        param_name = random.choice(list(self.TUNABLE_RANGES.keys()))
        if param_name not in params:
            # New tunable not yet in baseline — seed from midpoint of
            # its range so subsequent mutations have somewhere to walk
            # from. Without this, a tunable added after the loop started
            # never gets explored.
            low, high = self.TUNABLE_RANGES[param_name]
            params[param_name] = (low + high) / 2
        old_value, new_value = self._mutate_one(params, param_name)
        return params, param_name, old_value, new_value

    # ══════════════════════════════════════════════════════════════
    # EXPERIMENT RUNNER
    # ══════════════════════════════════════════════════════════════

    def _run_experiment(self, params: Dict) -> float:
        """
        Run the hedging strategy with given parameters over historical replay.

        Phase 2.3: prefers captured tape sessions when available
        (data_cache/ticks/ticks-*.jsonl). Each cycle replays one
        session; over `eval_cycles` cycles we sample the most recent
        sessions. Falls back to synthetic-GBM data when no captures
        exist (cold-start or testing).

        Synthetic data lacks every property Taleb Ch 15 says matters
        for option-strategy P&L (fat tails, vol regimes, skew, biased-
        asset asymmetry). Tuning on synthetic was the silent ceiling
        on the previous autoresearch loop — see the 2026-05-23 PDF-
        review entry in tasks/todo.md.

        Returns the primary metric value.
        """
        from research.backtest import (
            generate_synthetic_data, run_backtest,
            list_captured_sessions, load_captured_tape,
            load_iv_skew_seed, load_daily_iv_seed,
        )

        underlying = getattr(self.hedger, "underlying", "NIFTY")
        # Pin the replay window ONCE per loop instance: the most recent N
        # sessions matching eval_cycles. Replaying the SAME N sessions
        # across all experiments keeps the metric comparable — a different
        # per-cycle seed (as the old synthetic path used) made fitness
        # landscapes noisy enough that the one-at-a-time hill-climber
        # couldn't separate signal from luck. Re-listing per experiment
        # also let the window SHIFT mid-sweep (a Persistent=true catch-up
        # run on a trading day would pull in — and permanently cache — the
        # half-written live capture file).
        if self._replay_sessions is None:
            captured = list_captured_sessions(underlying)
            # Pre-flight the window (2026-07-12): walk BACKWARD from the
            # most recent session, parsing each tape into the sweep cache,
            # until eval_cycles VALID sessions are collected. A tape that
            # parses to an EMPTY frame (stillborn capture — ticks-2026-06-26
            # was 8 KB of epoch-zero snapshots and nothing else) is an
            # infrastructure defect shared by every experiment: inside the
            # cycle loop it raised, hit the per-cycle except, and flattened
            # the ENTIRE sweep to -999999 (25/25 experiments, 2026-07-12).
            # Same philosophy as the load-failure comment at the cache site
            # below: infrastructure must not masquerade as fitness. Excluding
            # the session here and back-filling with the next-older one keeps
            # every experiment on eval_cycles real market days; parse cost is
            # unchanged (each session was parsed once per sweep anyway — this
            # just fronts it). Hard LOAD failures still propagate and kill
            # the run with the real error.
            valid_newest_first = []
            for session in reversed(captured):
                if len(valid_newest_first) >= self.eval_cycles:
                    break
                if session not in self._tape_cache:
                    self._tape_cache[session] = load_captured_tape(
                        session, underlying,
                    )
                if self._tape_cache[session].empty:
                    del self._tape_cache[session]
                    logger.warning(
                        "session %s: tape parses to 0 rows (stillborn "
                        "capture) — excluded from the replay window, "
                        "back-filling with the next-older session", session,
                    )
                    continue
                valid_newest_first.append(session)
            self._replay_sessions = list(reversed(valid_newest_first))
            if self._replay_sessions:
                logger.info(
                    "Replaying %d captured sessions: %s",
                    len(self._replay_sessions), self._replay_sessions,
                )
                if len(self._replay_sessions) < self.eval_cycles:
                    logger.warning(
                        "Only %d captured sessions for eval_cycles=%d — "
                        "each session replays once per experiment "
                        "(no wrap-around double-counting).",
                        len(self._replay_sessions), self.eval_cycles,
                    )
            else:
                logger.info(
                    "No captured tape in data_cache/ticks/ — falling back "
                    "to synthetic GBM (Ch 15 properties absent; tuning on "
                    "this is structurally limited)."
                )
        replay_sessions = self._replay_sessions
        use_tape = len(replay_sessions) >= 1
        # Each session replays exactly once per experiment. The slice above
        # already caps the window at eval_cycles; when FEWER sessions exist,
        # repeating some (the old `cycle % len` wrap) would bias the mean
        # toward the duplicated days and shrink the variance penalty's std —
        # score what exists instead.
        n_cycles = len(replay_sessions) if use_tape else self.eval_cycles

        # Seed each backtest's IV/skew rolling history from the persisted
        # live history so the IV-percentile and skew gates leave warmup.
        # A tape replay fires a single entry scan per session; without a
        # seed _compute_iv_percentile sees <30 obs and returns the neutral
        # 50.0, pinning the IV-percentile / regime features and making those
        # tunables inert (the flat-fitness bug). Built once — constant
        # across experiments, so a shared seed cannot bias param ranking.
        if not hasattr(self, "_iv_seed"):
            drop = self.config.getint(
                "autoresearch", "iv_seed_drop_recent", fallback=0,
            )
            self._iv_seed, self._skew_seed = load_iv_skew_seed(
                underlying, drop_recent=drop,
            )
            logger.info(
                "Backtest IV seed: %d ATM-IV + %d skew obs (drop_recent=%d)",
                len(self._iv_seed), len(self._skew_seed), drop,
            )

        # The daily pool is what _compute_iv_percentile actually ranks
        # against. A tape replay hands run_backtest ONE session, which cannot
        # rank itself, so it must be supplied here — shared across every
        # experiment, exactly like the seeds above, so it cannot bias the
        # relative ranking the sweep is measuring. Its OWN hasattr guard:
        # tying it to _iv_seed's would skip it on any path that pre-set that
        # attribute, leaving _daily_iv_seed undefined at the call site.
        if not hasattr(self, "_daily_iv_seed"):
            self._daily_iv_seed = load_daily_iv_seed(underlying)
            logger.info("Backtest daily ATM-IV pool: %d session(s)",
                        len(self._daily_iv_seed))

        # Apply params to hedger so the backtest picks them up. Restore in
        # `finally`: the old restore sat after the cycle loop, so the
        # early -999999 return (and any propagating tape error) leaked the
        # rejected mutation into the hedger for the rest of the process.
        original_params = copy.deepcopy(self.hedger.tunable_params)
        self.hedger.tunable_params = copy.deepcopy(params)
        try:
            cycle_metrics = []

            for cycle in range(n_cycles):
                logger.debug("  Cycle %d/%d", cycle + 1, n_cycles)

                if use_tape:
                    session_date = replay_sessions[cycle]
                    # Parse each session ONCE per sweep, not once per
                    # experiment: the raw JSONL runs to several GB per
                    # session (~3 min to parse) while the resampled frame
                    # is ~10 MB. Without this cache a 25-experiment ×
                    # 15-session sweep spends >10 h re-reading identical
                    # files and blows the unit's TimeoutStartSec.
                    #
                    # The load sits OUTSIDE the try below on purpose: a
                    # session that fails to LOAD (corrupt archive, missing
                    # zstd binary) is an infrastructure failure shared by
                    # every experiment, not a property of the mutated
                    # params — scoring it -999999 would silently flatten
                    # the entire sweep. Let it propagate and kill the run
                    # with the real error instead.
                    if session_date not in self._tape_cache:
                        self._tape_cache[session_date] = load_captured_tape(
                            session_date, underlying,
                        )
                    # copy() hands each cycle its own frame — defense in
                    # depth alongside run_backtest's own input copy.
                    data = self._tape_cache[session_date].copy()
                    logger.debug("    session %s rows=%d", session_date, len(data))

                try:
                    if not use_tape:
                        data = generate_synthetic_data(
                            underlying=underlying, days=10, ticks_per_day=12,
                        )
                    results = run_backtest(
                        data, underlying=underlying,
                        config_path=getattr(self, "_config_path", "config.ini"),
                        tunable_params=params,
                        seed_iv_history=self._iv_seed,
                        seed_skew_history=self._skew_seed,
                        seed_daily_iv=getattr(self, "_daily_iv_seed", None),
                    )
                    metrics = results["metrics"]
                    if metrics.get("total_trades", 0) == 0:
                        # P&L objective: no trades = ₹0, a real outcome — don't
                        # penalize (penalizing pushes overtrading). Ratio
                        # objective: no trades is undefined → penalty.
                        if self.primary_metric in PNL_METRICS:
                            logger.debug("  Cycle %d: 0 trades — net P&L 0.0", cycle + 1)
                            metrics = {**metrics, self.primary_metric: 0.0}
                        else:
                            logger.debug("  Cycle %d: 0 trades — penalty %.0f",
                                         cycle + 1, ZERO_TRADE_PENALTY)
                            metrics = {**metrics, self.primary_metric: ZERO_TRADE_PENALTY}
                    cycle_metrics.append(metrics)

                except Exception as e:
                    logger.warning("  Cycle %d failed: %s", cycle + 1, e)
                    return VETO_FITNESS

            if not cycle_metrics:
                return VETO_FITNESS

            # Phase-3: expose this evaluation's per-session P&Ls so the driver
            # can keep the accepted/best config's series for the validation
            # bootstrap (build_validation_verdict). Attribute, not return —
            # every caller of _run_experiment expects a bare scalar.
            self._last_cycle_pnls = [
                float(m.get("net_pnl", 0.0)) for m in cycle_metrics]

            if self.primary_metric == "convexity_edge":
                # Component fitness (2026-07-18 redesign): computed whole in
                # its own method — the mean−½σ risk adjustment is embedded
                # there, so the generic variance penalty below must not be
                # applied twice. The shared max-DD veto still applies after.
                avg_metric = self._convexity_edge_fitness(cycle_metrics)
            else:
                primary_values = [m.get(self.primary_metric, 0) for m in cycle_metrics]
                avg_metric = float(np.mean(primary_values))

                # Phase 2.4: variance penalty. A single-cycle win shouldn't be
                # rewarded as much as a consistent winner — particularly when
                # primary_metric is gamma_theta_ratio (high single-day numerator
                # variance is common). penalty_factor is the coefficient on the
                # std-dev term; 0.5 means "subtract half a stddev from the mean".
                # The autoresearch [section] can tune this if needed; default
                # is moderate enough that a wide-but-fat-positive distribution
                # still wins over an unstable spike.
                if len(primary_values) >= 2:
                    penalty = float(np.std(primary_values))
                    penalty_factor = self.config.getfloat(
                        "autoresearch", "variance_penalty", fallback=0.5,
                    )
                    avg_metric -= penalty_factor * penalty

            # Also check drawdown constraint (convert absolute drawdown to % of capital)
            total_capital = self.hedger.immutable_params.get("total_capital", 500000)
            max_dd = max(m.get("max_drawdown", 0) for m in cycle_metrics)
            max_dd_pct = (max_dd / total_capital) * 100 if total_capital > 0 else 0
            if max_dd_pct > self.max_dd_threshold:
                logger.info("  Max drawdown %.2f%% (₹%.0f) exceeds threshold %.2f%%. Penalizing.",
                            max_dd_pct, max_dd, self.max_dd_threshold)
                avg_metric = VETO_FITNESS  # Reject any param set that blows drawdown

            return avg_metric
        finally:
            self.hedger.tunable_params = original_params

    def _convexity_edge_fitness(self, cycle_metrics: list) -> float:
        """Component fitness for the 2026-07-18 redesign (tasks/todo.md).

        Sums three rupee-denominated per-session components, then applies
        hard vetoes. Built on the Phase-1 metrics (PR #151), so it measures
        the strategy's thesis directly instead of sampling net P&L noise:

          spread_i = theoretical_scalp_pnl − max(theta_decay_paid, 0)
              The Taleb Ch.16 identity in rupees: what the session's realized
              variance was worth against the time rent PAID for it. This is
              the EDGE a config exposes itself to, measurable every session —
              unlike tail P&L, which needs a tail to happen. Rent is clamped
              at 0 so a short-premium session (theta_decay_paid < 0, rent
              earned) gets no carry credit — only long-convexity edge counts.
          middle_i = middle_band_worst_pnl (≤ 0)
              Worst P&L inside ±1.5% of spot. Penalizes the short-the-middle
              shape that lost ₹11.7k on 07-10's +1.02% move regardless of how
              well a crash day happened to pay (Phase-0 F2).
          pnl_i    = net_pnl — what execution actually kept, risk-adjusted
              with the same mean−½σ used by the legacy objective.

        Hard vetoes (→ -999999, logged):
          bleed:  any session losing more than convexity_bleed_cap_pct of
                  capital (default 1.5%, matching the live max_daily_loss_pct
                  guard) — unmanaged bleed is disqualifying, not a tiebreak.
          squandered edge: a session whose spread exceeded
                  convexity_spread_tail_pct of capital while net_pnl was
                  negative — the config had its tail and failed to keep it.

        Weights/caps read from [autoresearch] with code defaults; all terms
        are rupees, so weights are dimensionless and comparable.
        """
        capital = float(self.hedger.immutable_params.get("total_capital", 500000))
        cfg = self.config
        bleed_cap = capital * cfg.getfloat(
            "autoresearch", "convexity_bleed_cap_pct", fallback=1.5) / 100.0
        tail_min = capital * cfg.getfloat(
            "autoresearch", "convexity_spread_tail_pct", fallback=0.5) / 100.0
        w_spread = cfg.getfloat(
            "autoresearch", "convexity_w_spread", fallback=0.5)
        w_middle = cfg.getfloat(
            "autoresearch", "convexity_w_middle", fallback=1.0)

        pnl = np.array([m.get("net_pnl", 0.0) for m in cycle_metrics], dtype=float)
        # spread = realized-variance value − rent PAID. Guard (2026-07-18
        # review): rent is max(theta_decay_paid, 0), NOT the raw value.
        # theta_decay_paid is negative for a short-premium book (rent EARNED,
        # per the accrual comment in taleb_karpathy), so subtracting the raw
        # value would ADD collected theta into "spread" and reward short-vol
        # carry — exactly the short-the-middle structure this objective exists
        # to reject. Clamping rent at 0 credits only genuine long-convexity
        # edge: a short-gamma book still shows negative theoretical_scalp
        # (it loses to realized vol) and now earns no carry bonus.
        spread = np.array([
            m.get("theoretical_scalp_pnl", 0.0)
            - max(m.get("theta_decay_paid", 0.0), 0.0)
            for m in cycle_metrics], dtype=float)
        middle = np.array([
            m.get("middle_band_worst_pnl", 0.0) for m in cycle_metrics], dtype=float)

        worst = float(pnl.min()) if len(pnl) else 0.0
        if worst < -bleed_cap:
            logger.info("  convexity_edge VETO: session P&L %.0f breaches "
                        "bleed cap -%.0f", worst, bleed_cap)
            return VETO_FITNESS
        squandered = (spread >= tail_min) & (pnl < 0)
        if bool(squandered.any()):
            i = int(np.argmax(squandered))
            logger.info("  convexity_edge VETO: session had spread %.0f "
                        "(≥ %.0f) but net P&L %.0f — edge squandered",
                        spread[i], tail_min, pnl[i])
            return VETO_FITNESS

        fitness = float(np.mean(pnl)) if len(pnl) else 0.0
        if len(pnl) >= 2:
            fitness -= 0.5 * float(np.std(pnl))
        fitness += w_spread * float(np.mean(spread)) if len(spread) else 0.0
        fitness -= w_middle * float(np.mean(np.maximum(0.0, -middle))) if len(middle) else 0.0
        return fitness

    def _evaluate_experiment(self, metric_value: float) -> bool:
        """
        Decide whether to keep or discard the mutation.

        Simple rule (following Karpathy): keep if strictly better than baseline.
        No probabilistic acceptance (unlike simulated annealing) — we want
        monotonic improvement with guaranteed safety.

        Issue #159: a VETOED baseline (≤ VETO_FITNESS) is not a score to beat
        — "keep iff > −999999" accepts the first non-vetoed mutation however
        bad, and every later acceptance anchors to it. A vetoed baseline is
        treated as NO baseline: a mutation must clear the absolute floor
        (`vetoed_baseline_abs_floor`, default 0.0 = positive fitness) to be
        kept. The likely weekly outcome — 0 accepted, candidate = the vetoed
        seed — is the honest one; sweep_quality flags it loudly.
        """
        if self.baseline_metric is None:
            return True
        if self.baseline_metric <= VETO_FITNESS:
            return metric_value > self.vetoed_baseline_abs_floor
        return metric_value > self.baseline_metric

    # ══════════════════════════════════════════════════════════════
    # LOGGING AND PERSISTENCE
    # ══════════════════════════════════════════════════════════════

    def _init_results_file(self):
        """Create results.tsv with header if it doesn't exist."""
        if not Path(self.results_file).exists():
            with open(self.results_file, "w") as f:
                f.write(
                    "experiment_id\ttimestamp\tparam_mutated\told_value\tnew_value\t"
                    f"{self.primary_metric}\taccepted\t"
                    "rehedge_delta_threshold\tgamma_scalp_band_pct\t"
                    "position_size_pct\tvega_limit\t"
                    "max_holding_period_hours\tentry_iv_percentile_min\t"
                    "entry_iv_percentile_max\n"
                )

    def _log_experiment(
        self, experiment_id: int, mutated_param: str,
        old_value: float, new_value: float, metric_value: float,
        accepted: bool, params: Dict
    ):
        """Append experiment result to results.tsv."""
        with open(self.results_file, "a") as f:
            f.write(
                f"{experiment_id}\t{datetime.now().isoformat()}\t"
                f"{mutated_param}\t{old_value:.4f}\t{new_value:.4f}\t"
                f"{metric_value:.6f}\t{accepted}\t"
                f"{params.get('rehedge_delta_threshold', 0):.4f}\t"
                f"{params.get('gamma_scalp_band_pct', 0):.4f}\t"
                f"{params.get('position_size_pct', 0):.4f}\t"
                f"{params.get('vega_limit', 0):.0f}\t"
                f"{params.get('max_holding_period_hours', 0):.1f}\t"
                f"{params.get('entry_iv_percentile_min', 0):.0f}\t"
                f"{params.get('entry_iv_percentile_max', 0):.0f}\n"
            )

    def sweep_quality(self, seed_baseline: float) -> Dict:
        """Score the sweep itself (Rule 12): the 2026-06-20 run accepted
        0/40 mutations and 06-27 scored 29/40 experiments at one identical
        fitness, yet both wrote candidate files indistinguishable from a
        real optimization result. Every writer of a params file stamps
        this verdict so the manual promotion step can reject an
        uninformative sweep from the candidate file alone.

        Computed from the records run()/run_single_experiment accumulate,
        so both entrypoints (the weekly runners/run_autoresearch.py sweep and
        runners/run.py's LOOP-FOREVER mode) share one definition.
        """
        from collections import Counter
        records = self._experiment_records
        fitness_counts = Counter(round(r["metric_value"], 6) for r in records)
        plateau_share = (
            max(fitness_counts.values()) / len(records) if records else 0.0
        )
        n_accepted = sum(1 for r in records if r["accepted"])
        # bool() guards the JSON path: `A and B` returns B's type, and a
        # numpy scalar leaking into seed_baseline would make this np.bool_,
        # which _save_best_params's json.dump (no default=) cannot serialize.
        seed_vetoed = bool(seed_baseline is not None
                           and seed_baseline <= VETO_FITNESS)
        warnings = []
        if seed_vetoed:
            # Issue #159: a vetoed status quo is a HEADLINE finding — and it
            # makes every "beats seed" comparison in this run meaningless, so
            # the verdict must carry that context into the candidate file.
            warnings.append(
                "SEED VETOED — the current config is disqualified on this "
                "window ('beats seed' is meaningless; acceptance required "
                f"fitness > {self.vetoed_baseline_abs_floor:g} instead)")
        if n_accepted == 0:
            warnings.append("0 mutations accepted — candidate is the seed params")
        elif not seed_vetoed and self.best_metric_value <= seed_baseline:
            # Unreachable while acceptance is strictly-better-than-baseline;
            # kept as a tripwire should the acceptance rule ever admit ties.
            warnings.append("best never beat the seed baseline")
        if plateau_share > 0.5:
            plateau_value = fitness_counts.most_common(1)[0][0]
            warnings.append(
                f"{plateau_share:.0%} of experiments scored an identical "
                f"fitness ({plateau_value:.6f}) — landscape flat on this "
                f"replay window"
            )
        return {
            "experiments": len(records),
            "accepted": n_accepted,
            "distinct_fitness": len(fitness_counts),
            "plateau_share": round(plateau_share, 3),
            "seed_baseline": seed_baseline,
            "seed_vetoed": seed_vetoed,
            "best": self.best_metric_value,
            "informative": not warnings,
            "warnings": warnings,
        }

    def _save_best_params(self, out_file: str = "best_params.json",
                          sweep_quality: Optional[Dict] = None):
        """Save best parameters to a JSON file for easy loading.

        `sweep_quality` (optional) is the run's self-assessment from
        runners/run_autoresearch.py — accepted count, plateau share, informative
        verdict. Stamped into the output so the manual promotion step can
        reject an uninformative sweep from the candidate file alone.

        Preserves out-of-schema fields (e.g. `_migrations` semantic-shift
        history) from the canonical best_params.json so they survive each
        autoresearch run instead of being clobbered.

        Writes via a temp file + atomic rename (audit 2026-06-10 task 2.3):
        a `kill -9` mid-write can never leave `out_file` half-written. The
        weekly regen passes a dated candidate path here so it never touches
        the canonical file at all.
        """
        # Preservation always reads the canonical file — that's where the
        # _migrations history lives, regardless of where we're writing.
        preserved = {}
        try:
            with open("best_params.json") as f:
                existing = json.load(f)
            for k, v in existing.items():
                # sweep_quality is per-run — a stale one preserved from a
                # promoted candidate would mislabel THIS run's output.
                if k not in ("best_params", "best_metric",
                             "total_experiments", "timestamp",
                             "sweep_quality"):
                    preserved[k] = v
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        output = {
            "best_params": self.best_params,
            "best_metric": {self.primary_metric: self.best_metric_value},
            "total_experiments": self.experiment_number,
            "timestamp": datetime.now().isoformat(),
            **preserved,
        }
        if sweep_quality is not None:
            output["sweep_quality"] = sweep_quality
        # Atomic: write a sibling temp then rename. Path.replace is an
        # atomic os.replace on the same filesystem.
        out_path = Path(out_file)
        tmp_path = out_path.with_name(out_path.name + ".tmp")
        with open(tmp_path, "w") as f:
            json.dump(output, f, indent=2)
        tmp_path.replace(out_path)
        logger.info("Best parameters saved to %s", out_file)

    def _setup_logging(self):
        """Configure file logging for the autoresearch loop."""
        log_dir = Path(self.config.get("logging", "log_dir", fallback="./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)

        fh = logging.FileHandler(log_dir / self.log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        ))
        logging.getLogger().addHandler(fh)

    # ══════════════════════════════════════════════════════════════
    # ANALYSIS UTILITIES
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def load_results(results_file: str = "results.tsv") -> "pd.DataFrame":
        """Load results.tsv into a DataFrame for analysis."""
        import pandas as pd
        return pd.read_csv(results_file, sep="\t")

    @staticmethod
    def plot_progress(results_file: str = "results.tsv"):
        """
        Generate a progress plot showing metric improvement over experiments.
        Similar to the progress.png in Karpathy's autoresearch.
        """
        import pandas as pd
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            logger.warning("matplotlib not installed. Skipping plot.")
            return

        df = pd.read_csv(results_file, sep="\t")
        metric_col = [c for c in df.columns if c not in (
            "experiment_id", "timestamp", "param_mutated", "old_value",
            "new_value", "accepted"
        ) and "percentile" not in c and "pct" not in c
                       and "threshold" not in c and "limit" not in c
                       and "hours" not in c][0]

        fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

        # Top: metric over experiments
        ax1 = axes[0]
        accepted = df[df["accepted"]]
        rejected = df[~df["accepted"]]

        ax1.scatter(rejected["experiment_id"], rejected[metric_col],
                    c="red", alpha=0.4, s=20, label="Discarded")
        ax1.scatter(accepted["experiment_id"], accepted[metric_col],
                    c="green", alpha=0.8, s=40, label="Accepted")

        # Best so far line
        best_so_far = df[metric_col].expanding().max()
        ax1.plot(df["experiment_id"], best_so_far, "b-", linewidth=2, label="Best so far")

        ax1.set_ylabel(metric_col)
        ax1.set_title("Autoresearch: Strategy Optimization Progress")
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # Bottom: which params were mutated
        ax2 = axes[1]
        param_colors = {p: plt.cm.tab10(i) for i, p in enumerate(
            df["param_mutated"].unique()
        )}
        for _, row in df.iterrows():
            color = param_colors.get(row["param_mutated"], "gray")
            marker = "^" if row["accepted"] else "v"
            ax2.scatter(row["experiment_id"], row["param_mutated"],
                        c=[color], marker=marker, s=30)

        ax2.set_xlabel("Experiment #")
        ax2.set_ylabel("Parameter Mutated")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig("progress.png", dpi=150)
        logger.info("Progress plot saved to progress.png")
        plt.close()


# ── CLI entry point ──
if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    if len(sys.argv) > 1 and sys.argv[1] == "plot":
        HedgeResearchLoop.plot_progress()
    elif len(sys.argv) > 1 and sys.argv[1] == "analyze":
        df = HedgeResearchLoop.load_results()
        print("\n📊 Autoresearch Results Summary")
        print(f"   Total experiments: {len(df)}")
        print(f"   Accepted: {df['accepted'].sum()}")
        print(f"   Acceptance rate: {df['accepted'].mean()*100:.1f}%")
        metric_cols = [c for c in df.columns if c not in (
            "experiment_id", "timestamp", "param_mutated",
            "old_value", "new_value", "accepted"
        )]
        if metric_cols:
            best_row = df.loc[df[metric_cols[0]].idxmax()]
            print(f"   Best {metric_cols[0]}: {best_row[metric_cols[0]]:.4f} "
                  f"(experiment #{int(best_row['experiment_id'])})")
    else:
        print("Usage:")
        print("  python -m runners.autoresearch_loop plot     — Generate progress.png")
        print("  python -m runners.autoresearch_loop analyze  — Print results summary")
        print("\nTo run the loop, use the strategy integration (see SKILL.md)")
