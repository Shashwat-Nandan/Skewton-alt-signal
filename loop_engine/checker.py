"""Independent deterministic checker — the paper's "entire edge" (§III-C / §III-D').

The maker (the Kalman strategy) is the worst judge of whether its own signal is
alpha or noise, so a separate, fixed-rule verifier grades it. Because the maker
here is deterministic and the gates are deterministic inequalities, the checker is
plain code — NO model in the path (CLAUDE.md Rule 5). The checker re-derives
everything from a backtest it runs itself; it never consumes how the maker fit its
live params ("no exposure to the maker's reasoning trace").

The gates reduce to well-known statistical objects (§III-D'): annualized Sharpe,
maximum drawdown, and the Newey–West (HAC) t-statistic of the mean return — all
computed deterministically from one out-of-sample returns series, plus the OOS
span. Each gate is `value <op> threshold`; a candidate that fails ANY gate is
killed. A NaN statistic fails its gate (fail-closed — Rule 12).

DEVIATION FROM THE PAPER (Rule 1): the paper's 5th gate, `sector_expo < 0.30`, is
an equities-portfolio constraint and is meaningless for a single-instrument
futures trend follower; it is intentionally omitted, not faked. Four gates remain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

from loop_engine import memory

TRADING_DAYS = 252


# ──────────────────────────────────────────────────────────────────────────
# Deterministic statistics (§III-D')
# ──────────────────────────────────────────────────────────────────────────
def annualized_sharpe(returns: np.ndarray, periods_per_year: int = TRADING_DAYS) -> float:
    """Mean/std × √frequency. NaN when std is 0 (nothing varied) — same degenerate
    contract as optimize_kalman_trend._sharpe so the harnesses agree."""
    x = np.asarray(returns, float)
    sd = float(np.std(x))
    return float(np.mean(x) / sd * np.sqrt(periods_per_year)) if sd > 0 else float("nan")


def max_drawdown(returns: np.ndarray) -> float:
    """Largest peak-to-trough decline of the compounded equity curve, as a positive
    fraction (0.10 == a 10% drawdown). 0.0 for an empty or monotonically-up curve."""
    x = np.asarray(returns, float)
    if x.size == 0:
        return 0.0
    equity = np.cumprod(1.0 + x)
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    return float(-dd.min())


def newey_west_tstat(returns: np.ndarray, lags: Optional[int] = None) -> float:
    """t-stat of the mean return with a Newey–West (Bartlett) HAC standard error —
    the right inference tool when returns are serially correlated (§III-D'). Default
    lag = floor(4·(n/100)^(2/9)) (the Newey–West rule of thumb). NaN if undefined."""
    x = np.asarray(returns, float)
    n = x.size
    if n < 2:
        return float("nan")
    mu = float(x.mean())
    e = x - mu
    if lags is None:
        lags = int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    omega = float(np.dot(e, e) / n)                       # gamma_0
    for lag in range(1, lags + 1):
        if lag >= n:
            break
        weight = 1.0 - lag / (lags + 1.0)                # Bartlett kernel
        cov = float(np.dot(e[lag:], e[:-lag]) / n)       # gamma_lag
        omega += 2.0 * weight * cov
    if omega <= 0:
        return float("nan")
    se = np.sqrt(omega / n)
    return mu / se


# ──────────────────────────────────────────────────────────────────────────
# Gates
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class GateThresholds:
    sharpe_min: float = 1.5
    max_dd_max: float = 0.10
    nw_tstat_min: float = 2.0
    oos_months_min: float = 24.0

    @classmethod
    def from_skill(cls, strategy: str, root: Optional[Path] = None) -> "GateThresholds":
        """Read thresholds out of SKILL.md `## Rules` so the Phase-6 recalibration
        audit can tighten them in ONE place. Missing keys keep the defaults."""
        skill = memory.load_skill(strategy, root=root)
        values = {}
        for rule in skill.rules:
            if ":" not in rule:
                continue
            key, _, raw = rule.partition(":")
            key = key.strip()
            if key in cls.__dataclass_fields__:
                try:
                    values[key] = float(raw.strip())
                except ValueError:
                    continue
        return cls(**values)


@dataclass
class GateOutcome:
    name: str
    value: float
    threshold: float
    op: str          # ">=" or "<="
    passed: bool

    def __str__(self) -> str:
        return f"{self.name} {self.value:.2f}{self.op}{self.threshold:.2f}"


@dataclass
class CheckResult:
    passed: bool
    gates: List[GateOutcome] = field(default_factory=list)
    n_obs: int = 0
    note: str = ""

    def failures(self) -> List[str]:
        """Short 'name value<op>threshold' strings for the gates that FAILED — for
        the killed-signal log line in STATE.md."""
        return [str(g) for g in self.gates if not g.passed]

    def report(self) -> str:
        head = f"CHECK {'PASS' if self.passed else 'REJECT'} (n={self.n_obs})"
        if self.note:
            head += f" — {self.note}"
        lines = [head] + [f"  [{'ok' if g.passed else 'XX'}] {g}" for g in self.gates]
        return "\n".join(lines)


def _ge(name: str, value: float, threshold: float) -> GateOutcome:
    # NaN fails closed (np.nan >= x is False, which is the behaviour we want).
    return GateOutcome(name, value, threshold, ">=", bool(value >= threshold))


def _le(name: str, value: float, threshold: float) -> GateOutcome:
    return GateOutcome(name, value, threshold, "<=", bool(value <= threshold))


def apply_gates(
    returns: np.ndarray,
    thresholds: GateThresholds,
    periods_per_year: int = TRADING_DAYS,
) -> CheckResult:
    """Compute the four numbers and apply the four inequalities (§III-D')."""
    x = np.asarray(returns, float)
    n = int(x.size)
    if n == 0:
        # Nothing traded → no evidence of edge → kill (a low/empty result is a
        # warning sign, not a pass — §VI-A).
        return CheckResult(passed=False, gates=[], n_obs=0,
                           note="no observations (candidate never traded)")

    oos_months = n / periods_per_year * 12.0
    gates = [
        _ge("sharpe", annualized_sharpe(x, periods_per_year), thresholds.sharpe_min),
        _le("max_dd", max_drawdown(x), thresholds.max_dd_max),
        _ge("nw_tstat", newey_west_tstat(x), thresholds.nw_tstat_min),
        _ge("oos_months", oos_months, thresholds.oos_months_min),
    ]
    return CheckResult(passed=all(g.passed for g in gates), gates=gates, n_obs=n)


# ──────────────────────────────────────────────────────────────────────────
# Adapter: kalman_trend candidate → OOS returns → gates
# ──────────────────────────────────────────────────────────────────────────
def kalman_trend_oos_returns(
    closes: np.ndarray,
    *,
    train_len: int = 100,
    test_len: int = 30,
    step: int = 30,
    seed: int = 0,
    n_gen: int = 25,
    cost: float = 2.5,
) -> np.ndarray:
    """Build ONE deterministic pooled out-of-sample fractional-returns series for
    the Kalman candidate by reusing the existing walk-forward folds.

    Single seed (deterministic) — multi-seed robustness is the BACKTEST's verdict
    job; the checker is a fixed-rule gate on one OOS series (§III-D'). PnL is in
    price points (one unit traded), so return_t = pnl_t / close_t is the fractional
    return on notional; pooling across non-overlapping test slices gives the OOS
    curve. Reuses backtest_kalman_trend._fold_oos + optimize_kalman_trend, so the
    checker does not reimplement the simulator (Rules 7/8).
    """
    import backtest_kalman_trend as bt
    import optimize_kalman_trend as o

    closes = np.asarray(closes, float)
    n = closes.size
    starts = list(range(0, n - train_len - test_len + 1, step))
    pooled: List[np.ndarray] = []
    for a in starts:
        b, c = a + train_len, a + train_len + test_len
        kp = o.fit_kalman_reduced(closes[a:b], tick_size=1.0, cost_per_unit=cost,
                                  n_gen=n_gen, seed=seed)
        kr = bt._fold_oos(closes, a, b, c, kind="kalman", params=kp, cost=cost)
        seg = closes[b:c]
        pooled.append(np.asarray(kr.daily_pnl, float) / seg)   # points → fractional
    return np.concatenate(pooled) if pooled else np.array([], float)


def check_kalman_trend(
    closes: np.ndarray,
    thresholds: Optional[GateThresholds] = None,
    *,
    periods_per_year: int = TRADING_DAYS,
    **fold_kwargs,
) -> CheckResult:
    """Run the independent verifier on the Kalman candidate over trailing closes."""
    if thresholds is None:
        thresholds = GateThresholds.from_skill("kalman_trend")
    returns = kalman_trend_oos_returns(closes, **fold_kwargs)
    return apply_gates(returns, thresholds, periods_per_year)


def default_kalman_trend_checker(symbol: str = "NIFTY"):
    """Production checker for the orchestrator: an INDEPENDENT daily walk-forward
    edge gate on the cached closes for `symbol`.

    Returns a callable (outcome) -> CheckResult. It deliberately re-derives the
    verdict from a daily backtest it runs itself — a stricter, fully reproducible
    test of whether the trend method has edge at all — rather than grading the
    intraday paper A/B's session P&L (which the maker produced). No Kite, no maker
    reasoning consumed. NIFTY is the default (deepest cached history).

    LIMITATION (Rule 1): v1 gates on ONE index as a proxy for the trend method;
    per-symbol aggregation across NIFTY+BANKNIFTY is a deliberate future refinement.
    """
    def _check(outcome) -> CheckResult:
        from validate_kalman_trend import load_daily_closes

        _, closes = load_daily_closes(symbol)
        return check_kalman_trend(closes)

    return _check
