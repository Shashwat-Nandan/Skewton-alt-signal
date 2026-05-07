"""
Taleb Dynamic Hedger v2 — Core Hedging Engine
==============================================
Enhanced with all 22 Taleb gaps:
  - Bleed tracking and overnight forecast (Gaps #12, #13, #15)
  - Soft vs hard delta decision (Gaps #20, #21)
  - Gamma flip test before rebalancing (Gap #21)
  - Path dependence Monte Carlo for position sizing (Gap #19)
  - Three-level neutrality hierarchy (Gap #18)
  - Alpha monitoring for entry/exit (Gap #22)
"""

import time
import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from greeks_engine import (
    GreeksEngine, OptionContract, PortfolioGreeks,
    implied_volatility_bisect, time_to_expiry,
)
from trade_proposer import TradeProposer, TradeProposal
from risk_analyzer import (
    RiskAnalyzer, MonteCarloReport, StabilityReport,
    BleedForecast, HedgeDecision,
)

from .base import BaseStrategy, ExecutionMode, OrderValidationError, validate_order

# Kite Connect's quote feed indexes the *spot* price under the index's
# display name (with spaces), not the derivatives ticker. f"NSE:{u}"
# works for stocks (NSE:RELIANCE) but Kite silently returns {} for
# "NSE:NIFTY" — masked for two weeks by the bare-except in the old
# _get_spot_price, which made every tick a no-op. Extend this map when
# adding a new index underlying.
_INDEX_SPOT_SYMBOLS = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
}

logger = logging.getLogger(__name__)


def estimate_transaction_cost(
    price: float, quantity: int, lot_size: int, transaction_type: str,
    instrument_type: str = "OPT",
) -> float:
    """
    Estimate total transaction costs for an Indian options/futures order.
    Includes brokerage, STT, exchange fees, GST, SEBI charges, and stamp duty.

    Args:
        price: per-unit price
        quantity: number of lots (always positive)
        lot_size: units per lot
        transaction_type: "BUY" or "SELL"
        instrument_type: "OPT" for options, "FUT" for futures

    Returns:
        Total estimated cost in INR (always positive).
    """
    turnover = price * quantity * lot_size
    if turnover <= 0:
        return 0.0

    # Flat brokerage (discount broker like Zerodha: ₹20 per executed order)
    brokerage = 20.0

    # STT differs by instrument type
    stt = 0.0
    if instrument_type == "FUT":
        # Futures: 0.0125% on sell side (on turnover)
        if transaction_type == "SELL":
            stt = turnover * 0.000125
    else:
        # Options: 0.0625% on sell side (on premium turnover)
        if transaction_type == "SELL":
            stt = turnover * 0.000625

    # Exchange transaction charges differ by product
    if instrument_type == "FUT":
        exchange_charges = turnover * 0.0002  # ~0.02% for futures
    else:
        exchange_charges = turnover * 0.00053  # ~0.053% for options

    # SEBI charges: ₹10 per crore
    sebi = turnover * 0.000001

    # GST: 18% on (brokerage + exchange charges + SEBI)
    gst = (brokerage + exchange_charges + sebi) * 0.18

    # Stamp duty: 0.003% on buy side (same for both)
    stamp = 0.0
    if transaction_type == "BUY":
        stamp = turnover * 0.00003

    # Slippage: futures are more liquid, lower slippage
    if instrument_type == "FUT":
        slippage = turnover * 0.0002  # 0.02%
    else:
        slippage = turnover * 0.0005  # 0.05%

    return brokerage + stt + exchange_charges + sebi + gst + stamp + slippage


def _apply_best_params(tunable_params: Dict, path: Path) -> Tuple[int, List[str]]:
    """Overlay autoresearch output onto an existing tunable_params dict.

    Reads ``{"best_params": {...}}`` from ``path`` and assigns each value to
    matching keys in ``tunable_params`` in place. Unknown keys are skipped
    rather than added — the dict's keys define the permitted surface, and
    a stale autoresearch run with renamed params should not silently inject
    them. Returns ``(applied_count, ignored_keys)``. Missing or malformed
    files yield ``(0, [])`` and a warning so a misplaced file does not
    silently fall back to config defaults.
    """
    if not path.exists():
        return 0, []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("best_params at %s could not be read: %s — using config defaults", path, e)
        return 0, []
    best = data.get("best_params") if isinstance(data, dict) else None
    if not isinstance(best, dict):
        logger.warning("best_params at %s missing 'best_params' object — using config defaults", path)
        return 0, []
    applied = 0
    ignored: List[str] = []
    for k, v in best.items():
        if k in tunable_params:
            tunable_params[k] = v
            applied += 1
        else:
            ignored.append(k)
    return applied, ignored


@dataclass
class HedgeState:
    """Current state — enhanced with bleed and risk tracking."""
    positions: List[OptionContract] = field(default_factory=list)
    portfolio_greeks: Optional[PortfolioGreeks] = None
    entry_time: Optional[datetime] = None
    total_pnl: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    rehedge_count: int = 0
    gamma_scalp_pnl: float = 0.0
    theta_decay_paid: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0
    total_transaction_costs: float = 0.0
    # Per-trade PnL attribution. Each entry covers one open→close cycle.
    closed_trades: List[dict] = field(default_factory=list)
    _attribution_baseline: Optional[dict] = None
    _prev_snapshot_pnl: float = 0.0  # For computing P/L deltas
    _current_day_pnl: float = 0.0   # Accumulates intraday P/L for the current trading day
    _current_trading_date: object = None  # date object for the current trading day
    daily_pnl_history: List[float] = field(default_factory=list)
    # ── Futures hedge tracking ──
    futures_hedge_delta: float = 0.0  # Net delta from futures positions
    futures_entry_vwap: float = 0.0   # Volume-weighted average entry price for futures
    futures_lots: int = 0             # Net signed lot count (positive = long)
    # ── New: Bleed tracking ──
    bleed_history: List[BleedForecast] = field(default_factory=list)
    stability_history: List[StabilityReport] = field(default_factory=list)
    last_hedge_decision: Optional[HedgeDecision] = None
    monte_carlo_report: Optional[MonteCarloReport] = None


