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

import os
import copy
import json
import time
import random
import logging
import configparser
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


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
        "entry_iv_percentile_min": (10.0, 50.0),
        "entry_iv_percentile_max": (50.0, 95.0),
        "max_entry_alpha": (5000.0, 150000.0),
        "mc_worst_path_loss_pct": (1.0, 10.0),
        "cost_hurdle_factor": (1.0, 3.0),
        "min_rv_iv_ratio": (0.6, 1.5),
        "rv_window_days": (2.0, 15.0),
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

        self.experiment_number = 0
        self.baseline_params = copy.deepcopy(hedger.tunable_params)
        self.baseline_metric = None
        self.best_params = copy.deepcopy(hedger.tunable_params)
        self.best_metric_value = float("-inf")

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
            self._save_best_params()

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

    def _propose_mutation(self) -> Tuple[Dict, str, float, float]:
        """
        Mutate ONE random parameter within its valid range.

        Strategy: Gaussian random walk with step size proportional
        to the parameter's range. This encourages local exploration
        (small improvements) while occasionally making larger jumps.
        """
        params = copy.deepcopy(self.baseline_params)
        param_name = random.choice(list(self.TUNABLE_RANGES.keys()))
        low, high = self.TUNABLE_RANGES[param_name]
        old_value = params[param_name]

        # Gaussian step: mean=0, std=mutation_step * range
        param_range = high - low
        step = np.random.normal(0, self.mutation_step * param_range)
        new_value = old_value + step

        # Clamp to valid range
        new_value = max(low, min(high, new_value))

        # Round appropriately
        if param_name in ("entry_iv_percentile_min", "entry_iv_percentile_max"):
            new_value = round(new_value, 0)
        elif param_name == "vega_limit":
            new_value = round(new_value, 0)
        else:
            new_value = round(new_value, 4)

        # Ensure IV min < IV max
        if param_name == "entry_iv_percentile_min":
            new_value = min(new_value, params.get("entry_iv_percentile_max", 95) - 5)
        elif param_name == "entry_iv_percentile_max":
            new_value = max(new_value, params.get("entry_iv_percentile_min", 10) + 5)

        params[param_name] = new_value
        return params, param_name, old_value, new_value

    # ══════════════════════════════════════════════════════════════
    # EXPERIMENT RUNNER
    # ══════════════════════════════════════════════════════════════

    def _run_experiment(self, params: Dict) -> float:
        """
        Run the hedging strategy with given parameters over historical replay.

        Uses the backtest harness to replay synthetic or historical data through
        the hedger with the candidate parameters. This replaces the old live/paper
        sleep-based evaluation, producing deterministic and meaningful metrics.

        Returns the primary metric value.
        """
        from backtest import generate_synthetic_data, run_backtest

        # Apply params to hedger so the backtest picks them up
        original_params = copy.deepcopy(self.hedger.tunable_params)
        self.hedger.tunable_params = copy.deepcopy(params)

        cycle_metrics = []

        for cycle in range(self.eval_cycles):
            logger.debug("  Cycle %d/%d", cycle + 1, self.eval_cycles)

            try:
                # Generate a fresh synthetic dataset per cycle (different random seed)
                underlying = getattr(self.hedger, "underlying", "NIFTY")
                data = generate_synthetic_data(
                    underlying=underlying, days=10, ticks_per_day=12,
                )
                results = run_backtest(
                    data, underlying=underlying,
                    config_path=getattr(self, "_config_path", "config.ini"),
                    tunable_params=params,
                )
                metrics = results["metrics"]
                cycle_metrics.append(metrics)

            except Exception as e:
                logger.warning("  Cycle %d failed: %s", cycle + 1, e)
                return -999999.0

        # Restore original params
        self.hedger.tunable_params = original_params

        if not cycle_metrics:
            return -999999.0

        primary_values = [m.get(self.primary_metric, 0) for m in cycle_metrics]
        avg_metric = np.mean(primary_values)

        # Also check drawdown constraint (convert absolute drawdown to % of capital)
        total_capital = self.hedger.immutable_params.get("total_capital", 500000)
        max_dd = max(m.get("max_drawdown", 0) for m in cycle_metrics)
        max_dd_pct = (max_dd / total_capital) * 100 if total_capital > 0 else 0
        if max_dd_pct > self.max_dd_threshold:
            logger.info("  Max drawdown %.2f%% (₹%.0f) exceeds threshold %.2f%%. Penalizing.",
                        max_dd_pct, max_dd, self.max_dd_threshold)
            avg_metric = -999999.0  # Reject any param set that blows drawdown

        return avg_metric

    def _evaluate_experiment(self, metric_value: float) -> bool:
        """
        Decide whether to keep or discard the mutation.

        Simple rule (following Karpathy): keep if strictly better than baseline.
        No probabilistic acceptance (unlike simulated annealing) — we want
        monotonic improvement with guaranteed safety.
        """
        if self.baseline_metric is None:
            return True
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

    def _save_best_params(self):
        """Save best parameters to a JSON file for easy loading."""
        output = {
            "best_params": self.best_params,
            "best_metric": {self.primary_metric: self.best_metric_value},
            "total_experiments": self.experiment_number,
            "timestamp": datetime.now().isoformat(),
        }
        best_file = "best_params.json"
        with open(best_file, "w") as f:
            json.dump(output, f, indent=2)
        logger.info("Best parameters saved to %s", best_file)

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
        accepted = df[df["accepted"] == True]
        rejected = df[df["accepted"] == False]

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
        print("  python autoresearch_loop.py plot     — Generate progress.png")
        print("  python autoresearch_loop.py analyze  — Print results summary")
        print("\nTo run the loop, use the strategy integration (see SKILL.md)")
