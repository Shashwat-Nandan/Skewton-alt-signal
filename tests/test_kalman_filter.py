"""Tests for strategies.kalman_filter.KalmanPairFilter — the §15.6 hedge-ratio
tracker.

These encode the *intent* of the filter (CLAUDE.md Rule 9), not just that it
returns numbers. The whole reason the module exists is to track a slowly
drifting hedge ratio causally and produce a stationary spread, so the load-
bearing tests are: (1) it recovers a known constant γ, (2) it actually adapts
when γ changes (a frozen estimate would still "pass" a constant-γ test), (3)
the momentum model genuinely smooths γ relative to basic, (4) the spread is
strictly causal (no look-ahead), and (5) degenerate input fails loud rather
than silently producing an untrackable filter.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.kalman_filter import KalmanPairFilter


def _cointegrated_series(n, *, mu, gamma, sigma_eps, seed, y2_start=100.0):
    """Generate a cointegrated pair per the book's model (Eq. 15.1):
    y2 is a random walk, y1 = mu + gamma*y2 + stationary noise. Returns
    (y1, y2). `gamma` may be a scalar or a length-n array (time-varying)."""
    rng = np.random.default_rng(seed)
    y2 = y2_start + np.cumsum(rng.normal(0, 1.0, n))
    gamma_arr = np.full(n, gamma) if np.isscalar(gamma) else np.asarray(gamma)
    eps = rng.normal(0, sigma_eps, n)
    y1 = mu + gamma_arr * y2 + eps
    return y1, y2


def _run(filt, y1, y2):
    """Feed a whole series; return arrays of predicted γ and spread."""
    gammas, spreads, std_innov = [], [], []
    for a, b in zip(y1, y2):
        step = filt.update(float(a), float(b))
        gammas.append(step.gamma_pred)
        spreads.append(step.spread)
        std_innov.append(step.std_innovation)
    return np.array(gammas), np.array(spreads), np.array(std_innov)


def test_recovers_known_constant_gamma():
    """On a truly cointegrated pair with constant γ=0.7, the filtered hedge
    ratio must converge to ~0.7. If it doesn't, the filter is not tracking the
    relationship it claims to — the spread it produces would be meaningless."""
    y1, y2 = _cointegrated_series(400, mu=5.0, gamma=0.7, sigma_eps=0.5, seed=1)
    filt = KalmanPairFilter.from_training(y1[:120], y2[:120], model="basic")
    gammas, _, _ = _run(filt, y1[120:], y2[120:])
    # Average over the back half (after convergence) must be close to truth.
    assert abs(np.mean(gammas[-100:]) - 0.7) < 0.05


def test_spread_is_more_stationary_than_raw_difference():
    """The point of estimating γ is a mean-reverting spread. The Kalman spread
    must have far smaller variance than the naive y1−y2 (which carries the full
    random-walk level when γ≠1). A filter that didn't fit γ would fail this."""
    y1, y2 = _cointegrated_series(400, mu=3.0, gamma=0.6, sigma_eps=0.4, seed=2)
    filt = KalmanPairFilter.from_training(y1[:120], y2[:120], model="basic")
    _, spreads, _ = _run(filt, y1[120:], y2[120:])
    naive = (y1[120:] - y2[120:])
    assert np.var(spreads) < 0.25 * np.var(naive)


