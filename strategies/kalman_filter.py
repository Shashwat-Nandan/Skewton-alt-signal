"""
Kalman-Filter Hedge-Ratio Tracker for Pairs Trading
====================================================
Pure, I/O-free implementation of the time-varying spread model from Palomar,
*Portfolio Optimization* (2025), Chapter 15 §15.6 ("Kalman Filtering for Pairs
Trading"). This is the Phase-0 core of the separate Kalman pair system (see
tasks/kalman-pair-system-plan.md) — no Kite, no disk, no logging, so it is
fully unit-testable and reusable by the backtest, the paper runner, and tests.

The model
---------
We track  y1_t ≈ μ_t + γ_t · y2_t  where μ_t (intercept) and γ_t (hedge ratio)
drift slowly over time. Cast as a linear-Gaussian state-space model (§4.2):

    observation:  y1_t = Z_t α_t + ε_t,     ε_t ~ N(0, H),   H = σ²_ε
    state:        α_{t+1} = T α_t + η_t,     η_t ~ N(0, Q)

Two state designs from the book (selectable via `model=`):

  • "basic" — Eq. (15.3): α_t = (μ_t, γ_t), T = I₂, Z_t = [1, y2_t].
  • "momentum" — Eq. (15.4): α_t = (μ_t, γ_t, γ̇_t) adds a hedge-ratio
    velocity, T = [[1,0,0],[0,1,1],[0,0,1]], Z_t = [1, y2_t, 0]. The random
    walk drives the *velocity* γ̇ rather than γ directly, which makes γ_t
    smoother — the book shows this gives a better (more stationary) spread.

Normalized spread (leverage one), using the *predicted* state α_{t|t-1} so it
is strictly causal — no look-ahead (§15.6.3). Normalized by gross leverage
1+|γ| rather than the book's signed 1+γ: identical for γ>0, but well-defined
and non-degenerate for inversely-cointegrated pairs (γ<0, where signed 1+γ→0
near γ=−1 inflates the spread):

    z_t = ( y1_t − γ_{t|t-1}·y2_t − μ_{t|t-1} ) / (1 + |γ_{t|t-1}|)

Note z_t is the (scaled) one-step prediction error: y1_t − Z_t α_{t|t-1} is the
Kalman innovation v_t, and z_t = v_t / (1 + |γ_{t|t-1}|). This `z_t` is a price-
like *spread*, NOT a z-score — the trading z-score is obtained downstream by
rolling-window standardization of the z_t series (§15.6.4 uses a six-month
lookback). The filter also returns the standardized innovation v_t/√F_t as an
alternative, fully-causal signal to evaluate later.

Parameter init (§15.6.3 heuristic)
-----------------------------------
From an initial training window of T_LS samples, fit μ^LS, γ^LS by least
squares and take the residual ε^LS:

    σ²_ε = Var[ε^LS]
    Var[μ₁] = (1/T_LS)·Var[ε^LS]                 (initial state covariance)
    Var[γ₁] = (1/T_LS)·Var[ε^LS] / Var[y2]
    σ²_μ = α · Var[ε^LS]                          (process noise)
    σ²_γ = α · Var[ε^LS] / Var[y2]

where the hyper-parameter α sets the ratio of hidden-state drift to spread
variability. Book defaults: α = 1e-5 (basic), α = 1e-6 (momentum). Caveat from
the book (p. 437): too large an α shrinks the spread variance until profit
vanishes after costs; too small and γ_t cannot adapt.

The caller chooses whether y1/y2 are prices or log-prices — the filter is
agnostic. (The book's experiments use log-prices.)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Model = Literal["basic", "momentum"]

# Book defaults for the process-noise hyper-parameter (§15.6.4).
DEFAULT_ALPHA = {"basic": 1e-5, "momentum": 1e-6}

# Tiny floor on the γ-diagonal of Q in the momentum model. The book moves the
# random walk onto the velocity γ̇ (that is what smooths γ), so γ itself has no
# direct process noise; a small floor keeps Q strictly positive for numerical
# stability without materially adding noise to γ.
_GAMMA_Q_FLOOR = 1e-12


@dataclass(frozen=True)
class KalmanStep:
    """One filter step's outputs, all causal (computed from observations ≤ t).

    Predicted state is α_{t|t-1} (used to form the spread, no look-ahead);
    filtered state is α_{t|t} (after incorporating y_t)."""
    mu_pred: float        # μ_{t|t-1}
    gamma_pred: float     # γ_{t|t-1}  — the tradeable, causal hedge ratio
    mu_filt: float        # μ_{t|t}
    gamma_filt: float     # γ_{t|t}
    spread: float         # z_t, normalized spread (leverage one), per §15.6.3
    innovation: float     # v_t = y1_t − Z_t α_{t|t-1}
    innovation_var: float # F_t = Z_t P_{t|t-1} Z_tᵀ + H
    std_innovation: float # v_t / √F_t  — alternative causal z-score


class KalmanPairFilter:
    """Online Kalman filter tracking (μ_t, γ_t[, γ̇_t]) for a single pair.

    Construct via `from_training(...)` to use the book's §15.6.3 heuristic, or
    pass the state-space parameters directly. Then feed observations one at a
    time with `update(y1, y2)`; each call advances the causal forward pass
    (Durbin & Koopman 2012) and returns a `KalmanStep`.
    """

    def __init__(
        self,
        *,
        model: Model,
        a1: np.ndarray,        # initial predicted state α_{1|0}, shape (n,)
        P1: np.ndarray,        # initial state covariance, shape (n, n)
        H: float,              # observation noise variance σ²_ε
        Q: np.ndarray,         # state noise covariance, shape (n, n)
    ) -> None:
        if model not in ("basic", "momentum"):
            raise ValueError(f"unknown model {model!r} (want 'basic'/'momentum')")
        n = 2 if model == "basic" else 3
        a1 = np.asarray(a1, dtype=float).reshape(-1)
        P1 = np.asarray(P1, dtype=float)
        Q = np.asarray(Q, dtype=float)
        if a1.shape != (n,) or P1.shape != (n, n) or Q.shape != (n, n):
            raise ValueError(
                f"shape mismatch for model {model!r}: expected a1 ({n},), "
                f"P1/Q ({n},{n}); got {a1.shape}, {P1.shape}, {Q.shape}"
            )
        if not (np.isfinite(H) and H > 0):
            raise ValueError(f"H (σ²_ε) must be finite and > 0, got {H}")
        if not (np.all(np.isfinite(a1)) and np.all(np.isfinite(P1))
                and np.all(np.isfinite(Q))):
            raise ValueError("non-finite value in a1/P1/Q")

        self.model = model
        self.n = n
        # State transition T.
        if model == "basic":
            self.T = np.eye(2)
        else:
            self.T = np.array([[1.0, 0.0, 0.0],
                               [0.0, 1.0, 1.0],
                               [0.0, 0.0, 1.0]])
        self.H = float(H)
        self.Q = Q.copy()
        # Predicted state/covariance α_{t|t-1}, P_{t|t-1}. At t=1 these are the
        # priors α_{1|0}=a1, P_{1|0}=P1 (D&K convention).
        self.a = a1.copy()
        self.P = P1.copy()
        self.t = 0

    # ──────────────────────────────────────────────────────────────────
    # Construction from a training window (book §15.6.3 heuristic)
    # ──────────────────────────────────────────────────────────────────
    @classmethod
    def from_training(
        cls,
        y1_train: np.ndarray,
        y2_train: np.ndarray,
        *,
        model: Model = "momentum",
        alpha: float | None = None,
    ) -> "KalmanPairFilter":
        """Initialize via OLS on a training window, per §15.6.3.

        Fail loud (ValueError) on degenerate input — too few samples, NaNs, or
        zero variance in y2 / the residual — rather than silently producing a
        filter that cannot track (CLAUDE.md Rule 12)."""
        y1 = np.asarray(y1_train, dtype=float).reshape(-1)
        y2 = np.asarray(y2_train, dtype=float).reshape(-1)
        if y1.shape != y2.shape:
            raise ValueError(f"y1/y2 length mismatch: {y1.shape} vs {y2.shape}")
        T_LS = y1.size
        if T_LS < 3:
            raise ValueError(f"need >= 3 training samples, got {T_LS}")
        if not (np.all(np.isfinite(y1)) and np.all(np.isfinite(y2))):
            raise ValueError("non-finite value in training series")
        if model is None:
            model = "momentum"
        if alpha is None:
            alpha = DEFAULT_ALPHA[model]
        if not (np.isfinite(alpha) and alpha > 0):
            raise ValueError(f"alpha must be finite and > 0, got {alpha}")

        var_y2 = float(np.var(y2))
        if var_y2 <= 0:
            raise ValueError("Var[y2] is zero — y2 is constant, cannot fit γ")

        # OLS: y1 = μ + γ·y2 + ε  (lstsq keeps the module numpy-only).
        X = np.column_stack([np.ones(T_LS), y2])
        beta, *_ = np.linalg.lstsq(X, y1, rcond=None)
        mu_ls, gamma_ls = float(beta[0]), float(beta[1])
        resid = y1 - X @ beta
        sigma2_eps = float(np.var(resid))
        # Reject (near-)collinear pairs. A residual variance vanishingly small
        # relative to y1's scale means H ≈ 0, making the filter absurdly
        # overconfident; exact collinearity also leaves only float noise
        # (~1e-28), so an absolute `<= 0` check is not enough. Real cointegrated
        # pairs have residual variance many orders above this relative floor.
        if not (sigma2_eps > 1e-10 * max(float(np.var(y1)), 1e-300)):
            raise ValueError(
                "residual variance is ~0 relative to y1 — series are collinear"
            )

        # Process noise (§15.6.3).
        sigma2_mu = alpha * sigma2_eps
        sigma2_gamma = alpha * sigma2_eps / var_y2
        # Initial state covariance (§15.6.3): variance of the OLS estimates.
        var_mu1 = sigma2_eps / T_LS
        var_gamma1 = (sigma2_eps / T_LS) / var_y2

        if model == "basic":
            a1 = np.array([mu_ls, gamma_ls])
            P1 = np.diag([var_mu1, var_gamma1])
            Q = np.diag([sigma2_mu, sigma2_gamma])
        else:  # momentum: state (μ, γ, γ̇), random walk on the velocity γ̇
            a1 = np.array([mu_ls, gamma_ls, 0.0])
            P1 = np.diag([var_mu1, var_gamma1, sigma2_gamma])
            Q = np.diag([sigma2_mu, _GAMMA_Q_FLOOR, sigma2_gamma])

        return cls(model=model, a1=a1, P1=P1, H=sigma2_eps, Q=Q)

    # ──────────────────────────────────────────────────────────────────
    # Forward-pass recursion (Durbin & Koopman 2012, §4.2)
    # ──────────────────────────────────────────────────────────────────
    def update(self, y1: float, y2: float) -> KalmanStep:
        """Incorporate one observation (y1, y2) and advance the filter.

        Order matches the causal forward pass: form the innovation/spread from
        the *predicted* state α_{t|t-1}, update to α_{t|t}, then predict
        α_{t+1|t} for the next call.
        """
        if not (np.isfinite(y1) and np.isfinite(y2)):
            raise ValueError(f"non-finite observation (y1={y1}, y2={y2})")

        # Observation row Z_t = [1, y2] (basic) or [1, y2, 0] (momentum).
        Z = np.zeros(self.n)
        Z[0] = 1.0
        Z[1] = y2

        a_pred = self.a          # α_{t|t-1}
        P_pred = self.P          # P_{t|t-1}
        mu_pred = float(a_pred[0])
        gamma_pred = float(a_pred[1])

        # Innovation and its variance (scalar observation).
        v = float(y1 - Z @ a_pred)                 # v_t
        F = float(Z @ P_pred @ Z + self.H)         # F_t > 0 since H > 0
        std_innov = v / np.sqrt(F)

        # Normalized spread (leverage one), predicted-state form: v / gross
        # leverage. Gross leverage is 1+|γ| (long A + |γ|·B, same or opposite
        # side depending on sign of γ). Using |γ| rather than signed γ makes
        # this identical to the book's 1+γ for γ>0, but well-defined and
        # non-degenerate for inversely-cointegrated pairs (γ<0) — for γ near −1
        # the signed 1+γ vanishes and inflates the spread arbitrarily.
        denom = 1.0 + abs(gamma_pred)
        spread = v / denom if denom > 1e-8 else float("nan")

        # Measurement update: α_{t|t}, P_{t|t}.
        K = (P_pred @ Z) / F                        # Kalman gain, shape (n,)
        a_filt = a_pred + K * v
        P_filt = P_pred - np.outer(K, Z @ P_pred)
        # Symmetrize to curb floating-point drift in P.
        P_filt = 0.5 * (P_filt + P_filt.T)

        # Time update (prediction): α_{t+1|t}, P_{t+1|t}.
        self.a = self.T @ a_filt
        self.P = self.T @ P_filt @ self.T.T + self.Q
        self.t += 1

        return KalmanStep(
            mu_pred=mu_pred,
            gamma_pred=gamma_pred,
            mu_filt=float(a_filt[0]),
            gamma_filt=float(a_filt[1]),
            spread=spread,
            innovation=v,
            innovation_var=F,
            std_innovation=std_innov,
        )

    # ──────────────────────────────────────────────────────────────────
    # State serialization (for runner restart — Phase 1 needs the FULL
    # state, not just γ, or restarting resets the filter's uncertainty).
    # ──────────────────────────────────────────────────────────────────
    def serialize(self) -> dict:
        return {
            "model": self.model,
            "a": self.a.tolist(),
            "P": self.P.tolist(),
            "H": self.H,
            "Q": self.Q.tolist(),
            "t": self.t,
        }

    @classmethod
    def deserialize(cls, blob: dict) -> "KalmanPairFilter":
        f = cls(
            model=blob["model"],
            a1=np.asarray(blob["a"], dtype=float),
            P1=np.asarray(blob["P"], dtype=float),
            H=float(blob["H"]),
            Q=np.asarray(blob["Q"], dtype=float),
        )
        f.t = int(blob.get("t", 0))
        return f