class TalebKarpathyStrategy(BaseStrategy):
    """
    Taleb-Karpathy long-gamma straddle hedger.

    Long an ATM straddle, delta-rebalance via futures around a gamma-scalp band,
    Karpathy-style autoresearch tunes the parameters between sessions. See
    SKILL.md for the full theoretical framework.
    """

    name = "taleb_karpathy"

    def __init__(
        self,
        kite,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
    ):
        super().__init__(kite, config_path=config_path, mode=mode)

        # Market-hours gate uses naive datetime.now() against 09:15-15:30,
        # which silently breaks if TZ is not Asia/Kolkata (e.g. a redeploy
        # that loses the systemd unit's `Environment=TZ=`). Log loud at
        # boot so the issue is visible in journalctl rather than expressed
        # as "strategy did nothing today".
        self._warn_if_not_ist()

        self.greeks = GreeksEngine(risk_free_rate=0.065)
        self.risk = RiskAnalyzer(self.greeks)
        self.proposer = TradeProposer(kite, config_path)
        self.state = HedgeState()

        # ── Tunable parameters (autoresearch can modify) ──
        self.tunable_params = {
            "rehedge_delta_threshold": self.config.getfloat("strategy", "rehedge_delta_threshold"),
            "gamma_scalp_band_pct": self.config.getfloat("strategy", "gamma_scalp_band_pct"),
            "position_size_pct": self.config.getfloat("strategy", "position_size_pct"),
            "vega_limit": self.config.getfloat("strategy", "vega_limit"),
            "max_holding_period_hours": self.config.getfloat("strategy", "max_holding_period_hours"),
            "entry_iv_percentile_min": self.config.getfloat("strategy", "entry_iv_percentile_min"),
            "entry_iv_percentile_max": self.config.getfloat("strategy", "entry_iv_percentile_max"),
            # ── New tunable: max alpha for entry (Gap #22) ──
            "max_entry_alpha": self.config.getfloat("strategy", "max_entry_alpha", fallback=25000),
            # ── New tunable: Monte Carlo worst-path sizing (Gap #19) ──
            "mc_worst_path_loss_pct": self.config.getfloat("strategy", "mc_worst_path_loss_pct", fallback=3.0),
            # ── Cost-aware rehedge: min scalp/cost ratio to proceed (Taleb Ch 16) ──
            "cost_hurdle_factor": self.config.getfloat("strategy", "cost_hurdle_factor", fallback=1.5),
            # ── Realized-vs-implied vol regime gate ──
            # Long straddle is profitable when realized vol > implied. The
            # ratio threshold (default 1.0 = require RV ≥ IV) and rolling
            # window (default 5 days) gate entries on the structural thesis.
            "min_rv_iv_ratio": self.config.getfloat("strategy", "min_rv_iv_ratio", fallback=1.0),
            "rv_window_days": self.config.getfloat("strategy", "rv_window_days", fallback=5.0),
        }

        # Overlay autoresearch optimum on top of config defaults so the
        # output of run_autoresearch.py actually reaches live trading.
        # Disable with [strategy] use_best_params = false in config.ini.
        if self.config.getboolean("strategy", "use_best_params", fallback=True):
            bp_path = Path(self.config.get(
                "strategy", "best_params_path", fallback="best_params.json",
            ))
            if not bp_path.is_absolute():
                bp_path = Path(__file__).resolve().parent.parent / bp_path
            applied, ignored = _apply_best_params(self.tunable_params, bp_path)
            if applied:
                logger.info("Overlaid %d tunable params from %s", applied, bp_path)
            if ignored:
                logger.debug("best_params keys not in tunable schema (ignored): %s", ignored)

        # ── Immutable safety rails ──
        self.immutable_params = {
            "max_daily_loss_pct": self.config.getfloat("strategy", "max_daily_loss_pct"),
            "max_position_margin_pct": self.config.getfloat("strategy", "max_position_margin_pct"),
            "no_naked_shorts": self.config.getboolean("strategy", "no_naked_shorts"),
            "liquidity_min_spread_pct": self.config.getfloat("strategy", "liquidity_min_spread_pct"),
            "no_trade_last_minutes": self.config.getint("strategy", "no_trade_last_minutes"),
            "gap_exit_threshold_pct": self.config.getfloat("strategy", "gap_exit_threshold_pct"),
            "circuit_breaker_consecutive_losses": self.config.getint("strategy", "circuit_breaker_consecutive_losses"),
            "circuit_breaker_pause_minutes": self.config.getint("strategy", "circuit_breaker_pause_minutes"),
            "total_capital": self.config.getfloat("strategy", "total_capital"),
            "max_positions": self.config.getint("strategy", "max_positions"),
        }

        self.underlying = self.config["strategy"]["underlying"]
        self.exchange = self.config["strategy"]["exchange"]
        self._consecutive_losses = 0
        self._circuit_breaker_until = None
        self._daily_loss_stop_date = None  # Date on which daily loss limit was hit
        self._cached_lot_size = None
        self._cached_futures_symbol = None
        # Spot-fetch failure counter — escalates the per-tick log to ERROR
        # after 5 consecutive failures so a wrong symbol or session issue
        # surfaces clearly instead of being buried in unrelated stack traces.
        self._consecutive_spot_failures = 0
        self._clock = datetime.now  # Override for backtest replay
        # Persistent ATM IV history survives across runs so IV percentile is
        # computed against a real multi-session distribution. Backtests disable
        # persistence to keep experiments independent.
        self._persist_iv_history = True
        self._iv_history_max_size = 500
        self._atm_iv_history: List[float] = []
        # Rolling spot history is used to estimate realized vol for the
        # RV/IV entry gate. In-memory only; backtests rebuild it forward.
        self._spot_history: List[Tuple[datetime, float]] = []
        self._spot_history_max_size = 2000  # ~1.5 days of 1-min ticks or weeks of 5-min
        self._load_iv_history()

    # ══════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════

    def scan_and_propose(self) -> List[TradeProposal]:
        """Full scan → filter → stability test → MC sizing → propose cycle."""
        # One straddle at a time: rest of the engine (entry_time, max_holding,
        # attribution, exit gates) is written for a single active trade.
        # Without this guard, a tick that passes all entry gates while a
        # position is open stacks fills onto existing legs and silently blows
        # past position_size_pct.
        if self.state.positions:
            return []
        if not self._pre_trade_checks():
            return []

        spot = self._get_spot_price()
        if not self._check_spot(spot):
            return []
        self._record_spot_sample(self._clock(), spot)
        chain = self._get_options_chain()
        if chain.empty:
            return []

        iv_percentile = self._compute_iv_percentile(chain, spot)
        iv_min = self.tunable_params["entry_iv_percentile_min"]
        iv_max = self.tunable_params["entry_iv_percentile_max"]
        if not (iv_min <= iv_percentile <= iv_max):
            logger.info("IV percentile %.1f outside [%.0f, %.0f]. Waiting.", iv_percentile, iv_min, iv_max)
            return []

        # RV/IV regime gate: long-straddle thesis is RV > IV. Without this,
        # we pay theta and earn gamma that roughly cancels in calm regimes.
        # Returns None during warmup, in which case the gate is permissive.
        atm_iv = self._atm_iv_history[-1] if self._atm_iv_history else None
        rv_window = self.tunable_params.get("rv_window_days", 5.0)
        min_ratio = self.tunable_params.get("min_rv_iv_ratio", 1.0)
        realized_vol = self._compute_realized_vol(rv_window)
        if atm_iv and realized_vol is not None:
            ratio = realized_vol / atm_iv
            if ratio < min_ratio:
                logger.info(
                    "RV/IV ratio %.2f < min %.2f (RV %.1f%% / IV %.1f%% over %.1fd) — waiting for vol expansion",
                    ratio, min_ratio, realized_vol*100, atm_iv*100, rv_window,
                )
                return []

        proposals = self.proposer.propose_delta_neutral(
            chain=chain, spot=spot,
            capital=self.immutable_params["total_capital"],
            position_size_pct=self.tunable_params["position_size_pct"],
            greeks_engine=self.greeks,
        )
        proposals = self._apply_risk_filters(proposals, spot)

        # ── Gap #22: Alpha check — reject if gamma is too expensive ──
        if proposals:
            test_positions = self._proposals_to_contracts(proposals)
            T = time_to_expiry(proposals[0].expiry, self._clock()) if proposals[0].expiry else 1/365
            pf = self.greeks.compute_portfolio_greeks(test_positions, spot, T)
            if abs(pf.net_alpha) > self.tunable_params["max_entry_alpha"]:
                logger.info("Alpha %.0f exceeds max_entry_alpha. Gamma too expensive.", pf.net_alpha)
                return []

            # Pre-entry vega gate: size down if the proposed position would
            # trip the vega limit on tick 1. Without this, entry and exit
            # happen on the same bar and only transaction cost is booked.
            vega_limit_per_lot = self.tunable_params["vega_limit"]
            n_long_lots = max(sum(p.quantity for p in proposals if p.transaction_type == "BUY"), 1)
            effective_vega_limit = vega_limit_per_lot * n_long_lots
            if abs(pf.net_vega) > effective_vega_limit and abs(pf.net_vega) > 0:
                scale = effective_vega_limit / abs(pf.net_vega)
                if scale < 0.5:
                    logger.info("Proposal vega %.0f > limit %.0f; scale %.2f too aggressive — skipping entry",
                                abs(pf.net_vega), effective_vega_limit, scale)
                    return []
                for p in proposals:
                    p.quantity = max(int(p.quantity * scale), 1)
                logger.info("Scaled proposals by %.2f to respect vega limit", scale)
                # Recompute portfolio greeks after scaling
                test_positions = self._proposals_to_contracts(proposals)
                pf = self.greeks.compute_portfolio_greeks(test_positions, spot, T)
                # Re-validate against the post-scale lot count. The per-lot
                # budget shrinks with the position, so scaling alone cannot
                # rescue a structure whose single-lot vega already exceeds
                # vega_limit. Without this re-check the exit gate fires on
                # tick 1 and we book a same-bar round trip.
                n_long_lots = max(sum(p.quantity for p in proposals if p.transaction_type == "BUY"), 1)
                effective_vega_limit = vega_limit_per_lot * n_long_lots
                if abs(pf.net_vega) > effective_vega_limit:
                    logger.info("Post-scale vega %.0f still > limit %.0f — skipping entry",
                                abs(pf.net_vega), effective_vega_limit)
                    return []

            # ── Gap #14: Stability test ──
            stability = self.risk.stability_test(test_positions, spot, T)
            self.state.stability_history.append(stability)
            if not stability.is_stable:
                for w in stability.warnings:
                    logger.warning("Stability: %s", w)

            # ── Gap #19: Monte Carlo sizing ──
            # Derive a deterministic seed from the entry context so replays are
            # reproducible. Use hashlib (stable across processes) rather than
            # Python's built-in hash() which is salted by PYTHONHASHSEED.
            seed_payload = f"{self._clock().isoformat()}|{round(spot, 2)}".encode()
            mc_seed = int(hashlib.sha256(seed_payload).hexdigest()[:8], 16)
            mc = self.risk.path_dependence_monte_carlo(
                test_positions, spot, T, n_paths=50, trading_days=max(int(T*365), 5),
                seed=mc_seed,
            )
            self.state.monte_carlo_report = mc
            max_loss_allowed = self.immutable_params["total_capital"] * self.tunable_params["mc_worst_path_loss_pct"] / 100
            if abs(mc.worst_path_pnl) > max_loss_allowed:
                scale = max_loss_allowed / abs(mc.worst_path_pnl)
                # If scaling would push any leg below 1 lot, the cap is not
                # actually enforceable at our minimum executable size — reject
                # the entry rather than flooring at 1 lot, which would let the
                # MC worst-path loss exceed the configured budget.
                if any(int(p.quantity * scale) < 1 for p in proposals):
                    logger.info(
                        "MC worst path %.0f > max %.0f; required scale %.1f%% drops legs below 1 lot — skipping entry",
                        mc.worst_path_pnl, max_loss_allowed, scale * 100,
                    )
                    return []
                logger.info("MC worst path %.0f > max %.0f. Scaling to %.1f%%", mc.worst_path_pnl, max_loss_allowed, scale*100)
                for p in proposals:
                    p.quantity = int(p.quantity * scale)

        logger.info("Generated %d proposals after full Taleb analysis.", len(proposals))
        return proposals

    def check_and_rehedge(self) -> List[TradeProposal]:
        """
        Core Taleb loop v2: check delta → decide soft/hard hedge → rebalance.

        New: Uses gamma flip test (Gap #21) and soft/hard delta decision (Gap #20)
        before choosing how to rehedge.
        """
        if not self.state.positions:
            return []

        # A position cannot exit or rehedge on the same bar it entered.
        # In live trading the wall clock advances between API loops, so this
        # only kicks in during backtest replay where one timestamp drives
        # both scan_and_propose and check_and_rehedge — without this guard,
        # entry+exit collapse to a single quote and book only round-trip cost.
        if self.state.entry_time is not None and self.state.entry_time == self._clock():
            return []

        spot = self._get_spot_price()
        if not self._check_spot(spot):
            return []
        self._record_spot_sample(self._clock(), spot)
        self._update_positions_prices(spot)
        self._update_portfolio_greeks()

        greeks = self.state.portfolio_greeks
        if not greeks:
            return []

        # Check exit conditions
        if self._should_exit(greeks, spot):
            return self._generate_close_all_proposals()

        # Check if rehedge needed
        threshold = self.tunable_params["rehedge_delta_threshold"]
        lot_size = self._get_lot_size()
        delta_in_lots = abs(greeks.net_discrete_delta) / lot_size

        if delta_in_lots < threshold:
            return []

        logger.info("Delta drift: %.1f discrete (%.2f lots) > threshold %.2f", greeks.net_discrete_delta, delta_in_lots, threshold)

        # ── Cost-aware rehedge gate (Taleb Ch 16: balance gamma capture vs txn costs) ──
        expected_scalp = self._estimate_gamma_scalp_pnl(greeks, spot)
        hedge_lots = max(abs(round(greeks.net_discrete_delta / lot_size)), 1)
        cost_one_side = estimate_transaction_cost(spot, hedge_lots, lot_size, "BUY", "FUT")
        estimated_round_trip_cost = cost_one_side * 2
        cost_hurdle = self.tunable_params["cost_hurdle_factor"]
        if expected_scalp < estimated_round_trip_cost * cost_hurdle:
            logger.info(
                "Skipping rehedge: expected scalp %.0f < %.1fx cost %.0f",
                expected_scalp, cost_hurdle, estimated_round_trip_cost,
            )
            return []

        # ── Gap #20, #21: Soft vs hard delta decision ──
        T = time_to_expiry(self.state.positions[0].expiry, self._clock()) if self.state.positions[0].expiry else 1/365
        decision = self.risk.hedge_decision(self.state.positions, spot, T)
        self.state.last_hedge_decision = decision
        logger.info("Hedge decision: %s", decision.rationale)

        if decision.use_soft_delta:
            proposals = self._generate_soft_delta_proposals(greeks, spot, T)
        else:
            proposals = self._generate_hard_delta_proposals(greeks, spot)

        # Track gamma scalp P/L
        scalp_pnl = self._estimate_gamma_scalp_pnl(greeks, spot)
        self.state.gamma_scalp_pnl += scalp_pnl
        self.state.rehedge_count += 1

        return proposals

    def generate_eod_report(self) -> Dict:
        """
        End-of-day report with bleed forecast (Gaps #12, #13, #15)
        and three-level neutrality check (Gap #18).
        """
        if not self.state.positions:
            return {"status": "no_positions"}

        spot = self._get_spot_price()
        if not spot or spot <= 0:
            logger.warning("EOD report: spot unavailable; returning degraded report")
            return {"status": "spot_unavailable", "n_positions": len(self.state.positions)}
        T = time_to_expiry(self.state.positions[0].expiry, self._clock()) if self.state.positions[0].expiry else 1/365
        self._update_portfolio_greeks()
        pf = self.state.portfolio_greeks

        # Bleed forecast
        bleed = self.risk.bleed_forecast(self.state.positions, spot, T)
        self.state.bleed_history.append(bleed)

        # Neutrality check
        neutrality = self.risk.neutrality_check(pf)

        # Method of squares
        squares = self.risk.method_of_squares(self.state.positions, spot, T)

        report = {
            "timestamp": datetime.now().isoformat(),
            "spot": spot,
            "portfolio_greeks": {
                "delta": pf.net_delta, "gamma": pf.net_gamma,
                "sg_up": pf.net_shadow_gamma_up, "sg_down": pf.net_shadow_gamma_down,
                "theta": pf.net_theta, "shadow_theta": pf.net_shadow_theta,
                "vega": pf.net_vega, "modified_vega": pf.net_modified_vega,
                "vega_buckets": pf.vega_buckets,
                "alpha": pf.net_alpha,
                "lock_delta_up": pf.lock_delta_up, "lock_delta_down": pf.lock_delta_down,
                "moments": {"m1": pf.moment_1, "m2": pf.moment_2, "m3": pf.moment_3, "m4": pf.moment_4},
            },
            "bleed_forecast": {
                "delta_bleed": bleed.delta_bleed, "gamma_bleed": bleed.gamma_bleed,
                "direction": bleed.bleed_direction,
                "shadow_theta": bleed.shadow_theta_total,
                "forward_bleed": bleed.forward_bleed, "backward_bleed": bleed.backward_bleed,
                "warnings": bleed.warnings,
            },
            "neutrality": neutrality,
            "method_of_squares": squares,
            "pnl_profile": pf.pnl_profile,
            "session_stats": {
                "total_pnl": self.state.total_pnl,
                "gamma_scalp_pnl": self.state.gamma_scalp_pnl,
                "theta_paid": self.state.theta_decay_paid,
                "rehedge_count": self.state.rehedge_count,
                "max_drawdown": self.state.max_drawdown,
            },
        }

        # Log warnings
        for w in bleed.warnings:
            logger.warning("EOD: %s", w)
        if pf.moment_3 != 0 and abs(pf.moment_3) > 0.001:
            logger.warning("EOD: Non-trivial 3rd moment (gamma skew) = %.6f. Hidden directional risk.", pf.moment_3)
        if pf.lock_delta_up != 0 or pf.lock_delta_down != 0:
            logger.info("EOD: Lock delta — Up: %.0f  Down: %.0f", pf.lock_delta_up, pf.lock_delta_down)

        return report

    def reset_state(self):
        """Reset session state. Preserves ATM IV history (cross-session durable)."""
        self.state = HedgeState()
        self._consecutive_losses = 0
        self._circuit_breaker_until = None
        self._daily_loss_stop_date = None

    def get_strategy_metrics(self) -> Dict:
        """Return current strategy metrics for autoresearch evaluation."""
        capital = self.immutable_params["total_capital"]
        total_pnl = self.state.total_pnl
        max_dd = self.state.max_drawdown
        # Flush the current (incomplete) trading day into the daily history
        daily_pnls = list(self.state.daily_pnl_history)
        if self.state._current_day_pnl != 0 or self.state._current_trading_date is not None:
            daily_pnls.append(self.state._current_day_pnl)

        # Sharpe ratio (annualized, assuming daily P/L entries)
        sharpe_ratio = 0.0
        if len(daily_pnls) >= 2:
            arr = np.array(daily_pnls)
            mean_r = np.mean(arr)
            std_r = np.std(arr)
            if std_r > 0:
                sharpe_ratio = (mean_r / std_r) * np.sqrt(252)

        # Calmar ratio
        calmar_ratio = 0.0
        if max_dd > 0:
            calmar_ratio = total_pnl / max_dd

        # Sortino ratio
        sortino_ratio = 0.0
        if len(daily_pnls) >= 2:
            arr = np.array(daily_pnls)
            downside = arr[arr < 0]
            downside_std = np.std(downside) if len(downside) > 0 else 0
            if downside_std > 0:
                sortino_ratio = (np.mean(arr) / downside_std) * np.sqrt(252)

        return {
            "net_pnl": total_pnl,
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "max_drawdown": max_dd,
            "max_drawdown_pct": (max_dd / capital * 100) if capital > 0 else 0,
            "sharpe_ratio": sharpe_ratio,
            "calmar_ratio": calmar_ratio,
            "sortino_ratio": sortino_ratio,
            "gamma_scalp_pnl": self.state.gamma_scalp_pnl,
            "theta_decay_paid": self.state.theta_decay_paid,
            "rehedge_count": self.state.rehedge_count,
            "position_count": len(self.state.positions),
            "total_transaction_costs": self.state.total_transaction_costs,
        }

    # ══════════════════════════════════════════════════════════
    # INTERNAL METHODS
    # ══════════════════════════════════════════════════════════

    def _generate_hard_delta_proposals(self, greeks, spot):
        """Standard futures-based delta hedge using discrete delta (Taleb p.116-121)."""
        delta_to_hedge = -greeks.net_discrete_delta
        lot_size = self._get_lot_size()
        lots = round(delta_to_hedge / lot_size)
        if lots == 0:
            return []
        fut_symbol = self._get_futures_symbol()
        # Fetch live futures price
        fut_price = spot
        try:
            q = self.kite.quote([f"NFO:{fut_symbol}"])
            fut_price = q[f"NFO:{fut_symbol}"]["last_price"]
        except Exception:
            logger.warning("Could not fetch futures price for %s, using spot", fut_symbol)

        return [TradeProposal(
            tradingsymbol=fut_symbol, instrument_token=0,
            strike=0, expiry="", option_type="FUT", lot_size=lot_size,
            quantity=abs(lots), price=fut_price,
            transaction_type="BUY" if lots > 0 else "SELL",
            iv=0, bid_ask_spread_pct=0.01,
            margin_required=fut_price * lot_size * abs(lots) * 0.10,
            rationale=f"HARD delta hedge: {greeks.net_delta:.1f}Δ via {lots} lots futures",
        )]

    def _generate_soft_delta_proposals(self, greeks, spot, T):
        """
        Gap #20: Options-based delta hedge for positions with gamma flip risk.
        Buy options in the zone where gamma is short to avoid amplifying tail risk.
        Uses discrete delta (Taleb p.116-121) for sizing.
        """
        delta_to_hedge = -greeks.net_discrete_delta
        lot_size = self._get_lot_size()
        lots = max(abs(round(delta_to_hedge / lot_size)), 1)
        option_type = "CE" if delta_to_hedge > 0 else "PE"

        # Look up the actual option instrument from the chain
        chain = self._get_options_chain()
        if chain.empty:
            logger.warning("Cannot generate soft delta hedge: options chain unavailable")
            return []

        strike_interval = 50 if self.underlying == "NIFTY" else 100
        strike = round(spot / strike_interval) * strike_interval
        expiry = self.state.positions[0].expiry if self.state.positions else ""

        match = chain[(chain["strike"] == strike) & (chain["instrument_type"] == option_type)]
        if match.empty:
            # Fall back to nearest available strike
            type_chain = chain[chain["instrument_type"] == option_type]
            if type_chain.empty:
                logger.warning("No %s options available for soft delta hedge", option_type)
                return []
            idx = (type_chain["strike"] - strike).abs().idxmin()
            match = type_chain.loc[[idx]]
            strike = float(match.iloc[0]["strike"])

        row = match.iloc[0]
        symbol = row["tradingsymbol"]
        instrument_token = int(row["instrument_token"])

        # Fetch live price
        price = 0.0
        spread = 0.0
        try:
            q = self.kite.quote([f"NFO:{symbol}"])
            quote_data = q[f"NFO:{symbol}"]
            price = quote_data["last_price"]
            bid = quote_data.get("depth", {}).get("buy", [{}])[0].get("price", 0)
            ask = quote_data.get("depth", {}).get("sell", [{}])[0].get("price", 0)
            if bid > 0 and ask > 0:
                spread = (ask - bid) / ((bid + ask) / 2) * 100
        except Exception:
            logger.warning("Could not fetch quote for soft hedge %s", symbol)

        if price <= 0:
            logger.warning("Zero price for %s, cannot place soft delta hedge", symbol)
            return []

        return [TradeProposal(
            tradingsymbol=symbol, instrument_token=instrument_token,
            strike=strike, expiry=str(row.get("expiry", expiry)),
            option_type=option_type, lot_size=lot_size, quantity=lots,
            price=price, transaction_type="BUY", iv=0, bid_ask_spread_pct=spread,
            margin_required=price * lot_size * lots,
            rationale=f"SOFT delta hedge: buy {lots} {option_type} @ {strike} (gamma flip protection)",
        )]

    def _estimate_gamma_scalp_pnl(self, greeks, spot):
        """Use shadow gamma (directional) for more accurate scalp estimate."""
        # Use up or down gamma depending on direction of move
        gamma = greeks.net_shadow_gamma if greeks.net_shadow_gamma != 0 else greeks.net_gamma
        dS = spot * self.tunable_params["gamma_scalp_band_pct"] / 100.0
        return 0.5 * gamma * dS ** 2

    def _proposals_to_contracts(self, proposals):
        """Convert proposals to OptionContract list for analysis."""
        contracts = []
        for p in proposals:
            if p.option_type in ("CE", "PE"):
                contracts.append(OptionContract(
                    tradingsymbol=p.tradingsymbol, instrument_token=p.instrument_token,
                    strike=p.strike, expiry=p.expiry, option_type=p.option_type,
                    lot_size=p.lot_size, quantity=p.quantity if p.transaction_type == "BUY" else -p.quantity,
                    entry_price=p.price, current_price=p.price, iv=p.iv,
                ))
        return contracts

    def _update_portfolio_greeks(self):
        if not self.state.positions:
            self.state.portfolio_greeks = PortfolioGreeks()
            return
        spot = self._get_spot_price()
        if not spot or spot <= 0:
            logger.warning("Cannot update portfolio greeks: spot unavailable; keeping last value")
            return
        T = time_to_expiry(self.state.positions[0].expiry, self._clock()) if self.state.positions[0].expiry else 1/365
        self.state.portfolio_greeks = self.greeks.compute_portfolio_greeks(self.state.positions, spot, T)
        # Include futures hedge in net delta (both analytical and discrete)
        self.state.portfolio_greeks.net_delta += self.state.futures_hedge_delta
        self.state.portfolio_greeks.net_discrete_delta += self.state.futures_hedge_delta
        self.state.theta_decay_paid += abs(self.state.portfolio_greeks.net_shadow_theta)

    def execute_proposals(self, proposals):
        # signals mode: emit each proposal to the dashboard JSONL feed and
        # return without mutating any P&L / position state.
        if self.is_signals_mode:
            return [self._emit_signal(p) for p in proposals]

        results = []
        realized_pnl_before = self.state.realized_pnl
        costs_before = self.state.total_transaction_costs
        # An entry batch is one that begins with no live trade. We capture
        # this BEFORE the loop so the attribution baseline can record state
        # as of "before any entry costs were deducted", which lets gross_pnl
        # and costs each include both sides of the round trip.
        is_entry_batch = (self.state.entry_time is None)
        had_any_close = False

        for prop in proposals:
            result = self._paper_execute(prop) if self.is_paper_mode else self._live_execute(prop)
            results.append(result)

            # Skip state mutation if the live order failed
            if result.get("status") == "FAILED":
                logger.warning("Order FAILED for %s: %s — skipping state update",
                               prop.tradingsymbol, result.get("error", "unknown"))
                continue

            # Deduct transaction costs
            cost = estimate_transaction_cost(
                prop.price, prop.quantity, prop.lot_size, prop.transaction_type,
                instrument_type="FUT" if prop.option_type == "FUT" else "OPT",
            )
            self.state.total_transaction_costs += cost
            self.state.realized_pnl -= cost

            if prop.option_type == "FUT":
                # Track futures hedge as net delta + entry VWAP for P/L
                signed_lots = prop.quantity if prop.transaction_type == "BUY" else -prop.quantity
                delta = signed_lots * prop.lot_size
                self.state.futures_hedge_delta += delta

                old_lots = self.state.futures_lots
                new_lots = old_lots + signed_lots
                if old_lots * signed_lots >= 0:
                    # Adding to position — update VWAP
                    if new_lots != 0:
                        old_notional = old_lots * self.state.futures_entry_vwap
                        add_notional = signed_lots * prop.price
                        self.state.futures_entry_vwap = (old_notional + add_notional) / new_lots
                elif new_lots == 0:
                    # Fully closed — book realized P/L
                    realized = (prop.price - self.state.futures_entry_vwap) * old_lots * prop.lot_size
                    self.state.realized_pnl += realized
                    self.state.futures_entry_vwap = 0.0
                    had_any_close = True
                    logger.info("Closed futures hedge: realized P/L ₹%.0f", realized)
                else:
                    # Flipped direction — close old, open remainder
                    realized = (prop.price - self.state.futures_entry_vwap) * old_lots * prop.lot_size
                    self.state.realized_pnl += realized
                    self.state.futures_entry_vwap = prop.price
                    had_any_close = True
                    logger.info("Flipped futures hedge: realized P/L ₹%.0f", realized)
                self.state.futures_lots = new_lots
                logger.info("Futures hedge delta now: %.1f (%d lots @ %.2f)",
                            self.state.futures_hedge_delta, self.state.futures_lots, self.state.futures_entry_vwap)
            elif prop.option_type in ("CE", "PE"):
                signed_qty = prop.quantity if prop.transaction_type == "BUY" else -prop.quantity
                # Try to net against existing position with same symbol
                existing = next(
                    (p for p in self.state.positions if p.tradingsymbol == prop.tradingsymbol),
                    None,
                )
                if existing is not None:
                    old_qty = existing.quantity
                    new_qty = old_qty + signed_qty
                    if new_qty == 0:
                        # Fully closed — book realized P/L
                        realized = (prop.price - existing.entry_price) * old_qty * existing.lot_size
                        self.state.realized_pnl += realized
                        self.state.positions.remove(existing)
                        had_any_close = True
                        logger.info("Closed %s: realized P/L ₹%.0f", prop.tradingsymbol, realized)
                    else:
                        # Partial close or add to position
                        if old_qty * signed_qty < 0:
                            # Partial close: book realized P/L on the closed portion
                            closed_qty = min(abs(old_qty), abs(signed_qty)) * (1 if old_qty > 0 else -1)
                            realized = (prop.price - existing.entry_price) * closed_qty * existing.lot_size
                            self.state.realized_pnl += realized
                            had_any_close = True
                        existing.quantity = new_qty
                else:
                    # New position
                    self.state.positions.append(OptionContract(
                        tradingsymbol=prop.tradingsymbol, instrument_token=prop.instrument_token,
                        strike=prop.strike, expiry=prop.expiry, option_type=prop.option_type,
                        lot_size=prop.lot_size, quantity=signed_qty,
                        entry_price=prop.price, current_price=prop.price, iv=prop.iv,
                    ))

        if is_entry_batch and self.state.positions:
            self.state.entry_time = self._clock()
            # Snapshot the cumulative counters as of *before* this batch ran
            # so per-trade attribution captures both entry- and exit-side
            # costs in the gross/costs columns. Without the pre-batch snapshot
            # the entry costs are silently absorbed into the baseline.
            entry_legs = [p for p in self.state.positions if p.option_type in ("CE", "PE")]
            avg_entry_iv = (
                sum(p.iv for p in entry_legs) / len(entry_legs) if entry_legs else 0.0
            )
            self.state._attribution_baseline = {
                "entry_time": self.state.entry_time,
                "realized_pnl_at_entry": realized_pnl_before,
                "gamma_scalp_at_entry": self.state.gamma_scalp_pnl,
                "costs_at_entry": costs_before,
                "rehedges_at_entry": self.state.rehedge_count,
                "entry_atm_iv": avg_entry_iv,
                "n_legs": len(entry_legs),
            }
        self._update_portfolio_greeks()

        # If the book is now flat, zero out unrealized P/L and recompute total
        if not self.state.positions and self.state.futures_lots == 0:
            self.state.unrealized_pnl = 0.0
            self.state.total_pnl = self.state.realized_pnl
            # Emit per-trade attribution before clearing the baseline.
            baseline = self.state._attribution_baseline
            if baseline is not None:
                exit_time = self._clock()
                holding_minutes = (exit_time - baseline["entry_time"]).total_seconds() / 60.0
                gross_pnl = self.state.realized_pnl - baseline["realized_pnl_at_entry"]
                trade_costs = self.state.total_transaction_costs - baseline["costs_at_entry"]
                trade_gamma_scalp = self.state.gamma_scalp_pnl - baseline["gamma_scalp_at_entry"]
                # Residual = whatever moved gross PnL beyond gamma scalp net of
                # costs. For a long straddle this is dominated by theta bleed
                # (negative) offset by vega PnL when IV changes.
                residual = gross_pnl + trade_costs - trade_gamma_scalp
                self.state.closed_trades.append({
                    "entry_time": baseline["entry_time"],
                    "exit_time": exit_time,
                    "holding_minutes": holding_minutes,
                    "n_legs": baseline["n_legs"],
                    "n_rehedges": self.state.rehedge_count - baseline["rehedges_at_entry"],
                    "entry_atm_iv": baseline["entry_atm_iv"],
                    "gross_pnl": gross_pnl,
                    "costs": trade_costs,
                    "gamma_scalp": trade_gamma_scalp,
                    "residual": residual,
                })
                self.state._attribution_baseline = None
            self.state.entry_time = None  # Reset so next entry gets a fresh timestamp
            self._record_pnl_snapshot()

        # Evaluate consecutive loss streak based on NET realized P/L of the batch
        if had_any_close:
            batch_realized = self.state.realized_pnl - realized_pnl_before
            if batch_realized > 0:
                self._consecutive_losses = 0

        return results

    # ── Pre-trade checks, price fetching, risk filters (unchanged from v1) ──

    @staticmethod
    def _warn_if_not_ist() -> None:
        """The market-hours gate is timezone-naive; log loudly at startup
        if the system clock isn't on Asia/Kolkata so a TZ misconfig is
        visible in the journal, not expressed as a no-trade day."""
        tz_name = time.tzname[0] if time.tzname else "?"
        if tz_name != "IST":
            logger.error(
                "Local timezone is %r, not IST. Market-hours gate "
                "compares naive datetime.now() against 09:15-15:30 IST "
                "and will be off. Set Environment=TZ=Asia/Kolkata in the "
                "systemd unit.",
                tz_name,
            )

    def _pre_trade_checks(self):
        now = self._clock()
        market_open = now.replace(hour=9, minute=15, second=0)
        close_cutoff = now.replace(hour=15, minute=30, second=0) - timedelta(minutes=self.immutable_params.get("no_trade_last_minutes", 15))
        if not (market_open <= now <= close_cutoff):
            logger.info("Outside trading hours.")
            return False
        if self._circuit_breaker_until and now < self._circuit_breaker_until:
            logger.info("Circuit breaker active until %s", self._circuit_breaker_until)
            return False
        # Daily loss stop: no new entries for the rest of the day
        if self._daily_loss_stop_date == now.date():
            logger.info("Daily loss limit hit — no new entries until next trading day.")
            return False
        if len(self.state.positions) >= self.immutable_params.get("max_positions", 6):
            logger.info("Max positions reached.")
            return False
        return True

    def _should_exit(self, greeks, spot):
        capital = self.immutable_params["total_capital"]

        # Gate: max daily loss (check current day's PnL, not cumulative)
        max_loss = capital * self.immutable_params["max_daily_loss_pct"] / 100
        if self.state._current_day_pnl < -max_loss:
            logger.warning("Max daily loss hit: ₹%.0f (today)", self.state._current_day_pnl)
            self._daily_loss_stop_date = self._clock().date()
            self._record_loss()
            return True

        # Gate: max holding period
        if self.state.entry_time:
            hours_held = (self._clock() - self.state.entry_time).total_seconds() / 3600
            if hours_held > self.tunable_params["max_holding_period_hours"]:
                logger.info("Max holding period exceeded.")
                return True

        # Gate: gap exit — if spot moved > threshold since entry
        if self.state.positions and self.state.entry_time:
            gap_threshold = self.immutable_params["gap_exit_threshold_pct"]
            for pos in self.state.positions:
                if pos.entry_price > 0:
                    pct_move = abs(spot - pos.strike) / pos.strike * 100
                    # Check if underlying has gapped beyond threshold
                    pass  # Strike-based gap is approximate; real gap needs prev close
            # Simpler: check if unrealized loss exceeds gap threshold of capital
            if self.state.unrealized_pnl < -(capital * gap_threshold / 100):
                logger.warning("Gap exit triggered: unrealized P/L ₹%.0f exceeds gap threshold", self.state.unrealized_pnl)
                self._record_loss()
                return True

        # Gate: vega limit (scaled by number of long lots in position)
        vega_limit_per_lot = self.tunable_params["vega_limit"]
        n_long_lots = max(sum(p.quantity for p in self.state.positions if p.quantity > 0), 1)
        effective_vega_limit = vega_limit_per_lot * n_long_lots
        if greeks and abs(greeks.net_vega) > effective_vega_limit:
            logger.warning("Vega limit breached: %.1f > %.1f (%.0f/lot × %d lots)",
                           abs(greeks.net_vega), effective_vega_limit, vega_limit_per_lot, n_long_lots)
            return True

        return False

    def _record_loss(self):
        """Track consecutive losses for circuit breaker."""
        self._consecutive_losses += 1
        max_consec = self.immutable_params["circuit_breaker_consecutive_losses"]
        if self._consecutive_losses >= max_consec:
            pause_min = self.immutable_params["circuit_breaker_pause_minutes"]
            self._circuit_breaker_until = self._clock() + timedelta(minutes=pause_min)
            logger.warning(
                "Circuit breaker activated: %d consecutive losses. Pausing until %s",
                self._consecutive_losses, self._circuit_breaker_until,
            )

    def _get_lot_size(self) -> int:
        """Fetch lot size from instruments. Cached after first call."""
        if self._cached_lot_size:
            return self._cached_lot_size
        try:
            instruments = self.kite.instruments("NFO")
            df = pd.DataFrame(instruments)
            match = df[(df["name"] == self.underlying) & (df["instrument_type"].isin(["CE", "PE"]))]
            if not match.empty:
                self._cached_lot_size = int(match.iloc[0]["lot_size"])
                return self._cached_lot_size
        except Exception:
            pass
        # Fallback: current NSE defaults (as of 2025)
        fallback = {"NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25}
        self._cached_lot_size = fallback.get(self.underlying, 25)
        logger.warning("Using fallback lot size %d for %s", self._cached_lot_size, self.underlying)
        return self._cached_lot_size

    def _get_futures_symbol(self) -> str:
        """Look up the nearest-month futures tradingsymbol from Kite instruments."""
        if self._cached_futures_symbol:
            return self._cached_futures_symbol
        try:
            instruments = self.kite.instruments("NFO")
            df = pd.DataFrame(instruments)
            futs = df[(df["name"] == self.underlying) & (df["instrument_type"] == "FUT")]
            futs = futs.sort_values("expiry")
            if not futs.empty:
                self._cached_futures_symbol = futs.iloc[0]["tradingsymbol"]
                return self._cached_futures_symbol
        except Exception:
            pass
        logger.warning("Could not look up futures symbol for %s, using placeholder", self.underlying)
        return f"{self.underlying}FUT"

    def _spot_quote_key(self) -> str:
        return _INDEX_SPOT_SYMBOLS.get(self.underlying, f"NSE:{self.underlying}")

    def _get_spot_price(self) -> Optional[float]:
        """Fetch underlying spot price.

        Returns None on any failure. Callers MUST handle None and skip
        the tick — never substitute 0.0, that masks symbol/connectivity
        bugs and poisons every downstream Greek calculation with
        math.log(0/K) (see incident 2026-05-04).
        """
        sym = self._spot_quote_key()
        try:
            q = self.kite.quote([sym])
        except Exception as e:
            logger.warning("Spot quote raised for %s: %s: %s",
                           sym, type(e).__name__, e)
            return None
        if not q or sym not in q or not q[sym].get("last_price"):
            logger.warning(
                "Spot quote returned no usable price for %s (got keys=%s). "
                "Check _INDEX_SPOT_SYMBOLS for index underlyings.",
                sym, list(q.keys()) if q else [],
            )
            return None
        return float(q[sym]["last_price"])

    def _check_spot(self, spot: Optional[float]) -> bool:
        """Return True if spot is usable; otherwise log + bump counter."""
        consecutive = getattr(self, "_consecutive_spot_failures", 0)
        if spot and spot > 0:
            if consecutive:
                logger.info("Spot recovered after %d consecutive failure(s).", consecutive)
            self._consecutive_spot_failures = 0
            return True
        consecutive += 1
        self._consecutive_spot_failures = consecutive
        log = logger.error if consecutive >= 5 else logger.warning
        log("No usable spot for %s (consecutive failures=%d) — skipping tick",
            getattr(self, "underlying", "?"), consecutive)
        return False

    def _get_options_chain(self):
        """
        Get options chain for the best available expiry.
        Checks the 2 nearest expiries and picks the one with the most
        strikes around the current spot (handles weekly expiries with
        sparse OTM strikes).
        """
        try:
            instruments = self.kite.instruments("NFO")
            df = pd.DataFrame(instruments)
            df = df[df["name"] == self.underlying]
            df = df[df["instrument_type"].isin(["CE", "PE"])]
            df = df.sort_values("expiry")

            if df.empty:
                return pd.DataFrame()

            expiries = sorted(df["expiry"].unique())
            if not expiries:
                return pd.DataFrame()

            spot = self._get_spot_price()
            if not spot or spot <= 0:
                # Without spot we can't pick the best ATM expiry. Caller
                # treats empty chain as "skip this tick."
                return pd.DataFrame()
            best_chain = pd.DataFrame()
            best_atm_count = -1

            # Check up to 2 nearest expiries, pick the one with best ATM coverage
            for expiry in expiries[:2]:
                chain = df[df["expiry"] == expiry]
                strikes = chain["strike"].unique()
                # Count strikes within 3% of spot (ATM zone)
                atm_zone = [s for s in strikes if abs(s - spot) / spot < 0.03]
                # Need both CE and PE at the ATM strike
                atm_strike = min(strikes, key=lambda s: abs(s - spot)) if len(strikes) > 0 else 0
                has_ce = not chain[(chain["strike"] == atm_strike) & (chain["instrument_type"] == "CE")].empty
                has_pe = not chain[(chain["strike"] == atm_strike) & (chain["instrument_type"] == "PE")].empty

                if has_ce and has_pe and len(atm_zone) > best_atm_count:
                    best_atm_count = len(atm_zone)
                    best_chain = chain

            # Fallback: if no expiry has ATM CE+PE, return nearest anyway
            if best_chain.empty:
                nearest_expiry = expiries[0]
                return df[df["expiry"] == nearest_expiry]

            return best_chain
        except Exception as e:
            consecutive = getattr(self, "_consecutive_chain_failures", 0) + 1
            self._consecutive_chain_failures = consecutive
            log = logger.error if consecutive >= 5 else logger.warning
            log(
                "Options-chain fetch failed (consecutive=%d): %s",
                consecutive, e,
            )
            return pd.DataFrame()

    def _compute_iv_percentile(self, chain, spot):
        """
        Compute IV percentile: where current ATM IV sits relative to
        its own recent history (rolling window). This is the standard
        approach — compare current vol regime against past observations,
        not against the cross-sectional smile at the same tick.
        """
        strikes = chain["strike"].unique()
        atm_strike = strikes[np.argmin(np.abs(strikes - spot))]

        # Compute ATM IV
        atm_ce = chain[(chain["strike"] == atm_strike) & (chain["instrument_type"] == "CE")]
        if atm_ce.empty:
            return 50.0
        try:
            symbol = atm_ce.iloc[0]["tradingsymbol"]
            q = self.kite.quote([f"NFO:{symbol}"])
            atm_price = q[f"NFO:{symbol}"]["last_price"]
            expiry_str = str(atm_ce.iloc[0]["expiry"])
            T = time_to_expiry(expiry_str, self._clock())
            if T <= 0 or atm_price <= 0:
                return 50.0
            atm_iv = implied_volatility_bisect(atm_price, spot, atm_strike, T, 0.065, "CE")
        except Exception:
            return 50.0

        if not (0.01 < atm_iv < 3.0):
            return 50.0

        # Append to rolling history and compute percentile against it
        self._atm_iv_history.append(atm_iv)
        if len(self._atm_iv_history) > self._iv_history_max_size:
            self._atm_iv_history = self._atm_iv_history[-self._iv_history_max_size:]
        self._save_iv_history()

        # Below ~30 observations the rolling window is too thin for a
        # meaningful percentile; return neutral so the filter doesn't bite.
        if len(self._atm_iv_history) < 30:
            return 50.0

        from scipy.stats import percentileofscore
        return percentileofscore(self._atm_iv_history, atm_iv)

    def _record_spot_sample(self, ts: datetime, spot: float):
        """Append a spot quote to the rolling history. Dedup on timestamp so
        the same tick doesn't get counted twice when both scan_and_propose
        and check_and_rehedge fire on it."""
        if not (spot > 0):
            return
        if self._spot_history and self._spot_history[-1][0] == ts:
            return
        self._spot_history.append((ts, spot))
        if len(self._spot_history) > self._spot_history_max_size:
            self._spot_history = self._spot_history[-self._spot_history_max_size:]

    def _compute_realized_vol(self, window_days: float) -> Optional[float]:
        """Annualized realized vol from the rolling spot window.

        Uses the scaled-return estimator sigma^2 ≈ mean(r_i^2 / dt_i_years)
        which handles irregular sampling correctly under GBM. 365-day
        annualization to match the IV convention used elsewhere in the
        codebase (see greeks_engine.time_to_expiry).

        Returns None if fewer than 10 valid spot samples exist within the
        window — the gate is then permissive during warmup.
        """
        if len(self._spot_history) < 10:
            return None
        cutoff = self._clock() - timedelta(days=window_days)
        samples = [(ts, s) for ts, s in self._spot_history if ts >= cutoff]
        if len(samples) < 10:
            return None
        sumsq = 0.0
        n = 0
        YEAR_SECONDS = 365.0 * 86400.0
        for i in range(1, len(samples)):
            dt_seconds = (samples[i][0] - samples[i-1][0]).total_seconds()
            if dt_seconds <= 0:
                continue
            dt_years = dt_seconds / YEAR_SECONDS
            r = float(np.log(samples[i][1] / samples[i-1][1]))
            sumsq += r * r / dt_years
            n += 1
        if n < 5:
            return None
        return float(np.sqrt(sumsq / n))

    def _iv_history_path(self) -> Path:
        return Path("data_cache") / f"iv_history_{self.underlying}.json"

    def _load_iv_history(self):
        if not self._persist_iv_history:
            return
        path = self._iv_history_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            hist = data.get("atm_iv", []) if isinstance(data, dict) else data
            self._atm_iv_history = [float(x) for x in hist if 0.01 < float(x) < 3.0]
            logger.info("Loaded %d ATM IV observations from %s", len(self._atm_iv_history), path)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("Could not load IV history from %s: %s", path, e)

    def _save_iv_history(self):
        if not self._persist_iv_history:
            return
        path = self._iv_history_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "underlying": self.underlying,
                "atm_iv": self._atm_iv_history,
                "updated_at": datetime.now().isoformat(),
            }))
        except OSError as e:
            logger.warning("Could not save IV history to %s: %s", path, e)

    def _apply_risk_filters(self, proposals, spot):
        """
        All-or-nothing risk filter: if ANY leg fails liquidity or margin checks,
        reject the entire structure. This prevents malformed partial positions
        (e.g. keeping a short wing after its protective long is filtered out).
        """
        capital = self.immutable_params["total_capital"]
        max_margin_pct = self.immutable_params["max_position_margin_pct"]
        max_margin = capital * max_margin_pct / 100

        cumulative_margin = 0.0
        for prop in proposals:
            # Gate: liquidity check
            if prop.bid_ask_spread_pct > self.immutable_params.get("liquidity_min_spread_pct", 1.0):
                logger.info("Filtered %s: spread %.2f%% too wide — rejecting entire structure",
                            prop.tradingsymbol, prop.bid_ask_spread_pct)
                return []
            # Gate: margin check
            cumulative_margin += prop.margin_required
            if cumulative_margin > max_margin:
                logger.info("Filtered %s: cumulative margin ₹%.0f exceeds %.0f%% of capital — rejecting entire structure",
                            prop.tradingsymbol, cumulative_margin, max_margin_pct)
                return []
        return proposals

    def _generate_close_all_proposals(self):
        proposals = [TradeProposal(
            tradingsymbol=p.tradingsymbol, instrument_token=p.instrument_token,
            strike=p.strike, expiry=p.expiry, option_type=p.option_type,
            lot_size=p.lot_size, quantity=abs(p.quantity), price=p.current_price,
            transaction_type="SELL" if p.quantity > 0 else "BUY",
            iv=p.iv, bid_ask_spread_pct=0.0, margin_required=0.0,
            rationale="Close all (safety trigger)",
        ) for p in self.state.positions]

        # Close futures hedge if any
        if abs(self.state.futures_hedge_delta) > 0:
            lot_size = self._get_lot_size()
            fut_lots = abs(round(self.state.futures_hedge_delta / lot_size))
            if fut_lots > 0:
                proposals.append(TradeProposal(
                    tradingsymbol=self._get_futures_symbol(),
                    instrument_token=0, strike=0, expiry="", option_type="FUT",
                    lot_size=lot_size, quantity=fut_lots,
                    price=self._get_spot_price() or 0.0,
                    transaction_type="SELL" if self.state.futures_hedge_delta > 0 else "BUY",
                    iv=0, bid_ask_spread_pct=0.0, margin_required=0.0,
                    rationale="Close futures hedge (safety trigger)",
                ))
        return proposals

    def _record_pnl_snapshot(self):
        """Record P/L delta into daily buckets and update drawdown. Called after any total_pnl change."""
        pnl_delta = self.state.total_pnl - self.state._prev_snapshot_pnl
        self.state._prev_snapshot_pnl = self.state.total_pnl

        today = self._clock().date()
        if self.state._current_trading_date is None:
            self.state._current_trading_date = today
        if today != self.state._current_trading_date:
            self.state.daily_pnl_history.append(self.state._current_day_pnl)
            self.state._current_day_pnl = pnl_delta
            self.state._current_trading_date = today
        else:
            self.state._current_day_pnl += pnl_delta

        self._update_drawdown()

    def _update_drawdown(self):
        """Update peak P/L and max drawdown from current total_pnl."""
        if self.state.total_pnl > self.state.peak_pnl:
            self.state.peak_pnl = self.state.total_pnl
        dd = self.state.peak_pnl - self.state.total_pnl
        if dd > self.state.max_drawdown:
            self.state.max_drawdown = dd

    def _update_positions_prices(self, spot):
        for pos in self.state.positions:
            try:
                q = self.kite.quote([f"{self.exchange}:{pos.tradingsymbol}"])
                pos.current_price = q[list(q.keys())[0]]["last_price"]
            except Exception as e:
                # Don't carry stale marks forward — clearing forces the
                # downstream rehedge math to either get fresh quotes next
                # tick or skip. Silent fallback to the last good price
                # (the previous behaviour) was the 2026-05-04 incident class.
                consecutive = getattr(self, "_consecutive_quote_failures", 0) + 1
                self._consecutive_quote_failures = consecutive
                log = logger.error if consecutive >= 5 else logger.warning
                log(
                    "Quote failed for %s (consecutive=%d): %s",
                    pos.tradingsymbol, consecutive, e,
                )
                pos.current_price = pos.entry_price
        unrealized = sum((p.current_price - p.entry_price) * p.quantity * p.lot_size for p in self.state.positions)
        # Futures unrealized P/L: (current_spot - entry_vwap) * net_lots * lot_size
        if self.state.futures_lots != 0 and self.state.futures_entry_vwap > 0:
            lot_size = self._get_lot_size()
            unrealized += (spot - self.state.futures_entry_vwap) * self.state.futures_lots * lot_size
        self.state.unrealized_pnl = unrealized
        self.state.total_pnl = self.state.realized_pnl + unrealized

        self._record_pnl_snapshot()

    def _paper_execute(self, proposal):
        logger.info("[PAPER] %s %d lots %s @ %.2f — %s", proposal.transaction_type, proposal.quantity, proposal.tradingsymbol, proposal.price, proposal.rationale)
        return {"order_id": f"PAPER-{int(time.time())}", "status": "COMPLETE", "mode": "paper"}

    def _live_execute(self, proposal):
        try:
            validate_order(proposal)
        except OrderValidationError as e:
            logger.error("Order rejected pre-submit: %s — %s", e, proposal)
            return {"order_id": None, "status": "REJECTED", "error": str(e), "mode": "live"}
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR, exchange="NFO",
                tradingsymbol=proposal.tradingsymbol,
                transaction_type=self.kite.TRANSACTION_TYPE_BUY if proposal.transaction_type == "BUY" else self.kite.TRANSACTION_TYPE_SELL,
                quantity=abs(proposal.quantity) * proposal.lot_size,
                product=self.kite.PRODUCT_NRML, order_type=self.kite.ORDER_TYPE_LIMIT,
                price=proposal.price, validity=self.kite.VALIDITY_DAY,
            )
            return {"order_id": order_id, "status": "PENDING", "mode": "live"}
        except Exception as e:
            return {"order_id": None, "status": "FAILED", "error": str(e), "mode": "live"}
