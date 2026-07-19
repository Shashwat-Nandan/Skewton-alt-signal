# Autoresearch Pattern for Trading — Reference Guide

## How Karpathy's Autoresearch Maps to Dynamic Hedging

Karpathy's autoresearch (March 2026) introduced a pattern where an AI agent
autonomously runs experiments on a codebase, evaluating each change against
a single metric and keeping only improvements. We adapt this pattern from
ML training to options strategy optimization.

---

## Table of Contents

1. [The Core Pattern](#1-the-core-pattern)
2. [Mapping: ML → Trading](#2-mapping-ml--trading)
3. [The Experiment Loop](#3-the-experiment-loop)
4. [Mutation Strategy](#4-mutation-strategy)
5. [Evaluation Metrics](#5-evaluation-metrics)
6. [Safety Rails](#6-safety-rails)
7. [Results Analysis](#7-results-analysis)
8. [Operational Guide](#8-operational-guide)

---

## 1. The Core Pattern

Karpathy's insight: the research loop (hypothesize → experiment → evaluate →
keep/discard → repeat) can be automated. The human's job shifts from
executing experiments to designing the evaluation criteria and constraints.

Three files, three roles:
- **prepare.py** (read-only): Infrastructure that doesn't change
- **train.py** (agent-editable): The thing being optimized
- **program.md** (human-editable): Instructions for the agent

In our trading system:
- **prepare.py** → `core/kite_auth.py` + `core/greeks_engine.py` (fixed infrastructure)
- **train.py** → `tunable_params` dict in `dynamic_hedger.py` (optimized by loop)
- **program.md** → `config.ini` strategy section (human sets constraints)

## 2. Mapping: ML → Trading

| Autoresearch (ML) | Our System (Trading) |
|---|---|
| train.py (model + optimizer) | tunable_params (rehedge threshold, position size, etc.) |
| val_bpb (validation metric) | Sharpe ratio / net PnL / Calmar ratio |
| 5-minute training budget | N hedging cycles per experiment |
| Git branch per experiment | Parameter snapshot in results.tsv |
| LOOP FOREVER | Run continuously during market hours (or overnight for backtests) |
| Agent edits Python code | Loop mutates float parameters |
| prepare.py (fixed data pipeline) | Kite auth + Greeks engine (fixed infrastructure) |
| program.md (human instructions) | config.ini [autoresearch] section |

### Key Differences from ML

1. **Non-stationarity**: Markets change. A parameter that worked last month
   may not work this month. The loop must keep running to adapt.

2. **Real money at stake**: ML experiments have no cost beyond compute.
   Trading experiments cost real capital. Hence the safety rails.

3. **Fewer experiments per day**: ML can run 12 experiments/hour (5 min each).
   Trading experiments take hours/days for meaningful evaluation.
   Typical: 2-5 experiments per trading day.

4. **Multiple metrics matter**: ML optimizes one loss. Trading must balance
   returns (Sharpe), risk (max drawdown), and efficiency (gamma/theta ratio).

## 3. The Experiment Loop

```
┌─────────────────────────────────────────────┐
│  INITIALIZATION                              │
│  1. Load current params as baseline          │
│  2. Run baseline → record metric             │
├─────────────────────────────────────────────┤
│  LOOP FOREVER:                               │
│                                              │
│  3. Pick ONE random tunable parameter        │
│  4. Apply Gaussian random walk mutation      │
│  5. Clamp to valid range                     │
│  6. Run hedger with new params for N cycles  │
│  7. Compute primary metric (Sharpe/PnL/etc)  │
│  8. Check safety: max_drawdown < threshold?  │
│                                              │
│  9. IF metric improved AND safe:             │
│       → ACCEPT: update baseline              │
│     ELSE:                                    │
│       → REJECT: revert to baseline           │
│                                              │
│  10. Log to results.tsv                      │
│  11. GOTO 3                                  │
└─────────────────────────────────────────────┘
```

### Why Mutate ONE Parameter at a Time?

Following Karpathy's design: single-variable changes make it clear
WHAT caused improvement. Multi-variable mutations create confounders.
This is hill-climbing, not grid search — slower but more interpretable.

If you want parallel exploration (like the SkyPilot extension of
autoresearch), you could run multiple hedger instances with different
mutation proposals simultaneously. But start simple.

## 4. Mutation Strategy

### Gaussian Random Walk

Each mutation applies a normally distributed step to one parameter:

```python
step = normal(mean=0, std=mutation_step_size × param_range)
new_value = old_value + step
new_value = clamp(new_value, min_allowed, max_allowed)
```

The `mutation_step_size` (default 0.1 = 10% of range) controls exploration:
- Small steps (0.05): Fine-tuning near a local optimum
- Large steps (0.20): Broader exploration, may find new optima
- The human can adjust this in config.ini based on progress

### Parameter Ranges

| Parameter | Min | Max | Unit | Effect |
|-----------|-----|-----|------|--------|
| rehedge_delta_threshold | 0.05 | 0.30 | lots | How far delta drifts before rehedge |
| gamma_scalp_band_pct | 0.5 | 3.0 | % | Underlying move to trigger scalp |
| position_size_pct | 5.0 | 25.0 | % | Capital per trade |
| vega_limit | 100 | 2000 | abs | Max portfolio vega exposure |
| max_holding_period_hours | 4 | 168 | hours | Force close timer |
| entry_iv_percentile_min | 10 | 50 | pctl | Min IV for entry |
| entry_iv_percentile_max | 50 | 95 | pctl | Max IV for entry |

### Constraint: IV min < IV max

The loop enforces `entry_iv_percentile_min < entry_iv_percentile_max - 5`
to prevent degenerate ranges where no entry is ever triggered.

## 5. Evaluation Metrics

### Primary Metric (configurable)

Choose ONE primary metric for the loop to optimize:

- **sharpe_ratio** (default): Risk-adjusted returns. Best for general use.
  `sharpe = mean(daily_returns) / std(daily_returns) × √252`

- **net_pnl**: Raw profit. Use when you care about absolute returns.
  Risk: may accept high-variance strategies.

- **calmar_ratio**: Return / max drawdown. Good for drawdown-sensitive traders.
  `calmar = annualized_return / max_drawdown`

- **sortino_ratio**: Like Sharpe but only penalizes downside volatility.
  `sortino = mean(returns) / downside_std × √252`

### Secondary Metrics (always tracked)

These are logged but not used for accept/reject decisions:
- `rehedge_count`: Number of delta rebalances
- `gamma_scalp_pnl`: Cumulative P&L from gamma scalps
- `theta_decay_paid`: Cumulative theta cost
- `gamma_theta_ratio`: Efficiency measure (scalp revenue / theta cost)
- `max_drawdown`: Worst peak-to-trough decline

### The Drawdown Gate

Even if the primary metric improves, the experiment is REJECTED if
`max_drawdown > max_drawdown_threshold` (default 5%).

This prevents the loop from finding "profitable but catastrophic" strategies
that make money most of the time but occasionally blow up.

## 6. Safety Rails

### What the Loop CAN Change

All parameters in the `[strategy]` section marked as tunable:
- Rehedge thresholds and bands
- Position sizing percentages
- Vega limits
- Holding periods
- Entry IV filters

### What the Loop CANNOT Change

Hardcoded safety rules that protect against catastrophic loss:
- Max daily loss percentage
- No naked shorts rule
- Margin limits per position
- Liquidity filters
- End-of-day trading cutoff
- Gap exit threshold
- Circuit breaker logic
- Total capital amount

These are in `immutable_params` in the hedger and are never exposed
to the mutation engine.

### Why This Matters

In Karpathy's autoresearch, a bad experiment just wastes 5 minutes of GPU.
In our system, a bad experiment could lose real money. The safety rails
ensure the loop can explore freely within safe boundaries.

## 7. Results Analysis

### results.tsv Structure

Each row is one experiment:
```
experiment_id  timestamp  param_mutated  old_value  new_value  sharpe_ratio  accepted  [all params...]
```

### Analysis Commands

```bash
# Print summary
python -m runners.autoresearch_loop analyze

# Generate progress plot (like Karpathy's progress.png)
python -m runners.autoresearch_loop plot
```

### What to Look For

1. **Acceptance rate**: Should be 15-30%. Too high = steps too small.
   Too low = steps too large or strategy is already near optimal.

2. **Metric trajectory**: Should show monotonic improvement (accepted
   experiments only). If it plateaus, increase mutation step size.

3. **Which params matter most**: Count accepted mutations per parameter.
   Parameters with many accepted changes are high-impact levers.

4. **Parameter convergence**: If a parameter stabilizes around a value
   across many experiments, that's likely near its optimum.

## 8. Operational Guide

### First Run

1. Start in **paper mode** (`trading_mode = paper` in config.ini)
2. Run the loop for a full trading day
3. Review results.tsv and progress.png
4. Sanity-check the trade log — do the entries/exits make sense?

### Transitioning to Live

1. Run paper mode for at least 1 week
2. Verify gamma_theta_ratio > 1.0 consistently
3. Start live with MINIMUM position size (5%)
4. Gradually increase as confidence builds
5. Keep autoresearch running — it adapts to changing market conditions

### Overnight Runs

The loop can run overnight on historical data (backtest mode) or
simply sit idle outside market hours and resume at 9:15 AM IST.

For backtesting, you'd need to replace the Kite API calls with
historical data feeds — this is a natural extension but outside
the scope of the base skill.

### When to Intervene

The human should step in when:
- Max drawdown approaches the threshold (tighten constraints)
- Acceptance rate drops below 5% (widen mutation steps)
- Market regime changes dramatically (reset baseline)
- A Black Swan event occurs (flatten all, pause the system)

### Monitoring Dashboard

The skill generates:
- `results.tsv`: Full experiment history
- `trades.csv`: All executed trades
- `greeks.csv`: Point-in-time Greeks snapshots
- `progress.png`: Visual optimization progress
- `best_params.json`: Current optimal parameters
- `autoresearch.log`: Detailed execution log

Review these daily. The system is autonomous but not unsupervised.
