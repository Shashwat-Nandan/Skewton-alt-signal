"""
Risk Analyzer — Taleb-Compliant Portfolio Risk Assessment
==========================================================
Implements the advanced risk analysis tools from Dynamic Hedging:

  - Path Dependence Monte Carlo (Ch 16, Tables 16.2-16.4, Gap #19)
  - Ddeltadvol Stability Tests 1 & 2 (Ch 11, pp. 200-201, Gap #14)
  - Lock Delta Stress Test (Ch 11, pp. 204-206, Gap #17)
  - Method of Squares vega decomposition (Ch 9, pp. 164-166, Gap #11)
  - Soft vs Hard Delta Hedging Decision (Ch 16, p. 262, Gap #20)
  - Gamma Flip Test before rebalancing (Ch 16, p. 263, Gap #21)
  - Bleed Forecast Report (Ch 11, pp. 191-195, Gaps #12, #13, #15)
  - Three-Level Neutrality Check (Ch 16, p. 260, Gap #18)
"""

import logging
from typing import List, Dict, Optional
from dataclasses import dataclass, field

import numpy as np

from greeks_engine import (
    GreeksEngine, OptionContract, PortfolioGreeks,
)

logger = logging.getLogger(__name__)


@dataclass
class PathSimResult:
    """Result of a single Monte Carlo path simulation."""
    path_id: int
    final_pnl: float
    max_pnl: float
    min_pnl: float
    rehedge_count: int
    gamma_scalp_total: float
    theta_paid_total: float


@dataclass
class MonteCarloReport:
    """Aggregate Monte Carlo path dependence report (Gap #19)."""
    n_paths: int = 0
    mean_pnl: float = 0.0
    median_pnl: float = 0.0
    std_pnl: float = 0.0
    worst_path_pnl: float = 0.0
    best_path_pnl: float = 0.0
    pct_profitable: float = 0.0
    pnl_5th_percentile: float = 0.0
    pnl_95th_percentile: float = 0.0
    var_95: float = 0.0
    path_results: List[PathSimResult] = field(default_factory=list)


@dataclass
class StabilityReport:
    """Ddeltadvol stability test results (Gap #14)."""
    is_stable: bool = True
    ddeltadvol_portfolio: float = 0.0
    delta_at_base_vol: float = 0.0
    delta_at_high_vol: float = 0.0
    delta_at_low_vol: float = 0.0
    vega_flips_at: List[float] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class BleedForecast:
    """Overnight bleed forecast (Gaps #12, #13, #15)."""
    delta_bleed: float = 0.0
    gamma_bleed: float = 0.0
    theta_bleed: float = 0.0
    bleed_direction: str = ""  # "shortening" or "lengthening"
    shadow_theta_total: float = 0.0
    forward_bleed: float = 0.0  # effect of time
    backward_bleed: float = 0.0  # effect of expected vol change
    warnings: List[str] = field(default_factory=list)


@dataclass
class HedgeDecision:
    """Soft vs Hard delta hedging recommendation (Gaps #20, #21)."""
    use_soft_delta: bool = False
    use_hard_delta: bool = True
    gamma_flips: List[float] = field(default_factory=list)
    rationale: str = ""


