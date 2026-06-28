"""Tests for strategies.kalman_trend.KalmanTrendFilter — the §6/Table-1 trend
detector from Benhamou, *Kalman filter demystified*.

These encode the *intent* of the filter (CLAUDE.md Rule 9), not just that it
returns numbers. The whole reason the module exists is to track the trend
(position + velocity) of one price series causally and forecast the next close
better than a lagging moving average, so the load-bearing tests are: (1) it
recovers a known constant slope, (2) its one-step forecast beats a same-lag SMA
(the paper's claim — a frozen/lagging predictor would fail this), (3) it adapts
when the trend reverses (a frozen-velocity estimate would not), (4) the forecast
is strictly causal (no look-ahead), (5) serialize→restore preserves the FULL
state (covariance, not just the level), and (6) degenerate input / invalid model
specs fail loud.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.kalman_trend import KalmanTrendFilter


def _model1_filter(init_price, *, q_level=1e-2, q_vel=1e-4, R=1.0, P0=100.0):
    """A Newtonian (model-1) trend filter with hand-set noise levels."""
    p = [np.sqrt(q_level), 0.0, np.sqrt(q_vel), R, P0]
    return KalmanTrendFilter.from_params(p, model=1, init_price=init_price)


def _run(filt, prices):
    """Feed a whole series; return per-step (prediction, velocity_pred)."""
    preds, vels = [], []
    for z in prices:
        step = filt.update(float(z))
        preds.append(step.prediction)
        vels.append(step.velocity_pred)
    return np.array(preds), np.array(vels)


def _trend_series(n, *, slope, sigma, seed, level0=100.0):
    rng = np.random.default_rng(seed)
    return level0 + slope * np.arange(n) + rng.normal(0, sigma, n)


# ──────────────────────────────────────────────────────────────────────────
# 1. Recovers a known constant slope
# ──────────────────────────────────────────────────────────────────────────
def test_recovers_known_constant_slope():
    """On a clean linear uptrend (slope=0.5/bar) the filter's velocity estimate
    must converge to ~0.5. If it doesn't, the 'velocity' it reports is not the
    trend it claims to track and the long/short signal is meaningless."""
    prices = _trend_series(600, slope=0.5, sigma=1.0, seed=1)
    filt = _model1_filter(prices[0])
    _, vels = _run(filt, prices)
    settled = vels[-150:].mean()
    assert abs(settled - 0.5) < 0.12, f"velocity settled at {settled}, want ~0.5"


# ──────────────────────────────────────────────────────────────────────────
# 2. One-step forecast beats a same-lag SMA (the paper's core claim)
# ──────────────────────────────────────────────────────────────────────────
def test_forecast_beats_lagging_sma():
    """The paper's whole point: a Kalman one-step trend forecast lags less than
    a moving average. On a trending+noisy series, the filter's forecast of the
    NEXT close must have lower MSE than an SMA(10) used as the same forecast. A
    lagging predictor (which is what an SMA is) must lose here."""
    d = 10
    prices = _trend_series(500, slope=0.4, sigma=1.5, seed=7)
    filt = _model1_filter(prices[0])
    preds, _ = _run(filt, prices)

    # prediction at t forecasts close_{t+1}; align and skip the SMA warm-up.
    actual_next = prices[1:]
    kf_fore = preds[:-1]
    sma = np.array([prices[max(0, t - d + 1):t + 1].mean() for t in range(len(prices))])
    sma_fore = sma[:-1]
    j = d  # skip warm-up where the SMA window isn't full
    kf_mse = np.mean((kf_fore[j:] - actual_next[j:]) ** 2)
    sma_mse = np.mean((sma_fore[j:] - actual_next[j:]) ** 2)
    assert kf_mse < sma_mse, f"KF MSE {kf_mse:.3f} not < SMA MSE {sma_mse:.3f}"


# ──────────────────────────────────────────────────────────────────────────
# 3. Adapts when the trend reverses (a frozen-velocity estimate would not)
# ──────────────────────────────────────────────────────────────────────────
def test_tracks_trend_reversal():
    """Up for 250 bars then down for 250. The filter's velocity must flip from
    clearly positive to negative within N bars of the reversal. A model that
    froze the velocity at its pre-reversal (positive) value would keep
    forecasting up — which is exactly the failure this guards against."""
    up = _trend_series(250, slope=0.5, sigma=0.8, seed=3)
    down = up[-1] + (-0.5) * np.arange(1, 251) + np.random.default_rng(4).normal(0, 0.8, 250)
    prices = np.concatenate([up, down])
    filt = _model1_filter(prices[0])
    _, vels = _run(filt, prices)

    pre = vels[240:250].mean()
    assert pre > 0.2, f"pre-reversal velocity {pre} should be clearly positive"
    # first bar after the reversal where velocity goes negative
    after = vels[250:]
    flipped = np.argmax(after < 0) if np.any(after < 0) else len(after)
    assert flipped < 40, f"velocity flipped only after {flipped} bars (>40)"
    # a frozen-velocity extrapolation would still point up at the flip point —
    # confirms the live filter genuinely adapted rather than the data being easy.
    assert pre > 0 and after[flipped] < 0


# ──────────────────────────────────────────────────────────────────────────
# 4. Strictly causal — the forecast uses no future data
# ──────────────────────────────────────────────────────────────────────────
def test_forecast_is_causal_no_lookahead():
    """The forecast returned at step k must depend only on observations ≤ k.
    Running the filter on the truncated series prices[:k+1] must reproduce the
    exact same k-th forecast as running on the full series — future bars cannot
    leak into a past prediction."""
    prices = _trend_series(120, slope=0.3, sigma=1.0, seed=9)
    full = _model1_filter(prices[0])
    preds_full, _ = _run(full, prices)
    for k in (10, 50, 99):
        trunc = _model1_filter(prices[0])
        preds_trunc, _ = _run(trunc, prices[:k + 1])
        assert preds_trunc[-1] == pytest.approx(preds_full[k], rel=0, abs=0)


# ──────────────────────────────────────────────────────────────────────────
# 5. serialize → deserialize preserves the FULL state
# ──────────────────────────────────────────────────────────────────────────
def test_serialize_restore_is_identity():
    """Restart fidelity: persisting and restoring must reproduce byte-identical
    subsequent forecasts. Restoring only the level (not the covariance P) would
    reset the filter's uncertainty and diverge — so we prove identity through
    the next several steps, which depend on P via the Kalman gain."""
    prices = _trend_series(200, slope=0.45, sigma=1.0, seed=11)
    filt = _model1_filter(prices[0])
    _run(filt, prices[:120])

    restored = KalmanTrendFilter.deserialize(filt.serialize())
    for z in prices[120:140]:
        a = filt.update(float(z))
        b = restored.update(float(z))
        assert a == b


# ──────────────────────────────────────────────────────────────────────────
# 6. Degenerate input / invalid specs fail loud
# ──────────────────────────────────────────────────────────────────────────
def test_nan_observation_fails_loud():
    filt = _model1_filter(100.0)
    filt.update(100.0)
    with pytest.raises(ValueError):
        filt.update(float("nan"))


def test_indefinite_Q_fails_loud():
    """Table-1's Q parameterization is not guaranteed PSD; an indefinite Q must
    be rejected at construction, not silently filtered with a bad covariance."""
    with pytest.raises(ValueError, match="PSD"):
        KalmanTrendFilter(
            F=np.eye(2), H=np.array([1.0, 0.0]),
            Q=np.array([[1.0, 5.0], [5.0, 1.0]]),  # eigenvalues 6, -4
            R=1.0, x0=np.array([100.0, 0.0]), P0=np.eye(2),
        )


def test_negative_R_fails_loud():
    with pytest.raises(ValueError, match="R must"):
        KalmanTrendFilter(
            F=np.eye(2), H=np.array([1.0, 0.0]), Q=np.eye(2),
            R=-1.0, x0=np.array([100.0, 0.0]), P0=np.eye(2),
        )


def test_from_params_validation():
    # unknown model
    with pytest.raises(ValueError, match="model must"):
        KalmanTrendFilter.from_params([1, 0, 1, 1, 1], model=5, init_price=100.0)
    # too few params for model 3
    with pytest.raises(ValueError, match="needs"):
        KalmanTrendFilter.from_params([1, 0, 1, 1, 1], model=3, init_price=100.0)
    # model 4 with a nonzero control term is refused until Kt is verified
    p = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0,
         0.0, 0.0, 0.0, 7.0]  # p15 != 0
    with pytest.raises(NotImplementedError, match="control term"):
        KalmanTrendFilter.from_params(p, model=4, init_price=100.0)


def test_covariance_stays_psd_over_long_run():
    """Joseph-form update must keep P positive-semidefinite over a long session
    so restart never fails — the short form P−K(HP) drifts indefinite."""
    rng = np.random.default_rng(2)
    prices = 100 + np.cumsum(rng.normal(0, 1.0, 2000))
    f = _model1_filter(prices[0], q_level=1e-2, q_vel=1e-4, R=1.0, P0=100.0)
    for z in prices:
        f.update(float(z))
        assert np.linalg.eigvalsh(0.5 * (f.P + f.P.T)).min() >= -1e-12
    # and the drifted state still round-trips through deserialize without repair
    g = KalmanTrendFilter.deserialize(f.serialize())
    assert np.isfinite(g.update(float(prices[-1] + 1)).prediction)


def test_inflate_uncertainty_scales_P():
    f = _model1_filter(100.0)
    f.update(100.0)
    f.update(101.0)
    P0 = f.P.copy()
    f.inflate_uncertainty(100.0)
    assert np.allclose(f.P, P0 * 100.0)


def test_deserialize_repairs_tiny_psd_drift():
    """The short-form covariance update can drift slightly indefinite over a long
    session; restart must repair tiny drift, not crash."""
    f = _model1_filter(100.0)
    f.update(100.0)
    blob = f.serialize()
    blob["P"] = [[1.0, 0.0], [0.0, -1e-9]]      # drifted just below the PSD floor
    g = KalmanTrendFilter.deserialize(blob)     # must NOT raise (repaired)
    assert np.isfinite(g.update(101.0).prediction)


def test_deserialize_still_rejects_real_corruption():
    f = _model1_filter(100.0)
    f.update(100.0)
    blob = f.serialize()
    blob["P"] = [[1.0, 0.0], [0.0, -5.0]]       # genuine corruption, not drift
    with pytest.raises(ValueError, match="PSD"):
        KalmanTrendFilter.deserialize(blob)


def test_model4_zero_control_equals_model3():
    """model 4 with p12..p15 == 0 must behave exactly like model 3 (its optimum
    drives the control term to zero — Table 2). Uses benign, stable params: the
    paper's literal Table-2 optimum (Φ=[[24.8,0],[0,11.8]]) is an explosive
    transition that diverges (see test_paper_table2_optimum_diverges), so it is
    unfit for a structural-equivalence check."""
    # p1=1,p2=1,p3=1 → Φ=[[1,1],[0,1]] (Newtonian); p4=1,p5=0 → H=[1,0];
    # p6=0.1,p7=0,p8=0.1 → small Q; p9=1 → R; p10=10,p11=1 → P0.
    base = [1.0, 1.0, 1.0, 1.0, 0.0, 0.1, 0.0, 0.1, 1.0, 10.0, 1.0]
    f3 = KalmanTrendFilter.from_params(base, model=3, init_price=2500.0)
    f4 = KalmanTrendFilter.from_params(base + [0.0, 0.0, 0.0, 0.0],
                                       model=4, init_price=2500.0)
    rng = np.random.default_rng(0)
    prices = 2500 + np.cumsum(rng.normal(0, 5, 100))
    for z in prices:
        assert f3.update(float(z)) == f4.update(float(z))


def test_paper_table2_optimum_is_unusable():
    """Documents a real finding (Rule 12): the paper's reported optimal vector
    (Table 2) specifies Φ=[[24.8,0],[0,11.8]], which multiplies the position
    ~24.8x/bar. The Joseph-form filter no longer DIVERGES numerically on it (the
    old short-form 'divergence' was a covariance breakdown), but it is still an
    UNUSABLE model — its one-step predictions wander orders of magnitude off the
    actual ~2500 price. The OCR'd 15-d optimum is almost certainly mis-
    transcribed, and the fits never use model 3/4 anyway
    (test_fit_kalman_rejects_unstable_models)."""
    table2 = [24.8, 0.0, 11.8, 46.2, 77.5, 67.0, 100.0, 0.0, 0.0, 0.0, 100.0]
    filt = KalmanTrendFilter.from_params(table2, model=3, init_price=2500.0)
    prices = 2500 + np.cumsum(np.random.default_rng(0).normal(0, 5, 200))
    preds = np.array([filt.update(float(z)).prediction for z in prices])
    assert np.all(np.isfinite(preds))          # stable (Joseph form), but…
    assert np.max(np.abs(preds)) > 1e6         # …predictions detached from price
