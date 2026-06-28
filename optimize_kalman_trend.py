"""
Fixed-tick trend simulation + CMA-ES/L1 fit for the Kalman trend follower
=========================================================================
Implements the optimization half of Benhamou, *Kalman filter demystified*
(hal-02012471), §6: the deliberately-simple fixed-tick long/short trend strategy
(Algorithm 4 for Kalman, Algorithm 5 for the moving-average baseline) and the
joint parameter fit by CMA-ES maximizing the train-period Sharpe with an L1
penalty on the Kalman filter parameters.

Pure and I/O-free (no Kite, no disk) — the correctness gate
(`validate_kalman_trend.py`) and the backtest call into it; tests exercise it
directly.

Daily-close approximation (Rule 12)
-----------------------------------
The paper enters with a market order at the next OPEN and exits at a fixed
profit-target / stop-loss in ticks intraday. Our cached series are daily CLOSES
only, so we approximate faithfully but explicitly:
  • the directional signal is decided causally at close t (uses data ≤ t),
  • entry fills at close t (the bar whose close produced the signal),
  • a stop/target is hit when a later close crosses it, and the trade books at
    the stop/target price (not the close).
This understates intraday stop/target precision but keeps the comparison Kalman
vs MA on an identical, look-ahead-free footing — which is what the gate needs.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from strategies.kalman_trend import WARMUP_BARS, KalmanTrendFilter

TRADING_DAYS = 252
# WARMUP_BARS is imported (single source of truth) so the backtest optimizes the
# SAME rule the live IntradayTrendStrategy trades — see strategies/kalman_trend.py.
# Minimum trades for a verdict to mean anything — below this the OOS Sharpe is
# one-trade noise and "Kalman beats MA" is not a real claim.
MIN_VERDICT_TRADES = 3


def beats(kal: float, ma: float) -> bool:
    """True iff the Kalman metric strictly beats MA. NaN-aware: a NaN kal (no
    edge measured / no trades) never beats; a finite kal beats a NaN ma (MA
    didn't even trade). One definition, shared by both gate harnesses."""
    return bool(np.isfinite(kal) and (not np.isfinite(ma) or kal > ma))


def verdict_passed(kal: float, ma: float, kal_trades: int, win_rate: float) -> bool:
    """The single GO/NO-GO policy, shared by validate and backtest so the two
    can't drift: Kalman traded enough to matter, has a finite metric, STRICTLY
    beats MA, and wins a majority of (seed/fold) comparisons."""
    return bool(kal_trades >= MIN_VERDICT_TRADES and beats(kal, ma) and win_rate > 0.5)


# ──────────────────────────────────────────────────────────────────────────
# Signals (causal — decided at close t from observations ≤ t)
# ──────────────────────────────────────────────────────────────────────────
def kalman_direction(prices, p, *, model: int, mu: float,
                     warmup: int = WARMUP_BARS) -> np.ndarray:
    """Algorithm 4: +1/-1/0 per bar from the Kalman one-step forecast vs the
    current close with a dead-band µ. The forecast returned by update(close_t)
    is H x_{t+1|t} (uses data ≤ t), compared against close_t — strictly causal.

    The first `warmup` bars produce NO signal (the filter still updates) — this
    matches IntradayTrendStrategy.warmup_bars so the fit and the live book trade
    the same rule, and avoids the t=0 transient where prediction == price and a
    µ=0 dead-band would book a phantom entry.

    Raises (ValueError/NotImplementedError) if the filter params diverge, so the
    optimizer can assign bad fitness and steer away.

    Note (gap handling): this runs the filter over a CONTINUOUS price series with
    no session-boundary reset. That is exact for the daily gates (daily bars have
    no intraday gap). For INTRADAY bars concatenated across days it does NOT apply
    the live runner's overnight-gap inflation (IntradayTrendStrategy.on_session_
    start) — a known fit-vs-live gap for the intraday research backtest only; the
    daily GO/NO-GO is unaffected."""
    prices = np.asarray(prices, float)
    filt = KalmanTrendFilter.from_params(p, model=model, init_price=float(prices[0]))
    direction = np.zeros(len(prices))
    for t, c in enumerate(prices):
        step = filt.update(float(c))      # forecast of close_{t+1}, data ≤ t
        if t < warmup:
            continue
        if step.prediction >= c + mu:
            direction[t] = 1.0
        elif step.prediction <= c - mu:
            direction[t] = -1.0
    return direction


def ma_direction(prices, *, short: int, long: int, offset: float,
                 warmup: int = WARMUP_BARS) -> np.ndarray:
    """Algorithm 5: SMA(short) vs SMA(long) crossover with a dead-band offset.
    Causal — each SMA at t uses closes ≤ t. No signal until BOTH the long window
    is full and `warmup` bars have passed (parity with the live book, where the
    long window dominates so warmup is usually inert)."""
    prices = np.asarray(prices, float)
    short, long = int(round(short)), int(round(long))
    if short < 1 or long < 1:
        raise ValueError(f"SMA windows must be >= 1 (short={short}, long={long})")
    n = len(prices)
    direction = np.zeros(n)
    for t in range(n):
        if t + 1 < long or t < warmup:
            continue
        sma_s = prices[t - short + 1:t + 1].mean()
        sma_l = prices[t - long + 1:t + 1].mean()
        if sma_s > sma_l + offset:
            direction[t] = 1.0
        elif sma_s < sma_l - offset:
            direction[t] = -1.0
    return direction


# ──────────────────────────────────────────────────────────────────────────
# Execution engine (fixed-tick stop/target) + Sharpe
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SimResult:
    daily_pnl: np.ndarray      # per-bar mark-to-market P&L in price points
    n_trades: int
    realized_pnl: float        # sum of closed-trade P&L (after costs)
    sharpe: float              # annualized, from daily_pnl


def simulate(
    prices,
    direction,
    *,
    stop_ticks: float,
    target_ticks: float,
    tick_size: float = 1.0,
    cost_per_unit: float = 0.0,
) -> SimResult:
    """Walk the daily series: enter (at close) on a nonzero `direction` when
    flat, manage a fixed-tick stop/target, mark to market daily. `stop_ticks`
    and `target_ticks` are in ticks; price distance = ticks · tick_size.
    `cost_per_unit` is charged in price points on each entry and each exit."""
    prices = np.asarray(prices, float)
    direction = np.asarray(direction, float)
    n = len(prices)
    if n < 2:
        return SimResult(np.zeros(n), 0, 0.0, float("nan"))   # undefined, not a sentinel
    stop_d = abs(stop_ticks) * tick_size
    target_d = abs(target_ticks) * tick_size

    daily = np.zeros(n)
    pos = 0          # -1 / 0 / +1
    entry = stop = tgt = 0.0
    realized = 0.0
    n_trades = 0

    for t in range(n):
        c = prices[t]
        if pos != 0 and t > 0:
            daily[t] += pos * (c - prices[t - 1])      # mark to close
        if pos != 0:
            hit = None
            if pos > 0:
                hit = stop if c <= stop else (tgt if c >= tgt else None)
            else:
                hit = stop if c >= stop else (tgt if c <= tgt else None)
            if hit is not None:
                daily[t] += pos * (hit - c)            # adjust close→exit price
                daily[t] -= cost_per_unit
                realized += pos * (hit - entry) - 2 * cost_per_unit
                n_trades += 1
                pos = 0
        if pos == 0 and direction[t] != 0:
            pos = int(direction[t])
            entry = c
            stop = entry - pos * stop_d
            tgt = entry + pos * target_d
            daily[t] -= cost_per_unit

    # Force-close any open position at the last close.
    if pos != 0:
        realized += pos * (prices[-1] - entry) - 2 * cost_per_unit
        n_trades += 1

    return SimResult(daily, n_trades, realized, _sharpe(daily, n_trades))


def _sharpe(daily_pnl: np.ndarray, n_trades: int) -> float:
    """Annualized Sharpe of the daily P&L, or NaN when undefined (no trades / no
    variation). NaN — not a magic -10.0 — so 'no edge to measure' is distinct
    from 'a measured, terrible Sharpe' (a real -9 strategy used to collide with
    the old sentinel). Callers: the optimizer maps NaN→worst fitness (run_cmaes
    handles non-finite); the verdicts treat NaN as 'did not beat' (a NaN never
    satisfies `>` against a finite value)."""
    if n_trades < 1:
        return float("nan")
    sd = float(np.std(daily_pnl))
    if not (sd > 0):
        return float("nan")
    return float(np.mean(daily_pnl) / sd * np.sqrt(TRADING_DAYS))


# ──────────────────────────────────────────────────────────────────────────
# CMA-ES driver (the paper's optimizer, §4.7)
# ──────────────────────────────────────────────────────────────────────────
def run_cmaes(
    objective_min,
    x0,
    bounds: np.ndarray,
    *,
    sigma: float = 0.25,
    n_gen: int = 200,
    seed: int = 0,
    popsize: int | None = None,
):
    """Minimize `objective_min(x)` with CMA-ES over box `bounds` (shape (d,2)).

    The search is run in NORMALIZED [0,1]^d coordinates (so one scalar `sigma`
    is well-conditioned even when the real params span many orders of
    magnitude), mapping each candidate back to real space before scoring.
    `objective_min` receives the REAL-valued x. Returns (best_real_x, best_f)."""
    from cmaes import CMA

    bounds = np.asarray(bounds, float)
    lo, hi = bounds[:, 0], bounds[:, 1]
    span = np.where(hi > lo, hi - lo, 1.0)

    def to_real(u):
        return lo + np.clip(u, 0.0, 1.0) * span

    u0 = np.clip((np.asarray(x0, float) - lo) / span, 1e-6, 1 - 1e-6)
    norm_bounds = np.tile([0.0, 1.0], (len(lo), 1))
    kw = dict(mean=u0, sigma=sigma, bounds=norm_bounds, seed=seed)
    if popsize is not None:
        kw["population_size"] = popsize
    opt = CMA(**kw)
    best_x, best_f = to_real(u0), float("inf")
    for _ in range(n_gen):
        sols = []
        for _ in range(opt.population_size):
            u = opt.ask()
            x = to_real(u)
            f = float(objective_min(x))
            if not np.isfinite(f):
                f = 1e12
            sols.append((u, f))
            if f < best_f:
                best_f, best_x = f, x.copy()
        opt.tell(sols)
        if opt.should_stop():
            break
    return best_x, best_f


# ──────────────────────────────────────────────────────────────────────────
# Fits (train-period Sharpe maximization)
# ──────────────────────────────────────────────────────────────────────────
# Kalman model-1 (Newtonian) fit. Model 1 keeps Φ=[[1,1],[0,1]] FIXED (stable)
# and optimizes the noise levels + trade params — the only Table-1 spec that
# reproduces (model 3/4's free Φ diverges; finding #2). The decision vector is in
# STD-DEV / price-point space so every component is linearly scaled to the
# series (CMA-ES, run in normalized [0,1] coords, is then well-conditioned):
#
#   [s_level, s_off, s_vel, s_obs, s_p0, µ, stop, target]
#
# mapped to the paper's p-vector as p=[s_level, s_off, s_vel, s_obs², s_p0²]
# (R and P₀ are variances). Bounds scale with d = std of daily price changes.
def _kalman_bounds(d: float) -> np.ndarray:
    return np.array([
        [1e-4 * d, 5.0 * d],    # s_level  level process std
        [0.0, 5.0 * d],         # s_off    Q off-diagonal factor
        [1e-4 * d, 2.0 * d],    # s_vel    velocity process std
        [1e-2 * d, 10.0 * d],   # s_obs    obs noise std  (R = s_obs²)
        [1e-2 * d, 50.0 * d],   # s_p0     init state std (P0 = s_p0²)
        [0.0, 5.0 * d],         # µ        dead-band (price points)
        [0.2 * d, 50.0 * d],    # stop     (ticks)
        [0.2 * d, 100.0 * d],   # target   (ticks)
    ])


def _decision_to_pvector(x) -> np.ndarray:
    """[s_level,s_off,s_vel,s_obs,s_p0] → paper p=[p1,p2,p3,p4=R,p5=P0]."""
    return np.array([x[0], x[1], x[2], x[3] ** 2, x[4] ** 2])


def _daily_scale(prices: np.ndarray) -> float:
    d = float(np.std(np.diff(prices)))
    return d if d > 0 else 1.0


def fit_kalman_trend(
    train_prices,
    *,
    model: int = 1,
    tick_size: float = 1.0,
    cost_per_unit: float = 0.0,
    l1_lambda: float = 0.1,
    n_gen: int = 200,
    seed: int = 0,
) -> dict:
    """Joint CMA-ES fit of the model-1 Kalman trend strategy on `train_prices`,
    maximizing Sharpe − l1_lambda·‖p_filter‖₁ (paper §6) with the L1 taken in
    NORMALIZED parameter space so it is scale-invariant. Returns the fitted
    paper p-vector, trade params, train Sharpe and the (normalized) sparsity."""
    if model != 1:
        raise NotImplementedError(
            "fit currently supports model 1 (stable Newtonian); models 3/4 have "
            "a free Φ that diverges — see plan finding #2")
    prices = np.asarray(train_prices, float)
    d = _daily_scale(prices)
    bounds = _kalman_bounds(d)
    fhi = bounds[:5, 1]   # upper bounds of the 5 filter params (for normalized L1)

    def objective_min(x):
        p = _decision_to_pvector(x)
        mu, stop, target = x[5], x[6], x[7]
        try:
            direction = kalman_direction(prices, p, model=model, mu=mu)
        except (ValueError, NotImplementedError):
            return 1e12   # divergent filter → worst fitness
        res = simulate(prices, direction, stop_ticks=stop, target_ticks=target,
                       tick_size=tick_size, cost_per_unit=cost_per_unit)
        penalty = l1_lambda * float(np.sum(np.abs(x[:5]) / fhi))
        return -(res.sharpe - penalty)

    x0 = np.array([0.5 * d, 0.0, 0.1 * d, d, 2.0 * d, 0.2 * d, 5.0 * d, 10.0 * d])
    best_x, _ = run_cmaes(objective_min, x0, bounds=bounds, sigma=0.25,
                          n_gen=n_gen, seed=seed)
    p = _decision_to_pvector(best_x)
    direction = kalman_direction(prices, p, model=model, mu=best_x[5])
    res = simulate(prices, direction, stop_ticks=best_x[6], target_ticks=best_x[7],
                   tick_size=tick_size, cost_per_unit=cost_per_unit)
    return {
        "filter_params": p.tolist(),
        "mu": float(best_x[5]),
        "stop_ticks": float(best_x[6]),
        "target_ticks": float(best_x[7]),
        "train_sharpe": float(res.sharpe) if np.isfinite(res.sharpe) else None,
        "l1_norm_normalized": float(np.sum(np.abs(best_x[:5]) / fhi)),
        "n_trades": res.n_trades,
    }


def _ma_bounds(d: float) -> np.ndarray:
    return np.array([
        [2.0, 40.0],          # short window
        [10.0, 200.0],        # long window
        [0.0, 5.0 * d],       # offset
        [0.2 * d, 50.0 * d],  # stop  (ticks)
        [0.2 * d, 100.0 * d], # target(ticks)
    ])


def fit_ma_crossover(
    train_prices,
    *,
    tick_size: float = 1.0,
    cost_per_unit: float = 0.0,
    n_gen: int = 200,
    seed: int = 0,
) -> dict:
    """Joint CMA-ES fit of the moving-average crossover baseline (Algorithm 5),
    maximizing train Sharpe — the paper optimizes the baseline's params too, for
    a fair comparison."""
    prices = np.asarray(train_prices, float)
    d = _daily_scale(prices)
    bounds = _ma_bounds(d)

    def objective_min(x):
        short, long, offset, stop, target = x
        if round(short) >= round(long):
            return 1e12   # require short < long
        try:
            direction = ma_direction(prices, short=short, long=long, offset=offset)
        except ValueError:
            return 1e12
        res = simulate(prices, direction, stop_ticks=stop, target_ticks=target,
                       tick_size=tick_size, cost_per_unit=cost_per_unit)
        return -res.sharpe

    x0 = np.array([10.0, 50.0, 0.5 * d, 5.0 * d, 10.0 * d])
    best_x, _ = run_cmaes(objective_min, x0, bounds=bounds, sigma=0.25,
                          n_gen=n_gen, seed=seed)
    short, long, offset, stop, target = best_x
    direction = ma_direction(prices, short=short, long=long, offset=offset)
    res = simulate(prices, direction, stop_ticks=stop, target_ticks=target,
                   tick_size=tick_size, cost_per_unit=cost_per_unit)
    return {
        "short": int(round(short)), "long": int(round(long)),
        "offset": float(offset), "stop_ticks": float(stop),
        "target_ticks": float(target), "train_sharpe": float(res.sharpe) if np.isfinite(res.sharpe) else None,
        "n_trades": res.n_trades,
    }


# ──────────────────────────────────────────────────────────────────────────
# REDUCED Kalman fit (robustness discipline — Option B)
# ──────────────────────────────────────────────────────────────────────────
# The full 5-filter-param fit overfits a single 6mo window (train Sharpe 2–5 →
# OOS noise; see tasks/kalman-trend-findings.md). The reduced model keeps the
# stable Newtonian structure and exposes ONE filter knob — the velocity process
# std (signal-to-noise: how fast the trend may turn) — with R and P₀ seeded from
# the data. Decision vector: [s_vel, µ, stop, target] (4, all scaled to the daily
# move d). Fewer params + walk-forward selection is the regularization; no L1
# needed. Built on model 2 so level/velocity get distinct initial variances.
def _kalman_reduced_bounds(d: float) -> np.ndarray:
    return np.array([
        [1e-4 * d, 2.0 * d],    # s_vel  velocity process std (the only filter knob)
        [0.0, 3.0 * d],         # µ      dead-band (price points)
        [0.2 * d, 30.0 * d],    # stop   (ticks)
        [0.2 * d, 60.0 * d],    # target (ticks)
    ])


def _reduced_to_pvector(x, d: float) -> np.ndarray:
    """[s_vel,…] → model-2 p=[p1=0, p2=0, p3=s_vel, p4=R, p5=P0_level, p6=P0_vel].
    Q = diag(0, s_vel²) (random walk on velocity only); R = d² (obs noise ≈ the
    one-step change variance); P₀ = diag((10d)², d²) (mild diffuse prior)."""
    return np.array([0.0, 0.0, x[0], d * d, (10.0 * d) ** 2, d * d])


def fit_kalman_reduced(
    train_prices,
    *,
    tick_size: float = 1.0,
    cost_per_unit: float = 0.0,
    n_gen: int = 120,
    seed: int = 0,
) -> dict:
    """Reduced 4-param CMA-ES fit (Option B). Returns model-2 filter params + the
    trade params + train Sharpe. Tagged `model=2` so `evaluate` reconstructs it."""
    prices = np.asarray(train_prices, float)
    d = _daily_scale(prices)
    bounds = _kalman_reduced_bounds(d)

    def objective_min(x):
        p = _reduced_to_pvector(x, d)
        try:
            direction = kalman_direction(prices, p, model=2, mu=x[1])
        except (ValueError, NotImplementedError):
            return 1e12
        res = simulate(prices, direction, stop_ticks=x[2], target_ticks=x[3],
                       tick_size=tick_size, cost_per_unit=cost_per_unit)
        return -res.sharpe

    x0 = np.array([0.2 * d, 0.2 * d, 5.0 * d, 10.0 * d])
    best_x, _ = run_cmaes(objective_min, x0, bounds=bounds, sigma=0.25,
                          n_gen=n_gen, seed=seed)
    p = _reduced_to_pvector(best_x, d)
    direction = kalman_direction(prices, p, model=2, mu=best_x[1])
    res = simulate(prices, direction, stop_ticks=best_x[2], target_ticks=best_x[3],
                   tick_size=tick_size, cost_per_unit=cost_per_unit)
    return {
        "model": 2, "filter_params": p.tolist(), "s_vel": float(best_x[0]),
        "mu": float(best_x[1]), "stop_ticks": float(best_x[2]),
        "target_ticks": float(best_x[3]), "train_sharpe": float(res.sharpe) if np.isfinite(res.sharpe) else None,
        "n_trades": res.n_trades,
    }


def evaluate(prices, *, kind: str, params: dict, tick_size: float = 1.0,
             cost_per_unit: float = 0.0) -> SimResult:
    """Run a fitted parameter set forward on `prices` (e.g. the test slice).
    `kind` ∈ {'kalman','ma'}. Causal — the signal is regenerated on this slice."""
    prices = np.asarray(prices, float)
    if kind == "kalman":
        d = kalman_direction(prices, params["filter_params"],
                             model=params.get("model", 1), mu=params["mu"])
    elif kind == "ma":
        d = ma_direction(prices, short=params["short"], long=params["long"],
                         offset=params["offset"])
    else:
        raise ValueError(f"unknown kind {kind!r}")
    return simulate(prices, d, stop_ticks=params["stop_ticks"],
                    target_ticks=params["target_ticks"], tick_size=tick_size,
                    cost_per_unit=cost_per_unit)