class RiskAnalyzer:
    """
    Comprehensive risk analysis following Taleb's framework.
    Designed to be called before every major hedging decision.
    """

    def __init__(self, greeks_engine: GreeksEngine):
        self.greeks = greeks_engine

    # ═════════════════════════════════════════════════════════
    # Gap #19: PATH DEPENDENCE MONTE CARLO
    # ═════════════════════════════════════════════════════════

    def path_dependence_monte_carlo(
        self,
        positions: List[OptionContract],
        spot: float,
        T: float,
        daily_vol: float = 0.01,
        n_paths: int = 100,
        rehedge_threshold_delta: float = 0.10,
        trading_days: int = 30,
        seed: Optional[int] = None,
        charge_costs: bool = True,
    ) -> MonteCarloReport:
        """
        Taleb Ch 16, Tables 16.2-16.4: Shuffle the same returns into different
        orderings to estimate true P/L variance of the dynamic hedging strategy.

        Same start price, end price, and volatility — but different paths yield
        wildly different P/L for the dynamic hedger.

        charge_costs (default True): net every path of modeled transaction
        costs — option entry+exit legs, one futures order per rehedge, and
        the final hedge unwind. A cost-free mean_pnl systematically flatters
        rehedge-heavy structures (the kalman-trend #77 failure mode), and the
        mc_min_mean_pnl entry gate compares this number against a rupee
        floor, so the paths must be in net-of-cost rupees to mean anything.
        """
        rng = np.random.default_rng(seed)
        base_returns = rng.normal(0, daily_vol, trading_days)

        report = MonteCarloReport(n_paths=n_paths)
        all_pnls = []

        for path_id in range(n_paths):
            shuffled = rng.permutation(base_returns)
            result = self._simulate_single_path(
                positions, spot, T, shuffled, rehedge_threshold_delta,
                charge_costs=charge_costs
            )
            result.path_id = path_id
            report.path_results.append(result)
            all_pnls.append(result.final_pnl)

        pnls = np.array(all_pnls)
        report.mean_pnl = float(np.mean(pnls))
        report.median_pnl = float(np.median(pnls))
        report.std_pnl = float(np.std(pnls))
        report.worst_path_pnl = float(np.min(pnls))
        report.best_path_pnl = float(np.max(pnls))
        report.pct_profitable = float(np.sum(pnls > 0) / len(pnls) * 100)
        report.pnl_5th_percentile = float(np.percentile(pnls, 5))
        report.pnl_95th_percentile = float(np.percentile(pnls, 95))
        report.var_95 = -float(np.percentile(pnls, 5))

        logger.info(
            "Monte Carlo (%d paths): Mean P/L=%.0f  Worst=%.0f  Best=%.0f  "
            "Std=%.0f  Profitable=%.1f%%  VaR95=%.0f",
            n_paths, report.mean_pnl, report.worst_path_pnl, report.best_path_pnl,
            report.std_pnl, report.pct_profitable, report.var_95
        )
        return report

    def _simulate_single_path(
        self, positions, spot, T, daily_returns, rehedge_threshold,
        charge_costs: bool = True,
    ):
        """Simulate one path of dynamic hedging with daily rebalancing.

        With charge_costs, the path is netted of modeled transaction costs
        using the SAME estimate_transaction_cost the live fills book:
        option legs in+out, one futures order per rehedge, final unwind."""
        # Local import: taleb_karpathy imports this module at top level, so
        # the cost model can only be reached at call time, not import time.
        if charge_costs:
            from strategies.taleb_karpathy import estimate_transaction_cost

        current_spot = spot
        cumulative_pnl = 0.0
        gamma_total = 0.0
        theta_total = 0.0
        rehedge_count = 0
        max_pnl = 0.0
        min_pnl = 0.0
        hedge_delta = 0.0  # Delta of our futures hedge
        mc_lot = positions[0].lot_size if positions else 25

        if charge_costs:
            # Entry legs at t0 premiums (BS mid at current spot / full T; no
            # bid/ask spread on the premium itself — a stated simplification,
            # the spread cost rides in estimate_transaction_cost's slippage
            # term). Anchor BOTH extrema to the post-entry-cost level: net of
            # costs the path was never at 0.0, so a 0-initialized max_pnl
            # would report a break-even peak that never existed.
            cumulative_pnl -= self._option_leg_costs(
                positions, spot, max(T, 1 / 365), entering=True)
            max_pnl = min_pnl = cumulative_pnl

        for day_idx, ret in enumerate(daily_returns):
            new_spot = current_spot * (1 + ret)
            remaining_T = max(T - (day_idx + 1) / 365.0, 1/365)

            # Compute portfolio delta at new spot
            port_delta = 0.0
            port_gamma = 0.0
            port_theta = 0.0
            day_pnl = 0.0

            for opt in positions:
                sigma = self.greeks.vol_at_price(opt.iv or 0.2, spot, new_spot)
                old_price = self.greeks.bs_price(current_spot, opt.strike, remaining_T + 1/365, opt.iv or 0.2, opt.option_type)
                new_price = self.greeks.bs_price(new_spot, opt.strike, remaining_T, sigma, opt.option_type)
                day_pnl += (new_price - old_price) * opt.quantity * opt.lot_size
                port_delta += self.greeks.delta(new_spot, opt.strike, remaining_T, sigma, opt.option_type) * opt.quantity * opt.lot_size
                port_gamma += self.greeks.gamma(new_spot, opt.strike, remaining_T, sigma) * opt.quantity * opt.lot_size
                port_theta += self.greeks.theta(new_spot, opt.strike, remaining_T, sigma, opt.option_type) * opt.quantity * opt.lot_size

            # P/L from futures hedge
            day_pnl += hedge_delta * (new_spot - current_spot)

            # Rehedge if delta exceeds threshold
            net_delta = port_delta + hedge_delta
            if abs(net_delta) > rehedge_threshold * mc_lot:
                hedge_delta -= net_delta  # Adjust futures position
                rehedge_count += 1
                # Gamma scalp P/L approximation
                gamma_total += 0.5 * port_gamma * (new_spot - current_spot)**2
                if charge_costs:
                    # One futures order per rehedge, sized to the delta traded.
                    day_pnl -= estimate_transaction_cost(
                        new_spot, abs(net_delta) / mc_lot, mc_lot,
                        "SELL" if net_delta > 0 else "BUY", "FUT")

            theta_total += abs(port_theta)
            cumulative_pnl += day_pnl
            max_pnl = max(max_pnl, cumulative_pnl)
            min_pnl = min(min_pnl, cumulative_pnl)
            current_spot = new_spot

        if charge_costs:
            # Exit legs at end-of-path premiums, and the hedge unwind.
            end_T = max(T - len(daily_returns) / 365.0, 1 / 365)
            cumulative_pnl -= self._option_leg_costs(
                positions, current_spot, end_T, entering=False,
                entry_spot=spot)
            if abs(hedge_delta) > 1e-9:
                cumulative_pnl -= estimate_transaction_cost(
                    current_spot, abs(hedge_delta) / mc_lot, mc_lot,
                    "BUY" if hedge_delta < 0 else "SELL", "FUT")
            min_pnl = min(min_pnl, cumulative_pnl)

        return PathSimResult(
            path_id=0, final_pnl=cumulative_pnl,
            max_pnl=max_pnl, min_pnl=min_pnl,
            rehedge_count=rehedge_count,
            gamma_scalp_total=gamma_total,
            theta_paid_total=theta_total,
        )

    def _option_leg_costs(self, positions, price_spot: float, tenor: float,
                          entering: bool, entry_spot: float = None) -> float:
        """Total transaction cost for opening (entering=True) or closing all
        option legs, priced at BS mid at `price_spot`/`tenor`. ONE definition
        of the side flip: a long leg (quantity > 0) BUYs to enter and SELLs
        to exit — keeping entry/exit sign conventions in a single place so
        they cannot drift apart. On exit, sigma follows the sticky-strike
        vol_at_price adjustment from the path's start spot."""
        from strategies.taleb_karpathy import estimate_transaction_cost

        total = 0.0
        for opt in positions:
            iv = opt.iv or 0.2
            sigma = iv if entering else self.greeks.vol_at_price(
                iv, entry_spot if entry_spot is not None else price_spot,
                price_spot)
            px = self.greeks.bs_price(price_spot, opt.strike, tenor,
                                      sigma, opt.option_type)
            long_leg = opt.quantity > 0
            side = ("BUY" if long_leg else "SELL") if entering else \
                   ("SELL" if long_leg else "BUY")
            total += estimate_transaction_cost(
                px, abs(opt.quantity), opt.lot_size, side, "OPT")
        return total

    # ═════════════════════════════════════════════════════════
    # Gap #14: STABILITY TESTS
    # ═════════════════════════════════════════════════════════

    def stability_test(
        self, positions: List[OptionContract], spot: float, T: float,
        vol_bump: float = 0.05,
    ) -> StabilityReport:
        """
        Taleb Ch 11, pp. 200-201: Test 1 (Ddeltadvol) and Test 2 (Asymptotic Vega).

        Test 1: Raise vol and check if deltas flip → unstable.
        Test 2: Check if vega reverses sign at different spot levels → clustering risk.
        """
        report = StabilityReport()

        # Test 1: Ddeltadvol
        delta_base = delta_high = delta_low = 0.0
        for opt in positions:
            sigma = opt.iv or 0.2
            m = opt.quantity * opt.lot_size
            delta_base += self.greeks.delta(spot, opt.strike, T, sigma, opt.option_type) * m
            delta_high += self.greeks.delta(spot, opt.strike, T, sigma + vol_bump, opt.option_type) * m
            delta_low += self.greeks.delta(spot, opt.strike, T, max(sigma - vol_bump, 0.03), opt.option_type) * m

        report.delta_at_base_vol = delta_base
        report.delta_at_high_vol = delta_high
        report.delta_at_low_vol = delta_low
        report.ddeltadvol_portfolio = (delta_high - delta_base) / vol_bump

        if abs(report.ddeltadvol_portfolio) > abs(delta_base) * 0.5:
            report.is_stable = False
            report.warnings.append(
                f"UNSTABLE: Ddeltadvol={report.ddeltadvol_portfolio:.1f} is large relative to "
                f"delta={delta_base:.1f}. Position delta will shift significantly with vol changes."
            )

        if delta_base * delta_high < 0:
            report.is_stable = False
            report.warnings.append(
                "CRITICAL: Delta flips sign when vol rises. Position is inherently unstable."
            )

        # Test 2: Asymptotic Vega Test — check vega at different spot levels
        test_prices = np.linspace(spot * 0.92, spot * 1.08, 17)
        prev_vega = None
        for p in test_prices:
            total_vega = 0.0
            for opt in positions:
                sigma_p = self.greeks.vol_at_price(opt.iv or 0.2, spot, p)
                total_vega += self.greeks.vega(p, opt.strike, T, sigma_p) * opt.quantity * opt.lot_size
            if prev_vega is not None and prev_vega * total_vega < 0:
                report.vega_flips_at.append(round(p, 2))
            prev_vega = total_vega

        if report.vega_flips_at:
            report.warnings.append(
                f"Vega flips sign at {report.vega_flips_at}. Option clustering risk detected."
            )

        return report

    # ═════════════════════════════════════════════════════════
    # Gaps #12, #13, #15: BLEED FORECAST
    # ═════════════════════════════════════════════════════════

    def bleed_forecast(
        self, positions: List[OptionContract], spot: float, T: float,
        expected_vol_change: float = -0.005,
        per_leg_T: dict = None,
    ) -> BleedForecast:
        """
        Taleb Ch 11: Compute overnight bleed — delta/gamma drift from time + vol changes.

        Forward bleed: effect of time advancing (expiry shrinking).
        Backward bleed: effect of vol changing (can reverse or amplify time effect).

        per_leg_T (Phase 3 / review-fix #5): optional {tradingsymbol: T_years}
        for multi-expiry books. Without it, all legs use the same T,
        which mis-prices back-month bleed in calendars/diagonals.
        """
        bf = BleedForecast()
        dt = 1.0 / 365.0

        delta_today = delta_tomorrow = 0.0
        gamma_today = gamma_tomorrow = 0.0
        theta_today = theta_tomorrow = 0.0
        sg_up_today = sg_down_today = 0.0

        def _leg_T(opt):
            if per_leg_T is not None:
                return per_leg_T.get(opt.tradingsymbol, T)
            return T

        for opt in positions:
            sigma = opt.iv or 0.2
            m = opt.quantity * opt.lot_size
            T_leg = _leg_T(opt)
            T_leg_tomorrow = max(T_leg - dt, 1/365)

            # Today
            delta_today += self.greeks.delta(spot, opt.strike, T_leg, sigma, opt.option_type) * m
            gamma_today += self.greeks.gamma(spot, opt.strike, T_leg, sigma) * m
            theta_today += self.greeks.theta(spot, opt.strike, T_leg, sigma, opt.option_type) * m
            _, su, sd = self.greeks.shadow_gamma_directional(spot, opt.strike, T_leg, sigma, opt.option_type)
            sg_up_today += su * m; sg_down_today += sd * m

            # Tomorrow (time + vol change)
            sigma_new = max(sigma + expected_vol_change, 0.03)
            delta_tomorrow += self.greeks.delta(spot, opt.strike, T_leg_tomorrow, sigma_new, opt.option_type) * m
            gamma_tomorrow += self.greeks.gamma(spot, opt.strike, T_leg_tomorrow, sigma_new) * m
            theta_tomorrow += self.greeks.theta(spot, opt.strike, T_leg_tomorrow, sigma_new, opt.option_type) * m

        bf.delta_bleed = delta_today - delta_tomorrow
        bf.gamma_bleed = gamma_today - gamma_tomorrow
        bf.theta_bleed = theta_today - theta_tomorrow

        # Forward bleed (time only): how much delta you LOSE from time passing.
        # Negate charm because charm = delta(T-dt) - delta(T) = delta_tomorrow - delta_today,
        # but forward_bleed should = delta_today - delta_tomorrow (Taleb p.191).
        bf.forward_bleed = -sum(
            self.greeks.charm(spot, o.strike, _leg_T(o), o.iv or 0.2, o.option_type) * o.quantity * o.lot_size
            for o in positions
        )
        # Backward bleed (vol change component)
        bf.backward_bleed = bf.delta_bleed - bf.forward_bleed

        bf.shadow_theta_total = sum(
            self.greeks.shadow_theta(spot, o.strike, _leg_T(o), o.iv or 0.2, o.option_type) * o.quantity * o.lot_size
            for o in positions
        )

        # Gap #13: Determine bleed direction
        if sg_up_today > 0 and sg_down_today < 0:
            bf.bleed_direction = "shortening (long up-gamma, short down-gamma → delta bleeds shorter)"
        elif sg_up_today < 0 and sg_down_today > 0:
            bf.bleed_direction = "lengthening (short up-gamma, long down-gamma → delta bleeds longer)"
        else:
            bf.bleed_direction = "mixed"

        if abs(bf.delta_bleed) > 20:
            bf.warnings.append(
                f"WARNING: Large overnight delta bleed of {bf.delta_bleed:.1f}. "
                "Consider adjusting position before market close."
            )
        if abs(bf.gamma_bleed) > 0.5:
            bf.warnings.append(
                f"WARNING: Significant gamma bleed of {bf.gamma_bleed:.4f}. "
                "Gamma protection may degrade overnight."
            )

        return bf

    # ═════════════════════════════════════════════════════════
    # Gaps #20, #21: SOFT vs HARD DELTA DECISION
    # ═════════════════════════════════════════════════════════

    def hedge_decision(
        self, positions: List[OptionContract], spot: float, T: float,
    ) -> HedgeDecision:
        """
        Taleb Ch 16, pp. 262-263: Decide between soft (options) and hard (futures) delta hedging.

        Rule: If gamma flips to negative further out, using hard deltas (futures)
        would make tail P/L worse. Use soft deltas (options) instead.
        """
        decision = HedgeDecision()
        flips = self.greeks.find_gamma_flip_points(positions, spot, T)
        decision.gamma_flips = flips

        if not flips:
            decision.use_hard_delta = True
            decision.use_soft_delta = False
            decision.rationale = "No gamma flip points detected. Safe to use futures for delta hedging."
        else:
            # Check if gamma becomes negative in the tails
            gamma_grid = self.greeks.gamma_grid(positions, spot, T, range_pct=8.0, steps=33)
            tail_prices = sorted(gamma_grid.keys())
            low_tail_gamma = gamma_grid[tail_prices[0]]
            high_tail_gamma = gamma_grid[tail_prices[-1]]

            if low_tail_gamma < 0 or high_tail_gamma < 0:
                decision.use_soft_delta = True
                decision.use_hard_delta = False
                decision.rationale = (
                    f"Gamma flips at {flips} and becomes negative in tails "
                    f"(low={low_tail_gamma:.4f}, high={high_tail_gamma:.4f}). "
                    "Use soft deltas (options) to avoid amplifying tail risk."
                )
            else:
                decision.use_hard_delta = True
                decision.use_soft_delta = False
                decision.rationale = (
                    f"Gamma flips at {flips} but remains positive in tails. "
                    "Futures hedging is acceptable."
                )

        return decision

    # ═════════════════════════════════════════════════════════
    # Gap #11: METHOD OF SQUARES
    # ═════════════════════════════════════════════════════════

    def method_of_squares(
        self, positions: List[OptionContract], spot: float, T: float,
        strike_buckets: int = 5, time_buckets: int = 3,
        per_leg_T: dict = None,
    ) -> Dict[str, Dict[str, float]]:
        """
        Taleb Ch 9, pp. 164-166: Cut position into squares of strike × time
        and estimate vega per square. Catches bumpy risk profiles.

        per_leg_T (review-fix #5): {tradingsymbol: T_years} for
        multi-expiry books. Without it, back-month strikes are priced
        with the front-month T and the per-square vega is wrong.
        """
        if not positions:
            return {}

        def _leg_T(opt):
            if per_leg_T is not None:
                return per_leg_T.get(opt.tradingsymbol, T)
            return T

        strikes = sorted(set(o.strike for o in positions))
        min_k, max_k = min(strikes), max(strikes)
        strike_edges = np.linspace(min_k * 0.95, max_k * 1.05, strike_buckets + 1)

        # Collapse time dimension for single-expiry books (all positions share T)
        expiries = set(o.expiry for o in positions if o.expiry)
        is_single_expiry = len(expiries) <= 1
        effective_time_buckets = 1 if is_single_expiry else time_buckets
        days = max(T * 365, 1)
        time_edges = np.linspace(0, days, effective_time_buckets + 1)

        squares = {}
        for si in range(strike_buckets):
            for ti in range(effective_time_buckets):
                k_lo, k_hi = strike_edges[si], strike_edges[si + 1]
                t_lo, t_hi = time_edges[ti], time_edges[ti + 1]
                if is_single_expiry:
                    label = f"K[{k_lo:.0f}-{k_hi:.0f}]"
                else:
                    label = f"K[{k_lo:.0f}-{k_hi:.0f}]_T[{t_lo:.0f}-{t_hi:.0f}d]"

                vega_in_square = 0.0
                gamma_in_square = 0.0
                delta_in_square = 0.0

                for opt in positions:
                    if k_lo <= opt.strike < k_hi:
                        sigma = opt.iv or 0.2
                        m = opt.quantity * opt.lot_size
                        T_leg = _leg_T(opt)
                        vega_in_square += self.greeks.vega(spot, opt.strike, T_leg, sigma) * m
                        gamma_in_square += self.greeks.gamma(spot, opt.strike, T_leg, sigma) * m
                        delta_in_square += self.greeks.delta(spot, opt.strike, T_leg, sigma, opt.option_type) * m

                if abs(vega_in_square) > 0.01 or abs(gamma_in_square) > 0.0001:
                    squares[label] = {
                        "vega": round(vega_in_square, 2),
                        "gamma": round(gamma_in_square, 4),
                        "delta": round(delta_in_square, 2),
                    }

        return squares

    # ═════════════════════════════════════════════════════════
    # Gap #18: THREE-LEVEL NEUTRALITY CHECK
    # ═════════════════════════════════════════════════════════

    def neutrality_check(self, portfolio: PortfolioGreeks) -> Dict[str, dict]:
        """
        Taleb Ch 16, p. 260: Three-level neutrality hierarchy.
        Level 1: Delta (including rho). Level 2: Gamma. Level 3: Vega.
        """
        checks = {
            "level_1_delta": {
                "value": portfolio.net_delta,
                "neutral": abs(portfolio.net_delta) < 50,
                "priority": "CRITICAL",
            },
            "level_2_gamma": {
                "value": portfolio.net_gamma,
                "shadow_up": portfolio.net_shadow_gamma_up,
                "shadow_down": portfolio.net_shadow_gamma_down,
                "neutral": True,  # For long gamma strategy, we want positive gamma
                "priority": "HIGH",
            },
            "level_3_vega": {
                "raw_vega": portfolio.net_vega,
                "modified_vega": portfolio.net_modified_vega,
                "buckets": portfolio.vega_buckets,
                "priority": "MEDIUM",
            },
            "stability": {
                "ddeltadvol": portfolio.net_ddeltadvol,
                "moment_3_skew": portfolio.moment_3,
                "moment_4_kurtosis": portfolio.moment_4,
                "lock_delta_up": portfolio.lock_delta_up,
                "lock_delta_down": portfolio.lock_delta_down,
            },
            "bleed": {
                "charm": portfolio.net_charm,
                "gamma_bleed": portfolio.net_gamma_bleed,
            },
        }
        return checks
