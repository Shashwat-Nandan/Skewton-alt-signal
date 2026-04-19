"""
Greeks Engine v2 — Full Taleb Dynamic Hedging Implementation
=============================================================
All 22 gaps from reading Ch 7-11, 16 addressed.
See SKILL.md for the complete gap-to-implementation mapping.
"""

import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Literal, Callable, Dict, Tuple

import numpy as np
from scipy.stats import norm

logger = logging.getLogger(__name__)


@dataclass
class OptionContract:
    tradingsymbol: str
    instrument_token: int
    strike: float
    expiry: str
    option_type: Literal["CE", "PE", "FUT"]
    lot_size: int
    quantity: int
    entry_price: float = 0.0
    current_price: float = 0.0
    iv: float = 0.0


@dataclass
class GreeksResult:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0
    discrete_delta: float = 0.0
    shadow_gamma: float = 0.0
    shadow_gamma_up: float = 0.0
    shadow_gamma_down: float = 0.0
    shadow_theta: float = 0.0
    alpha: float = 0.0
    charm: float = 0.0
    gamma_bleed: float = 0.0
    ddeltadvol: float = 0.0


@dataclass
class PortfolioGreeks:
    net_delta: float = 0.0
    net_discrete_delta: float = 0.0
    net_gamma: float = 0.0
    net_theta: float = 0.0
    net_vega: float = 0.0
    net_shadow_gamma: float = 0.0
    net_shadow_gamma_up: float = 0.0
    net_shadow_gamma_down: float = 0.0
    net_shadow_theta: float = 0.0
    net_charm: float = 0.0
    net_gamma_bleed: float = 0.0
    net_ddeltadvol: float = 0.0
    net_alpha: float = 0.0
    lock_delta_up: float = 0.0
    lock_delta_down: float = 0.0
    vega_identity_error: float = 0.0
    net_modified_vega: float = 0.0
    vega_buckets: Dict[str, float] = field(default_factory=dict)
    pnl_profile: Dict[float, float] = field(default_factory=dict)
    delta_profile: Dict[float, float] = field(default_factory=dict)
    gamma_profile: Dict[float, float] = field(default_factory=dict)
    moment_1: float = 0.0
    moment_2: float = 0.0
    moment_3: float = 0.0
    moment_4: float = 0.0
    position_count: int = 0
    positions: List[dict] = field(default_factory=list)


def default_indian_vol_scenario(base_vol: float, spot: float, new_spot: float) -> float:
    """Asymmetric vol-spot dependence for Indian equity markets (Taleb Ch8 Table 8.4)."""
    pct_move = (new_spot - spot) / spot * 100.0
    if pct_move <= -7:
        vol_shift = 10.0
    elif pct_move <= -5:
        vol_shift = 7.0
    elif pct_move <= -3:
        vol_shift = 4.0
    elif pct_move <= -2:
        vol_shift = 2.0
    elif pct_move <= -1:
        vol_shift = 1.0
    elif pct_move <= -0.5:
        vol_shift = 0.5
    elif pct_move <= 0.5:
        vol_shift = 0.0
    elif pct_move <= 1:
        vol_shift = -0.2
    elif pct_move <= 2:
        vol_shift = -0.5
    elif pct_move <= 3:
        vol_shift = 0.3
    elif pct_move <= 5:
        vol_shift = 1.5
    elif pct_move <= 7:
        vol_shift = 3.0
    else:
        vol_shift = 5.0
    return max(base_vol + vol_shift / 100.0, 0.03)


