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
import math
import json
import hashlib
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field, fields

import numpy as np
import pandas as pd

# Cost model moved verbatim to core/costs.py (2026-07-21) — one canonical
# home instead of a strategy module doubling as shared infrastructure. The
# re-export keeps every existing import site (pair_trading, kalman pairs,
# arbitrage, risk_analyzer, replay, tests) and the tests' monkeypatch target
# `strategies.taleb_karpathy.estimate_transaction_cost` working unchanged.
from core.costs import (  # noqa: F401  (re-export)
    _FUT_EXCHANGE_RATE,
    _FUT_EXCHANGE_RATE_LEGACY,
    estimate_transaction_cost,
)
from core.data_cache_io import find_tables, read_table
from core.greeks_engine import (
    GreeksEngine, OptionContract, PortfolioGreeks,
    implied_volatility_bisect, time_to_expiry,
)
from core.trade_proposer import TradeProposer, TradeProposal
from core.risk_analyzer import (
    RiskAnalyzer, MonteCarloReport, StabilityReport,
    BleedForecast, HedgeDecision,
)
from core.regime_classifier import (
    RegimeFeatures, Structure, Thresholds as RegimeThresholds, classify,
)

from .base import BaseStrategy, ExecutionMode

# Process-level memo for the daily ATM IV pool (see _load_daily_atm_iv).
# Keyed by (underlying, min_dte, max_dates, archive fingerprint) so a
# changed EOD archive can never be served from a stale entry. Module level
# rather than per-instance because the point is to share it ACROSS the
# hundreds of strategy constructions an autoresearch sweep performs.
_DAILY_ATM_IV_CACHE: Dict[tuple, List[Tuple[date, float]]] = {}

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

# Audit 3.5: bleed_history / stability_history are append-only per-tick
# diagnostics that nothing reads back and that serialize_state deliberately
# skips. Bound them so a long or wedged session can't grow them without limit
# (a normal session is ~360 ticks; this only bites pathological cases).
_DIAG_HISTORY_CAP = 500

