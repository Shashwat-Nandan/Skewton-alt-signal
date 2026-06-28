"""
Kalman-Filter Trend Detector (single instrument)
================================================
Pure, I/O-free implementation of the trend-detection state-space model from
Benhamou, *Kalman filter demystified* (hal-02012471 / arXiv 1811.11618), §6 and
Table 1. This is the Phase-0 core of the Kalman *trend-following* system (see
tasks/kalman-trend-system-plan.md) — no Kite, no disk, no logging, so it is
fully unit-testable and reusable by the optimizer, the backtest, the paper
runner and the tests.

This is a SEPARATE application from `strategies/kalman_filter.py` (the pairs
hedge-ratio tracker). Here the filter tracks the *trend of one instrument*:
the latent state is (position, velocity) of the price itself, not a hedge ratio.

The model (paper Eq. 5.1 / 5.2)
-------------------------------
    state:        x_{t+1} = F x_t + c + w_t,   w_t ~ N(0, Q)      (5.1)
    observation:  z_t     = H x_t     + v_t,   v_t ~ N(0, R)      (5.2)

with the 2-D state x_t = (position, velocity). Table 1 gives four model
specifications, parameterized by p1…p15 (see `from_params`):

  model 1: F=[[1,dt],[0,1]],   H=[1,0], Q=L₁L₁ᵀ, R=p4, P0=diag(p5,p5),  c=0
  model 2: as 1 but P0=diag(p5,p6)
  model 3: F=[[p1,p2],[0,p3]], H=[p4,p5], Q=L₃L₃ᵀ, R=p9, P0=diag(p10,p11), c=0
  model 4: as 3 plus the control term c_t = (p12(p13−Kt), p14(p15−Kt))

Q is parameterized as a *Cholesky factor product* Q = L Lᵀ — L₁=[[p1,0],[p2,p3]],
L₃=[[p6,0],[p7,p8]] — so Q is positive-semidefinite by construction:

    Q₁ = [[p1², p1p2], [p1p2, p2²+p3²]] ,  Q₃ = [[p6², p6p7], [p6p7, p7²+p8²]]

Table 1 prints the (2,2) entry as p3²/p8², which makes the paper's OWN reported
optimum (Table 2: p6=67, p7=100, p8=0) an *indefinite* matrix — not a valid
covariance. Under the Cholesky reading that optimum is Q=[[4489,6700],[6700,
10000]] with det 0 exactly: a clean rank-1 PSD covariance, the degenerate corner
the L1 penalty (p8→0) drives toward. That det=0 coincidence makes clear the table
dropped the p7² term; the Cholesky form is the faithful, valid intent (Rule 1).

Model 4 (15 filter params) is the one the paper optimized; its optimum (Table 2)
drives the control term to zero, i.e. it collapses onto model 3. The Kt-dependent
form of c_t in Table 1 is not fully disambiguated by the OCR of the paper, so
this module implements c as a *constant* control vector and `from_params(model=4)`
requires p12…p15 == 0 (⇒ c=0, the optimum) — anything else fails loud until the
Kt term is verified against the PDF (Rule 8 / Rule 12). The filter itself accepts
an arbitrary constant `c` so the control term can be wired in later without a
rewrite.

The trend signal (paper Algorithm 4)
------------------------------------
Each step's `prediction` is the strictly-causal one-step-ahead forecast of the
*next* close, H·(F x_{t|t} + c) = H x_{t+1|t}, formed from observations ≤ t. The
strategy (Phase 1) compares it to the current close with a dead-band µ:

    prediction ≥ close_t + µ  → long ;   prediction ≤ close_t − µ  → short

so the decision for day t+1 uses only data through day t — no look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Eigenvalue floor below which a covariance is treated as indefinite (invalid).
# Q in Table 1's parameterization is NOT guaranteed PSD (det = p1²(p3²−p2²) can
# be negative), so the optimizer can propose invalid filters — we reject them
# loud and let the optimizer's penalty steer away (Rule 12).
_PSD_EIG_FLOOR = -1e-10


@dataclass(frozen=True)
class TrendStep:
    """One filter step's outputs, all causal (computed from observations ≤ t).

    `prediction` is the one-step-ahead forecast of the NEXT close H x_{t+1|t}
    (the tradeable, look-ahead-free signal). `level_pred`/`velocity_pred` are
    that same predicted next state x_{t+1|t}; `level_filt`/`velocity_filt` are
    the current filtered state x_{t|t}."""
    prediction: float       # H x_{t+1|t} — forecast of next close (the signal)
    level_pred: float       # x_{t+1|t}[0]
    velocity_pred: float    # x_{t+1|t}[1] — sign = trend direction
    level_filt: float       # x_{t|t}[0]
    velocity_filt: float    # x_{t|t}[1]
    innovation: float       # v_t = z_t − H x_{t|t-1}
    innovation_var: float   # F_t = H P_{t|t-1} Hᵀ + R
    std_innovation: float   # v_t / √F_t


def _check_psd(M: np.ndarray, name: str) -> None:
    """Fail loud unless M is symmetric positive-semidefinite."""
    if not np.allclose(M, M.T, atol=1e-12):
        raise ValueError(f"{name} is not symmetric:\n{M}")
    w = np.linalg.eigvalsh(0.5 * (M + M.T))
    if w.min() < _PSD_EIG_FLOOR:
        raise ValueError(f"{name} is not PSD (min eigenvalue {w.min():.3e}):\n{M}")


# Tolerance below which a negative eigenvalue is treated as float drift to be
# repaired (the short-form covariance update loses PSD-ness by ~1e-9 over a long
# session); anything more negative is genuine corruption and is left to fail loud.
_PSD_REPAIR_TOL = -1e-6


def _repair_psd(M: np.ndarray) -> np.ndarray:
    """Symmetrize and clamp TINY negative eigenvalues (drift) to 0; pass real
    corruption through unchanged so the constructor's _check_psd still rejects it."""
    M = 0.5 * (M + M.T)
    w, V = np.linalg.eigh(M)
    if _PSD_REPAIR_TOL <= w.min() < 0.0:
        M = (V * np.clip(w, 0.0, None)) @ V.T
        M = 0.5 * (M + M.T)
    return M


