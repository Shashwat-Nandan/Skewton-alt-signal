# autoresearch — Karpathy-style weekly parameter sweep

One-line: a weekly mutation loop that perturbs `taleb_karpathy`
parameters one at a time, backtests each candidate over windowed
historical data with held-out validation, and writes a new
`best_params.json` if the winner beats the baseline. Runs Saturday
10:00 IST under a systemd timer.

## Contents
- [Overview](#overview)
- [Theoretical pattern](#theoretical-pattern)
- [Schedule and systemd unit](#schedule-and-systemd-unit)
- [Parameter space](#parameter-space)
- [Mutation strategy](#mutation-strategy)
- [Evaluation: backtest harness + windowed splits](#evaluation-backtest-harness--windowed-splits)
- [Objective metrics](#objective-metrics)
- [Hold-out validation](#hold-out-validation)
- [Safety rails](#safety-rails)
- [Output files](#output-files)
- [Integration with live strategy](#integration-with-live-strategy)
- [Logging and monitoring](#logging-and-monitoring)
- [Known issues](#known-issues)
- [Files involved](#files-involved)

---

## Overview

`runners/autoresearch_loop.py` (651 lines) implements the loop. `runners/run_autoresearch.py`
(344 lines) is the runner with windowed/holdout splits and CLI flags.
`deploy/taleb-autoresearch.timer` fires it weekly.

What gets tuned: `taleb_karpathy` tunable parameters (rehedge band,
position size, vega cap, IV-percentile entry window, alpha cap, MC
worst-path budget, cost hurdle, RV/IV gate, skew-percentile gate,
T-0 band tightening). NOT immutable safety rails (max loss, no naked
shorts, total capital, etc.).

What doesn't get tuned (yet): `pair_trading` or `varsity_equity_swing`.
Sweep scripts exist for them (`research/sweep_pair_params.py`,
`research/sweep_arbitrage_thresholds.py`) but aren't on the weekly cron — they
are ops tools the operator runs manually.

## Theoretical pattern

The Karpathy "autoresearch" pattern is documented in
[`autoresearch_pattern.md`](./autoresearch_pattern.md). This doc
COMPLEMENTS that with implementation specifics — when the loop fires,
what it actually mutates, and how its outputs reach the live strategy.

The high-level pattern from `runners/autoresearch_loop.py:48–63`:

```
1. Snapshot current params as "baseline"
2. Mutate ONE parameter (random walk)
3. Run hedger for N evaluation cycles
4. Measure: Sharpe, PnL, MaxDD, etc.
5. If better AND within risk limits → KEEP
   Else → DISCARD, revert to baseline
6. Log experiment to results.tsv
7. GOTO 1 (LOOP FOREVER)
```

The cron version exits after `--experiments N` rather than looping
forever, but the inner loop logic is the same.

## Schedule and systemd unit

`deploy/taleb-autoresearch.timer`:
```ini
OnCalendar=Sat *-*-* 10:00:00 Asia/Kolkata
Persistent=true
RandomizedDelaySec=300
```

Saturday 10:00 IST — NSE closed (no Kite contention), Friday's bars
are settled, weekend leaves room for the operator to review.

Why not nightly: each sweep takes ~30–90 minutes (sampling-dependent)
and the parameter-space convergence wants more data than a single
trading day delivers. Weekly cadence matches the strategy's parameter
half-life better.

`deploy/taleb-autoresearch.service` runs:
```
…/python -m runners.run_autoresearch --experiments 100 --metric net_pnl \
  --data data_cache/NIFTY_<from>_<to>_eod.csv \
  --hold-out-days 5
```

(Exact flags vary — check the deployed service file with
`systemctl cat taleb-autoresearch.service`.)

## Parameter space

`HedgeResearchLoop.TUNABLE_RANGES` (line 66):

| Param | Range | Units | Notes |
|---|---|---|---|
| `rehedge_delta_threshold` | (0.5, 1.5) | lots | Base rehedge band |
| `gamma_scalp_band_pct` | (0.5, 3.0) | % of spot | Scalp-pnl estimate denominator |
| `position_size_pct` | (5.0, 25.0) | % capital | Per-entry deployment |
| `vega_limit` | (1000, 8000) | per-lot | Scales with position size |
| `max_holding_period_hours` | (4, 168) | hours | 4h to 1 week |
| `entry_iv_percentile_min` | (10, 50) | 0–100 | IV window lower band |
| `entry_iv_percentile_max` | (50, 95) | 0–100 | IV window upper band |
| `max_entry_alpha` | (5000, 150000) | INR | Gamma-cost cap |
| `mc_worst_path_loss_pct` | (1.0, 10.0) | % capital | MC budget |
| `cost_hurdle_factor` | (1.0, 8.0) | linear-equiv | Cube-root applied internally; hurdle=8 → ~2× effective |
| `min_rv_iv_ratio` | (0.6, 1.5) | ratio | Long-straddle thesis gate |
| `rv_window_days` | (2.0, 15.0) | days | RV rolling window |
| `skew_pct_max` | (70.0, 100.0) | 0–100 | Put-skew percentile veto threshold (Phase 1.3) |
| `t0_band_factor` | (0.33, 1.0) | factor | T-0 band tightening (Phase 5; 1.0 = disabled) |

NOT in TUNABLE_RANGES (intentionally — Phase 3/4 toggles, manually
operator-managed):
- `enable_regime_dispatch` (boolean) — flip-on requires more careful
  observation than mutation-loop discovery
- `max_layered_structures` (integer) — same rationale

## Mutation strategy

`_mutate_one_param` (around line 250+ of `runners/autoresearch_loop.py`):

1. Pick a random parameter from `TUNABLE_RANGES.keys()`
2. Read current value
3. Compute step: `mutation_step × (high − low)` of the range
4. Apply Gaussian or uniform random walk: `new_value = old +
   random_signed_step`
5. Clip to range
6. Return mutated `params` dict

One parameter per experiment — multivariate jumps are too noisy to
attribute outcomes to. This is the classic "perturb-and-evaluate"
pattern; convergence is slow but stable.

## Evaluation: backtest harness + windowed splits

Each candidate is evaluated by `_run_experiment` which:

1. Loads historical data (CSV from
   `data_cache/NIFTY_<from>_<to>_eod.csv` — produced by
   `market_data/fetch_historical_data.py` or `market_data/fetch_bhavcopy.py`)
2. Splits into N non-overlapping windows of `window_days=5` each
   (`run_autoresearch._split_data_into_windows`, line 38)
3. Runs `backtest.run_backtest(strategy, window)` for each window
4. Aggregates per-window metrics into a single scalar (the primary
   metric — see [Objective metrics](#objective-metrics))
5. Returns the scalar; if zero trades in any window → `ZERO_TRADE_PENALTY`
   (`-1e6`); if hard failure → `-999999` (line 41–45)

Window step = `window_days` (non-overlapping) prevents data leakage
between windows used for the same experiment.

`MockKite` (imported from `research/backtest.py`) replays the historical CSV as
synthetic Kite quotes so the strategy's `_get_spot_price` and
`_get_options_chain` work transparently in the loop.

## Objective metrics

Configurable via `[autoresearch] metric` in `config.ini` or
`--metric` CLI flag. Choices computed by
`strategy.get_strategy_metrics()` (see
`strategies/taleb_karpathy.py:797`):

| Metric | Definition | When to use |
|---|---|---|
| `net_pnl` | `state.total_pnl` | Default; raw INR result |
| `sharpe_ratio` | `mean(daily_pnls) / std(daily_pnls) × √252` | Risk-adjusted |
| `sortino_ratio` | mean(daily) / std(daily<0) × √252 | Penalises downside only |
| `calmar_ratio` | `total_pnl / max_drawdown` | DD-aware |
| `gamma_theta_ratio` | `gamma_scalp_pnl / theta_decay_paid` (Phase 2.4) | Taleb-framework efficiency; only meaningful once `theta_paid > 1.0` |
| `realized_pnl` | accrued realized only | Avoids unrealised inflation |
| `max_drawdown` | peak-to-trough | (negate for maximisation) |

The `gamma_theta_ratio` and `net_pnl` paths were added in commit
`0dac251` (memory: this was the metric-path expansion). Per
`strategies/taleb_karpathy.py:830`:

> Phase 2.4: `gamma_theta_ratio` is the Taleb-framework efficiency
> metric. Numerator is realized gamma scalp P&L (Phase 1.1 fix);
> denominator is realized theta decay (Phase 1.1 fix). Ratio > 1
> means scalps exceeded the time-decay rent — the strategy's
> core thesis.

When `theta_paid ≤ 1.0`, returns 0 to avoid divide-by-zero infinity
in the variance penalty.

## Hold-out validation

`runners/run_autoresearch.py:_split_data_into_windows` reserves the last
`--hold-out-days` trading days exclusively for validation:

```python
holdout_dates = dates[-holdout_days:]   # validation
train_dates   = dates[:-holdout_days]    # all windowed sweeps
```

After the loop converges on `best_params`, it runs ONE final backtest
of `best_params` on the held-out set. If the held-out metric is
significantly worse than the in-sample best (configurable threshold),
the candidate is rejected → `best_params.json` NOT updated.

This is the "no-look-ahead" guarantee: the chosen params have never
seen the validation data during the sweep.

## Safety rails

1. **Immutable params NEVER mutated** — `TUNABLE_RANGES` is the
   allow-list; `max_daily_loss_pct`, `no_naked_shorts`, etc. are
   off-limits (line 17 of `runners/autoresearch_loop.py`).
2. **`mc_worst_path_loss_pct` capped** at 10% per range — even the
   most aggressive candidate can't propose >10% daily loss budget.
3. **Drawdown rejection** — `max_drawdown_threshold` (default in
   `config.ini` `[autoresearch]`); candidates that exceed this
   are auto-rejected even if their primary metric is high.
4. **Pre-autoresearch snapshot** — the active `best_params.json` is
   copied to `best_params.preautoresearch.<YYYY-MM-DD>.json` BEFORE
   the sweep runs. Recent snapshots in repo root: `.preautoresearch.2026-05-23.json`,
   `.preautoresearch_netpnl.json`, `.preautoresearch_phase3seed.json`.
5. **`candidate_params_<YYYY-MM-DD>.json`** — every sweep emits a
   timestamped candidate file (not just `best_params.json` overwrite)
   so a regrettable sweep can be rolled back by `mv` the snapshot.
6. **Manual operator review** — Saturday 10:00 timing leaves the
   weekend for the operator to inspect `results.tsv` and
   `candidate_params_*.json` before Monday's live strategy picks up
   the new params at boot.

## Output files

All in repo root:

| File | When written | Purpose |
|---|---|---|
| `best_params.json` | After successful sweep + holdout pass | Live params (read by `strategies/taleb_karpathy.py:282` at strategy boot) |
| `best_params.preautoresearch.<YYYY-MM-DD>.json` | Before each sweep | Rollback snapshot |
| `candidate_params_<YYYY-MM-DD>.json` | After each sweep regardless of accept | Audit trail of every winner candidate |
| `results.tsv` | Per experiment (during the sweep) | Append-only log of all attempts |

`results.tsv` columns (from `_init_results_file`):
```
experiment_id, mutated_param, old_value, new_value,
metric_value, accepted, <all_tunable_params_inline>
```

## Integration with live strategy

`strategies/taleb_karpathy.py:282`:

```python
if self.config.getboolean("strategy", "use_best_params", fallback=True):
    bp_path = Path(self.config.get(
        "strategy", "best_params_path", fallback="best_params.json",
    ))
    applied, ignored = _apply_best_params(self.tunable_params, bp_path)
```

So:
- Every `taleb-hedger.timer` fire (09:10 IST Mon–Fri) reads
  `best_params.json` at strategy `__init__`
- If autoresearch wrote a new `best_params.json` over the weekend, the
  Monday morning fire picks it up automatically
- No live-reload during a session — each session uses one params set
  for the whole day
- Disable with `[strategy] use_best_params = false` in config.ini

Unknown keys in `best_params.json` (e.g. params renamed between
sweep and current strategy code) are SKIPPED — never silently injected
into `tunable_params`. `_apply_best_params` returns
`(applied_count, ignored_keys)` and the runner logs both.

## Logging and monitoring

Logs in `logs/autoresearch-YYYY-MM-DD.log`. Key lines:

| Symptom | Grep |
|---|---|
| Sweep started | `grep "AUTORESEARCH LOOP STARTED" autoresearch-*.log` |
| Baseline metric | `grep "\[Experiment 0\] Baseline" autoresearch-*.log` |
| Mutation accepted | `grep "ACCEPTED" autoresearch-*.log` |
| Mutation rejected | `grep "REJECTED" autoresearch-*.log` |
| Zero trades (penalty) | `grep "zero trades" autoresearch-*.log` |
| Hard failure | `grep "EXCEPTION\|FAILED" autoresearch-*.log` |
| Hold-out pass / fail | `grep "Hold-out" autoresearch-*.log` |
| Final write | `grep "best_params.json.*written" autoresearch-*.log` |

`results.tsv` is the post-mortem source of truth — load into pandas
to plot metric trajectory, accept-rate per param, etc.

Failure alert: nonzero exit → `notify-failure@taleb-autoresearch.service`
→ Telegram.

## Known issues

1. **Backtest contract drift for varsity_equity_swing (EQ-FU-2)** —
   `research/backtest_varsity_equity.py` doesn't apply the gap-skip / max-age
   filters that live `_fill_pending_entries` enforces. Autoresearch
   for equity-swing (when enabled) would optimise against a higher
   trade count than live delivers. See
   [`tasks/live-readiness-deferred.md`](../../tasks/live-readiness-deferred.md).

2. **No autoresearch for pair_trading or varsity_equity_swing** — only
   `taleb_karpathy` is on the cron. Manual sweeps via
   `research/sweep_pair_params.py` / `research/sweep_arbitrage_thresholds.py`.

3. **Manual data-window curation** — the historical CSV must be
   refreshed periodically (`market_data/fetch_historical_data.py` or
   `market_data/fetch_bhavcopy.py`). The cron service has the CSV path hardcoded;
   stale data → params optimised against an outdated regime.

4. **Synthetic data fallback** — `runners/run_autoresearch.py` defaults to
   `backtest.generate_synthetic_data` if no `--data` flag is given.
   Useful for smoke tests; not appropriate for production sweeps.

5. **Per-experiment time** — ~30–90 minutes per sweep depending on
   `--experiments` and `eval_cycles_per_experiment`. The systemd
   service has `TimeoutStartSec=0` (no timeout) but operators should
   verify the run completes well before Monday market open.

6. **`enable_regime_dispatch` not tunable** — Phase 3.1 dispatch
   intentionally excluded from auto-mutation. Operator must flip
   manually after observing regime-on behaviour in paper mode.

## Files involved

| File | Role |
|---|---|
| `runners/autoresearch_loop.py` | `HedgeResearchLoop` class, mutation + accept/reject |
| `runners/run_autoresearch.py` | Runner: data load, windowed split, holdout, CLI |
| `research/backtest.py` | `run_backtest`, `generate_synthetic_data`, `MockKite` |
| `strategies/taleb_karpathy.py` | `_apply_best_params` consumer (line 117); `get_strategy_metrics` |
| `research/sweep_entry_params.py`, `research/sweep_pair_params.py`, `research/sweep_rehedge_params.py`, `research/sweep_arbitrage_thresholds.py`, `research/sweep_rv_iv_gate.py`, `research/sweep_top.py` | Per-dimension sweep tools (manual / dev) |
| `config.ini` | `[autoresearch]` section: `eval_cycles_per_experiment`, `metric`, `max_drawdown_threshold`, `results_file`, `log_file`, `mutation_step_size` |
| `best_params.json` | Active live params (read by hedger at boot) |
| `best_params.preautoresearch.<DATE>.json` | Pre-sweep rollback snapshots |
| `candidate_params_<DATE>.json` | Per-sweep winner audit |
| `results.tsv` | Append-only experiment log |
| `data_cache/NIFTY_<from>_<to>_eod.csv` | Historical input for sweeps |
| `logs/autoresearch-YYYY-MM-DD.log` | Per-day sweep log |
| `deploy/taleb-autoresearch.service` / `.timer` | systemd cron (Sat 10:00 IST) |
| `deploy/notify-failure@.service` | Failure alert |
| `docs/research/autoresearch_pattern.md` | Theoretical pattern (read first) |