class GreeksEngine:
    def __init__(self, risk_free_rate=0.065, vol_scenario_fn=None, reference_maturity_days=90):
        self.r = risk_free_rate
        self.vol_at_price = vol_scenario_fn or default_indian_vol_scenario
        self.ref_maturity_days = reference_maturity_days

    @staticmethod
    def _d1(S, K, T, r, sigma):
        if T <= 0 or sigma <= 0: return 0.0
        return (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))

    @staticmethod
    def _d2(S, K, T, r, sigma):
        if T <= 0 or sigma <= 0: return 0.0
        return GreeksEngine._d1(S, K, T, r, sigma) - sigma * math.sqrt(T)

    def bs_price(self, S, K, T, sigma, option_type="CE"):
        if option_type == "FUT":
            return S  # Futures price ≈ spot (ignoring cost of carry for simplicity)
        if T <= 0:
            return max(S - K, 0) if option_type == "CE" else max(K - S, 0)
        d1, d2 = self._d1(S, K, T, self.r, sigma), self._d2(S, K, T, self.r, sigma)
        if option_type == "CE":
            return S * norm.cdf(d1) - K * math.exp(-self.r * T) * norm.cdf(d2)
        return K * math.exp(-self.r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    def delta(self, S, K, T, sigma, option_type):
        if option_type == "FUT":
            return 1.0
        if T <= 0 or sigma <= 0:
            return (1.0 if S > K else 0.0) if option_type == "CE" else (-1.0 if S < K else 0.0)
        d1 = self._d1(S, K, T, self.r, sigma)
        return norm.cdf(d1) if option_type == "CE" else norm.cdf(d1) - 1.0

    def gamma(self, S, K, T, sigma):
        if T <= 0 or sigma <= 0 or S <= 0: return 0.0
        d1 = self._d1(S, K, T, self.r, sigma)
        return norm.pdf(d1) / (S * sigma * math.sqrt(T))

    def theta(self, S, K, T, sigma, option_type):
        if option_type == "FUT": return 0.0
        if T <= 0 or sigma <= 0: return 0.0
        d1, d2 = self._d1(S, K, T, self.r, sigma), self._d2(S, K, T, self.r, sigma)
        common = -(S * norm.pdf(d1) * sigma) / (2 * math.sqrt(T))
        if option_type == "CE":
            val = common - self.r * K * math.exp(-self.r * T) * norm.cdf(d2)
        else:
            val = common + self.r * K * math.exp(-self.r * T) * norm.cdf(-d2)
        return val / 365.0

    def vega(self, S, K, T, sigma):
        if T <= 0 or sigma <= 0: return 0.0
        d1 = self._d1(S, K, T, self.r, sigma)
        return S * math.sqrt(T) * norm.pdf(d1) / 100.0

    # ── Taleb Extensions ──

    def discrete_delta(self, S, K, T, sigma, option_type, bump_pct=0.5):
        dS = S * bump_pct / 100.0
        return (self.bs_price(S+dS, K, T, sigma, option_type) - self.bs_price(S-dS, K, T, sigma, option_type)) / (2*dS)

    def shadow_gamma_directional(self, S, K, T, sigma, option_type, bump_pct=1.0):
        """Returns (combined, up, down) shadow gamma using vol scenario grid."""
        dS = S * bump_pct / 100.0
        sigma_up = self.vol_at_price(sigma, S, S + dS)
        sigma_down = self.vol_at_price(sigma, S, S - dS)
        d_cur = self.delta(S, K, T, sigma, option_type)
        d_up = self.delta(S + dS, K, T, sigma_up, option_type)
        d_down = self.delta(S - dS, K, T, sigma_down, option_type)
        return (d_up - d_down)/(2*dS), (d_up - d_cur)/dS, (d_cur - d_down)/dS

    def shadow_theta(self, S, K, T, sigma, option_type, vol_decay_per_day=0.001):
        reg_theta = self.theta(S, K, T, sigma, option_type)
        vega_raw = self.vega(S, K, T, sigma) * 100
        return reg_theta - abs(vega_raw) * vol_decay_per_day

    def alpha_gamma_rent(self, S, K, T, sigma, option_type):
        g = self.gamma(S, K, T, sigma)
        t = self.theta(S, K, T, sigma, option_type)
        return t / g if abs(g) > 1e-10 else 0.0

    def shadow_alpha(self, S, K, T, sigma, option_type):
        """Taleb p.179: Alpha using shadow gamma and shadow theta for accuracy."""
        sg_combined, _, _ = self.shadow_gamma_directional(S, K, T, sigma, option_type)
        st = self.shadow_theta(S, K, T, sigma, option_type)
        return st / sg_combined if abs(sg_combined) > 1e-10 else 0.0

    def charm(self, S, K, T, sigma, option_type):
        if T <= 1/365 or sigma <= 0: return 0.0
        dt = 1.0 / 365.0
        return self.delta(S, K, T-dt, sigma, option_type) - self.delta(S, K, T, sigma, option_type)

    def gamma_bleed(self, S, K, T, sigma):
        if T <= 1/365 or sigma <= 0: return 0.0
        dt = 1.0 / 365.0
        return self.gamma(S, K, T-dt, sigma) - self.gamma(S, K, T, sigma)

    def ddeltadvol(self, S, K, T, sigma, option_type, vol_bump=0.01):
        if T <= 0 or sigma <= 0: return 0.0
        return (self.delta(S, K, T, sigma+vol_bump, option_type) - self.delta(S, K, T, sigma, option_type)) / vol_bump

    def modified_vega_weight(self, days_to_expiry):
        if days_to_expiry <= 0: return 1.0
        return math.sqrt(self.ref_maturity_days / days_to_expiry)

    def vega_gamma_identity_check(self, S, K, T, sigma):
        v = self.vega(S, K, T, sigma) * 100
        g = self.gamma(S, K, T, sigma)
        identity = sigma * T * S * S * g
        return abs(v - identity) / abs(identity) * 100 if abs(identity) > 1e-10 else 0.0

    def correct_back_month_gamma(self, gamma_raw, front_vol, back_vol):
        return gamma_raw * front_vol / back_vol if back_vol > 0 else gamma_raw

    # ── Portfolio Level ──

    def compute_option_greeks(self, option, spot_price, T):
        S, K = spot_price, option.strike
        sigma = option.iv if option.iv > 0 else 0.20
        otype, m = option.option_type, option.quantity * option.lot_size
        sg_c, sg_u, sg_d = self.shadow_gamma_directional(S, K, T, sigma, otype)
        return GreeksResult(
            delta=self.delta(S,K,T,sigma,otype)*m, gamma=self.gamma(S,K,T,sigma)*m,
            theta=self.theta(S,K,T,sigma,otype)*m, vega=self.vega(S,K,T,sigma)*m,
            discrete_delta=self.discrete_delta(S,K,T,sigma,otype)*m,
            shadow_gamma=sg_c*m, shadow_gamma_up=sg_u*m, shadow_gamma_down=sg_d*m,
            shadow_theta=self.shadow_theta(S,K,T,sigma,otype)*m,
            alpha=self.shadow_alpha(S,K,T,sigma,otype),
            charm=self.charm(S,K,T,sigma,otype)*m,
            gamma_bleed=self.gamma_bleed(S,K,T,sigma)*m,
            ddeltadvol=self.ddeltadvol(S,K,T,sigma,otype)*m,
        )

    def compute_portfolio_greeks(self, positions, spot_price, T, price_range_pct=8.0, price_steps=33):
        pf = PortfolioGreeks()
        S = spot_price
        for opt in positions:
            g = self.compute_option_greeks(opt, S, T)
            pf.net_delta += g.delta; pf.net_discrete_delta += g.discrete_delta
            pf.net_gamma += g.gamma
            pf.net_theta += g.theta; pf.net_vega += g.vega
            pf.net_shadow_gamma += g.shadow_gamma
            pf.net_shadow_gamma_up += g.shadow_gamma_up
            pf.net_shadow_gamma_down += g.shadow_gamma_down
            pf.net_shadow_theta += g.shadow_theta
            pf.net_charm += g.charm
            pf.net_gamma_bleed += g.gamma_bleed
            pf.net_ddeltadvol += g.ddeltadvol
            days = max(T * 365, 1)
            pf.net_modified_vega += g.vega * self.modified_vega_weight(days)
            bucket = self._get_vega_bucket(days)
            pf.vega_buckets[bucket] = pf.vega_buckets.get(bucket, 0) + g.vega
            pf.position_count += 1
            pf.positions.append({
                "symbol": opt.tradingsymbol, "strike": opt.strike, "type": opt.option_type,
                "qty": opt.quantity, "delta": round(g.delta,2), "gamma": round(g.gamma,4),
                "sg_up": round(g.shadow_gamma_up,4), "sg_dn": round(g.shadow_gamma_down,4),
                "theta": round(g.theta,2), "s_theta": round(g.shadow_theta,2),
                "vega": round(g.vega,2), "charm": round(g.charm,4),
                "ddvol": round(g.ddeltadvol,4), "alpha": round(g.alpha,2),
            })
        if abs(pf.net_shadow_gamma) > 1e-10:
            pf.net_alpha = pf.net_shadow_theta / pf.net_shadow_gamma
        elif abs(pf.net_gamma) > 1e-10:
            pf.net_alpha = pf.net_theta / pf.net_gamma
        # P/L, delta, gamma profiles
        prices = np.linspace(S*(1-price_range_pct/100), S*(1+price_range_pct/100), price_steps)
        for p in prices:
            pnl, dlt, gam = 0.0, 0.0, 0.0
            for opt in positions:
                sig0 = opt.iv if opt.iv > 0 else 0.2
                sig_p = self.vol_at_price(sig0, S, p)
                pnl += (self.bs_price(p, opt.strike, T, sig_p, opt.option_type) - self.bs_price(S, opt.strike, T, sig0, opt.option_type)) * opt.quantity * opt.lot_size
                dlt += self.delta(p, opt.strike, T, sig_p, opt.option_type) * opt.quantity * opt.lot_size
                gam += self.gamma(p, opt.strike, T, sig_p) * opt.quantity * opt.lot_size
            pf.pnl_profile[round(p,2)] = round(pnl,2)
            pf.delta_profile[round(p,2)] = round(dlt,2)
            pf.gamma_profile[round(p,2)] = round(gam,4)
        # Lock delta
        pf.lock_delta_up = sum((1.0 if o.option_type=="CE" else 0)*o.quantity*o.lot_size for o in positions)
        pf.lock_delta_down = sum((-1.0 if o.option_type=="PE" else 0)*o.quantity*o.lot_size for o in positions)
        # Moments
        gvals = list(pf.gamma_profile.values())
        pkeys = list(pf.gamma_profile.keys())
        if len(gvals) > 4:
            pf.moment_1 = pf.net_delta; pf.moment_2 = pf.net_gamma
            dg = np.gradient(gvals, pkeys); ci = len(dg)//2
            pf.moment_3 = round(float(dg[ci]),6)
            d2g = np.gradient(dg, pkeys)
            pf.moment_4 = round(float(d2g[ci]),8)
        return pf

    def gamma_grid(self, positions, spot, T, range_pct=5.0, steps=21):
        grid = {}
        for p in np.linspace(spot*(1-range_pct/100), spot*(1+range_pct/100), steps):
            g = sum(self.gamma(p, o.strike, T, self.vol_at_price(o.iv or 0.2, spot, p))*o.quantity*o.lot_size for o in positions)
            grid[round(p,2)] = round(g,4)
        return grid

    def find_gamma_flip_points(self, positions, spot, T, range_pct=5.0, steps=51):
        grid = self.gamma_grid(positions, spot, T, range_pct, steps)
        prices = sorted(grid.keys()); flips = []
        for i in range(1, len(prices)):
            if grid[prices[i-1]] * grid[prices[i]] < 0:
                flips.append((prices[i-1] + prices[i]) / 2.0)
        return flips

    @staticmethod
    def _get_vega_bucket(days):
        if days <= 30: return "0-30d"
        elif days <= 60: return "30-60d"
        elif days <= 90: return "60-90d"
        elif days <= 180: return "90-180d"
        return "180d+"


def implied_volatility_bisect(market_price, S, K, T, r, option_type, tol=1e-5, max_iter=100):
    engine = GreeksEngine(risk_free_rate=r)
    low, high = 0.01, 5.0
    for _ in range(max_iter):
        mid = (low + high) / 2.0
        price = engine.bs_price(S, K, T, mid, option_type)
        if abs(price - market_price) < tol: return mid
        if price > market_price: high = mid
        else: low = mid
    return (low + high) / 2.0

def time_to_expiry(expiry_date_str, reference_time=None):
    from datetime import datetime
    expiry = datetime.strptime(expiry_date_str, "%Y-%m-%d")
    now = reference_time if reference_time is not None else datetime.now()
    return max((expiry - now).total_seconds() / 86400.0, 0.0) / 365.0