class KalmanTrendFilter:
    """Online Kalman filter tracking (position, velocity) of one price series.

    Construct from explicit matrices, or via `from_params(p, model=…)` to use the
    Table-1 parameterization the paper optimizes. Feed observations one at a time
    with `update(close)`; each call advances the causal forward pass (Durbin &
    Koopman 2012, §4.2) and returns a `TrendStep`.
    """

    def __init__(
        self,
        *,
        F: np.ndarray,      # state transition, (2, 2)
        H: np.ndarray,      # observation row, (2,)
        Q: np.ndarray,      # state noise covariance, (2, 2)
        R: float,           # observation noise variance, scalar >= 0
        x0: np.ndarray,     # initial predicted state x_{1|0}, (2,)
        P0: np.ndarray,     # initial state covariance P_{1|0}, (2, 2)
        c: np.ndarray | None = None,   # constant control term, (2,)
        model: int | None = None,      # provenance only (1..4), optional
    ) -> None:
        F = np.asarray(F, float)
        H = np.asarray(H, float).reshape(-1)
        Q = np.asarray(Q, float)
        P0 = np.asarray(P0, float)
        x0 = np.asarray(x0, float).reshape(-1)
        c = np.zeros(2) if c is None else np.asarray(c, float).reshape(-1)
        for nm, M, shape in (("F", F, (2, 2)), ("Q", Q, (2, 2)), ("P0", P0, (2, 2)),
                             ("H", H, (2,)), ("x0", x0, (2,)), ("c", c, (2,))):
            if M.shape != shape:
                raise ValueError(f"{nm} must have shape {shape}, got {M.shape}")
        for nm, M in (("F", F), ("H", H), ("Q", Q), ("P0", P0), ("x0", x0), ("c", c)):
            if not np.all(np.isfinite(M)):
                raise ValueError(f"non-finite value in {nm}")
        if not (np.isfinite(R) and R >= 0):
            raise ValueError(f"R must be finite and >= 0, got {R}")
        _check_psd(Q, "Q")
        _check_psd(P0, "P0")

        self.F = F.copy()
        self.H = H.copy()
        self.Q = Q.copy()
        self.R = float(R)
        self.c = c.copy()
        self.model = model
        # Predicted state/cov x_{t|t-1}, P_{t|t-1}. At t=1 these are the priors.
        self.x = x0.copy()
        self.P = P0.copy()
        self.t = 0

    # ──────────────────────────────────────────────────────────────────
    # Table-1 parameterization (paper §5 / Table 1)
    # ──────────────────────────────────────────────────────────────────
    @classmethod
    def from_params(
        cls,
        p,
        *,
        model: int,
        init_price: float,
        dt: float = 1.0,
    ) -> "KalmanTrendFilter":
        """Build a filter from the paper's p1…p15 vector for `model` ∈ {1,2,3,4}.

        `p` is 0-indexed: p[0]=p1, … . The initial state is (init_price, 0):
        level starts at the first observed close, velocity at zero. Fails loud
        on the wrong number of params or an unimplemented control term."""
        p = np.asarray(p, float).reshape(-1)
        if not np.all(np.isfinite(p)):
            raise ValueError("non-finite value in parameter vector p")
        if not (np.isfinite(init_price)):
            raise ValueError(f"init_price must be finite, got {init_price}")
        need = {1: 5, 2: 6, 3: 11, 4: 15}
        if model not in need:
            raise ValueError(f"model must be 1, 2, 3 or 4, got {model}")
        if p.size < need[model]:
            raise ValueError(
                f"model {model} needs >= {need[model]} params, got {p.size}")
        x0 = np.array([init_price, 0.0])

        if model in (1, 2):
            p1, p2, p3, p4, p5 = p[0], p[1], p[2], p[3], p[4]
            F = np.array([[1.0, dt], [0.0, 1.0]])
            H = np.array([1.0, 0.0])
            # Q = L Lᵀ, L=[[p1,0],[p2,p3]] — PSD by construction (see docstring).
            Q = np.array([[p1 * p1, p1 * p2], [p1 * p2, p2 * p2 + p3 * p3]])
            R = p4
            p6 = p[5] if model == 2 else p5
            P0 = np.diag([p5, p6])
            c = np.zeros(2)
        else:  # models 3, 4
            p1, p2, p3, p4, p5 = p[0], p[1], p[2], p[3], p[4]
            p6, p7, p8, p9 = p[5], p[6], p[7], p[8]
            p10, p11 = p[9], p[10]
            F = np.array([[p1, p2], [0.0, p3]])
            H = np.array([p4, p5])
            # Q = L Lᵀ, L=[[p6,0],[p7,p8]] — PSD by construction (see docstring).
            Q = np.array([[p6 * p6, p6 * p7], [p6 * p7, p7 * p7 + p8 * p8]])
            R = p9
            P0 = np.diag([p10, p11])
            c = np.zeros(2)
            if model == 4 and np.any(p[11:15] != 0.0):
                # Table 1's c_t = (p12(p13−Kt), p14(p15−Kt)) depends on an
                # under-specified Kt; refuse a nonzero control term rather than
                # implement a guessed form. The paper's optimum has p12..p15=0.
                raise NotImplementedError(
                    "model 4 control term (p12..p15 != 0) needs the Kt term "
                    "verified against the PDF; the paper's optimum sets these "
                    "to zero (use model 3 or model 4 with p12..p15 == 0)")
        return cls(F=F, H=H, Q=Q, R=R, x0=x0, P0=P0, c=c, model=model)

    # ──────────────────────────────────────────────────────────────────
    # Forward-pass recursion (Durbin & Koopman 2012, §4.2)
    # ──────────────────────────────────────────────────────────────────
    def update(self, z: float) -> TrendStep:
        """Incorporate one observation (close `z`) and advance the filter.

        Forms the innovation from the *predicted* state x_{t|t-1}, updates to
        x_{t|t}, then predicts x_{t+1|t} for the next call. The returned
        `prediction` (= H x_{t+1|t}) is the look-ahead-free forecast of the next
        close used by the trading signal.
        """
        if not np.isfinite(z):
            raise ValueError(f"non-finite observation z={z}")

        x_pred = self.x        # x_{t|t-1}
        P_pred = self.P        # P_{t|t-1}

        # Innovation and its variance (scalar observation).
        v = float(z - self.H @ x_pred)
        F_inn = float(self.H @ P_pred @ self.H + self.R)
        if not (F_inn > 0):
            raise ValueError(
                f"innovation variance F_t={F_inn} is not > 0 (degenerate "
                "H/P/R); filter cannot update")
        std_innov = v / np.sqrt(F_inn)

        # Measurement update: x_{t|t}, P_{t|t}.
        K = (P_pred @ self.H) / F_inn          # Kalman gain, (2,)
        x_filt = x_pred + K * v
        P_filt = P_pred - np.outer(K, self.H @ P_pred)
        P_filt = 0.5 * (P_filt + P_filt.T)     # curb float drift

        # Time update (prediction): x_{t+1|t}, P_{t+1|t}.
        x_next = self.F @ x_filt + self.c
        P_next = self.F @ P_filt @ self.F.T + self.Q
        P_next = 0.5 * (P_next + P_next.T)

        self.x = x_next
        self.P = P_next
        self.t += 1

        return TrendStep(
            prediction=float(self.H @ x_next),
            level_pred=float(x_next[0]),
            velocity_pred=float(x_next[1]),
            level_filt=float(x_filt[0]),
            velocity_filt=float(x_filt[1]),
            innovation=v,
            innovation_var=F_inn,
            std_innovation=std_innov,
        )

    def inflate_uncertainty(self, factor: float = 100.0) -> None:
        """Scale up the predicted state covariance P — call across a discontinuity
        (e.g. an overnight gap for an intraday filter) so the next observation
        gets a high Kalman gain and the jump is absorbed into the level instead
        of being read as one bar of velocity. `factor` controls how much trust to
        drop; the covariance is re-symmetrized to stay clean."""
        if not (np.isfinite(factor) and factor > 0):
            raise ValueError(f"inflate factor must be finite and > 0, got {factor}")
        self.P = 0.5 * (self.P + self.P.T) * factor

    # ──────────────────────────────────────────────────────────────────
    # State serialization (for runner restart — needs the FULL state, not
    # just the level, or restarting resets the filter's uncertainty).
    # ──────────────────────────────────────────────────────────────────
    def serialize(self) -> dict:
        return {
            "F": self.F.tolist(),
            "H": self.H.tolist(),
            "Q": self.Q.tolist(),
            "R": self.R,
            "c": self.c.tolist(),
            "x": self.x.tolist(),
            "P": self.P.tolist(),
            "t": self.t,
            "model": self.model,
        }

    @classmethod
    def deserialize(cls, blob: dict) -> "KalmanTrendFilter":
        f = cls(
            F=np.asarray(blob["F"], float),
            H=np.asarray(blob["H"], float),
            Q=np.asarray(blob["Q"], float),
            R=float(blob["R"]),
            x0=np.asarray(blob["x"], float),
            # Repair tiny PSD drift accumulated by the short-form covariance
            # update over a long session, so restart-from-state doesn't crash on
            # a P that drifted to e.g. min-eig -3e-9 (genuine corruption still
            # fails loud via the constructor's _check_psd).
            P0=_repair_psd(np.asarray(blob["P"], float)),
            c=np.asarray(blob.get("c", [0.0, 0.0]), float),
            model=blob.get("model"),
        )
        f.t = int(blob.get("t", 0))
        return f