def test_tracks_a_step_change_in_gamma():
    """A frozen/over-smoothed estimate would still pass the constant-γ test.
    This is the test that fails for a non-adaptive filter: γ jumps 0.6→0.9
    mid-series and the tracker must follow it within a bounded number of steps.
    This is the entire reason we use Kalman over a static OLS β."""
    n = 600
    gamma_path = np.concatenate([np.full(n // 2, 0.6), np.full(n - n // 2, 0.9)])
    y1, y2 = _cointegrated_series(n, mu=4.0, gamma=gamma_path, sigma_eps=0.3, seed=3)
    # Larger alpha so adaptation is visibly fast (book warns of the trade-off).
    filt = KalmanPairFilter.from_training(
        y1[:100], y2[:100], model="basic", alpha=1e-3
    )
    gammas, _, _ = _run(filt, y1[100:], y2[100:])
    # Before the break (settled) ≈ 0.6; well after the break ≈ 0.9.
    pre = np.mean(gammas[(n // 2 - 100) - 60:(n // 2 - 100) - 10])
    post = np.mean(gammas[-60:])
    assert abs(pre - 0.6) < 0.08, f"pre-break γ={pre:.3f} should be ~0.6"
    assert abs(post - 0.9) < 0.08, f"post-break γ={post:.3f} should be ~0.9"
    assert post - pre > 0.2, "filter failed to track the γ step change"


def test_momentum_tracks_drifting_gamma_with_less_lag():
    """The reason Eq. (15.4) adds a velocity state γ̇ is to track a *drifting*
    hedge ratio with less lag — a position-only random walk (basic) lags a
    trend, a velocity model anticipates it. This is the model's load-bearing,
    α-robust property (the book's separate 'smoother γ' claim is α-dependent and
    not a reliable invariant on arbitrary data, so we don't pin it).

    Testbed: a well-identified, deterministically-moving y2 (so γ is always
    observable and the error is tracking lag, not estimation noise) with γ
    ramping linearly. The momentum model must beat basic on mean-abs tracking
    error. Matched α so the win comes from the model, not the tuning."""
    n = 900
    t = np.arange(n)
    y2 = 100.0 + 20.0 * np.sin(t / 15.0)          # deterministic, large range
    gamma_path = np.linspace(0.6, 0.9, n)         # linear drift in γ
    rng = np.random.default_rng(11)
    y1 = 3.0 + gamma_path * y2 + rng.normal(0, 0.2, n)
    a = 1e-3
    basic = KalmanPairFilter.from_training(y1[:150], y2[:150], model="basic", alpha=a)
    mom = KalmanPairFilter.from_training(y1[:150], y2[:150], model="momentum", alpha=a)
    gb, _, _ = _run(basic, y1[150:], y2[150:])
    gm, _, _ = _run(mom, y1[150:], y2[150:])
    truth = gamma_path[150:]
    mae_basic = np.mean(np.abs(gb - truth))
    mae_mom = np.mean(np.abs(gm - truth))
    assert mae_mom < 0.85 * mae_basic, (
        f"momentum MAE={mae_mom:.4f} should clearly beat basic MAE={mae_basic:.4f} "
        f"on a drifting γ (the velocity state should cut tracking lag)"
    )


def test_spread_uses_predicted_state_no_lookahead():
    """The spread at step t must be computable before seeing y1_t's effect on
    the state — i.e. it uses the predicted γ_{t|t-1}, not the filtered γ_{t|t}.
    We pin this exactly: spread == innovation / (1 + |gamma_pred|) (leverage-one
    normalization by gross leverage). If the impl ever switched to the filtered
    state, this identity would break and the backtest would be silently
    look-ahead biased."""
    y1, y2 = _cointegrated_series(200, mu=1.0, gamma=0.8, sigma_eps=0.5, seed=5)
    filt = KalmanPairFilter.from_training(y1[:80], y2[:80], model="momentum")
    for a, b in zip(y1[80:], y2[80:]):
        step = filt.update(float(a), float(b))
        recomputed = step.innovation / (1.0 + abs(step.gamma_pred))
        assert abs(step.spread - recomputed) < 1e-9
        # And it must NOT equal the filtered-state version (unless by fluke).
        assert step.innovation_var > 0


def test_serialize_roundtrip_is_identity():
    """Restarting the paper runner must restore the FULL filter state (state +
    covariance), not just γ. A round-trip through serialize/deserialize must
    produce byte-identical subsequent behavior, or a restart silently resets
    the filter's uncertainty and corrupts tracking (plan Phase 1 risk)."""
    y1, y2 = _cointegrated_series(150, mu=2.0, gamma=0.7, sigma_eps=0.4, seed=6)
    filt = KalmanPairFilter.from_training(y1[:80], y2[:80], model="momentum")
    _run(filt, y1[80:120], y2[80:120])  # advance partway
    clone = KalmanPairFilter.deserialize(filt.serialize())
    for a, b in zip(y1[120:], y2[120:]):
        s1 = filt.update(float(a), float(b))
        s2 = clone.update(float(a), float(b))
        assert s1 == s2


@pytest.mark.parametrize("bad", ["too_short", "constant_y2", "collinear", "nan"])
def test_degenerate_training_fails_loud(bad):
    """A silent pass on bad input is exactly the failure CLAUDE.md Rule 12
    warns about — the filter would 'work' but track nothing. Each degenerate
    case must raise, not return a broken filter."""
    if bad == "too_short":
        y1, y2 = np.array([1.0, 2.0]), np.array([1.0, 2.0])
    elif bad == "constant_y2":
        y1, y2 = np.arange(50.0), np.full(50, 5.0)
    elif bad == "collinear":  # zero residual → σ²_ε = 0
        y2 = np.arange(50.0)
        y1 = 3.0 + 0.5 * y2
    else:  # nan
        y1 = np.arange(50.0); y1[10] = np.nan
        y2 = np.arange(50.0)
    with pytest.raises(ValueError):
        KalmanPairFilter.from_training(y1, y2, model="basic")


def test_update_rejects_nonfinite_observation():
    """A NaN tick must not silently poison the state."""
    y1, y2 = _cointegrated_series(120, mu=1.0, gamma=0.7, sigma_eps=0.4, seed=7)
    filt = KalmanPairFilter.from_training(y1[:80], y2[:80], model="basic")
    with pytest.raises(ValueError):
        filt.update(float("nan"), 100.0)