# Audit 3.5: cap closed_trades written to the (per-tick-rewritten) state file.
# The dashboard reads today-only, and a session closes well under this many,
# so today's trades survive a same-day restart while the file stops growing
# unboundedly across sessions.
_CLOSED_TRADES_PERSIST = 200


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
    # Phase-1 attribution (2026-07-18 fitness redesign): theoretical realized-
    # variance P&L, accrued as ½·Γ_shadow·(ΔS)² on EVERY greeks update while
    # positions exist — independent of whether a rehedge was emitted. The
    # existing gamma_scalp_pnl accrues only on emitted rehedges, so it freezes
    # for weeks when the Whalley-Wilmott gate blocks (Phase-0 finding F1/F4);
    # the ratio gamma_scalp_pnl / theoretical_scalp_pnl is the scalp-capture
    # efficiency the new fitness objective needs. Attribution only — never
    # feeds a trading decision.
    theoretical_scalp_pnl: float = 0.0
    _last_scalp_anchor_spot: Optional[float] = None
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
    # The contract the hedge was actually entered on, and its last good
    # LTP. Both are STATE, not caches: they must survive a restart and a
    # monthly roll. _get_futures_symbol() resolves whatever is front-month
    # *now*, so after settlement it returns the NEXT month — marking that
    # against futures_entry_vwap would book the calendar spread as phantom
    # P&L, i.e. re-introduce the exact bug this accounting fix exists to
    # remove. Always mark the contract you hold.
    futures_symbol: str = ""          # Tradingsymbol the hedge sits in
    futures_last_mark: float = 0.0    # Last good LTP for futures_symbol
    # ── New: Bleed tracking ──
    bleed_history: List[BleedForecast] = field(default_factory=list)
    stability_history: List[StabilityReport] = field(default_factory=list)
    last_hedge_decision: Optional[HedgeDecision] = None
    monte_carlo_report: Optional[MonteCarloReport] = None
    # Anchor for realized-theta integration. Set on first tick that has
    # positions, cleared when book goes flat. theta_decay_paid is
    # accumulated as -net_shadow_theta × (now − anchor) / 1 day.
    _last_theta_anchor_time: Optional[datetime] = None
    # Anchor for realized gamma-scalp P&L. Set on entry and after each
    # rehedge. The realized scalp at rehedge is 0.5 × |γ| × (ΔS)² where
    # ΔS is the actual spot move since the anchor — not a static band.
    _last_rehedge_spot: Optional[float] = None
    # C2: wall-clock of the last emitted rehedge, for the cooldown gate.
    # Set when a rehedge is actually emitted, cleared when the book goes flat.
    _last_rehedge_time: Optional[datetime] = None
    # Phase 4 layering: structure *types* currently on the book (e.g.
    # ["straddle", "risk_reversal_long_put"]). scan_and_propose appends the
    # entered structure on each open and consults this list to refuse layering
    # a structure whose type is already held — the type-difference invariant
    # the docstring promises but _count_active_structures (expiry-based) cannot
    # enforce. Cleared when the book goes flat.
    active_structure_types: List[str] = field(default_factory=list)


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
            # ── Gap #2 (2026-06-04): MC expected-value entry gate ──
            # Reject entries whose Monte Carlo MEAN path P/L is below this
            # floor (₹). Gates on EXPECTANCY, not win-rate: a long-gamma
            # straddle is positive-skew (low win-rate, positive EV by
            # design), so a pct_profitable floor would wrongly reject the
            # convex trades the book exists to hold. Default 0.0 = refuse
            # negative-expectancy entries. Set strongly negative to disable.
            "mc_min_mean_pnl": self.config.getfloat("strategy", "mc_min_mean_pnl", fallback=0.0),
            # ── Cost-aware rehedge: min scalp/cost ratio to proceed (Taleb Ch 16) ──
            "cost_hurdle_factor": self.config.getfloat("strategy", "cost_hurdle_factor", fallback=1.5),
            # ── Realized-vs-implied vol regime gate ──
            # Long straddle is profitable when realized vol > implied. The
            # ratio threshold (default 1.0 = require RV ≥ IV) and rolling
            # window (default 5 days) gate entries on the structural thesis.
            "min_rv_iv_ratio": self.config.getfloat("strategy", "min_rv_iv_ratio", fallback=1.0),
            "rv_window_days": self.config.getfloat("strategy", "rv_window_days", fallback=5.0),
            # ── Phase 1.3: put-skew percentile gate (Taleb Ch 8 / Ch 15) ──
            # Skew = IV(25Δ put) − IV(25Δ call). Percentile-ranked over a
            # rolling window. When skew is unusually rich, an ATM straddle
            # pays a premium it can't recover via delta-hedging — the
            # right structure is a risk reversal or ratio, not the body.
            # Default 80 means: reject straddle entry when current skew
            # sits in the top quintile of its history. 100 disables.
            "skew_pct_max": self.config.getfloat("strategy", "skew_pct_max", fallback=80.0),
            # Phase 3.1: enable regime → structure dispatch. When True,
            # scan_and_propose routes through regime_classifier.classify
            # and may emit any of {straddle, calendar, risk reversal,
            # backspread, asymmetric strangle, no_trade}. When False
            # (default during Phase 3 rollout), only straddles are
            # emitted — same behaviour as before Phase 3.
            "enable_regime_dispatch": self.config.getboolean(
                "strategy", "enable_regime_dispatch", fallback=False,
            ),
            # Phase 4: cap on parallel structures the book can hold.
            # 1 = legacy (one structure at a time). 2-3 = layered books
            # that the regime classifier can use to combine e.g. a
            # straddle with a hedging risk reversal. Higher than ~3
            # makes the attribution / exit logic harder to reason
            # about and risks oversizing aggregate notional.
            "max_layered_structures": self.config.getint(
                "strategy", "max_layered_structures", fallback=1,
            ),
            # Phase 5: T-0 (expiry day) band tightening factor. When a
            # leg has < 1 day to expiry, multiply the rehedge band by
            # this factor. 1.0 = disabled (legacy). 0.33 = aggressive
            # sticky-strike harvest. Tightens the band only; the cost
            # gate continues to filter sub-EV rehedges.
            "t0_band_factor": self.config.getfloat(
                "strategy", "t0_band_factor", fallback=1.0,
            ),
            # Phase 3.1 regime-classifier cutoffs. These are the thresholds the
            # classifier ACTUALLY routes on when enable_regime_dispatch=True, so
            # they must be tunable for the autoresearch loop to optimise the
            # structure-routing it picks (core/regime_classifier.py promised this but
            # the call site passed no Thresholds — the cutoffs were inert).
            # Fallbacks mirror regime_classifier.Thresholds defaults exactly.
            "regime_straddle_iv_pct_max": self.config.getfloat(
                "strategy", "regime_straddle_iv_pct_max", fallback=60.0),
            "regime_straddle_rv_iv_ratio_min": self.config.getfloat(
                "strategy", "regime_straddle_rv_iv_ratio_min", fallback=1.0),
            "regime_straddle_skew_pct_max": self.config.getfloat(
                "strategy", "regime_straddle_skew_pct_max", fallback=70.0),
            "regime_calendar_iv_pct_min": self.config.getfloat(
                "strategy", "regime_calendar_iv_pct_min", fallback=70.0),
            "regime_calendar_skew_pct_max": self.config.getfloat(
                "strategy", "regime_calendar_skew_pct_max", fallback=60.0),
            "regime_risk_reversal_skew_pct_min": self.config.getfloat(
                "strategy", "regime_risk_reversal_skew_pct_min", fallback=80.0),
            "regime_backspread_vvol_min": self.config.getfloat(
                "strategy", "regime_backspread_vvol_min", fallback=0.15),
            "regime_asymmetric_strangle_rv_iv_min": self.config.getfloat(
                "strategy", "regime_asymmetric_strangle_rv_iv_min", fallback=1.30),
            "regime_asymmetric_strangle_skew_pct_min": self.config.getfloat(
                "strategy", "regime_asymmetric_strangle_skew_pct_min", fallback=70.0),
            # C2: rehedge-churn bounds. The band trigger + WW cost gate decide
            # whether a rehedge is +EV; these cap how often and how big it can
            # be so an optimistic scalp estimate can't churn the book into a
            # cost bleed. Each is disabled by setting it to 0.
            "max_rehedge_lots_per_tick": self.config.getint(
                "strategy", "max_rehedge_lots_per_tick", fallback=20,
            ),
            "rehedge_cooldown_seconds": self.config.getfloat(
                "strategy", "rehedge_cooldown_seconds", fallback=180.0,
            ),
            "max_rehedges_per_session": self.config.getint(
                "strategy", "max_rehedges_per_session", fallback=20,
            ),
        }

        # Overlay autoresearch optimum on top of config defaults so the
        # output of runners/run_autoresearch.py actually reaches live trading.
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
            # Issue #160: how the MC entry-gate paths are generated —
            # "gbm" (iid Gaussian, pre-#160 behaviour) or "bootstrap"
            # (block-bootstrap of real daily returns). IMMUTABLE on purpose:
            # the autoresearch sweep must never flip the gate's distribution
            # mid-search. Default gbm = merge changes nothing; the operator
            # enables bootstrap per-config, paper first (safety rule 3).
            "mc_path_source": self._read_mc_path_source(),
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
        # Same ledger for the futures leg (H-6a). Without it a frozen
        # futures mark or a repeatedly-refused hedge logs at the same
        # severity forever, which is the silent-carry failure mode the
        # option-leg ledger exists to prevent.
        self._consecutive_futures_failures = 0
        self._clock = datetime.now  # Override for backtest replay
        # Persistent ATM IV history survives across runs so IV percentile is
        # computed against a real multi-session distribution. Backtests disable
        # persistence to keep experiments independent.
        self._persist_iv_history = True
        self._iv_history_max_size = 500
        self._atm_iv_history: List[float] = []
        # Daily ATM IV pool bounds (see _load_daily_atm_iv). Expiry-day IV
        # back-solves are unstable — T → 0 inflates the implied vol that any
        # pinning/settlement noise implies — so the pool skips the last
        # _daily_iv_min_dte days of a cycle. Measured on the EOD snapshots:
        # mean ATM IV 0.315 at DTE ≤ 3 vs 0.175 beyond it (BANKNIFTY), and a
        # 0.70 outlier at DTE ≤ 3 (NIFTY). Without the filter every mid-cycle
        # tick ranks against a pool inflated by those readings.
        self._daily_iv_min_dte = 3
        self._daily_iv_max_dates = 250
        # Rolling spot history is used to estimate realized vol for the
        # RV/IV entry gate. Live ticks are appended in-memory; backtests
        # rebuild it forward. Cron paper sessions are oneshot, so the
        # history is also seeded from the daily EOD CSV at startup —
        # without this seed the gate is permissive every morning until
        # ~10 minutes of intraday ticks accumulate, by which point an
        # entry has typically already fired.
        self._spot_history: List[Tuple[datetime, float]] = []
        self._spot_history_max_size = 2000  # ~1.5 days of 1-min ticks or weeks of 5-min
        # Issue #160: DATED daily close-to-close log returns (date, return)
        # from the same EOD snapshot _load_spot_history parses — the MC entry
        # gate's empirical pool when mc_path_source=bootstrap. A SEPARATE list
        # from _spot_history on purpose: that one is tick-appended and capped
        # at 2000 samples, so a few live sessions would evict the daily
        # anchors and silently starve the bootstrap back to Gaussian. Rebuilt
        # each session start (the runner restarts daily; the EOD file is
        # refreshed by the nightly fetch). Dates are kept so the gate can
        # filter to returns strictly BEFORE the current session — a no-op live
        # (newest EOD is yesterday's) but essential in tape replay, where the
        # newest EOD snapshot otherwise leaks future returns into a past
        # session's gate (look-ahead).
        self._daily_return_history: List[Tuple[date, float]] = []
        # Phase 1.3: rolling history of (25Δ put IV − 25Δ call IV) used to
        # rank current skew. Persisted alongside _atm_iv_history.
        self._skew_history: List[float] = []
        # DATED daily ATM IV pool — the distribution _compute_iv_percentile
        # ranks against. A SEPARATE list from _atm_iv_history for exactly the
        # reason _daily_return_history is separate from _spot_history (#160).
        #
        # The defect is not "the tick series is too smooth" — it is that the
        # tick series is tick-appended and capped at _iv_history_max_size, so
        # the span of wall-clock it covers is ARBITRARY and depends on how
        # often the runner reached the IV solve. Measured 2026-08-02 on the
        # persisted series (500 obs each):
        #
        #            tick sd   tick sd/mean   daily sd   ratio d/t
        #   BANKNIFTY  0.0037       2.9%       0.0318      8.5x
        #   NIFTY      0.0920      45.8%       0.0539      0.59x
        #
        # BANKNIFTY's 500 obs span ~1.5 sessions (range 0.1177-0.1362), so
        # its "percentile" ranked the tick against intraday micro-noise —
        # readings of 100.0 → 5.4 → 81.4 within eight minutes on 2026-07-31,
        # making the [8,43] band a per-tick coin flip. NIFTY's 500 obs span
        # MONTHS (range 0.0656-0.3285) because its runner solves IV far less
        # often per session, so there the same window is a stale multi-month
        # mixture. Neither is a defined reference distribution, and the two
        # fail in opposite directions — which is the point: one observation
        # per session is the only construction that means the same thing on
        # every underlying.
        #
        # Dates are kept so tape replay can exclude observations from the
        # session under test and later (look-ahead guard) — the same reason
        # #160 dated its bootstrap pool.
        self._daily_atm_iv_history: List[Tuple[date, float]] = []
        self._load_iv_history()
        self._load_spot_history()
        self._load_daily_atm_iv()

    # ══════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════

    def scan_and_propose(self) -> List[TradeProposal]:
        """Full scan → filter → stability test → MC sizing → propose cycle.

        Phase 4: when `max_layered_structures` > 1, allow a second
        (or third…) structure to layer on top of an existing book —
        provided the new structure's type differs from any active one
        and the same per-entry filters (vega cap, alpha cap, MC sizing)
        still pass. Each layer is still treated as a discrete entry by
        the attribution baseline; the existing close-all-on-flat logic
        continues to work because per-leg netting is unaffected.

        max_layered_structures = 1 (default) preserves the legacy
        one-structure-at-a-time invariant — required by the existing
        attribution / exit / MC code paths until they're audited for
        multi-structure books in a follow-up.
        """
        # Phase 4 layering check. Robust to tests / contexts that
        # bypass __init__ (no tunable_params dict yet) — fall back to
        # legacy "one structure at a time" behaviour.
        tunable = getattr(self, "tunable_params", {}) or {}
        max_layers = tunable.get("max_layered_structures", 1)
        if self.state.positions:
            # T-0 block: never layer on expiry day. _count_active_structures
            # counts unique expiries, so same-expiry/same-strike straddles
            # stack as "1 structure" and slip past the max_layers gate —
            # on 2026-05-26 (May expiry) this let the book grow to 24 lots
            # in 13 minutes; 9 rehedges burned ₹14,279 / 93% of gross loss.
            now = self._clock()
            min_days_to_exp = float("inf")
            for p in self.state.positions:
                if not p.expiry:
                    continue
                try:
                    exp = datetime.fromisoformat(p.expiry[:10])
                    days = (exp - now).total_seconds() / 86400.0
                    if days < min_days_to_exp:
                        min_days_to_exp = days
                except (TypeError, ValueError):
                    continue
            if min_days_to_exp < 1.0:
                logger.info(
                    "Layering disabled on expiry day (min %.2f days to "
                    "expiry) — managing existing structure(s) only.",
                    min_days_to_exp,
                )
                return []

            existing_struct_count = self._count_active_structures()
            if existing_struct_count >= max_layers:
                return []
            # Layering allowed, but only when regime dispatch is on —
            # the legacy straddle-only path doesn't make sense to
            # layer (would just be more of the same).
            if not tunable.get("enable_regime_dispatch", False):
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
        # Phase 3.2: chain now spans up to two expiries so the calendar
        # builder can fire. IV percentile, skew percentile, and the
        # legacy straddle path are single-expiry by construction, so
        # they consume the primary slice. The full chain is forwarded
        # only to `propose_for_structure` below (regime dispatch path),
        # which is the path that may route to CALENDAR.
        primary = self._primary_expiry_slice(chain)

        iv_percentile = self._compute_iv_percentile(primary, spot)
        if iv_percentile is None:
            # ATM quote gap / unsolvable IV / warmup — cannot assess the vol
            # regime this tick. Skip rather than trade blind or block on a
            # fabricated neutral 50.0 (issue #75). Distinct from the
            # mid-range "outside band" log below so the two are separable.
            logger.info("IV percentile not computable this tick — waiting.")
            return []
        iv_min = self.tunable_params["entry_iv_percentile_min"]
        iv_max = self.tunable_params["entry_iv_percentile_max"]
        if not (iv_min <= iv_percentile <= iv_max):
            logger.info("IV percentile %.1f outside [%.0f, %.0f]. Waiting.", iv_percentile, iv_min, iv_max)
            return []

        # Phase 1.3 / 3.1: compute skew percentile (mutates _skew_history)
        # whether or not the legacy gate is enabled, so the regime
        # classifier always has a fresh observation. The legacy gate
        # remains in place for operators who want a hard block on rich
        # skew; the regime classifier instead ROUTES rich-skew regimes
        # to RISK_REVERSAL_LONG_PUT.
        skew_pct = self._compute_skew_percentile(primary, spot)
        skew_max = self.tunable_params.get("skew_pct_max", 100.0)
        regime_enabled = self.tunable_params.get(
            "enable_regime_dispatch", False,
        )
        if skew_max < 100.0 and not regime_enabled and skew_pct > skew_max:
            logger.info(
                "Skew percentile %.1f > max %.1f — ATM straddle would "
                "pay rich downside skew; waiting (or enable regime "
                "dispatch to route to risk reversal)",
                skew_pct, skew_max,
            )
            return []

        # RV/IV regime gate: long-straddle thesis is RV > IV. Without this,
        # we pay theta and earn gamma that roughly cancels in calm regimes.
        # Returns None during warmup, in which case the gate is permissive.
        # When regime dispatch is enabled, RV/IV becomes a FEATURE not
        # a HARD BLOCK — biased-asset / backspread regimes legitimately
        # trade at low RV/IV.
        atm_iv = self._atm_iv_history[-1] if self._atm_iv_history else None
        rv_window = self.tunable_params.get("rv_window_days", 5.0)
        min_ratio = self.tunable_params.get("min_rv_iv_ratio", 1.0)
        realized_vol = self._compute_realized_vol(rv_window)
        rv_iv_ratio = None
        if atm_iv and realized_vol is not None:
            rv_iv_ratio = realized_vol / atm_iv
            if not regime_enabled and rv_iv_ratio < min_ratio:
                logger.info(
                    "RV/IV ratio %.2f < min %.2f (RV %.1f%% / IV %.1f%% over %.1fd) — waiting for vol expansion",
                    rv_iv_ratio, min_ratio, realized_vol*100, atm_iv*100, rv_window,
                )
                return []

        # Phase 3.1 dispatch: when enabled, the regime classifier picks
        # the structure. When disabled (default), preserve legacy
        # behaviour (always long ATM straddle).
        if regime_enabled:
            # vol-of-vol from recent ATM IV samples — std / mean.
            vvol = None
            if len(self._atm_iv_history) >= 10:
                tail = self._atm_iv_history[-20:]
                mean_iv = float(np.mean(tail))
                if mean_iv > 0:
                    vvol = float(np.std(tail)) / mean_iv
            features = RegimeFeatures(
                iv_percentile=iv_percentile,
                rv_iv_ratio=rv_iv_ratio if rv_iv_ratio is not None else 1.0,
                skew_percentile=skew_pct,
                vol_of_vol=vvol,
            )
            # Build classifier thresholds from the regime_* tunables, falling
            # back to RegimeThresholds defaults for any key a partial
            # tunable_params omits (keeps callers that set a subset working).
            tp = self.tunable_params
            thresholds = RegimeThresholds(**{
                f.name: tp[f"regime_{f.name}"]
                for f in fields(RegimeThresholds)
                if f"regime_{f.name}" in tp
            })
            structure = classify(features, thresholds)
            logger.info(
                "Regime classifier → %s (iv_pct=%.1f, rv_iv=%.2f, "
                "skew_pct=%.1f, vvol=%s)",
                structure.value, iv_percentile,
                rv_iv_ratio if rv_iv_ratio is not None else float("nan"),
                skew_pct,
                f"{vvol:.3f}" if vvol is not None else "n/a",
            )
            if structure == Structure.NO_TRADE:
                return []
            # Type-difference layering gate. _count_active_structures counts
            # unique expiries, so two same-expiry/same-type structures (e.g. a
            # straddle layered on a straddle) slip past the max_layers count.
            # Refuse to stack a structure whose type is already on the book —
            # the invariant this method's docstring promises. Only applies when
            # layering (positions held); a flat book has no active types.
            if self.state.positions and structure.value in self.state.active_structure_types:
                logger.info(
                    "Layering blocked: structure '%s' already active (book holds "
                    "%s) — refusing to stack the same structure type.",
                    structure.value, self.state.active_structure_types,
                )
                return []
            entered_structure = structure.value
            proposals = self.proposer.propose_for_structure(
                structure=structure.value, chain=chain, spot=spot,
                capital=self.immutable_params["total_capital"],
                position_size_pct=self.tunable_params["position_size_pct"],
                greeks_engine=self.greeks,
            )
        else:
            # Legacy single-expiry straddle — pass primary slice only.
            entered_structure = "straddle"
            proposals = self.proposer.propose_delta_neutral(
                chain=primary, spot=spot,
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
            if len(self.state.stability_history) > _DIAG_HISTORY_CAP:
                del self.state.stability_history[:-_DIAG_HISTORY_CAP]
            if not stability.is_stable:
                for w in stability.warnings:
                    logger.warning("Stability: %s", w)

            # ── Gap #19: Monte Carlo sizing ──
            # Derive a deterministic seed from the entry context so replays are
            # reproducible. Use hashlib (stable across processes) rather than
            # Python's built-in hash() which is salted by PYTHONHASHSEED.
            seed_payload = f"{self._clock().isoformat()}|{round(spot, 2)}".encode()
            mc_seed = int(hashlib.sha256(seed_payload).hexdigest()[:8], 16)
            # Calibrate the simulated path vol to the CURRENT realized vol
            # instead of the hardcoded 1%/day (~16% ann.). For an RV-vs-IV
            # strategy the sign of the simulated edge is an artifact of this
            # number: sim-RV above position IV flatters every long-gamma
            # entry regardless of market conditions. 365-day annualization
            # matches _compute_realized_vol / greeks_engine conventions.
            rv = self._compute_realized_vol(
                self.tunable_params.get("rv_window_days", 5.0))
            # `is not None`: a legitimately flat window (rv == 0.0) must NOT
            # fall back to 16%-annualized fake vol — zero dispersion is the
            # honest simulation of a dead-calm regime (and correctly starves
            # a long-gamma entry of scalp). Fail loud on the fallback: on
            # warmup — and on EVERY tape replay, where a session fires one
            # scan before _spot_history has 5 in-window samples — the gate
            # runs on the old miscalibrated constant, and that must be
            # visible in the log, not silent (Rule 12). Tape-side fix is
            # spot-history seeding in run_backtest (tracked in issue #92).
            if rv is not None:
                mc_daily_vol = rv / math.sqrt(365.0)
            else:
                mc_daily_vol = 0.01
                logger.info(
                    "MC vol calibration: realized vol unavailable (warmup/"
                    "tape replay) — falling back to 0.01/day (~16%% ann.); "
                    "the expectancy gate is running on the uncalibrated "
                    "constant this tick.")
            # Issue #160: with mc_path_source=bootstrap, feed the gate real
            # daily returns (block-bootstrapped per path in risk_analyzer;
            # falls back to Gaussian loudly if the pool is too thin). Default
            # gbm → None → behaviour identical to pre-#160.
            mc_empirical = self._mc_empirical_returns()
            mc = self.risk.path_dependence_monte_carlo(
                test_positions, spot, T, n_paths=50, trading_days=max(int(T*365), 5),
                seed=mc_seed, daily_vol=mc_daily_vol,
                empirical_returns=mc_empirical,
            )
            self.state.monte_carlo_report = mc

            # ── Gap #2 (2026-06-04): expected-value gate ──
            # mc.mean_pnl is the entry's expectancy across simulated paths.
            # Gate on EXPECTANCY, not win-rate: a long-gamma book is
            # positive-skew (bleed small, win big), so a low pct_profitable
            # is normal and healthy — only a NEGATIVE mean means we expect
            # to pay more theta than the gamma scalp recovers. Rejecting
            # here (not scaling) blocks the 2026-06-04 straddle add
            # (mean -3,371, 14% profitable) while passing the earlier add
            # (mean +23,614, 100% profitable). NOTE those figures are from
            # the GROSS, fixed-1%-vol era: since 2026-07-06 mean_pnl is net
            # of modeled entry/exit/rehedge costs and simulated at live RV,
            # so it reads systematically lower — recalibrate any floor
            # intuition against the net numbers, not these.
            mc_min_mean = self.tunable_params.get("mc_min_mean_pnl", 0.0)
            if mc.mean_pnl < mc_min_mean:
                logger.info(
                    "MC mean P/L %.0f < floor %.0f (%.0f%% paths profitable) "
                    "— negative-expectancy entry, skipping.",
                    mc.mean_pnl, mc_min_mean, mc.pct_profitable,
                )
                return []

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
        # Record the structure type now that this entry is committed, so the
        # next tick's type-difference gate sees it. Guarded on non-empty
        # proposals: an empty list here means every leg was filtered out.
        if proposals and entered_structure not in self.state.active_structure_types:
            self.state.active_structure_types.append(entered_structure)
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

        # ── Asymmetric, vol-aware rehedge band (Taleb Ch 8 shadow gamma) ──
        # The base threshold is rescaled per direction using the side-specific
        # shadow gamma the engine already computes. For biased assets like
        # NIFTY/BANKNIFTY, downside gamma exceeds upside gamma because vol
        # expands on sell-offs. A single symmetric band leaves too much
        # downside delta on the book exactly when it matters most.
        #
        # Scaling: band_lots = base × √(γ_side / γ_avg). Sqrt is gentler
        # than the Whalley-Wilmott γ^(2/3) on the band itself; the (2/3)
        # exponent shows up below in the cost-side gate. The net effect is
        # tighter triggers on the side where γ_side < γ_avg (less gamma
        # per unit delta → drift represents a bigger price move → hedge
        # sooner) and looser triggers on the gamma-heavy side.
        base_threshold = self.tunable_params["rehedge_delta_threshold"]
        lot_size = self._get_lot_size()
        delta = greeks.net_discrete_delta  # signed
        g_avg = max(abs(greeks.net_shadow_gamma), abs(greeks.net_gamma), 1e-6)
        g_up = max(abs(greeks.net_shadow_gamma_up), g_avg * 0.1)
        g_down = max(abs(greeks.net_shadow_gamma_down), g_avg * 0.1)
        # delta > 0 (long-side drift): a hedge would SELL futures, prompted
        # by an UP-move. Use shadow_gamma_up. Mirror for delta < 0.
        if delta >= 0:
            band_lots = base_threshold * math.sqrt(g_up / g_avg)
        else:
            band_lots = base_threshold * math.sqrt(g_down / g_avg)

        # Phase 5: T-0 band tightening (Taleb Ch 13 sticky-strike harvest).
        # On expiry day, gamma is huge — even small moves create big
        # delta drift. Tightening the band lets us scalp aggressively
        # into pin behaviour. Default factor=1.0 (no change); set < 1.0
        # to enable. Bounded by the existing cost gate so we don't
        # rehedge into negative-EV trades.
        t0_factor = self.tunable_params.get("t0_band_factor", 1.0)
        if t0_factor < 1.0 and self.state.positions:
            now = self._clock()
            min_days_to_exp = float("inf")
            for p in self.state.positions:
                if not p.expiry:
                    continue
                try:
                    exp = datetime.fromisoformat(p.expiry[:10])
                    days = (exp - now).total_seconds() / 86400.0
                    if days < min_days_to_exp:
                        min_days_to_exp = days
                except (TypeError, ValueError):
                    continue
            if min_days_to_exp < 1.0:
                band_lots *= t0_factor
                logger.info(
                    "Phase 5 T-0 tightening: band ×%.2f (min %.2f days to "
                    "expiry) → band=%.3f lots",
                    t0_factor, min_days_to_exp, band_lots,
                )
        delta_in_lots = abs(delta) / lot_size

        if delta_in_lots < band_lots:
            return []

        logger.info(
            "Delta drift: %.1f discrete (%.2f lots) > band %.2f "
            "(side=%s, γ_up=%.4f γ_down=%.4f γ_avg=%.4f)",
            delta, delta_in_lots, band_lots,
            "up" if delta >= 0 else "down", g_up, g_down, g_avg,
        )

        # ── C2: rehedge-churn bounds ──
        # The band fired (a rehedge is *wanted*), but frequency/count caps
        # bound the cost bleed regardless of whether the cost gate below
        # passes. Both gate ONLY rehedges — exits/close-all returned above at
        # _should_exit are never throttled. Disabled when set to 0.
        now = self._clock()
        session_cap = self.tunable_params.get("max_rehedges_per_session", 0)
        if session_cap and self.state._attribution_baseline is not None:
            rehedges_this_trade = (
                self.state.rehedge_count
                - self.state._attribution_baseline["rehedges_at_entry"]
            )
            if rehedges_this_trade >= session_cap:
                logger.warning(
                    "Skipping rehedge: session cap reached (%d/%d this trade). "
                    "Delta %.1f left unhedged until exit or a new trade.",
                    rehedges_this_trade, session_cap, delta,
                )
                return []

        cooldown = self.tunable_params.get("rehedge_cooldown_seconds", 0)
        if cooldown and self.state._last_rehedge_time is not None:
            since = (now - self.state._last_rehedge_time).total_seconds()
            if since < cooldown:
                logger.info(
                    "Skipping rehedge: cooldown active (%.0fs since last < %.0fs). "
                    "Delta %.1f deferred to next eligible tick.",
                    since, cooldown, delta,
                )
                return []

        # ── Whalley-Wilmott cost gate (Taleb Ch 16: balance gamma vs cost) ──
        # WW's optimal-band result gives required-move-to-rehedge ∝
        # (cost / γ)^(1/3). Translating to scalp/cost gives required
        # scalp ∝ cost × (cost / γ × spot²)^(2/3) — or, more pragmatically,
        # scalp must beat cost × hurdle^(1/3). The cube-root softens the
        # linear hurdle: a 2× cost only demands a 1.26× larger scalp, not
        # a 2× larger one. This recovers scalps that the previous linear
        # gate killed when γ was modest and cost was high.
        expected_scalp = self._estimate_gamma_scalp_pnl(greeks, spot)
        hedge_lots = max(abs(round(delta / lot_size)), 1)
        cost_one_side = estimate_transaction_cost(spot, hedge_lots, lot_size, "BUY", "FUT")
        estimated_round_trip_cost = cost_one_side * 2
        cost_hurdle = self.tunable_params["cost_hurdle_factor"]
        # cost_hurdle is the linear-equivalent (still a tunable). Apply the
        # cube root so the autoresearch loop's existing tuning range stays
        # meaningful — hurdle=1.5 (old linear) becomes ~1.14 (cube-root),
        # hurdle=8 becomes ~2.0. Operator can set hurdle=1.0 to disable.
        ww_required = estimated_round_trip_cost * (cost_hurdle ** (1 / 3))
        if expected_scalp < ww_required:
            logger.info(
                "Skipping rehedge: scalp %.0f < WW threshold %.0f "
                "(cost %.0f, hurdle %.2f → cube-root %.2f)",
                expected_scalp, ww_required, estimated_round_trip_cost,
                cost_hurdle, cost_hurdle ** (1 / 3),
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

        # ── C2: per-tick lots cap ──
        # Bound a single oversized hedge (the 05-26 mode: few rehedges, large
        # lots). Clamp quantity and scale margin_required to match. In normal
        # operation a hedge is round(delta/lot_size) ≈ 1-3 lots, so this only
        # bites a runaway sizing. Disabled when set to 0.
        lots_cap = self.tunable_params.get("max_rehedge_lots_per_tick", 0)
        if lots_cap:
            for prop in proposals:
                if prop.quantity > lots_cap:
                    logger.warning(
                        "Clamping rehedge %s from %d to %d lots (max_rehedge_"
                        "lots_per_tick); residual delta left for next tick.",
                        prop.tradingsymbol, prop.quantity, lots_cap,
                    )
                    prop.margin_required *= lots_cap / prop.quantity
                    prop.quantity = lots_cap

        # Post-emission bookkeeping runs ONLY when a rehedge is actually
        # emitted. An empty proposal list (e.g. a sub-1-lot hard hedge that
        # rounds to 0, reachable when the band is tightened on T-0) is not a
        # rehedge: it must not start the cooldown, count toward the session
        # cap (rehedge_count), or re-anchor the gamma-scalp baseline.
        if proposals:
            self.state._last_rehedge_time = now

            # Realized gamma-scalp P/L: 0.5 × γ × (ΔS)² where ΔS is the actual
            # spot move since the last anchor (entry or prior rehedge). The
            # SIGN of γ matters: a long-gamma book scalps positive on any
            # move; a short-gamma book LOSES money to realized vol — taking
            # abs(γ) would silently invert that loss into a fictitious gain
            # and bias the autoresearch metric toward the losing parameter
            # set under Phase 3+ regimes that book short-gamma legs.
            anchor_spot = self.state._last_rehedge_spot
            if anchor_spot is not None and anchor_spot > 0:
                dS = spot - anchor_spot
                gamma_for_scalp = (
                    greeks.net_shadow_gamma if greeks.net_shadow_gamma != 0
                    else greeks.net_gamma
                )
                self.state.gamma_scalp_pnl += 0.5 * gamma_for_scalp * dS * dS
            self.state._last_rehedge_spot = spot
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
        # Build per-leg T for multi-expiry books (calendars / diagonals
        # from Phase 3). Single-expiry books pass per_leg_T=None and
        # the helpers fall back to a single T as before.
        clock_now = self._clock()
        per_leg_T = {}
        for p in self.state.positions:
            if p.expiry:
                per_leg_T[p.tradingsymbol] = time_to_expiry(p.expiry, clock_now)
        T = next(iter(per_leg_T.values()), 1/365)
        per_leg_arg = per_leg_T if len(set(per_leg_T.values())) > 1 else None
        self._update_portfolio_greeks()
        pf = self.state.portfolio_greeks

        # Bleed forecast (review-fix #5: pass per_leg_T)
        bleed = self.risk.bleed_forecast(
            self.state.positions, spot, T, per_leg_T=per_leg_arg,
        )
        self.state.bleed_history.append(bleed)
        if len(self.state.bleed_history) > _DIAG_HISTORY_CAP:
            del self.state.bleed_history[:-_DIAG_HISTORY_CAP]

        # Neutrality check
        neutrality = self.risk.neutrality_check(pf)

        # Method of squares (review-fix #5: pass per_leg_T)
        squares = self.risk.method_of_squares(
            self.state.positions, spot, T, per_leg_T=per_leg_arg,
        )

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

        # Sharpe / Sortino degenerate in TWO independent ways and both must be
        # guarded, or the optimizer ranks on a noise spike:
        #   (1) too few daily points — a 1-2 point series has no meaningful
        #       dispersion. MIN_SHARPE_DAYS is the count floor.
        #   (2) near-zero dispersion with a nonzero mean — a near-flat P&L
        #       week (e.g. a calm-regime straddle) has std≈0 but mean≠0, so
        #       mean/std explodes to ±1e8+ even with many points. A bare
        #       `std > 0` check does NOT catch this; the original -569035
        #       autoresearch baseline was this exact failure at 2 points.
        # Guard (2) with a coefficient-of-variation floor: only compute the
        # ratio when std exceeds a small fraction of |mean| (i.e. the series
        # has real relative dispersion). Below that the Sharpe is undefined →
        # neutral 0.0. NOTE: a single-session tape replay yields ~1 daily
        # bucket, so sharpe_ratio is legitimately 0.0 there — net_pnl /
        # gamma_theta_ratio are the right metrics for single-session data.
        # ddof=1 (sample std): daily_pnls is a sample, not the population.
        MIN_SHARPE_DAYS = 5
        # std must be at least this fraction of |mean| to count as real
        # dispersion (rejects the flat-week artifact); and at least a small
        # absolute floor so a near-zero-mean flat series doesn't sneak past.
        _MIN_REL_STD = 1e-3   # std/|mean| below this → treat as degenerate
        # Absolute ₹ std floor: catches the near-zero-MEAN flat series the
        # relative guard misses (tiny mean ⇒ tiny rel-threshold). Daily P&L on
        # a ₹5L book has std in the hundreds+; a sub-₹1 daily std means the
        # book isn't trading, so this never rejects a real signal.
        _MIN_ABS_STD = 1.0

        def _annualized(values: np.ndarray, denom_std: float) -> float:
            """mean/std * sqrt(252), or 0.0 if dispersion is degenerate."""
            mean_v = float(np.mean(values))
            if denom_std <= _MIN_ABS_STD:
                return 0.0
            if denom_std < abs(mean_v) * _MIN_REL_STD:
                return 0.0
            return (mean_v / denom_std) * np.sqrt(252)

        sharpe_ratio = 0.0
        if len(daily_pnls) >= MIN_SHARPE_DAYS:
            arr = np.array(daily_pnls)
            sharpe_ratio = _annualized(arr, float(np.std(arr, ddof=1)))

        # Calmar ratio
        calmar_ratio = 0.0
        if max_dd > 0:
            calmar_ratio = total_pnl / max_dd

        # Sortino ratio — same count floor + CV guard; denominator is the
        # sample std of the downside days (needs >= 2 of them).
        sortino_ratio = 0.0
        if len(daily_pnls) >= MIN_SHARPE_DAYS:
            arr = np.array(daily_pnls)
            downside = arr[arr < 0]
            if len(downside) >= 2:
                sortino_ratio = _annualized(arr, float(np.std(downside, ddof=1)))

        # Phase 2.4: gamma_theta_ratio is the Taleb-framework efficiency
        # metric. Numerator is realized gamma scalp P&L (Phase 1.1 fix);
        # denominator is realized theta decay (Phase 1.1 fix). Ratio > 1
        # means scalps exceeded the time-decay rent — the strategy's
        # core thesis. Both inputs are now realized (not estimates), so
        # this ratio is well-defined.
        gamma_scalp = self.state.gamma_scalp_pnl
        theta_paid = self.state.theta_decay_paid
        # Guard against tiny / non-positive denominators. When theta is
        # near zero (just-entered or short-premium book) the ratio is
        # not meaningful — return 0 so the autoresearch loop's variance
        # penalty doesn't get fed a divide-by-zero infinity.
        if theta_paid > 1.0:
            gamma_theta_ratio = gamma_scalp / theta_paid
        else:
            gamma_theta_ratio = 0.0

        # ── Phase-1 component metrics (2026-07-18 fitness redesign) ──
        # All are attribution/measurement only; degenerate cases emit 0.0 so
        # downstream mean()/variance math never sees None. Spot proxy is the
        # last greeks-update anchor (no broker I/O from a metrics call).
        g = self.state.portfolio_greeks
        spot_proxy = self.state._last_scalp_anchor_spot or 0.0

        # Breakeven daily move: the |move| that re-earns one day of theta rent,
        # ½·Γ·(S·r)² = θ_day → r = √(2θ/(Γ·S²)). Defined only for a book that
        # pays theta AND is long gamma; a short-middle book (Γ<0) emits 0.0 —
        # its "breakeven" is not a move size, it's the absence of one.
        breakeven_move_pct = 0.0
        if g is not None and spot_proxy > 0:
            theta_day = g.net_shadow_theta if g.net_shadow_theta != 0 else g.net_theta
            gamma_now = g.net_shadow_gamma if g.net_shadow_gamma != 0 else g.net_gamma
            if theta_day > 0 and gamma_now > 0:
                breakeven_move_pct = float(
                    np.sqrt(2.0 * theta_day / (gamma_now * spot_proxy ** 2)) * 100.0
                )

        # Worst P&L across the ±1.5% spot band — the "middle" where 17 of 41
        # observed sessions actually land (Phase-0 F2: the book kept losing on
        # 0.5–1.5% moves while its profile only paid off outside them). Uses
        # the pnl_profile compute_portfolio_greeks already built; flat → 0.0.
        middle_band_worst_pnl = 0.0
        if g is not None and spot_proxy > 0 and g.pnl_profile:
            band = [pnl for price, pnl in g.pnl_profile.items()
                    if abs(price - spot_proxy) <= 0.015 * spot_proxy]
            if band:
                middle_band_worst_pnl = float(min(band))

        # Scalp-capture efficiency: rehedge-gated scalp vs the always-on
        # theoretical ½Γ(ΔS)² accrual. Only meaningful for a long-gamma book
        # with real accrued variance (denominator > ₹1); else 0.0.
        theoretical_scalp = self.state.theoretical_scalp_pnl
        scalp_capture_efficiency = (
            gamma_scalp / theoretical_scalp if theoretical_scalp > 1.0 else 0.0
        )

        # Entry-pricing / holding ingredients from the per-structure
        # attribution baseline (0.0 when flat — no open structure).
        entry_atm_iv = 0.0
        structure_hold_hours = 0.0
        ab = self.state._attribution_baseline
        if ab is not None:
            entry_atm_iv = float(ab.get("entry_atm_iv") or 0.0)
            entry_t = ab.get("entry_time")
            if entry_t is not None:
                structure_hold_hours = float(
                    (self._clock() - entry_t).total_seconds() / 3600.0
                )

        return {
            "breakeven_move_pct": breakeven_move_pct,
            "middle_band_worst_pnl": middle_band_worst_pnl,
            "theoretical_scalp_pnl": theoretical_scalp,
            "scalp_capture_efficiency": scalp_capture_efficiency,
            "entry_atm_iv": entry_atm_iv,
            "structure_hold_hours": structure_hold_hours,
            "net_pnl": total_pnl,
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "max_drawdown": max_dd,
            "max_drawdown_pct": (max_dd / capital * 100) if capital > 0 else 0,
            "sharpe_ratio": sharpe_ratio,
            "calmar_ratio": calmar_ratio,
            "sortino_ratio": sortino_ratio,
            "gamma_scalp_pnl": gamma_scalp,
            "theta_decay_paid": theta_paid,
            "gamma_theta_ratio": gamma_theta_ratio,
            "rehedge_count": self.state.rehedge_count,
            "position_count": len(self.state.positions),
            "total_transaction_costs": self.state.total_transaction_costs,
        }

    # ── Phase-1 dated session attribution (2026-07-18 fitness redesign) ──
    # Phase-0 finding F1: none of the persisted records could answer "what did
    # this session pay in theta / capture in scalp / spend in costs" — counters
    # are lifetime totals, daily_pnl_history is undated floats, closed_trades
    # has no P&L. These two methods give the runner a dated, per-session,
    # delta-based record: snapshot at session start, diff at session end.

    ATTRIBUTION_COUNTERS = (
        "total_pnl", "realized_pnl", "theta_decay_paid", "gamma_scalp_pnl",
        "theoretical_scalp_pnl", "total_transaction_costs", "rehedge_count",
    )

    def snapshot_attribution_counters(self) -> Dict:
        """Session-start snapshot of the lifetime counters, taken by the
        runner right after state restore."""
        return {k: getattr(self.state, k) for k in self.ATTRIBUTION_COUNTERS}

    def get_session_attribution(self, anchor: Dict) -> Dict:
        """Dated end-of-session attribution record: this session's deltas over
        `anchor` (from snapshot_attribution_counters) plus the EOD component
        metrics. The runner appends it to the per-underlying attribution JSONL
        — the input the redesigned fitness objective consumes."""
        deltas = {
            f"session_{k}": getattr(self.state, k) - anchor.get(k, 0.0)
            for k in self.ATTRIBUTION_COUNTERS
        }
        m = self.get_strategy_metrics()
        return {
            "date": self._clock().date().isoformat(),
            **deltas,
            "eod_position_count": len(self.state.positions),
            "eod_structures": list(self.state.active_structure_types or []),
            "eod_spot": self.state._last_scalp_anchor_spot,
            "breakeven_move_pct": m["breakeven_move_pct"],
            "middle_band_worst_pnl": m["middle_band_worst_pnl"],
            "scalp_capture_efficiency": m["scalp_capture_efficiency"],
            "entry_atm_iv": m["entry_atm_iv"],
            "structure_hold_hours": m["structure_hold_hours"],
            "lifetime_total_pnl": self.state.total_pnl,
        }

    # ══════════════════════════════════════════════════════════
    # CROSS-SESSION PERSISTENCE
    # ══════════════════════════════════════════════════════════

    def serialize_state(self) -> Dict:
        """Snapshot HedgeState so runners/run_paper.py can persist it between sessions.
        Counterpart of restore_state(). Used when the runner is configured to
        hold positions overnight rather than EOD-flatten (2026-05-19).

        Skips computable derivatives:
          - portfolio_greeks: recomputed every tick from positions+spot+T
          - bleed_history / stability_history / monte_carlo_report /
            last_hedge_decision: rolling diagnostic outputs, rebuilt next tick
        Skips runtime caches (_atm_iv_history / _spot_history) — IV history
        has its own persistence (_save_iv_history); spot history rebuilds
        in ~10 ticks.

        Persists _attribution_baseline: a trade held across a session boundary
        keeps its per-trade anchor, so (a) the C2 session rehedge cap stays
        bound to the right baseline and (b) the closed-trade attribution emitted
        on exit spans the real open→close window rather than being dropped.
        """
        def _iso(ts):
            if ts is None:
                return None
            if isinstance(ts, datetime):
                return ts.isoformat()
            return str(ts)
        return {
            "saved_at": datetime.now().isoformat(),
            "state": {
                "positions": [
                    {
                        "tradingsymbol": p.tradingsymbol,
                        "instrument_token": p.instrument_token,
                        "strike": p.strike,
                        "expiry": p.expiry,
                        "option_type": p.option_type,
                        "lot_size": p.lot_size,
                        "quantity": p.quantity,
                        "entry_price": p.entry_price,
                        "current_price": p.current_price,
                        "iv": p.iv,
                    }
                    for p in self.state.positions
                ],
                "entry_time": _iso(self.state.entry_time),
                "total_pnl": self.state.total_pnl,
                "realized_pnl": self.state.realized_pnl,
                "unrealized_pnl": self.state.unrealized_pnl,
                "rehedge_count": self.state.rehedge_count,
                "gamma_scalp_pnl": self.state.gamma_scalp_pnl,
                "theta_decay_paid": self.state.theta_decay_paid,
                "theoretical_scalp_pnl": self.state.theoretical_scalp_pnl,
                "_last_scalp_anchor_spot": self.state._last_scalp_anchor_spot,
                "max_drawdown": self.state.max_drawdown,
                "peak_pnl": self.state.peak_pnl,
                "total_transaction_costs": self.state.total_transaction_costs,
                # Audit 3.5: persist only the most recent closed trades. The
                # state file is rewritten every tick (H1), so serializing the
                # full ever-growing history bloats each write. The dashboard
                # only reads TODAY's closed trades (positions.py _is_today
                # filter), and a session closes far fewer than this cap, so
                # today's are always present after a same-day restart.
                "closed_trades": self.state.closed_trades[-_CLOSED_TRADES_PERSIST:],
                "_prev_snapshot_pnl": self.state._prev_snapshot_pnl,
                "_current_day_pnl": self.state._current_day_pnl,
                "_current_trading_date": (
                    self.state._current_trading_date.isoformat()
                    if self.state._current_trading_date else None
                ),
                "daily_pnl_history": list(self.state.daily_pnl_history),
                "futures_hedge_delta": self.state.futures_hedge_delta,
                "futures_entry_vwap": self.state.futures_entry_vwap,
                "futures_lots": self.state.futures_lots,
                "futures_symbol": self.state.futures_symbol,
                "futures_last_mark": self.state.futures_last_mark,
                # Anchors for realized-theta and realized-gamma-scalp
                # accounting. Re-anchored at the next tick if missing,
                # so absence in an older blob is tolerated by restore.
                "_last_theta_anchor_time": _iso(self.state._last_theta_anchor_time),
                "_last_rehedge_spot": self.state._last_rehedge_spot,
                "_last_rehedge_time": _iso(self.state._last_rehedge_time),
                # Phase 4 layering: structure types held across the session
                # boundary, so the type-difference gate stays enforced on resume.
                "active_structure_types": list(self.state.active_structure_types),
                # Per-trade attribution anchor (entry_time is a datetime; the
                # rest are floats/ints). Absent in older blobs → restored None.
                "_attribution_baseline": (
                    {**self.state._attribution_baseline,
                     "entry_time": _iso(self.state._attribution_baseline["entry_time"])}
                    if self.state._attribution_baseline is not None else None
                ),
            },
        }

    def restore_state(self, blob: Dict) -> None:
        """Inverse of serialize_state(). Fails loudly on shape mismatch — a
        corrupted or partial state file must not silently degrade into a
        fresh-start strategy (Rule 12)."""
        from core.greeks_engine import OptionContract
        s = blob["state"]
        self.state.positions = [
            OptionContract(
                tradingsymbol=p["tradingsymbol"],
                instrument_token=int(p["instrument_token"]),
                strike=float(p["strike"]),
                expiry=p["expiry"],
                option_type=p["option_type"],
                lot_size=int(p["lot_size"]),
                quantity=int(p["quantity"]),
                entry_price=float(p["entry_price"]),
                current_price=float(p.get("current_price", p["entry_price"])),
                iv=float(p.get("iv", 0.0)),
            )
            for p in s["positions"]
        ]
        et = s.get("entry_time")
        self.state.entry_time = datetime.fromisoformat(et) if et else None
        self.state.total_pnl = float(s["total_pnl"])
        self.state.realized_pnl = float(s["realized_pnl"])
        self.state.unrealized_pnl = float(s["unrealized_pnl"])
        self.state.rehedge_count = int(s["rehedge_count"])
        self.state.gamma_scalp_pnl = float(s["gamma_scalp_pnl"])
        self.state.theta_decay_paid = float(s["theta_decay_paid"])
        # Phase-1 attribution fields — .get() defaults keep pre-2026-07-18
        # state files loadable (backcompat: counters simply start at zero).
        self.state.theoretical_scalp_pnl = float(
            s.get("theoretical_scalp_pnl", 0.0))
        lsa = s.get("_last_scalp_anchor_spot")
        self.state._last_scalp_anchor_spot = (
            float(lsa) if lsa is not None else None)
        self.state.max_drawdown = float(s["max_drawdown"])
        self.state.peak_pnl = float(s["peak_pnl"])
        self.state.total_transaction_costs = float(s["total_transaction_costs"])
        self.state.closed_trades = list(s.get("closed_trades", []))
        self.state._prev_snapshot_pnl = float(s.get("_prev_snapshot_pnl", 0.0))
        self.state._current_day_pnl = float(s.get("_current_day_pnl", 0.0))
        ctd = s.get("_current_trading_date")
        self.state._current_trading_date = (
            date.fromisoformat(ctd) if ctd else None
        )
        self.state.daily_pnl_history = list(s.get("daily_pnl_history", []))
        self.state.futures_hedge_delta = float(s["futures_hedge_delta"])
        self.state.futures_entry_vwap = float(s["futures_entry_vwap"])
        self.state.futures_lots = int(s["futures_lots"])
        # Tolerated absent: blobs written before the hedge carried its own
        # contract identity. An empty futures_symbol degrades to "quote
        # whatever is front-month", which is the pre-fix behaviour — loud
        # via _futures_mark's log, not silently wrong.
        self.state.futures_symbol = str(s.get("futures_symbol", "") or "")
        self.state.futures_last_mark = float(s.get("futures_last_mark", 0.0) or 0.0)
        # Optional fields — older state files predate Phase 1.1
        # realized-accounting anchors; tolerate their absence.
        lta = s.get("_last_theta_anchor_time")
        self.state._last_theta_anchor_time = (
            datetime.fromisoformat(lta) if lta else None
        )
        lrs = s.get("_last_rehedge_spot")
        self.state._last_rehedge_spot = float(lrs) if lrs is not None else None
        lrt = s.get("_last_rehedge_time")
        self.state._last_rehedge_time = (
            datetime.fromisoformat(lrt) if lrt else None
        )
        # Optional — older blobs predate Phase 4 layering; default to empty.
        self.state.active_structure_types = list(s.get("active_structure_types", []))
        ab = s.get("_attribution_baseline")
        if ab is not None:
            ab = dict(ab)
            ab_et = ab.get("entry_time")
            ab["entry_time"] = datetime.fromisoformat(ab_et) if ab_et else None
        self.state._attribution_baseline = ab

    def legs_expire_on(self, today: date) -> bool:
        """True if any held leg's contract has its last trading day on `today`.
        Covers both option legs (each carries its own ISO expiry string) and
        the futures hedge (looked up against the NFO instruments dump).

        H18: kite.instruments('NFO') is retried up to 3× with exponential
        backoff; persistent failure raises rather than silently returning
        False. Silent False on expiry day means carrying a contract into
        cash settlement — fail loud and let the runner exit non-zero so
        notify-failure@ alerts the operator to manually flatten.
        """
        for p in self.state.positions:
            if not p.expiry:
                continue
            try:
                if datetime.fromisoformat(p.expiry[:10]).date() == today:
                    return True
            except (TypeError, ValueError):
                continue
        # Futures hedge: only one contract at a time, identified by the cached
        # tradingsymbol. Look up its expiry from the instruments dump.
        if abs(self.state.futures_hedge_delta) > 0 or self.state.futures_lots != 0:
            fut_symbol = self._get_futures_symbol()
            instruments = self._fetch_nfo_instruments_with_retry()
            if not instruments:
                raise RuntimeError(
                    "instruments('NFO') returned an empty list — cannot "
                    "verify whether the futures hedge expires today. "
                    "Refusing to silently return False."
                )
            for row in instruments:
                if row.get("tradingsymbol") != fut_symbol:
                    continue
                exp = row.get("expiry")
                if isinstance(exp, str):
                    try:
                        exp = datetime.strptime(exp[:10], "%Y-%m-%d").date()
                    except ValueError:
                        return False
                elif hasattr(exp, "date"):
                    exp = exp.date()
                if exp == today:
                    return True
                break
        return False

    def _fetch_nfo_instruments_with_retry(
        self, *, max_retries: int = 3, base_backoff_s: float = 1.0,
    ) -> List[dict]:
        """H18: retry kite.instruments('NFO') with exponential backoff;
        raise the last exception on persistent failure. Used by
        legs_expire_on at session end — a hiccup that leaves us unable
        to detect expiry day is louder than a hiccup that just delays
        startup."""
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                result = self.kite.instruments("NFO") or []
                if attempt > 0:
                    logger.info(
                        "instruments('NFO') succeeded on retry #%d", attempt,
                    )
                return result
            except Exception as e:
                last_exc = e
                if attempt + 1 < max_retries:
                    wait = base_backoff_s * (2 ** attempt)
                    logger.warning(
                        "instruments('NFO') failed (attempt %d/%d): %s — "
                        "retrying in %.1fs",
                        attempt + 1, max_retries, e, wait,
                    )
                    time.sleep(wait)
        raise RuntimeError(
            f"instruments('NFO') failed {max_retries} consecutive times; "
            f"last error: {last_exc!r}"
        )

    # ══════════════════════════════════════════════════════════
    # INTERNAL METHODS
    # ══════════════════════════════════════════════════════════

    def _generate_hard_delta_proposals(self, greeks, spot):
        """Standard futures-based delta hedge using discrete delta (Taleb p.116-121)."""
        delta_to_hedge = -greeks.net_discrete_delta
        lot_size = self._get_lot_size()
        lots = round(delta_to_hedge / lot_size)
        if lots == 0:
            # Drift cleared the threshold gate but rounded to <1 lot.
            # Without this log the no-hedge looks identical to a successful one.
            logger.info(
                "Hard hedge sized to 0 lots: %.1f delta / %d lot_size = %.2f rounds to 0. "
                "Raise rehedge_delta_threshold so threshold passes only when round() >= 1.",
                delta_to_hedge, lot_size, delta_to_hedge / lot_size,
            )
            return []
        fut_symbol = self._get_futures_symbol()
        # Entry price MUST come off the futures contract, not spot: the leg
        # is marked and flattened off the same series, so a spot-priced
        # entry VWAP injects the basis as phantom P&L (see
        # _get_futures_price). Refusing the hedge for one tick is the
        # smaller risk — drift re-proposes it on the next tick, whereas a
        # corrupt VWAP follows the position to its grave and feeds the
        # daily-loss breaker. Same "refuse rather than guess" stance as
        # H-6c/H-6d above.
        fut_price = self._get_futures_price(fut_symbol)
        if not fut_price or fut_price <= 0:
            # _get_futures_price already counted this failure and escalated
            # WARNING -> ERROR at 5 consecutive, so a feed that stays down
            # cannot sit at one severity for the whole session.
            logger.error(
                "Hard delta hedge: no usable futures price for %s — skipping "
                "this hedge tick rather than opening it at spot (%.1f delta "
                "left unhedged, %d consecutive failures).",
                fut_symbol, delta_to_hedge,
                getattr(self, "_consecutive_futures_failures", 0),
            )
            return []

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

        # Phase 3.2: chain may span two expiries. Soft hedge MUST sit on
        # the same expiry as the existing position(s) — otherwise we'd
        # hedge the front-month greek surface with a back-month option
        # whose theta/gamma profile is wrong and the hedge would
        # introduce its own basis risk. If flat (shouldn't happen here,
        # but defensive), pin to the primary slice.
        if self.state.positions and self.state.positions[0].expiry:
            position_expiry = self.state.positions[0].expiry
            chain = chain[chain["expiry"] == position_expiry]
            if chain.empty:
                logger.warning(
                    "Soft delta hedge: position expiry %s not in chain — "
                    "cannot hedge with matching expiry option",
                    position_expiry,
                )
                return []
        else:
            chain = self._primary_expiry_slice(chain)

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

    def _count_active_structures(self) -> int:
        """Phase 4: number of distinct *structures* layered on the book —
        the unit `scan_and_propose` caps with `max_layered_structures`.

        Prefers `active_structure_types` (one entry per layered structure,
        which is the correct unit: a calendar is ONE structure even though
        it spans two expiries, and two same-expiry straddles are TWO
        structures even though they share one expiry). Falls back to the
        coarse unique-expiry count only when the type list is empty but
        positions exist — i.e. a book restored from a pre-Phase-4 state
        blob that predates the list — so a held book is never under-counted,
        which would wrongly permit layering.
        """
        if not self.state.positions:
            return 0
        if self.state.active_structure_types:
            return len(self.state.active_structure_types)
        return len({p.expiry for p in self.state.positions if p.expiry})

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
            # Flat book: drop the theta anchor so the next entry starts a
            # fresh integration window. Same for the theoretical-scalp spot
            # anchor — a flat gap must not accrue ½Γ(ΔS)² across the re-entry.
            self.state._last_theta_anchor_time = None
            self.state._last_scalp_anchor_spot = None
            return
        spot = self._get_spot_price()
        if not spot or spot <= 0:
            logger.warning("Cannot update portfolio greeks: spot unavailable; keeping last value")
            return
        # Phase 3.3: build a per-leg T map. Single-expiry books (all
        # legs share an expiry, like a vanilla straddle) use the default
        # T from the first position. Multi-expiry books (calendars,
        # diagonals from Phase 3.2) need each leg priced at its own T —
        # otherwise the back-month gamma and theta are wrong by orders
        # of magnitude. Empty expiry strings (futures) fall back to the
        # default T which the greeks engine ignores for option_type=FUT.
        clock_now = self._clock()
        per_leg_T = {}
        default_T = None
        for p in self.state.positions:
            if not p.expiry:
                continue
            t_p = time_to_expiry(p.expiry, clock_now)
            per_leg_T[p.tradingsymbol] = t_p
            if default_T is None:
                default_T = t_p
        if default_T is None:
            default_T = 1 / 365
        # If all legs share T, fall back to the single-T path (per_leg_T
        # is then redundant; passing None keeps the call cleaner).
        unique_T = set(per_leg_T.values())
        per_leg_arg = per_leg_T if len(unique_T) > 1 else None
        T = default_T
        self.state.portfolio_greeks = self.greeks.compute_portfolio_greeks(
            self.state.positions, spot, T, per_leg_T=per_leg_arg,
        )
        # Include futures hedge in net delta (both analytical and discrete)
        self.state.portfolio_greeks.net_delta += self.state.futures_hedge_delta
        self.state.portfolio_greeks.net_discrete_delta += self.state.futures_hedge_delta
        # Realized theta accounting: integrate -net_shadow_theta over the
        # interval since the last update. net_shadow_theta is in ₹/day
        # (greeks_engine returns daily theta after dividing by 365), so
        # multiplying by elapsed days gives ₹ of decay realized over that
        # interval. The sign is flipped so theta_decay_paid grows positive
        # for a long-premium book (rupees lost to time) and shrinks for a
        # short-premium book (rupees earned). The previous abs(...) sum
        # was a gross instantaneous accumulator, not realized decay.
        now = self._clock()
        anchor = self.state._last_theta_anchor_time
        if anchor is not None:
            elapsed_days = (now - anchor).total_seconds() / 86400.0
            if elapsed_days > 0:
                self.state.theta_decay_paid += (
                    -self.state.portfolio_greeks.net_shadow_theta * elapsed_days
                )
        self.state._last_theta_anchor_time = now

        # Theoretical realized-variance accrual (Phase-1 attribution): ½·Γ·(ΔS)²
        # per greeks update, SIGNED gamma (a short-gamma book accrues negative —
        # same sign convention as the rehedge-gated gamma_scalp_pnl). Uses the
        # shadow gamma when available, mirroring the scalp accrual at rehedge.
        anchor_spot = self.state._last_scalp_anchor_spot
        if anchor_spot is not None and anchor_spot > 0:
            dS = spot - anchor_spot
            g = self.state.portfolio_greeks
            gamma_for_accrual = (
                g.net_shadow_gamma if g.net_shadow_gamma != 0 else g.net_gamma
            )
            self.state.theoretical_scalp_pnl += 0.5 * gamma_for_accrual * dS * dS
        self.state._last_scalp_anchor_spot = spot

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

            # C-1 fix (audit 2026-06-10, task 1.2): COMPLETE-whitelist, not
            # FAILED-blacklist. _live_execute used to return PENDING for
            # every placed order (fill unknown) and REJECTED pre-submit;
            # the old `== "FAILED"` skip booked both as fills — phantom
            # positions, costs, and realized P&L. Mirrors pair_trading's
            # execute_proposals: only a confirmed COMPLETE mutates state.
            if result.get("status") != "COMPLETE":
                logger.warning(
                    "Order not COMPLETE for %s: status=%s error=%s — "
                    "skipping state update",
                    prop.tradingsymbol, result.get("status"),
                    result.get("error", ""))
                continue

            # Book at the actual fill when the executor reports one (live
            # marketable LIMITs can fill inside the protection pad). Paper
            # results carry no average_price → prop.price, so paper
            # accounting is unchanged. Mirrors pair_trading._apply_fill.
            fill_price = float(result.get("average_price") or 0.0) or prop.price

            # Deduct transaction costs
            cost = estimate_transaction_cost(
                fill_price, prop.quantity, prop.lot_size, prop.transaction_type,
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
                        add_notional = signed_lots * fill_price
                        self.state.futures_entry_vwap = (old_notional + add_notional) / new_lots
                elif new_lots == 0:
                    # Fully closed — book realized P/L
                    realized = (fill_price - self.state.futures_entry_vwap) * old_lots * prop.lot_size
                    self.state.realized_pnl += realized
                    self.state.futures_entry_vwap = 0.0
                    had_any_close = True
                    logger.info("Closed futures hedge: realized P/L ₹%.0f", realized)
                else:
                    # Flipped direction — close old, open remainder
                    realized = (fill_price - self.state.futures_entry_vwap) * old_lots * prop.lot_size
                    self.state.realized_pnl += realized
                    self.state.futures_entry_vwap = fill_price
                    had_any_close = True
                    logger.info("Flipped futures hedge: realized P/L ₹%.0f", realized)
                self.state.futures_lots = new_lots
                # Bind the hedge to the contract it actually filled on, and
                # seed its mark from the fill. Without the seed, a quote
                # outage on the very next tick (or straight after a
                # restart) has nothing to carry and the leg would mark
                # flat — hiding the whole futures move from the
                # daily-loss breaker.
                if new_lots == 0:
                    self.state.futures_symbol = ""
                    self.state.futures_last_mark = 0.0
                else:
                    self.state.futures_symbol = prop.tradingsymbol
                    self.state.futures_last_mark = fill_price
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
                        realized = (fill_price - existing.entry_price) * old_qty * existing.lot_size
                        self.state.realized_pnl += realized
                        self.state.positions.remove(existing)
                        had_any_close = True
                        logger.info("Closed %s: realized P/L ₹%.0f", prop.tradingsymbol, realized)
                    else:
                        # Partial close or add to position
                        if old_qty * signed_qty < 0:
                            # Partial close: book realized P/L on the closed portion
                            closed_qty = min(abs(old_qty), abs(signed_qty)) * (1 if old_qty > 0 else -1)
                            realized = (fill_price - existing.entry_price) * closed_qty * existing.lot_size
                            self.state.realized_pnl += realized
                            had_any_close = True
                        existing.quantity = new_qty
                else:
                    # New position
                    self.state.positions.append(OptionContract(
                        tradingsymbol=prop.tradingsymbol, instrument_token=prop.instrument_token,
                        strike=prop.strike, expiry=prop.expiry, option_type=prop.option_type,
                        lot_size=prop.lot_size, quantity=signed_qty,
                        entry_price=fill_price, current_price=fill_price, iv=prop.iv,
                    ))

        if is_entry_batch and self.state.positions:
            self.state.entry_time = self._clock()
            # Anchor the rehedge-spot tracker at the entry spot so the
            # first scalp computes ΔS from the actual entry price, not
            # from None. Use the entry-leg spot estimate via the most
            # recent quote; fall back to a fresh _get_spot_price() call.
            entry_spot = self._get_spot_price()
            self.state._last_rehedge_spot = (
                entry_spot if entry_spot and entry_spot > 0 else None
            )
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
                    # active_structure_types still holds the entered structure(s)
                    # here — it is reset to [] further below. Record it so the
                    # dashboard can label the trade by its real structure
                    # (straddle / asymmetric_strangle / …) instead of assuming
                    # a straddle.
                    "structure": ", ".join(self.state.active_structure_types),
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
            # Drop the rehedge-spot anchor; theta anchor is dropped in
            # _update_portfolio_greeks() when it sees an empty book. Also clear
            # the cooldown clock so the next trade's first rehedge isn't blocked
            # by the prior trade's spacing.
            self.state._last_rehedge_spot = None
            self.state._last_rehedge_time = None
            # Flat book holds no structures — reset the layering type list so
            # the next entry starts clean.
            self.state.active_structure_types = []
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

        # Gate: gap exit — if spot moved > threshold since entry.
        # (A per-strike pct-move check was stubbed here for years and never
        # implemented — strike-based gap is approximate; real gap needs prev
        # close. The unrealized-loss proxy below is the actual gate.)
        if self.state.positions and self.state.entry_time:
            gap_threshold = self.immutable_params["gap_exit_threshold_pct"]
            # Check if unrealized loss exceeds gap threshold of capital
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
        except Exception as e:
            raise RuntimeError(
                f"Could not resolve lot size for {self.underlying} from "
                "kite.instruments — refusing to size on a guess. (Audit "
                "H-6c: the old silent fallback table was pre-Nov-2024 — "
                "NIFTY 25 vs actual 75 sized hedges 3x wrong.)"
            ) from e
        raise RuntimeError(
            f"No {self.underlying} option rows in kite.instruments('NFO') — "
            "cannot determine lot size; refusing to size on a guess (H-6c)."
        )

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
        except Exception as e:
            raise RuntimeError(
                f"Could not resolve front-month futures symbol for "
                f"{self.underlying} from kite.instruments (H-6d: the old "
                f"'{self.underlying}FUT' placeholder is not a real contract "
                "— paper booked fake fills on it, live would reject)."
            ) from e
        raise RuntimeError(
            f"No {self.underlying} FUT rows in kite.instruments('NFO') — "
            "cannot resolve futures symbol (H-6d); refusing the placeholder."
        )

    def _get_futures_price(self, symbol: Optional[str] = None) -> Optional[float]:
        """Fetch a futures LTP. Returns None on any failure.

        `symbol` pins the contract to quote; without it the front-month is
        resolved. Marking an OPEN hedge must always pass the contract the
        hedge sits in (see _futures_mark) — front-month changes at the
        monthly roll.

        The futures hedge MUST be entered, marked AND flattened off this
        one series. Mixing it with index spot books the basis as phantom
        P&L: the hedge is opened at the futures price but was previously
        marked (_update_positions_prices) and flattened
        (_generate_close_all_proposals) at spot, so a long hedge showed an
        instant unrealized loss of basis x lots x lot_size the moment it
        went on. On 2026-07-10 the NIFTY basis averaged +31.8 pts, which
        put ~Rs 7.7k of phantom loss on a 4-lot hedge, tripped the Rs 15k
        daily-loss breaker at -Rs 15,297 (real: ~-Rs 7.6k), flattened the
        book near the low and locked out entries for the rest of the
        session. Same contract on both sides or the number is fiction.
        """
        if symbol:
            fut_symbol = symbol
        else:
            try:
                fut_symbol = self._get_futures_symbol()
            except Exception as e:
                self._note_futures_failure(
                    "Futures price: cannot resolve symbol: %s", e)
                return None
        key = f"NFO:{fut_symbol}"
        # Payload parsing stays INSIDE the try. _update_positions_prices
        # calls this after it has already mutated pos.current_price for
        # every option leg, so a raise here would leave a half-updated book
        # with total_pnl unset and _should_exit never evaluated — worse
        # than a skipped tick. (_get_spot_price parses outside its try; that
        # is the older shape, not one to copy onto a money-affecting mark.)
        try:
            q = self.kite.quote([key])
            if not q or key not in q or not q[key].get("last_price"):
                self._note_futures_failure(
                    "Futures quote returned no usable price for %s (got keys=%s).",
                    fut_symbol, list(q.keys()) if q else [],
                )
                return None
            price = float(q[key]["last_price"])
        except Exception as e:
            self._note_futures_failure("Futures quote raised for %s: %s: %s",
                                       fut_symbol, type(e).__name__, e)
            return None
        # getattr default: many tests (and the backtest builders) construct
        # the strategy via __new__, bypassing __init__ — same lazy-init
        # convention as _consecutive_quote_failures / _stale_marks.
        if getattr(self, "_consecutive_futures_failures", 0):
            logger.info("Futures quote recovered after %d consecutive failure(s).",
                        self._consecutive_futures_failures)
        self._consecutive_futures_failures = 0
        # Remember the last good mark for THIS contract so a quote outage
        # carries it forward instead of marking the leg flat.
        if self.state.futures_symbol == fut_symbol:
            self.state.futures_last_mark = price
        return price

    def _note_futures_failure(self, msg: str, *args) -> int:
        """H-6a ledger for the futures leg: count consecutive failures and
        escalate WARNING -> ERROR at 5, exactly like _check_spot and the
        option-leg carry. A frozen futures mark feeds _should_exit's
        daily-loss breaker, so 'same severity forever' is the silent-carry
        failure mode, not a cosmetic one."""
        n = getattr(self, "_consecutive_futures_failures", 0) + 1
        self._consecutive_futures_failures = n
        (logger.error if n >= 5 else logger.warning)(
            msg + " (consecutive futures-quote failures=%d)", *args, n)
        return n

    def _futures_mark(self) -> Optional[float]:
        """Mark price for the futures hedge: live quote of the contract we
        actually hold, else its last good mark. Returns None when neither
        is available — callers decide how to degrade.

        Quotes `state.futures_symbol`, NOT whatever `_get_futures_symbol()`
        currently calls front-month. After a monthly settlement those
        differ, and quoting the new contract against the old contract's
        entry VWAP would book the roll spread as phantom P&L — the same
        class of error as marking at index spot. Deliberately does NOT
        fall back to entry VWAP: a VWAP is not a mark, and returning it
        here silently books the leg flat.
        """
        symbol = self.state.futures_symbol or None
        price = self._get_futures_price(symbol)
        if price and price > 0:
            return price
        carried = self.state.futures_last_mark
        if carried and carried > 0:
            logger.warning(
                "Futures quote unavailable for %s — carrying last good mark "
                "%.2f (H-6a).", symbol or "front-month", carried,
            )
            return carried
        return None

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
        Get options chain for the two nearest expiries.

        Returns a DataFrame containing rows from up to the two nearest
        expiries, with the "primary" expiry (best ATM coverage among
        the two) rows ordered first. The primary expiry is also marked
        in `chain.attrs["primary_expiry"]` so callers can recover it
        explicitly via `_primary_expiry_slice(chain)`.

        Why two expiries: the calendar builder
        (`trade_proposer.propose_calendar_short_front`) requires a
        chain spanning two expiries to construct front-vs-back legs.
        Single-expiry callers (IV percentile, skew percentile, soft
        delta hedge, legacy straddle proposer) must filter via
        `_primary_expiry_slice` to preserve pre-Phase-3.2 semantics.

        Primary selection is unchanged from the pre-Phase-3.2 logic
        (pick the expiry with the most strikes within 3% of spot,
        falling back to nearest if neither has an ATM CE/PE pair).
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

            candidates = []
            for expiry in expiries[:2]:
                slice_ = df[df["expiry"] == expiry]
                strikes = slice_["strike"].unique()
                if len(strikes) == 0:
                    continue
                atm_zone = [s for s in strikes if abs(s - spot) / spot < 0.03]
                atm_strike = min(strikes, key=lambda s: abs(s - spot))
                has_ce = not slice_[(slice_["strike"] == atm_strike) & (slice_["instrument_type"] == "CE")].empty
                has_pe = not slice_[(slice_["strike"] == atm_strike) & (slice_["instrument_type"] == "PE")].empty
                candidates.append({
                    "expiry": expiry,
                    "atm_count": len(atm_zone),
                    "has_atm_pair": has_ce and has_pe,
                    "slice": slice_,
                })

            if not candidates:
                return pd.DataFrame()

            valid = [c for c in candidates if c["has_atm_pair"]]
            if valid:
                primary = max(valid, key=lambda c: c["atm_count"])
                primary_expiry = primary["expiry"]
            else:
                primary_expiry = expiries[0]

            ordered = sorted(
                candidates,
                key=lambda c: (0 if c["expiry"] == primary_expiry else 1, c["expiry"]),
            )
            chain = pd.concat([c["slice"] for c in ordered], ignore_index=True)
            chain.attrs["primary_expiry"] = primary_expiry
            return chain
        except Exception as e:
            consecutive = getattr(self, "_consecutive_chain_failures", 0) + 1
            self._consecutive_chain_failures = consecutive
            log = logger.error if consecutive >= 5 else logger.warning
            log(
                "Options-chain fetch failed (consecutive=%d): %s",
                consecutive, e,
            )
            return pd.DataFrame()

    def _primary_expiry_slice(self, chain):
        """Filter `chain` to its primary expiry rows.

        Restores pre-Phase-3.2 single-expiry semantics for callers
        that don't understand a multi-expiry chain (IV percentile,
        skew percentile, soft delta hedge, legacy straddle proposer).
        Reads `chain.attrs["primary_expiry"]` (set by
        `_get_options_chain`); falls back to the first row's expiry
        (matches the sort order written by `_get_options_chain`)
        when the attribute has been stripped — pandas `.attrs` is
        not preserved across all DataFrame operations.
        """
        if chain.empty:
            return chain
        primary_expiry = chain.attrs.get("primary_expiry")
        if primary_expiry is None:
            primary_expiry = chain.iloc[0]["expiry"]
        return chain[chain["expiry"] == primary_expiry]

    def _compute_iv_percentile(self, chain, spot):
        """
        Compute IV percentile: where current ATM IV sits relative to the
        DAILY ATM IV distribution of prior sessions (`_daily_atm_iv_history`,
        seeded from the EOD option-chain archive). This is the standard
        approach — compare the current vol regime against past regimes, not
        against the cross-sectional smile at the same tick, and not against
        the last ~1.5 sessions of intraday ticks (which is what the
        pre-2026-07-31 version did; see `_daily_atm_iv_history` in __init__).

        Returns None when the percentile cannot be computed this tick —
        an ATM quote gap, an unsolvable IV, or a daily pool still in
        warmup (<30 sessions). The caller must treat None as "not ready" and
        skip the scan; it must NOT be conflated with a genuine mid-range
        reading. Returning a neutral 50.0 for these cases (the pre-#75
        behaviour) made the IV-band tunables degenerate to a binary
        "does [min,max] contain 50" switch and let autoresearch accept
        band mutations that only zeroed trades on compute-failure ticks.
        """
        strikes = chain["strike"].unique()
        atm_strike = strikes[np.argmin(np.abs(strikes - spot))]

        # Compute ATM IV
        atm_ce = chain[(chain["strike"] == atm_strike) & (chain["instrument_type"] == "CE")]
        if atm_ce.empty:
            logger.debug("IV percentile: no ATM CE row at strike %s — not ready", atm_strike)
            return None
        try:
            symbol = atm_ce.iloc[0]["tradingsymbol"]
            q = self.kite.quote([f"NFO:{symbol}"])
            atm_price = q[f"NFO:{symbol}"]["last_price"]
            expiry_str = str(atm_ce.iloc[0]["expiry"])
            T = time_to_expiry(expiry_str, self._clock())
            if T <= 0 or atm_price <= 0:
                logger.debug("IV percentile: T=%.4f atm_price=%s — not ready", T, atm_price)
                return None
            atm_iv = implied_volatility_bisect(atm_price, spot, atm_strike, T, 0.065, "CE")
        except Exception as e:
            logger.debug("IV percentile: ATM quote/IV solve failed for %s (%s) — not ready",
                         atm_ce.iloc[0]["tradingsymbol"], e)
            return None

        if not (0.01 < atm_iv < 3.0):
            logger.debug("IV percentile: back-solved IV %.4f out of (0.01,3.0) — not ready", atm_iv)
            return None

        # Append to the tick-level rolling history. This series is NOT what
        # the percentile ranks against (see below) — it feeds the vol-of-vol
        # regime feature and supplies the current-IV reading for the RV/IV
        # gate, both of which legitimately want the intraday series.
        self._atm_iv_history.append(atm_iv)
        if len(self._atm_iv_history) > self._iv_history_max_size:
            self._atm_iv_history = self._atm_iv_history[-self._iv_history_max_size:]
        self._save_iv_history()

        # Rank against the DAILY pool, restricted to sessions strictly before
        # the current one. Ranking against _atm_iv_history — what this did
        # before — ranked the tick against ~1.5 sessions of its own intraday
        # micro-noise, which is a coin flip, not a vol regime (see
        # _daily_atm_iv_history in __init__). The date filter is a no-op live
        # (the newest EOD snapshot is yesterday's) and is the look-ahead guard
        # under tape replay, matching the #160 bootstrap pool.
        today = self._clock().date()
        pool = [v for d, v in self._daily_atm_iv_history if d < today]

        # Below 30 observations the pool is too thin for a meaningful
        # percentile. Return None (not ready) — a fabricated neutral 50.0
        # would either block (band excludes 50) or wave the trade through
        # (band includes 50) on no real evidence.
        if len(pool) < 30:
            logger.debug("IV percentile: warmup (%d/30 daily obs) — not ready", len(pool))
            return None

        from scipy.stats import percentileofscore
        return percentileofscore(pool, atm_iv)

    def _compute_skew_percentile(self, chain, spot):
        """Phase 1.3: percentile rank of IV(25Δ put) − IV(25Δ call).

        Why this matters: NIFTY/BANKNIFTY exhibit persistent put skew —
        downside strikes trade at higher IV than upside strikes. When
        the skew is *unusually rich* (high percentile), an ATM straddle
        is paying for both legs at a vol that's higher than the
        symmetric-pricing world would set — particularly the put leg.
        That premium isn't fully recoverable through delta-hedged gamma
        scalping under standard BS dynamics; Ch 15 ("path dependence")
        argues the skew should be traded directly via risk reversals or
        ratios rather than absorbed into an ATM body.

        Method:
          - For each strike in the chain, fetch market price and back
            out IV; compute the option's delta given that IV.
          - Pick the put with delta nearest −0.25 (long-dated put-OTM
            tail) and the call nearest +0.25.
          - Skew = IV_put_25Δ − IV_call_25Δ. Append to rolling history,
            return percentile against the history. Returns 50.0 during
            warmup (< 30 observations) so the filter doesn't bite.

        Performance: this is called every flat-book scan_and_propose
        tick. We BATCH the option-chain quotes into a single
        kite.quote([...]) call so a 40-strike weekly costs one REST
        request, not 40 — Kite Connect's documented quote limit is
        ~3/sec, so per-strike iteration would breach the rate limit
        within seconds and the swallowed exceptions would make the
        gate silently inert (review-fix #6).
        """
        from scipy.stats import percentileofscore

        if chain.empty:
            return 50.0
        try:
            expiry_str = str(chain.iloc[0]["expiry"])
            T = time_to_expiry(expiry_str, self._clock())
            if T <= 0:
                return 50.0
        except Exception:
            return 50.0

        # Single batched quote() for the whole chain. Kite returns a
        # dict keyed by the same symbol string we passed in; missing
        # keys (illiquid strikes, no trade today) simply don't appear
        # in the result. The single network call avoids the per-strike
        # rate-limit failure mode flagged in code review.
        relevant = chain[
            chain["instrument_type"].isin(("CE", "PE"))
            & chain["strike"].notna()
            & chain["tradingsymbol"].notna()
        ]
        if relevant.empty:
            return 50.0
        symbols = [f"NFO:{s}" for s in relevant["tradingsymbol"]]
        try:
            quotes = self.kite.quote(symbols) or {}
        except Exception as e:
            logger.warning(
                "Skew batch quote failed (%s: %s) — returning neutral 50.0",
                type(e).__name__, e,
            )
            return 50.0

        best_put = {"delta_distance": float("inf"), "iv": None}
        best_call = {"delta_distance": float("inf"), "iv": None}

        for _, row in relevant.iterrows():
            opt_type = row["instrument_type"]
            strike = row["strike"]
            symbol = row["tradingsymbol"]
            q = quotes.get(f"NFO:{symbol}")
            if not q:
                continue
            price = q.get("last_price", 0)
            if not price or price <= 0:
                continue
            try:
                iv = implied_volatility_bisect(price, spot, strike, T, 0.065, opt_type)
                if not (0.03 < iv < 3.0):
                    continue
                d = self.greeks.delta(spot, strike, T, iv, opt_type)
            except Exception as e:
                logger.debug("Skew IV/delta failed for %s: %s", symbol, e)
                continue
            # Put: target delta −0.25. Call: target delta +0.25.
            target = -0.25 if opt_type == "PE" else 0.25
            dist = abs(d - target)
            slot = best_put if opt_type == "PE" else best_call
            if dist < slot["delta_distance"]:
                slot["delta_distance"] = dist
                slot["iv"] = iv

        if best_put["iv"] is None or best_call["iv"] is None:
            return 50.0
        # Skew = put IV − call IV. Positive ⇒ put skew (typical Indian
        # equity index). Negative ⇒ reverse skew (unusual; happens in
        # gold-like assets per Taleb Ch 15).
        skew = best_put["iv"] - best_call["iv"]

        self._skew_history.append(skew)
        if len(self._skew_history) > self._iv_history_max_size:
            self._skew_history = self._skew_history[-self._iv_history_max_size:]
        # Audit 3.5: no separate save here — _save_iv_history persists BOTH
        # histories and already fired in _compute_iv_percentile earlier this
        # tick (it runs before the IV-band gate, so on every scan). This skew
        # append rides the next tick's save (and end_of_session's), avoiding a
        # redundant second file write per tick.

        if len(self._skew_history) < 30:
            return 50.0
        return percentileofscore(self._skew_history, skew)

    def _mc_empirical_returns(self) -> Optional[List[float]]:
        """Empirical daily-return pool for the bootstrap MC gate (issue #160),
        or None when mc_path_source != bootstrap (→ Gaussian paths).

        Returns dated on/after the current session are excluded: live this is
        a no-op (the newest EOD snapshot is yesterday's, all dates are past),
        but in tape replay the snapshot spans dates AFTER the replayed session,
        and without this filter a past session's gate would resample its own
        future (look-ahead). Thinning below min_empirical is handled loudly in
        risk_analyzer (falls back to Gaussian, path_source='gbm')."""
        if self.immutable_params.get("mc_path_source") != "bootstrap":
            return None
        today = self._clock().date()
        return [r for d, r in self._daily_return_history if d < today]

    def _read_mc_path_source(self) -> str:
        """Validated [strategy] mc_path_source (issue #160). Unknown values
        warn loudly and fall back to gbm — the entry path must not crash on a
        config typo, but the operator must see the intended source was NOT
        applied (Rule 12)."""
        raw = self.config.get("strategy", "mc_path_source", fallback="gbm").strip()
        if raw not in ("gbm", "bootstrap"):
            logger.warning(
                "[strategy] mc_path_source=%r is not one of ('gbm', "
                "'bootstrap') — using gbm. Fix the config to enable the "
                "bootstrap MC gate.", raw)
            return "gbm"
        return raw

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
        # 5 in-window samples (≥4 returns) is the floor for the regime call.
        # The post-loop `n < 4` check below is the final accuracy gate.
        # Earlier this was 10, which assumed intraday-tick warmup; under
        # daily seeding the in-window sample count is naturally lower.
        if len(samples) < 5:
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
        # 4 returns is the floor (5 in-window samples). The earlier `n < 5`
        # here was an off-by-one: it required 6 samples, contradicting the
        # `len(samples) < 5` guard above and the documented 5-sample floor —
        # so under daily EOD seeding the gate never produced an RV and the
        # RV/IV regime feature was permanently None (autoresearch could not
        # see rv_window_days / min_rv_iv_ratio). Same fn drives live.
        if n < 4:
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
            if isinstance(data, dict):
                hist = data.get("atm_iv", [])
                skew = data.get("skew", [])
            else:
                hist = data
                skew = []
            self._atm_iv_history = [float(x) for x in hist if 0.01 < float(x) < 3.0]
            # Skew is in absolute IV difference (typically −0.5 to +0.5);
            # range-filter conservatively so corrupted entries don't poison
            # the percentile.
            self._skew_history = [float(x) for x in skew if -1.0 < float(x) < 1.0]
            logger.info(
                "Loaded %d ATM IV and %d skew observations from %s",
                len(self._atm_iv_history), len(self._skew_history), path,
            )
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
                "skew": self._skew_history,
                "updated_at": datetime.now().isoformat(),
            }))
        except OSError as e:
            logger.warning("Could not save IV history to %s: %s", path, e)

    def _load_spot_history(self):
        # Seed _spot_history with one underlying_price per date from the most
        # recent EOD CSV produced by market_data/fetch_historical_data.py. Daily granularity
        # is sufficient for the RV/IV regime gate's default 5-day window.
        cache_dir = Path("data_cache")
        if not cache_dir.exists():
            return
        candidates = find_tables(cache_dir, f"{self.underlying}_*_eod")
        if not candidates:
            return
        path = candidates[-1]  # filenames embed end-date YYYYMMDD; lex sort = recency
        try:
            df = read_table(path, usecols=["timestamp", "underlying_price"])
            # One row per option per snapshot — dedupe to one row per date.
            # The timestamp column is a string in CSVs/backfilled parquet but
            # a real datetime in freshly fetched parquet; normalise before
            # slicing. Inside the try: a malformed timestamp must degrade
            # gracefully, not crash strategy startup.
            df["date"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d")
        except (OSError, ValueError, pd.errors.ParserError) as e:
            logger.warning("Could not load spot history from %s: %s", path, e)
            return
        df = df.drop_duplicates(subset="date", keep="last").sort_values("date")
        seeded = []
        for _, row in df.iterrows():
            try:
                # Anchor each daily sample at 15:30 IST (close), naive to match
                # the strategy's _clock() convention. The exact intraday time
                # doesn't matter for daily-spaced returns.
                ts = datetime.strptime(row["date"], "%Y-%m-%d").replace(hour=15, minute=30)
                seeded.append((ts, float(row["underlying_price"])))
            except (TypeError, ValueError):
                continue
        if seeded:
            self._spot_history = seeded[-self._spot_history_max_size:]
            logger.info("Seeded %d daily spot samples from %s", len(self._spot_history), path)
            # Issue #160: DATED daily log returns for the bootstrap MC pool,
            # from the FULL deduped daily series (not the tick-capped copy
            # above). Each return is dated by the SECOND day (the day the move
            # is realized), so the gate can exclude returns >= the current
            # session date (look-ahead guard on tape replay).
            closes = np.array([s for _, s in seeded], dtype=float)
            if len(closes) >= 2 and np.all(closes > 0):
                rets = np.diff(np.log(closes))
                rdates = [ts.date() for ts, _ in seeded[1:]]
                self._daily_return_history = list(zip(rdates, rets))
                logger.info("MC bootstrap pool: %d daily returns from %s",
                            len(self._daily_return_history), path)

    def _load_daily_atm_iv(self):
        """Seed the dated daily ATM IV pool from the EOD option-chain
        snapshots (`data_cache/<UNDERLYING>_*_eod.*`, written by
        `market_data/fetch_bhavcopy.py --underlying <index>`; the same
        archive `_load_spot_history` reads for spot).

        One observation per session: the mean `iv` of the CE/PE rows at the
        strike nearest that session's underlying price, on the nearest expiry
        at least `_daily_iv_min_dte` days out.

        Reads newest file first and stops at `_daily_iv_max_dates` distinct
        sessions, holding ONE file in memory at a time. `_load_spot_history`
        reads only `candidates[-1]` because a 5-day RV window fits in one
        file; a percentile pool needs depth, and NIFTY's archive is 80 files
        / 6.1M rows — not something to concatenate at strategy startup (cf.
        the 2026-07-11 autoresearch OOM).

        Failure is soft but LOUD: a missing/unreadable archive leaves the
        pool empty, which makes `_compute_iv_percentile` return None and the
        strategy inert. That must not be silent, so it warns here at startup
        rather than only via the per-tick debug line.
        """
        cache_dir = Path("data_cache")
        candidates = find_tables(cache_dir, f"{self.underlying}_*_eod") if cache_dir.exists() else []
        if not candidates:
            logger.warning(
                "No %s_*_eod snapshot in data_cache — daily ATM IV pool is EMPTY, "
                "so the IV-percentile entry gate cannot be computed and NO entry "
                "will fire. Run: python -m market_data.fetch_bhavcopy --underlying %s",
                self.underlying, self.underlying,
            )
            return

        # Process-level memo. This runs in __init__ and takes ~10s against
        # the NIFTY archive (80 files / 6.1M rows), but the archive is
        # immutable for the life of a sweep: the 2026-08-01 autoresearch run
        # built 393 strategies and so spent ~65 min of its 226 min wall clock
        # re-reading the same parquet. Keyed on (path, size, mtime_ns) of
        # every candidate, so ANY archive change — a new fetch_bhavcopy, a
        # rewritten file — misses the cache rather than serving a stale pool.
        try:
            fingerprint = tuple(
                (str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in candidates
            )
        except OSError as e:      # racing fetch_bhavcopy — just don't cache
            logger.debug("Daily ATM IV pool: cannot fingerprint archive (%s)", e)
            fingerprint = None
        key = (self.underlying, self._daily_iv_min_dte,
               self._daily_iv_max_dates, fingerprint)
        if fingerprint is not None and key in _DAILY_ATM_IV_CACHE:
            self._daily_atm_iv_history = list(_DAILY_ATM_IV_CACHE[key])
            logger.debug("Daily ATM IV pool: %d sessions (cached)",
                         len(self._daily_atm_iv_history))
            return

        by_date: Dict[date, float] = {}
        for path in reversed(candidates):  # filenames embed end-date; newest first
            if len(by_date) >= self._daily_iv_max_dates:
                break
            try:
                df = read_table(path, usecols=[
                    "timestamp", "underlying_price", "strike",
                    "option_type", "expiry", "iv",
                ])
                # Same sanity band _load_iv_history applies to the persisted
                # series, so one corrupt snapshot cannot poison the ranking.
                df = df[(df["iv"] > 0.01) & (df["iv"] < 3.0)]
                if df.empty:
                    continue
                df = df.assign(
                    _d=pd.to_datetime(df["timestamp"], utc=True)
                        .dt.tz_convert("Asia/Kolkata").dt.date,
                    _e=pd.to_datetime(df["expiry"]).dt.date,
                )
            except (OSError, ValueError, KeyError, TypeError, pd.errors.ParserError) as e:
                logger.warning("Could not read daily ATM IV from %s: %s", path, e)
                continue

            skipped = 0
            for d, g in df.groupby("_d", sort=False):
                if d in by_date:
                    continue  # newer file already supplied this session
                # Per-SESSION guard, mirroring _load_spot_history's per-row
                # guard above ("must degrade gracefully, not crash strategy
                # startup"). read_table is called without a dtype map, so one
                # non-numeric cell — NSE bhavcopy writes '-' for missing
                # values — leaves the column as object dtype and the float()
                # or the subtraction below raises. That used to propagate out
                # of __init__, which is called unconditionally at line ~445:
                # run_paper would die before its session loop and every
                # autoresearch experiment would score -999999. One bad
                # session must cost that session, not the trading day.
                try:
                    g = g[g["_e"] >= d + timedelta(days=self._daily_iv_min_dte)]
                    if g.empty:
                        continue
                    g = g[g["_e"] == g["_e"].min()]
                    spot = float(g["underlying_price"].iloc[0])
                    if not (spot > 0):        # also rejects NaN
                        skipped += 1
                        continue
                    atm = g["strike"].iloc[(g["strike"] - spot).abs().argsort().iloc[0]]
                    atm_rows = g[g["strike"] == atm]
                    if atm_rows.empty:
                        # NaN strike (pandas parses 'n/a'/'-' as NaN, and
                        # NaN == NaN is False) or no row at the chosen
                        # strike. A data anomaly, not the expected
                        # near-expiry filter above — count it.
                        skipped += 1
                        continue
                    iv = float(atm_rows["iv"].mean())
                except (TypeError, ValueError) as e:
                    skipped += 1
                    logger.debug("Daily ATM IV: skipping %s in %s: %s", d, path, e)
                    continue
                if iv != iv:                  # NaN mean — no usable ATM row
                    skipped += 1
                    continue
                by_date[d] = iv
            if skipped:
                # Loud at file granularity: per-session would spam, silence
                # would hide an archive quietly degrading the entry gate.
                logger.warning(
                    "Daily ATM IV: skipped %d unusable session(s) in %s — "
                    "the IV-percentile pool is thinner than the archive.",
                    skipped, path,
                )

        self._daily_atm_iv_history = sorted(by_date.items())[-self._daily_iv_max_dates:]
        if fingerprint is not None:
            _DAILY_ATM_IV_CACHE[key] = list(self._daily_atm_iv_history)
        if len(self._daily_atm_iv_history) < 30:
            logger.warning(
                "Daily ATM IV pool for %s has only %d session(s) (need 30) — the "
                "IV-percentile entry gate stays in warmup and NO entry will fire. "
                "Widen the EOD archive (fetch_bhavcopy --days N).",
                self.underlying, len(self._daily_atm_iv_history),
            )
        else:
            ivs = [v for _, v in self._daily_atm_iv_history]
            logger.info(
                "Daily ATM IV pool: %d sessions %s→%s (min %.4f max %.4f) from %s_*_eod",
                len(ivs), self._daily_atm_iv_history[0][0],
                self._daily_atm_iv_history[-1][0], min(ivs), max(ivs), self.underlying,
            )

    def _apply_risk_filters(self, proposals, spot):
        """
        All-or-nothing risk filter: if ANY leg fails liquidity, or the
        structure's margin exceeds the cap, reject the entire structure. This
        prevents malformed partial positions (e.g. keeping a short wing after
        its protective long is filtered out).

        Margin is computed at the STRUCTURE level (see _structure_margin), so a
        genuine hedge — the long leg of a vertical/backspread, or the offsetting
        leg of a calendar — credits against its short instead of every leg
        paying full naked margin. An uncovered short (e.g. the short call of a
        risk reversal, whose long put does not cap upside) still costs full
        naked margin, by design.
        """
        capital = self.immutable_params["total_capital"]
        max_margin_pct = self.immutable_params["max_position_margin_pct"]
        max_margin = capital * max_margin_pct / 100

        # Gate: liquidity (per leg, all-or-nothing).
        for prop in proposals:
            if prop.bid_ask_spread_pct > self.immutable_params.get("liquidity_min_spread_pct", 1.0):
                logger.info("Filtered %s: spread %.2f%% too wide — rejecting entire structure",
                            prop.tradingsymbol, prop.bid_ask_spread_pct)
                return []

        # Gate: structure margin vs cap.
        structure_margin = self._structure_margin(proposals)
        if structure_margin > max_margin:
            logger.info("Filtered structure: net margin ₹%.0f exceeds %.0f%% of capital "
                        "(₹%.0f) — rejecting entire structure",
                        structure_margin, max_margin_pct, max_margin)
            return []
        return proposals

    def _structure_margin(self, proposals):
        """SPAN-style margin for a multi-leg options structure: the worst-case
        loss the position can take, which credits genuine hedges instead of
        summing naked per-leg margins. Never exceeds the gross per-leg sum.

        Single-expiry: scan the expiry payoff at every strike plus the 0 and
        far-OTM boundaries. A bounded worst case (defined-risk: verticals, ratio
        backspreads, long straddles/strangles) is margined at that max loss. An
        uncovered short call makes the upside unbounded — genuinely naked risk —
        so we fall back to the gross per-leg sum.

        Multi-expiry (calendars can't be expiry-scanned on one date): a long
        calendar's max loss is its net debit, so a net-debit / net-long
        structure is margined at the debit; anything else falls back to gross.
        """
        gross = sum(p.margin_required for p in proposals)
        opt = [p for p in proposals if p.option_type in ("CE", "PE")]
        if not opt or not any(p.transaction_type == "SELL" for p in opt):
            return gross  # all-long (or no options): gross == premium outlay
        # Non-option legs (e.g. a futures hedge) keep their own naked margin.
        non_opt_margin = sum(p.margin_required for p in proposals
                             if p.option_type not in ("CE", "PE"))

        # Uncovered short call ⇒ unbounded upside loss ⇒ naked (gross). Short
        # puts are bounded (max intrinsic = strike), so the scan handles them.
        net_call_lots = sum((p.quantity if p.transaction_type == "BUY" else -p.quantity)
                            for p in opt if p.option_type == "CE")
        if net_call_lots < 0:
            return gross

        if len({str(p.expiry) for p in opt}) == 1:
            strikes = sorted({float(p.strike) for p in opt})
            test_pts = [0.0] + strikes + [max(strikes) * 3.0]

            def _pnl_at(S):
                total = 0.0
                for p in opt:
                    intrinsic = (max(S - float(p.strike), 0.0) if p.option_type == "CE"
                                 else max(float(p.strike) - S, 0.0))
                    contracts = p.lot_size * p.quantity
                    total += ((intrinsic - p.price) if p.transaction_type == "BUY"
                              else (p.price - intrinsic)) * contracts
                return total

            max_loss = max(-min(_pnl_at(S) for S in test_pts), 0.0)
            return min(gross, max_loss + non_opt_margin)

        # Multi-expiry: margin at net debit when the book is net long / net debit.
        net_debit = -sum(((-p.price) if p.transaction_type == "BUY" else p.price)
                         * p.lot_size * p.quantity for p in opt)
        net_lots = sum((p.quantity if p.transaction_type == "BUY" else -p.quantity) for p in opt)
        if net_debit > 0 and net_lots >= 0:
            return min(gross, net_debit + non_opt_margin)
        return gross

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
            # H-6c/d now raise on lookup failure. In THIS path (safety
            # trigger) a raise must not abort the option-leg closes above:
            # degrade to options-only and page the operator about the
            # unflattened hedge instead of flattening nothing.
            try:
                lot_size = self._get_lot_size()
                fut_symbol = self._get_futures_symbol()
            except Exception as e:
                logger.critical(
                    "close-all: cannot resolve futures contract (%s) — "
                    "closing option legs only; futures hedge (net delta "
                    "%.1f) NOT flattened — SQUARE IT MANUALLY before the "
                    "next session.", e, self.state.futures_hedge_delta,
                )
                return proposals
            fut_lots = abs(round(self.state.futures_hedge_delta / lot_size))
            if fut_lots > 0:
                # Price the flatten off the FUTURES contract, not spot: the
                # leg was entered at the futures price, so closing it at
                # spot books the basis as realized loss (2026-07-10:
                # -Rs 12,979 booked where the futures level gave -Rs 5,322).
                # _futures_mark keeps the H-6b guarantee — it never returns
                # 0.0, so validate_order's price>0 gate cannot reject the
                # flatten — and degrades quote -> last mark -> entry VWAP.
                fut_price = self._futures_mark() or self.state.futures_entry_vwap
                if not fut_price or fut_price <= 0:
                    logger.critical(
                        "close-all: no usable futures price AND no entry VWAP "
                        "— futures hedge (net delta %.1f) NOT flattened; "
                        "SQUARE IT MANUALLY before the next session.",
                        self.state.futures_hedge_delta,
                    )
                    return proposals
                proposals.append(TradeProposal(
                    tradingsymbol=fut_symbol,
                    instrument_token=0, strike=0, expiry="", option_type="FUT",
                    lot_size=lot_size, quantity=fut_lots,
                    price=fut_price,
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
        # H-6a staleness ledger (lazy init — same pattern as
        # _consecutive_quote_failures; many tests construct via __new__).
        if not hasattr(self, "_stale_marks"):
            self._stale_marks = {}
        for pos in self.state.positions:
            try:
                q = self.kite.quote([f"{self.exchange}:{pos.tradingsymbol}"])
                pos.current_price = q[list(q.keys())[0]]["last_price"]
                self._stale_marks.pop(pos.tradingsymbol, None)
                self._consecutive_quote_failures = 0
            except Exception as e:
                # H-6a (audit 1.6): carry the LAST GOOD mark and flag
                # staleness — never reset to entry_price. The old reset
                # zeroed the leg's unrealized P&L exactly when
                # _should_exit's loss gates needed it, and quote outages
                # correlate with the volatile tape that trips those gates.
                # The 2026-05-04 incident class was a SILENT carry; this
                # carry is loud: per-leg stale counter + escalating log.
                consecutive = getattr(self, "_consecutive_quote_failures", 0) + 1
                self._consecutive_quote_failures = consecutive
                stale = self._stale_marks.get(pos.tradingsymbol, 0) + 1
                self._stale_marks[pos.tradingsymbol] = stale
                log = logger.error if consecutive >= 5 else logger.warning
                log(
                    "Quote failed for %s (leg stale ticks=%d, consecutive=%d): "
                    "%s — carrying last good mark %.2f",
                    pos.tradingsymbol, stale, consecutive, e, pos.current_price,
                )
        unrealized = sum((p.current_price - p.entry_price) * p.quantity * p.lot_size for p in self.state.positions)
        # Futures unrealized P/L: (futures LTP - entry_vwap) * net_lots * lot_size.
        # NOT spot — the entry VWAP is a futures price, so marking against
        # the index books the basis as a standing phantom loss on a long
        # hedge. This feeds _should_exit's daily-loss breaker, so the error
        # is not merely cosmetic: see _get_futures_price for the 2026-07-10
        # session it cost.
        if self.state.futures_lots != 0 and self.state.futures_entry_vwap > 0:
            lot_size = self._get_lot_size()
            fut_price = self._futures_mark()
            if not fut_price or fut_price <= 0:
                # No quote and no carried mark (the mark is seeded at fill
                # and persisted, so this is a genuinely broken feed). Book
                # the leg flat at entry VWAP — omitting it entirely would
                # understate exposure to the daily-loss breaker with no
                # trace — and escalate, because a flat futures leg makes
                # the loss gates blind to the whole hedge.
                fut_price = self.state.futures_entry_vwap
                self._note_futures_failure(
                    "Marking futures hedge FLAT at entry VWAP %.2f — no quote "
                    "and no carried mark, so the loss gates cannot see this "
                    "leg. Check the futures feed.", fut_price,
                )
            unrealized += (fut_price - self.state.futures_entry_vwap) * self.state.futures_lots * lot_size
        self.state.unrealized_pnl = unrealized
        self.state.total_pnl = self.state.realized_pnl + unrealized

        self._record_pnl_snapshot()

    def _paper_execute(self, proposal):
        logger.info("[PAPER] %s %d lots %s @ %.2f — %s", proposal.transaction_type, proposal.quantity, proposal.tradingsymbol, proposal.price, proposal.rationale)
        return {"order_id": f"PAPER-{int(time.time())}", "status": "COMPLETE", "mode": "paper"}

    def _live_execute(self, proposal):
        # Audit 1.2 step 2: delegate to the shared executor (place →
        # poll-until-terminal → cancel/partial-reverse, marketable LIMIT —
        # the semantics the pair runner proved live on 2026-06-11). The
        # C-1 whitelist in execute_proposals books state only on the
        # executor's confirmed COMPLETE.
        executor = self._order_executor()
        # Rebind in case the runner swapped the kite client (token refresh).
        executor.kite = self.kite
        return executor.execute(proposal)

    def _order_executor(self):
        # Lazy so __new__-bypass tests and paper/signals runs never build
        # it (and it isn't an __init__ attr the backtest bootstrap must
        # mirror).
        if getattr(self, "_live_order_executor", None) is None:
            from .order_executor import KiteOrderExecutor
            try:
                lpp = self.config.getfloat(
                    "strategy", "limit_protection_pct", fallback=0.25)
            except Exception:
                lpp = 0.25
            self._live_order_executor = KiteOrderExecutor(
                self.kite,
                order_tag=f"taleb-{self.underlying}",
                limit_protection_pct=lpp,
                exchange=self.exchange,
                get_instruments=self._fetch_nfo_instruments_with_retry,
            )
        return self._live_order_executor
