"""
Kalman Pair Trading Strategy — time-varying hedge ratio
=======================================================
Long-short on a cointegrated NIFTY 50 stock-futures pair, but with the hedge
ratio γ_t tracked by a Kalman filter (Palomar Ch.15 §15.6) instead of the
static screener β used by strategies/pair_trading.py. Phase 1 of the separate
Kalman system (tasks/kalman-pair-system-plan.md).

How it differs from the static PairTradingStrategy
--------------------------------------------------
  • Hedge ratio γ_t and intercept μ_t come from a `KalmanPairFilter`, updated
    once per trading day on the official close (decision D1 — daily updates,
    matching the book and avoiding the 2026-05-13 intraday-std-collapse). The
    filter is seeded from a training window via the §15.6.3 OLS heuristic.
  • The tradeable spread is the filter's causal normalized spread z_t (uses the
    PREDICTED state α_{t|t-1} — no look-ahead). Intraday, entry/exit decisions
    recompute that spread from live prices using the day's frozen predicted
    state, then z-score it against a rolling window of the daily Kalman spreads
    (decision D3 — rolling-z on the Kalman spread).
  • β-lock: on entry we freeze the entry-time (μ, γ) into the position and
    manage the open position against them, so a drifting filter cannot move the
    exit band out from under a live position (mirrors the static system's
    locked hedge_ratio). The filter keeps tracking in the background.

Scope (Phase 1): decision logic + paper fills + cross-session persistence, all
testable without Kite via an injected `quote_fn`. Live execution and futures
resolution are Phase 3 (the runner). Signal emission uses base `_emit_signal`
(what `main` has); the richer §4 contract (`_publish_signal`) lives on an
unmerged branch — `_publish` is the single seam to swap it in when it lands.

Convention note: the spread uses LOG prices, matching the book —
spread = (log p_a − γ·log p_b − μ)/(1+|γ|) (normalized by gross leverage 1+|γ|;
identical to the book's 1+γ for γ>0, non-degenerate for γ<0). γ is therefore an
elasticity (~1 for cointegrated pairs), so leg sizing is dollar-weighted
(notional_b ≈ γ·notional_a) and the ₹-per-spread-point carries a price factor
(d·log P = return). The Kalman
filter, fills, and P&L all see actual prices only at the boundary — the filter
is fed log prices; fills and realized/unrealized P&L use raw prices.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Dict, List, Literal, Optional, Tuple

import numpy as np

from trade_proposer import TradeProposal

from .base import BaseStrategy, ExecutionMode
from .kalman_filter import KalmanPairFilter, Model
# Reuse the static system's leg dataclass, trading-day counter, and holiday
# loader rather than duplicating them (Rule 2 / Rule 8 — read before reuse).
from .pair_trading import PairLeg, _load_holidays, _trading_days_between

logger = logging.getLogger(__name__)

PairPosition = Literal["FLAT", "LONG_SPREAD", "SHORT_SPREAD"]

# Same tradeable-β guard as the static system: outside this band leg B is
# either negligible (no hedge) or notionally explosive relative to leg A.
HEDGE_RATIO_MIN = 0.1
HEDGE_RATIO_MAX = 10.0


@dataclass
class KalmanPairState:
    position: PairPosition = "FLAT"
    entry_z: float = 0.0
    entry_time: Optional[datetime] = None
    entry_spread: float = 0.0
    effective_stop_z: float = 0.0
    # β-lock: the (μ, γ) the position was entered under. The open position's
    # spread/z is computed against these, not the drifting filter state.
    entry_mu: float = 0.0
    entry_gamma: float = 0.0
    legs: List[PairLeg] = field(default_factory=list)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_transaction_costs: float = 0.0
    closed_trades: List[dict] = field(default_factory=list)
    mean_revert_streak: int = 0
    realized_at_entry: float = 0.0
    tx_costs_at_entry: float = 0.0


class KalmanPairStrategy(BaseStrategy):

    name = "kalman_pair_trading"

    def __init__(
        self,
        kite,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
        *,
        symbol_a: str,
        symbol_b: str,
        tradingsymbol_a: str,
        tradingsymbol_b: str,
        lot_size_a: int,
        lot_size_b: int,
        training_a: np.ndarray,
        training_b: np.ndarray,
        model: Model = "momentum",
        alpha: Optional[float] = None,
        quote_fn: Optional[Callable[[str], Optional[float]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        super().__init__(kite, config_path=config_path, mode=mode)
        cfg = (
            dict(self.config["kalman_pair_trading"])
            if self.config.has_section("kalman_pair_trading")
            else {}
        )

        self.symbol_a, self.symbol_b = symbol_a, symbol_b
        self.tradingsymbol_a, self.tradingsymbol_b = tradingsymbol_a, tradingsymbol_b
        self.lot_size_a, self.lot_size_b = int(lot_size_a), int(lot_size_b)
        self.model = model

        # Risk band — same defaults/rationale as the static pair system.
        self.entry_z = float(cfg.get("entry_z", 2.0))
        self.exit_z = float(cfg.get("exit_z", 0.75))
        self.stop_z = float(cfg.get("stop_z", 4.0))
        self.max_entry_z = float(cfg.get("max_entry_z", 5.0))
        self.safety_buffer = float(cfg.get("safety_buffer", 0.75))
        self.lookback_days = int(cfg.get("lookback_days", 60))
        self.lots_per_leg = int(cfg.get("lots_per_leg", 1))
        self.exit_debounce_ticks = max(1, int(cfg.get("exit_debounce_ticks", 2)))
        self.max_holding_days = int(cfg.get("max_holding_days", 7))
        self.paper_slippage_bps = float(cfg.get("paper_slippage_bps", 5.0))
        # Cost-hurdle (parity with the production static system): refuse entries
        # whose expected ₹ move from z_now back to the exit band is below
        # min_edge_multiplier × round-trip cost. Without it the strategy churns
        # on cost-unworthy z-crossings (the 2026-05-13 bleed). 0 disables.
        self.min_edge_multiplier = float(cfg.get("min_edge_multiplier", 1.5))
        mln = str(cfg.get("max_leg_notional", "")).strip()
        self.max_leg_notional: Optional[float] = float(mln) if mln else None
        if self.mode != "signals" and self.max_leg_notional is None:
            raise ValueError(
                "max_leg_notional must be set in [kalman_pair_trading] config "
                f"when mode={self.mode!r}; refusing to run without a notional cap"
            )
        self.total_capital = self.config.getfloat(
            "strategy", "total_capital", fallback=500000
        )

        self._quote_fn = quote_fn
        self._clock = clock or datetime.now
        self._holidays_set = _load_holidays()

        # Build and seed the filter from the training window — fed LOG prices.
        log_a = np.log(np.asarray(training_a, dtype=float))
        log_b = np.log(np.asarray(training_b, dtype=float))
        if not (np.all(np.isfinite(log_a)) and np.all(np.isfinite(log_b))):
            raise ValueError("training prices must be positive (log-price model)")
        self._filter = KalmanPairFilter.from_training(
            log_a, log_b, model=model, alpha=alpha,
        )
        # Daily Kalman spreads — the rolling z-score window (D3). Seeded by
        # replaying the filter over the training window so the z-score has a
        # distribution from day one.
        self._spread_history: List[float] = []
        self._seed_spread_history(log_a, log_b)
        # Predicted state to use for TODAY's intraday decisions (α_{t+1|t} after
        # the latest daily update). Captured here and in step_daily_close.
        self._mu_today = float(self._filter.a[0])
        self._gamma_today = float(self._filter.a[1])
        # Sanity-guard the seed elasticity: a γ wildly outside [0.1, 10] points
        # at corrupted training data, not a tradeable hedge.
        if not HEDGE_RATIO_MIN <= abs(self._gamma_today) <= HEDGE_RATIO_MAX:
            raise ValueError(
                f"seed hedge ratio |γ|={abs(self._gamma_today):.3f} out of "
                f"[{HEDGE_RATIO_MIN}, {HEDGE_RATIO_MAX}] for "
                f"{symbol_a}/{symbol_b}"
            )

        self.state = KalmanPairState()
        self._session_start_realized = 0.0
        self._session_start_unrealized = 0.0
        self._pending_exit_reason: Optional[str] = None
        self._last_risk_band: Optional[dict] = None
        # Date of the last daily filter step — used by the runner to detect/
        # catch up missed sessions after a restart (see step_daily_close).
        self._last_step_date: Optional[date] = None

    # ──────────────────────────────────────────────────────────────────
    # Filter / spread plumbing
    # ──────────────────────────────────────────────────────────────────
    # The filter starts at the OLS prior, so its first innovations are a
    # convergence transient (prior → steady state), not representative dispersion.
    # Drop a burn-in so the seeded z-window std reflects steady-state behavior
    # rather than the startup transient (which would bias early-session z-scores).
    _SEED_BURN_IN = 20

    def _seed_spread_history(self, log_a, log_b) -> None:
        """Replay the freshly-seeded filter over the (log-price) training window
        to build the initial daily-spread distribution for the rolling z-score.
        Uses a throwaway clone so the live filter's state is the post-training
        prior, not advanced past it. The first _SEED_BURN_IN steps (the filter's
        convergence transient) are skipped so the seed std isn't distorted by
        startup. Always keeps at least the tail so a short window still seeds."""
        clone = KalmanPairFilter.deserialize(self._filter.serialize())
        log_a = np.asarray(log_a, float)
        log_b = np.asarray(log_b, float)
        burn = self._SEED_BURN_IN if len(log_a) > 2 * self._SEED_BURN_IN else 0
        for i, (a, b) in enumerate(zip(log_a, log_b)):
            spread = clone.update(float(a), float(b)).spread
            if i >= burn:
                self._spread_history.append(spread)

    @property
    def hedge_ratio(self) -> float:
        """The hedge ratio used for sizing/decisions: the entry-locked γ while a
        position is open (β-lock), else the filter's current predicted γ."""
        if self.state.position != "FLAT":
            return self.state.entry_gamma
        return self._gamma_today

    def step_daily_close(self, close_a: float, close_b: float) -> float:
        """D1: advance the Kalman filter one trading day on the official close.
        Appends the day's Kalman spread to the rolling z-window and refreshes
        the predicted state used for intraday decisions. The runner calls this
        once per session; on restart after missed sessions it is also called
        once per missed bhavcopy day to catch the filter up (see the runner's
        catch_up_filters). Returns the day's Kalman spread."""
        if close_a <= 0 or close_b <= 0:
            raise ValueError("prices must be positive (log-price model)")
        step = self._filter.update(float(np.log(close_a)), float(np.log(close_b)))
        self._spread_history.append(step.spread)
        # α_{t+1|t} — the predicted state for the NEXT day's intraday decisions.
        self._mu_today = float(self._filter.a[0])
        self._gamma_today = float(self._filter.a[1])
        self._last_step_date = self._clock().date()
        # β-lock drift warning (parity with pair_trading._warn_on_std_drift): an
        # open position is z-scored against this evolving window while its spread
        # is priced with the entry-locked (μ,γ). If the window has drifted enough
        # that the entry_spread now standardizes far from the saved entry_z, the
        # stop/exit bands fire against a shifted distribution — surface it.
        if self.state.position != "FLAT":
            stats = self._rolling_window_stats()
            if stats is not None:
                mean, std = stats
                z_now = (self.state.entry_spread - mean) / std
                if abs(z_now - self.state.entry_z) > 0.5:
                    logger.warning(
                        "[%s/%s] z-window drift: entry_z=%.2f now standardizes "
                        "to %.2f (Δ=%.2f σ); exit/stop bands are calibrated to "
                        "the entry distribution.",
                        self.symbol_a, self.symbol_b, self.state.entry_z, z_now,
                        z_now - self.state.entry_z,
                    )
        return step.spread

    def _current_state(self) -> Tuple[float, float]:
        """(μ, γ) to price the spread with: entry-locked when open, else today's
        predicted state."""
        if self.state.position != "FLAT":
            return self.state.entry_mu, self.state.entry_gamma
        return self._mu_today, self._gamma_today

    def _normalized_spread(self, price_a: float, price_b: float) -> float:
        if price_a <= 0 or price_b <= 0:
            return float("nan")
        mu, gamma = self._current_state()
        # Leverage-one normalization by gross leverage 1+|γ| (matches
        # KalmanPairFilter and the ₹-per-point conversion in _structure_risk_band
        # / _expected_edge_passes_cost_hurdle). Signed (1+γ) would disagree with
        # those for γ<0 and vanish near γ=−1.
        denom = 1.0 + abs(gamma)
        if denom < 1e-8:
            return float("nan")
        return (np.log(price_a) - gamma * np.log(price_b) - mu) / denom

    def _observe_spread(self) -> Tuple[Optional[float], Dict[str, float]]:
        """Fetch live prices for both legs via the injected quote_fn and return
        (normalized_spread, {symbol: price}). Returns (None, {}) if either quote
        is missing — never fabricates a spread."""
        if self._quote_fn is None:
            raise RuntimeError("quote_fn not configured (Phase 3 wires Kite)")
        pa = self._quote_fn(self.tradingsymbol_a)
        pb = self._quote_fn(self.tradingsymbol_b)
        # Reject missing / non-finite / non-positive quotes. Kite returns
        # last_price=0 for halted/illiquid instruments; a 0 would pass an
        # isfinite-only check, drive a NaN spread, yet still leak into the
        # prices dict and corrupt _update_unrealized (false 5-figure loss
        # persisted to state + EOD). None checks come first so `<= 0` never
        # evaluates against None.
        if (pa is None or pb is None or pa <= 0 or pb <= 0
                or not (np.isfinite(pa) and np.isfinite(pb))):
            return None, {}
        spread = self._normalized_spread(float(pa), float(pb))
        if not np.isfinite(spread):   # e.g. γ ≈ -1 → denominator blows up
            return None, {}
        return spread, {self.symbol_a: float(pa), self.symbol_b: float(pb)}

    def _rolling_window_stats(self) -> Optional[Tuple[float, float]]:
        recent = self._spread_history[-self.lookback_days:]
        if len(recent) < max(20, self.lookback_days // 4):
            return None
        mean, std = float(np.mean(recent)), float(np.std(recent))
        if std == 0:
            return None
        return mean, std

    def _z_score(self, spread: Optional[float]) -> Optional[float]:
        if spread is None or not np.isfinite(spread):
            return None
        stats = self._rolling_window_stats()
        if stats is None:
            return None
        mean, std = stats
        return (spread - mean) / std

    # ──────────────────────────────────────────────────────────────────
    # Strategy interface
    # ──────────────────────────────────────────────────────────────────
    def scan_and_propose(self) -> List[TradeProposal]:
        if self.state.position != "FLAT":
            return []
        spread, prices = self._observe_spread()
        z = self._z_score(spread)
        if z is None:
            return []
        if abs(z) >= self.max_entry_z:   # regime break, not a signal
            return []
        if z <= -self.entry_z:
            return self._build_entry_proposals("LONG_SPREAD", z, spread, prices)
        if z >= self.entry_z:
            return self._build_entry_proposals("SHORT_SPREAD", z, spread, prices)
        return []

    def check_and_rehedge(self) -> List[TradeProposal]:
        if self.state.position == "FLAT":
            return []
        spread, prices = self._observe_spread()
        if spread is None:
            return []
        z = self._z_score(spread)
        self._update_unrealized(prices)

        if self.state.entry_time:
            held = _trading_days_between(
                self.state.entry_time.date(), self._clock().date(),
                self._holidays_set,
            )
            if held >= self.max_holding_days:
                return self._build_exit_proposals("MAX_HOLD", z or 0.0, prices)
        if z is None:
            return []
        if abs(z) <= self.exit_z:
            self.state.mean_revert_streak += 1
            if self.state.mean_revert_streak >= self.exit_debounce_ticks:
                return self._build_exit_proposals("MEAN_REVERT", z, prices)
            return []
        self.state.mean_revert_streak = 0
        stop = self.state.effective_stop_z or self.stop_z
        if abs(z) >= stop:
            return self._build_exit_proposals("STOP", z, prices)
        return []

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        if not proposals:
            return []
        if self.is_signals_mode:
            self._publish(proposals, is_entry=(self.state.position == "FLAT"))
            return [self._emit_signal(p) for p in proposals]
        if self.is_live_mode:
            raise NotImplementedError("live execution is wired in Phase 3")

        # paper mode
        is_entry = (self.state.position == "FLAT")
        self._publish(proposals, is_entry=is_entry)
        # Snapshot the per-trade P&L/cost baseline BEFORE applying entry fills,
        # so _record_close's per-trade realized_pnl and transaction_costs include
        # BOTH entry and exit sides. Capturing it after the fills (as
        # _set_position_from_legs would) silently drops the entry-side costs and
        # biases win-rate / profitable-pair counts optimistic.
        pre_realized = self.state.realized_pnl
        pre_costs = self.state.total_transaction_costs
        results = []
        for prop in proposals:
            fill_price = self._paper_fill_price(prop)
            self._apply_fill(prop, fill_price)
            results.append({
                "order_id": f"PAPER-{self._clock().timestamp():.0f}",
                "status": "COMPLETE", "average_price": fill_price,
                "filled_lots": prop.quantity, "mode": "paper",
            })
        if is_entry:
            self._set_position_from_legs()
            self.state.realized_at_entry = pre_realized
            self.state.tx_costs_at_entry = pre_costs
        elif not self.state.legs:
            self._record_close()
            self._reset_after_close()
        return results

    def generate_eod_report(self) -> Dict:
        spread, _ = (self._observe_spread() if self._quote_fn else (None, {}))
        z = self._z_score(spread)
        return {
            "strategy": self.name,
            "pair": (self.symbol_a, self.symbol_b),
            "model": self.model,
            "hedge_ratio": self.hedge_ratio,
            "gamma_filter": self._gamma_today,
            "mu_filter": self._mu_today,
            "position": self.state.position,
            "current_z": z,
            "entry_z": self.state.entry_z,
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "transaction_costs": self.state.total_transaction_costs,
            "n_closed_trades": len(self.state.closed_trades),
            "spread_history_size": len(self._spread_history),
            "risk_band": self._last_risk_band,
            "session_realized_delta": (
                self.state.realized_pnl - self._session_start_realized
            ),
            "session_unrealized_delta": (
                self.state.unrealized_pnl - self._session_start_unrealized
            ),
        }

    # ──────────────────────────────────────────────────────────────────
    # Proposal builders
    # ──────────────────────────────────────────────────────────────────
    def _build_entry_proposals(self, direction: PairPosition, z: float,
                               spread: float, prices: Dict[str, float]
                               ) -> List[TradeProposal]:
        pa, pb = prices[self.symbol_a], prices[self.symbol_b]
        sized = self._size_legs(pa, pb)
        if sized is None:
            logger.info("[%s/%s] entry skipped: notional cap infeasible",
                        self.symbol_a, self.symbol_b)
            return []
        lots_a, lots_b = sized
        if not self._expected_edge_passes_cost_hurdle(z, pa, pb, lots_a, lots_b):
            return []
        # Leg directions. For γ > 0 the legs are opposed (BUY A / SELL B for
        # LONG_SPREAD); for γ < 0 (inversely-cointegrated pair — passes the
        # |γ| guard) ∂spread/∂log p_b flips sign, so leg B takes the SAME side
        # as A. Mirror pair_trading.py's beta_sign branch (parity).
        beta_sign = 1 if self.hedge_ratio >= 0 else -1
        if direction == "LONG_SPREAD":
            txn_a, txn_b = "BUY", ("SELL" if beta_sign > 0 else "BUY")
        else:
            txn_a, txn_b = "SELL", ("BUY" if beta_sign > 0 else "SELL")
        self._last_risk_band = self._structure_risk_band(z, lots_a, pa)
        rationale = (f"{direction} z={z:.2f} γ={self.hedge_ratio:.4f} "
                     f"({self.model} Kalman)")
        return [
            self._make_proposal(self.tradingsymbol_a, self.lot_size_a, lots_a,
                                pa, txn_a, rationale),
            self._make_proposal(self.tradingsymbol_b, self.lot_size_b, lots_b,
                                pb, txn_b, rationale),
        ]

    def _build_exit_proposals(self, reason: str, z: float,
                              prices: Dict[str, float]) -> List[TradeProposal]:
        self._pending_exit_reason = reason
        rationale = f"EXIT ({reason}) z={z:.2f}"
        proposals = []
        for leg in self.state.legs:
            px = prices.get(leg.symbol, leg.current_price)
            # Close = opposite side of the open quantity.
            txn = "SELL" if leg.quantity > 0 else "BUY"
            proposals.append(self._make_proposal(
                leg.tradingsymbol, leg.lot_size, abs(leg.quantity), px, txn,
                rationale,
            ))
        return proposals

    def _structure_risk_band(self, entry_z: float, lots_a: int,
                             price_a: float) -> Optional[dict]:
        """Structure-scoped stop/target band in z and in ₹ (the spread is a
        function of both legs, so risk is STRUCTURE-scoped — same reasoning as
        the static system's §4.5 directives).

        Log-price ₹ conversion: the traded portfolio P&L per unit of the
        UN-normalized log-spread is the leg-A notional (d·log P = return, and
        the dollar-weighted hedge holds γ·notional_a in B). The normalized
        spread folds in 1/(1+|γ|), so ₹ per unit normalized-spread ≈
        notional_a·(1+|γ|) = lots_a·lot_a·price_a·(1+|γ|)."""
        stats = self._rolling_window_stats()
        if stats is None:
            return None
        _, std = stats
        eff_stop_z = max(self.stop_z, abs(entry_z) + self.safety_buffer)
        inr_per_point = (
            lots_a * self.lot_size_a * price_a * (1.0 + abs(self.hedge_ratio))
        )
        stop_inr = max(eff_stop_z - abs(entry_z), 0.0) * std * inr_per_point
        target_inr = max(abs(entry_z) - self.exit_z, 0.0) * std * inr_per_point
        return {
            "scope": "STRUCTURE",
            "entry_z": round(entry_z, 4),
            "effective_stop_z": round(eff_stop_z, 4),
            "exit_z": self.exit_z,
            "spread_std": round(std, 6),
            "stop_inr": round(stop_inr, 2),
            "target_inr": round(target_inr, 2),
        }

    def _expected_edge_passes_cost_hurdle(self, z_now: float, price_a: float,
                                          price_b: float, lots_a: int,
                                          lots_b: int) -> bool:
        """True if expected ₹ gain at mean-reversion ≥ multiplier × round-trip
        cost (parity with pair_trading._expected_edge_passes_cost_hurdle, log-
        price form). Expected Δ(normalized spread) = (|z|−exit_z)·std; ₹ per
        unit ≈ leg-A notional·(1+|γ|) (the log-price conversion derived in
        _structure_risk_band). Round-trip = entry+exit on both legs at current
        quotes (conservative: exit prices unknown at entry)."""
        if self.min_edge_multiplier <= 0:
            return True
        stats = self._rolling_window_stats()
        if stats is None:
            return False
        _, std = stats
        expected_dspread = (abs(z_now) - self.exit_z) * std
        if expected_dspread <= 0:
            return False
        inr_per_point = (
            lots_a * self.lot_size_a * price_a * (1.0 + abs(self.hedge_ratio))
        )
        expected_gain_inr = expected_dspread * inr_per_point

        from strategies.taleb_karpathy import estimate_transaction_cost
        rt_cost = sum(
            estimate_transaction_cost(px, lots, lot, side, instrument_type="FUT")
            for px, lots, lot in (
                (price_a, lots_a, self.lot_size_a),
                (price_b, lots_b, self.lot_size_b),
            )
            for side in ("BUY", "SELL")
        )
        return expected_gain_inr >= self.min_edge_multiplier * rt_cost

    def _size_legs(self, price_a: float, price_b: float
                   ) -> Optional[Tuple[int, int]]:
        """lots_a fixed at lots_per_leg; lots_b dollar-weighted so leg B holds
        |γ|·(leg-A notional) — the correct hedge for a log-price spread (γ is an
        elasticity). If either ratio-correct leg exceeds max_leg_notional, skip
        the entry (return None) — we never scale-and-distort the ratio, which
        would leave net directional exposure."""
        gamma = abs(self.hedge_ratio)
        lots_a = self.lots_per_leg
        notional_a = price_a * self.lot_size_a * lots_a
        lots_b = max(1, round(gamma * notional_a / (self.lot_size_b * price_b)))
        notional_b = price_b * self.lot_size_b * lots_b
        # If either ratio-correct leg exceeds the cap, skip — do NOT scale down
        # (int-truncating both legs distorts the dollar-weighted hedge ratio and
        # leaves net directional exposure; for lots_per_leg=1 it silently floors
        # to 0 lots). A pair we can't size neutrally within the cap is one we
        # don't trade. Fail loud (logged), not silent.
        if self.max_leg_notional and max(notional_a, notional_b) > self.max_leg_notional:
            logger.info(
                "[%s/%s] entry skipped: leg notional ₹%.0f > cap ₹%.0f "
                "(raise --max-leg-notional or lower lots_per_leg)",
                self.symbol_a, self.symbol_b,
                max(notional_a, notional_b), self.max_leg_notional,
            )
            return None
        return lots_a, lots_b

    def _make_proposal(self, tradingsymbol: str, lot_size: int, quantity: int,
                       price: float, transaction_type: str, rationale: str
                       ) -> TradeProposal:
        notional = price * lot_size * quantity
        return TradeProposal(
            tradingsymbol=tradingsymbol, instrument_token=0, strike=0.0,
            expiry="", option_type="FUT", lot_size=int(lot_size),
            quantity=int(quantity), price=float(price),
            transaction_type=transaction_type, iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=notional * 0.20, rationale=rationale,
        )

    # ──────────────────────────────────────────────────────────────────
    # Fill handling / state updates (paper)
    # ──────────────────────────────────────────────────────────────────
    def _paper_fill_price(self, prop: TradeProposal) -> float:
        """Apply one-way slippage: BUY fills above, SELL below (we cross the
        spread). Live (Phase 3) uses real fills."""
        slip = self.paper_slippage_bps / 1e4
        return prop.price * (1 + slip) if prop.transaction_type == "BUY" \
            else prop.price * (1 - slip)

    def _apply_fill(self, prop: TradeProposal, fill_price: float) -> None:
        symbol = self.symbol_a if prop.tradingsymbol == self.tradingsymbol_a \
            else self.symbol_b
        signed = prop.quantity if prop.transaction_type == "BUY" else -prop.quantity

        from strategies.taleb_karpathy import estimate_transaction_cost
        cost = estimate_transaction_cost(
            fill_price, prop.quantity, prop.lot_size, prop.transaction_type,
            instrument_type="FUT",
        )
        self.state.total_transaction_costs += cost
        self.state.realized_pnl -= cost

        existing = next((l for l in self.state.legs if l.symbol == symbol), None)
        if existing is None:
            self.state.legs.append(PairLeg(
                symbol=symbol, tradingsymbol=prop.tradingsymbol,
                lot_size=prop.lot_size, quantity=signed,
                entry_price=fill_price, current_price=fill_price,
            ))
            return
        old, new = existing.quantity, existing.quantity + signed
        if new == 0:
            self.state.realized_pnl += (
                (fill_price - existing.entry_price) * old * existing.lot_size
            )
            self.state.legs.remove(existing)
        elif old * signed < 0:
            closed = min(abs(old), abs(signed)) * (1 if old > 0 else -1)
            self.state.realized_pnl += (
                (fill_price - existing.entry_price) * closed * existing.lot_size
            )
            existing.quantity = new
        else:
            existing.entry_price = (
                existing.entry_price * old + fill_price * signed
            ) / new
            existing.quantity = new

    def _set_position_from_legs(self) -> None:
        leg_a = next((l for l in self.state.legs if l.symbol == self.symbol_a), None)
        leg_b = next((l for l in self.state.legs if l.symbol == self.symbol_b), None)
        if leg_a is None or leg_b is None:
            return
        self.state.position = "LONG_SPREAD" if leg_a.quantity > 0 else "SHORT_SPREAD"
        self.state.entry_time = self._clock()
        # β-lock: freeze the (μ, γ) used to manage this position.
        self.state.entry_mu = self._mu_today
        self.state.entry_gamma = self._gamma_today
        entry_spread = self._normalized_spread(leg_a.entry_price, leg_b.entry_price)
        self.state.entry_spread = entry_spread
        self.state.entry_z = self._z_score(entry_spread) or 0.0
        self.state.effective_stop_z = max(
            self.stop_z, abs(self.state.entry_z) + self.safety_buffer,
        )
        # realized_at_entry / tx_costs_at_entry are set by execute_proposals from
        # the PRE-entry-fill baseline (so per-trade P&L includes entry costs).

    def _update_unrealized(self, prices: Dict[str, float]) -> None:
        unreal = 0.0
        for leg in self.state.legs:
            cur = prices.get(leg.symbol, leg.current_price)
            leg.current_price = cur
            unreal += (cur - leg.entry_price) * leg.quantity * leg.lot_size
        self.state.unrealized_pnl = unreal

    def _record_close(self) -> None:
        self.state.closed_trades.append({
            "exit_time": self._clock(),
            "entry_time": self.state.entry_time,
            "entry_z": self.state.entry_z,
            "entry_spread": self.state.entry_spread,
            "entry_gamma": self.state.entry_gamma,
            "realized_pnl": self.state.realized_pnl - self.state.realized_at_entry,
            "transaction_costs": (
                self.state.total_transaction_costs - self.state.tx_costs_at_entry
            ),
            "cumulative_realized_pnl": self.state.realized_pnl,
            "position": self.state.position,
            "exit_reason": self._pending_exit_reason,
        })

    def _reset_after_close(self) -> None:
        self.state.position = "FLAT"
        self.state.entry_time = None
        self.state.entry_z = 0.0
        self.state.entry_spread = 0.0
        self.state.effective_stop_z = 0.0
        self.state.entry_mu = 0.0
        self.state.entry_gamma = 0.0
        self.state.mean_revert_streak = 0
        self.state.unrealized_pnl = 0.0
        self._pending_exit_reason = None

    # ──────────────────────────────────────────────────────────────────
    # Signal publishing (seam for the §4 contract)
    # ──────────────────────────────────────────────────────────────────
    def _publish(self, proposals: List[TradeProposal], *, is_entry: bool) -> None:
        """Telemetry seam. On `main` only base._emit_signal (per-leg JSONL) is
        available, so signals mode already covers it; this is the hook where the
        multi-leg §4 contract (`_publish_signal`, structure risk directives) gets
        wired once that branch merges to main. Intentionally a no-op for now in
        paper mode so we don't double-log alongside _emit_signal."""
        return

    # ──────────────────────────────────────────────────────────────────
    # Cross-session persistence — MUST round-trip the FULL filter state.
    # ──────────────────────────────────────────────────────────────────
    def serialize_state(self) -> Dict:
        """Snapshot for the paper runner. Unlike the static system (which
        re-seeds its z-window from bhavcopy), we persist BOTH the full Kalman
        filter state (vector + covariance) AND the daily-spread history —
        neither is recoverable from bhavcopy alone without replaying the filter,
        and restoring only γ would reset the filter's uncertainty and corrupt
        tracking (plan Phase-1 requirement)."""
        return {
            "pair": [self.symbol_a, self.symbol_b],
            "model": self.model,
            "filter": self._filter.serialize(),
            "spread_history": list(self._spread_history),
            "mu_today": self._mu_today,
            "gamma_today": self._gamma_today,
            "last_step_date": (self._last_step_date.isoformat()
                               if self._last_step_date else None),
            "state": {
                "position": self.state.position,
                "entry_z": self.state.entry_z,
                "entry_time": (self.state.entry_time.isoformat()
                               if self.state.entry_time else None),
                "entry_spread": self.state.entry_spread,
                "effective_stop_z": self.state.effective_stop_z,
                "entry_mu": self.state.entry_mu,
                "entry_gamma": self.state.entry_gamma,
                "legs": [
                    {"symbol": l.symbol, "tradingsymbol": l.tradingsymbol,
                     "lot_size": l.lot_size, "quantity": l.quantity,
                     "entry_price": l.entry_price, "current_price": l.current_price}
                    for l in self.state.legs
                ],
                "realized_pnl": self.state.realized_pnl,
                "unrealized_pnl": self.state.unrealized_pnl,
                "total_transaction_costs": self.state.total_transaction_costs,
                "mean_revert_streak": self.state.mean_revert_streak,
                "realized_at_entry": self.state.realized_at_entry,
                "tx_costs_at_entry": self.state.tx_costs_at_entry,
                "closed_trades": [self._serialise_trade(t)
                                  for t in self.state.closed_trades],
            },
        }

    def restore_state(self, blob: Dict) -> None:
        """Inverse of serialize_state(). Fails loud on a pair mismatch — a state
        file for a different pair must not silently load (Rule 12)."""
        pair = tuple(blob.get("pair", []))
        if pair and pair != (self.symbol_a, self.symbol_b):
            raise ValueError(
                f"state file pair {pair} != configured "
                f"({self.symbol_a}, {self.symbol_b})"
            )
        self._filter = KalmanPairFilter.deserialize(blob["filter"])
        self._spread_history = list(blob.get("spread_history", []))
        self._mu_today = float(blob.get("mu_today", self._filter.a[0]))
        self._gamma_today = float(blob.get("gamma_today", self._filter.a[1]))
        lsd = blob.get("last_step_date")
        self._last_step_date = date.fromisoformat(lsd) if lsd else None

        s = blob["state"]
        self.state = KalmanPairState(
            position=s.get("position", "FLAT"),
            entry_z=float(s.get("entry_z", 0.0)),
            entry_time=(datetime.fromisoformat(s["entry_time"])
                        if s.get("entry_time") else None),
            entry_spread=float(s.get("entry_spread", 0.0)),
            effective_stop_z=float(s.get("effective_stop_z", 0.0)),
            entry_mu=float(s.get("entry_mu", 0.0)),
            entry_gamma=float(s.get("entry_gamma", 0.0)),
            legs=[PairLeg(**leg) for leg in s.get("legs", [])],
            realized_pnl=float(s.get("realized_pnl", 0.0)),
            unrealized_pnl=float(s.get("unrealized_pnl", 0.0)),
            total_transaction_costs=float(s.get("total_transaction_costs", 0.0)),
            mean_revert_streak=int(s.get("mean_revert_streak", 0)),
            realized_at_entry=float(s.get("realized_at_entry", 0.0)),
            tx_costs_at_entry=float(s.get("tx_costs_at_entry", 0.0)),
            closed_trades=[self._deserialise_trade(t)
                           for t in s.get("closed_trades", [])],
        )
        self._session_start_realized = self.state.realized_pnl
        self._session_start_unrealized = self.state.unrealized_pnl

    @staticmethod
    def _serialise_trade(trade: Dict) -> Dict:
        out = dict(trade)
        for k in ("exit_time", "entry_time"):
            if isinstance(out.get(k), datetime):
                out[k] = out[k].isoformat()
        return out

    @staticmethod
    def _deserialise_trade(blob: Dict) -> Dict:
        out = dict(blob)
        for k in ("exit_time", "entry_time"):
            if isinstance(out.get(k), str):
                try:
                    out[k] = datetime.fromisoformat(out[k])
                except ValueError:
                    pass
        return out
