"""
Pair Trading Strategy — Long-Short on Cointegrated Stock Futures
================================================================
Trades a cointegrated NIFTY 50 stock-futures pair (chosen by screen_pairs.py).

Logic:
  - Spread = price_a - hedge_ratio * price_b (hedge_ratio from screener)
  - Z-score against rolling window of spread history
  - Entry (book flat, |z| >= entry_z):
      * z < -entry_z → LONG_SPREAD  (buy A, sell hedge B)
      * z >  entry_z → SHORT_SPREAD (sell A, buy hedge B)
  - Exit  (book open):
      * |z| <= exit_z   → mean-revert exit
      * |z| >= stop_z   → stop-loss exit
  - One position at a time (no pyramiding).

History seeding:
  At init, load last `lookback_days` of front-month STF closes from
  data_cache/bhavcopy_raw/ via screen_pairs.load_front_month_panel().
  Each scan tick appends the current observation.

Mode dispatch:
  - signals: emit structured JSONL via base._emit_signal (no state mutation)
  - paper:   log + update state.positions and P&L (mock fills)
  - live:    place real kite orders + update state
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import numpy as np
import pandas as pd

from trade_proposer import TradeProposal

from .base import BaseStrategy, ExecutionMode, OrderValidationError, validate_order

logger = logging.getLogger(__name__)

PairPosition = Literal["FLAT", "LONG_SPREAD", "SHORT_SPREAD"]
PAIR_CANDIDATES_PATH = Path("data_cache/pair_candidates.csv")

# Tradeable hedge-ratio range. |β| < 0.1 means leg B is so small the spread
# is essentially leg A alone (no hedge); |β| > 10 means leg B notional
# explodes relative to leg A. Used both as the __init__ guard and as the
# filter `_top_screener_pair` applies before picking row 0.
HEDGE_RATIO_MIN = 0.1
HEDGE_RATIO_MAX = 10.0


@dataclass
class PairLeg:
    """One leg of an open pair position."""
    symbol: str            # cash-equity ticker (e.g. "RELIANCE")
    tradingsymbol: str     # NFO trading symbol (e.g. "RELIANCE26APRFUT")
    lot_size: int
    quantity: int          # signed lots: +N long, -N short
    entry_price: float
    current_price: float = 0.0


@dataclass
class PairState:
    position: PairPosition = "FLAT"
    entry_z: float = 0.0
    entry_time: Optional[datetime] = None
    entry_spread: float = 0.0
    legs: List[PairLeg] = field(default_factory=list)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_transaction_costs: float = 0.0
    closed_trades: List[dict] = field(default_factory=list)


class PairTradingStrategy(BaseStrategy):

    name = "pair_trading"

    def __init__(
        self,
        kite,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
        symbol_a: Optional[str] = None,
        symbol_b: Optional[str] = None,
        hedge_ratio: Optional[float] = None,
    ):
        super().__init__(kite, config_path=config_path, mode=mode)

        cfg = (
            dict(self.config["pair_trading"])
            if self.config.has_section("pair_trading")
            else {}
        )

        # Pair selection: explicit args win, then config, then top of screener.
        if symbol_a and symbol_b:
            self.symbol_a, self.symbol_b = symbol_a, symbol_b
            self.hedge_ratio = float(hedge_ratio) if hedge_ratio is not None else None
        elif cfg.get("symbol_a") and cfg.get("symbol_b"):
            self.symbol_a = cfg["symbol_a"]
            self.symbol_b = cfg["symbol_b"]
            self.hedge_ratio = float(cfg["hedge_ratio"]) if cfg.get("hedge_ratio") else None
        else:
            self.symbol_a, self.symbol_b, self.hedge_ratio = self._top_screener_pair()
            logger.info(
                "No pair configured — using screener top: %s/%s (β=%.4f)",
                self.symbol_a, self.symbol_b, self.hedge_ratio,
            )

        if self.hedge_ratio is None:
            raise ValueError("hedge_ratio must be supplied via arg, config, or screener output")

        # Defensive bound on β: anything outside [HEDGE_RATIO_MIN, HEDGE_RATIO_MAX]
        # either points at a corrupted screener output or a pair so mismatched it
        # shouldn't be traded as a hedge in the first place. Refuse to construct
        # the strategy rather than letting bad β size leg-B unbounded.
        if not HEDGE_RATIO_MIN <= abs(self.hedge_ratio) <= HEDGE_RATIO_MAX:
            raise ValueError(
                f"hedge_ratio out of range for {self.symbol_a}/{self.symbol_b}: "
                f"|β|={abs(self.hedge_ratio):.4f} not in "
                f"[{HEDGE_RATIO_MIN}, {HEDGE_RATIO_MAX}]"
            )

        # Risk band
        self.entry_z = float(cfg.get("entry_z", 2.0))
        self.exit_z = float(cfg.get("exit_z", 0.5))
        self.stop_z = float(cfg.get("stop_z", 4.0))
        self.lookback_days = int(cfg.get("lookback_days", 60))
        self.lots_per_leg = int(cfg.get("lots_per_leg", 1))
        self.max_holding_days = int(cfg.get("max_holding_days", 10))

        # Optional per-leg notional cap (₹). Without it, high-β pairs can
        # silently deploy huge amounts (e.g. β=10 with 1 lot of A → ~10 lots
        # of B by notional). When set, sizing scales BOTH legs down so the
        # hedge ratio is preserved; if even 1 lot of the larger leg breaks
        # the cap, the entry is skipped.
        mln = cfg.get("max_leg_notional", "").strip()
        self.max_leg_notional: Optional[float] = float(mln) if mln else None

        # max_leg_notional is the only hard cap on per-entry deployed notional.
        # In signals-only mode it's informational, but for paper / live it must
        # be set so a misconfigured hedge_ratio cannot multiply leg-B sizing.
        if self.mode != "signals" and self.max_leg_notional is None:
            raise ValueError(
                "max_leg_notional must be set in [pair_trading] config when "
                f"mode={self.mode!r}; refusing to run without a notional cap"
            )

        # Shared sizing/risk knobs from [strategy]
        self.total_capital = self.config.getfloat("strategy", "total_capital", fallback=500000)

        # State
        self.state = PairState()
        self._spread_history: List[float] = []
        self._cached_futures: Dict[str, dict] = {}  # symbol → {tradingsymbol, lot_size, expiry}
        self._clock = datetime.now

        self._seed_spread_history()

    # ══════════════════════════════════════════════════════════
    # PUBLIC API (BaseStrategy interface)
    # ══════════════════════════════════════════════════════════

    def scan_and_propose(self) -> List[TradeProposal]:
        if self.state.position != "FLAT":
            return []
        spread, prices = self._observe_spread()
        if spread is None:
            return []
        z = self._z_score(spread)
        if z is None:
            logger.debug("Spread history too thin (%d obs) for z-score", len(self._spread_history))
            return []

        if z <= -self.entry_z:
            return self._build_entry_proposals(direction="LONG_SPREAD", z=z, spread=spread, prices=prices)
        if z >= self.entry_z:
            return self._build_entry_proposals(direction="SHORT_SPREAD", z=z, spread=spread, prices=prices)
        return []

    def check_and_rehedge(self) -> List[TradeProposal]:
        if self.state.position == "FLAT":
            return []

        spread, prices = self._observe_spread()
        if spread is None:
            return []
        z = self._z_score(spread)

        # Update marks for unrealized P&L reporting
        self._update_unrealized(prices)

        # Time-based exit (positions shouldn't drift forever)
        if self.state.entry_time:
            held_days = (self._clock() - self.state.entry_time).total_seconds() / 86400.0
            if held_days >= self.max_holding_days:
                return self._build_exit_proposals(reason="MAX_HOLD", z=z or 0.0, prices=prices)

        if z is None:
            return []

        if abs(z) <= self.exit_z:
            return self._build_exit_proposals(reason="MEAN_REVERT", z=z, prices=prices)
        if abs(z) >= self.stop_z:
            return self._build_exit_proposals(reason="STOP", z=z, prices=prices)
        return []

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        # signals mode: emit and return without touching state
        if self.is_signals_mode:
            return [self._emit_signal(p) for p in proposals]

        results = []
        is_entry_batch = (self.state.position == "FLAT")

        for prop in proposals:
            result = self._paper_execute(prop) if self.is_paper_mode else self._live_execute(prop)
            results.append(result)
            if result.get("status") == "FAILED":
                logger.warning("Order FAILED for %s: %s", prop.tradingsymbol, result.get("error"))
                continue
            self._apply_fill(prop)

        # Classify the batch outcome to set position direction
        if is_entry_batch and self.state.legs:
            self._set_position_from_legs()

        # If the book is now flat, log the closed trade
        if not self.state.legs and not is_entry_batch:
            self._record_close()
            self.state.position = "FLAT"
            self.state.entry_time = None
            self.state.entry_z = 0.0
            self.state.entry_spread = 0.0
            self.state.unrealized_pnl = 0.0

        return results

    def generate_eod_report(self) -> Dict:
        spread, prices = self._observe_spread()
        z = self._z_score(spread) if spread is not None else None
        return {
            "strategy": self.name,
            "pair": (self.symbol_a, self.symbol_b),
            "hedge_ratio": self.hedge_ratio,
            "position": self.state.position,
            "current_z": z,
            "entry_z": self.state.entry_z,
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "transaction_costs": self.state.total_transaction_costs,
            "n_closed_trades": len(self.state.closed_trades),
            "spread_history_size": len(self._spread_history),
        }

    # ══════════════════════════════════════════════════════════
    # SPREAD / Z-SCORE
    # ══════════════════════════════════════════════════════════

    # Spreads quoted in ₹; treat values within 1 paisa as the same observation
    # so intraday tick noise around an unchanged closing print doesn't queue
    # a new bar. Conservative: real intraday spread moves are typically
    # several paisa even on quiet names.
    _SPREAD_EPSILON = 0.01

    def _observe_spread(self) -> Tuple[Optional[float], Dict[str, float]]:
        """Fetch live front-month quotes for both legs and return (spread, prices).

        Only appends to `_spread_history` when the spread has actually moved
        from the previous observation. Without this guard, an off-hours tick
        loop keeps re-appending the same closing spread; the rolling window
        fills with repeats, std collapses toward 0, and z drifts on every
        tick despite no real price change. Operator-visible symptom:
        z-score changing on every dashboard refresh while NSE is closed.
        """
        prices = {}
        for sym in (self.symbol_a, self.symbol_b):
            fut = self._resolve_futures(sym)
            if not fut:
                return None, {}
            quote = self._get_last_price(fut["tradingsymbol"])
            if quote is None:
                return None, {}
            prices[sym] = quote
        spread = prices[self.symbol_a] - self.hedge_ratio * prices[self.symbol_b]

        if (
            not self._spread_history
            or abs(spread - self._spread_history[-1]) > self._SPREAD_EPSILON
        ):
            self._spread_history.append(spread)
            # cap memory — only the most recent lookback*2 observations matter
            max_keep = max(self.lookback_days * 2, 500)
            if len(self._spread_history) > max_keep:
                self._spread_history = self._spread_history[-max_keep:]
        return spread, prices

    def _z_score(self, spread_now: float) -> Optional[float]:
        # Use the *prior* observations as the rolling distribution so the current
        # bar doesn't bias its own z-score downward.
        history = self._spread_history[:-1] if len(self._spread_history) > self.lookback_days else self._spread_history
        recent = history[-self.lookback_days:]
        if len(recent) < max(20, self.lookback_days // 4):
            return None
        mean = float(np.mean(recent))
        std = float(np.std(recent))
        if std == 0:
            return None
        return (spread_now - mean) / std

    # ══════════════════════════════════════════════════════════
    # PROPOSAL BUILDERS
    # ══════════════════════════════════════════════════════════

    def _build_entry_proposals(
        self, direction: PairPosition, z: float,
        spread: float, prices: Dict[str, float],
    ) -> List[TradeProposal]:
        """
        LONG_SPREAD  → buy A, sell hedge-equivalent B (expect spread to rise)
        SHORT_SPREAD → sell A, buy hedge-equivalent B (expect spread to fall)

        Sizing follows Varsity Trading Systems Ch. 13/14 (share-count β-weighted):
        spread = A − β·B is hedged by qty_B_shares = |β|·qty_A_shares. We anchor
        on `lots_per_leg` lots of A; if that would round B below 1 lot
        (Varsity Ch. 14: HDFC β=0.79 vs ICICI lot 2750), we anchor on B and
        scale A up to preserve the share-count ratio.

        If max_leg_notional is set, both legs scale down proportionally so the
        hedge ratio is preserved.
        """
        fut_a = self._resolve_futures(self.symbol_a)
        fut_b = self._resolve_futures(self.symbol_b)
        if not (fut_a and fut_b):
            return []

        # Refuse if 1 lot of EITHER leg busts the cap — we can't size below
        # 1 lot, so the cap can't be honoured under any anchoring.
        if self.max_leg_notional:
            one_lot_a_notional = fut_a["lot_size"] * prices[self.symbol_a]
            one_lot_b_notional = fut_b["lot_size"] * prices[self.symbol_b]
            if max(one_lot_a_notional, one_lot_b_notional) > self.max_leg_notional:
                logger.warning(
                    "%s/%s: 1 lot of the larger leg deploys ₹%.0f, exceeds cap ₹%.0f — skipping entry",
                    self.symbol_a, self.symbol_b,
                    max(one_lot_a_notional, one_lot_b_notional),
                    self.max_leg_notional,
                )
                return []

        # Share-count β-weighted sizing (Varsity Ch. 13/14).
        beta_abs = abs(self.hedge_ratio)
        qty_a = self.lots_per_leg
        target_b_shares = beta_abs * qty_a * fut_a["lot_size"]
        qty_b = round(target_b_shares / fut_b["lot_size"])
        if qty_b < 1:
            # Anchoring on A would put B below 1 lot — flip the anchor to B
            # and scale A up so the realized ratio still tracks β. This is
            # the Varsity HDFC/ICICI case (small β, big lot mismatch).
            qty_b = self.lots_per_leg
            target_a_shares = qty_b * fut_b["lot_size"] / beta_abs
            qty_a = max(round(target_a_shares / fut_a["lot_size"]), 1)

        notional_a = qty_a * fut_a["lot_size"] * prices[self.symbol_a]
        notional_b = qty_b * fut_b["lot_size"] * prices[self.symbol_b]

        # Apply per-leg cap by scaling BOTH legs down proportionally.
        if self.max_leg_notional:
            max_natural = max(notional_a, notional_b)
            if max_natural > self.max_leg_notional:
                scale = self.max_leg_notional / max_natural
                qty_a = max(int(round(qty_a * scale)), 1)
                qty_b = max(int(round(qty_b * scale)), 1)

        # Sign convention: spread = A - β·B
        # LONG_SPREAD wants spread to rise → +A, sign of -β on B
        # SHORT_SPREAD wants spread to fall → -A, sign of +β on B
        beta_sign = 1 if self.hedge_ratio >= 0 else -1
        if direction == "LONG_SPREAD":
            side_a, side_b = "BUY", ("SELL" if beta_sign > 0 else "BUY")
        else:
            side_a, side_b = "SELL", ("BUY" if beta_sign > 0 else "SELL")

        rationale = (
            f"{direction} on {self.symbol_a}/{self.symbol_b} "
            f"z={z:.2f} (entry_z={self.entry_z}) "
            f"spread={spread:.2f} β={self.hedge_ratio:.4f}"
        )
        return [
            self._make_fut_proposal(fut_a, qty_a, prices[self.symbol_a], side_a, rationale),
            self._make_fut_proposal(fut_b, qty_b, prices[self.symbol_b], side_b, rationale),
        ]

    def _build_exit_proposals(
        self, reason: str, z: float, prices: Dict[str, float],
    ) -> List[TradeProposal]:
        rationale = (
            f"EXIT_{reason} on {self.symbol_a}/{self.symbol_b} "
            f"z={z:.2f} entry_z={self.state.entry_z:.2f}"
        )
        proposals = []
        for leg in self.state.legs:
            fut = self._resolve_futures(leg.symbol)
            if not fut:
                continue
            side = "SELL" if leg.quantity > 0 else "BUY"
            proposals.append(
                self._make_fut_proposal(
                    fut, abs(leg.quantity), prices.get(leg.symbol, leg.current_price),
                    side, rationale,
                )
            )
        return proposals

    def _make_fut_proposal(
        self, fut: dict, quantity: int, price: float,
        transaction_type: str, rationale: str,
    ) -> TradeProposal:
        notional = price * fut["lot_size"] * quantity
        return TradeProposal(
            tradingsymbol=fut["tradingsymbol"],
            instrument_token=int(fut.get("instrument_token", 0)),
            strike=0.0,
            expiry=str(fut.get("expiry", "")),
            option_type="FUT",
            lot_size=int(fut["lot_size"]),
            quantity=int(quantity),
            price=float(price),
            transaction_type=transaction_type,
            iv=0.0,
            bid_ask_spread_pct=0.0,
            margin_required=notional * 0.20,  # rough 20% SPAN+exposure proxy
            rationale=rationale,
        )

    # ══════════════════════════════════════════════════════════
    # FILL HANDLING / STATE UPDATES
    # ══════════════════════════════════════════════════════════

    def _apply_fill(self, prop: TradeProposal) -> None:
        symbol = self._symbol_from_tradingsymbol(prop.tradingsymbol)
        signed_qty = prop.quantity if prop.transaction_type == "BUY" else -prop.quantity

        # Transaction cost — stock futures cost model is close enough to
        # the existing FUT branch in dynamic_hedger.estimate_transaction_cost.
        from strategies.taleb_karpathy import estimate_transaction_cost
        cost = estimate_transaction_cost(
            prop.price, prop.quantity, prop.lot_size, prop.transaction_type,
            instrument_type="FUT",
        )
        self.state.total_transaction_costs += cost
        self.state.realized_pnl -= cost

        existing = next((l for l in self.state.legs if l.symbol == symbol), None)
        if existing is None:
            self.state.legs.append(PairLeg(
                symbol=symbol, tradingsymbol=prop.tradingsymbol,
                lot_size=prop.lot_size, quantity=signed_qty,
                entry_price=prop.price, current_price=prop.price,
            ))
            return

        old_qty = existing.quantity
        new_qty = old_qty + signed_qty
        if new_qty == 0:
            realized = (prop.price - existing.entry_price) * old_qty * existing.lot_size
            self.state.realized_pnl += realized
            self.state.legs.remove(existing)
            logger.info("Closed %s leg: realized ₹%.0f", symbol, realized)
        elif old_qty * signed_qty < 0:
            closed_qty = min(abs(old_qty), abs(signed_qty)) * (1 if old_qty > 0 else -1)
            realized = (prop.price - existing.entry_price) * closed_qty * existing.lot_size
            self.state.realized_pnl += realized
            existing.quantity = new_qty
        else:
            # Adding to position — VWAP entry price
            existing.entry_price = (
                existing.entry_price * old_qty + prop.price * signed_qty
            ) / new_qty
            existing.quantity = new_qty

    def _set_position_from_legs(self) -> None:
        leg_a = next((l for l in self.state.legs if l.symbol == self.symbol_a), None)
        if leg_a is None:
            return
        self.state.position = "LONG_SPREAD" if leg_a.quantity > 0 else "SHORT_SPREAD"
        self.state.entry_time = self._clock()
        if self._spread_history:
            self.state.entry_spread = self._spread_history[-1]
            self.state.entry_z = self._z_score(self._spread_history[-1]) or 0.0

    def _update_unrealized(self, prices: Dict[str, float]) -> None:
        unrealized = 0.0
        for leg in self.state.legs:
            cur = prices.get(leg.symbol, leg.current_price)
            leg.current_price = cur
            unrealized += (cur - leg.entry_price) * leg.quantity * leg.lot_size
        self.state.unrealized_pnl = unrealized

    def _record_close(self) -> None:
        self.state.closed_trades.append({
            "exit_time": self._clock(),
            "entry_time": self.state.entry_time,
            "entry_z": self.state.entry_z,
            "entry_spread": self.state.entry_spread,
            "realized_pnl": self.state.realized_pnl,
            "transaction_costs": self.state.total_transaction_costs,
            "position": self.state.position,
        })

    # ══════════════════════════════════════════════════════════
    # KITE / DATA HELPERS
    # ══════════════════════════════════════════════════════════

    def _resolve_futures(self, symbol: str) -> Optional[dict]:
        """Return {tradingsymbol, lot_size, expiry, instrument_token} for the
        front-month STF on `symbol`, cached per session."""
        if symbol in self._cached_futures:
            return self._cached_futures[symbol]
        try:
            instruments = self.kite.instruments("NFO")
        except Exception as e:
            logger.warning("instruments('NFO') failed: %s", e)
            return None

        today = self._clock().date()
        candidates = [
            i for i in instruments
            if i.get("name") == symbol and i.get("instrument_type") == "FUT"
        ]
        if not candidates:
            logger.warning("No FUT contracts found for %s", symbol)
            return None

        # Smallest expiry on or after today
        def _exp_date(row):
            exp = row.get("expiry")
            if isinstance(exp, str):
                return datetime.strptime(exp[:10], "%Y-%m-%d").date()
            if hasattr(exp, "date"):
                return exp.date()
            return exp

        future_rows = [r for r in candidates if _exp_date(r) >= today]
        if not future_rows:
            return None
        front = min(future_rows, key=_exp_date)

        info = {
            "tradingsymbol": front["tradingsymbol"],
            "lot_size": int(front.get("lot_size", 0) or 0),
            "expiry": front.get("expiry"),
            "instrument_token": int(front.get("instrument_token", 0) or 0),
        }
        self._cached_futures[symbol] = info
        return info

    def _get_last_price(self, tradingsymbol: str) -> Optional[float]:
        try:
            quote = self.kite.quote([f"NFO:{tradingsymbol}"])
            return float(quote[f"NFO:{tradingsymbol}"]["last_price"])
        except Exception as e:
            logger.warning("quote failed for %s: %s", tradingsymbol, e)
            return None

    def _symbol_from_tradingsymbol(self, tradingsymbol: str) -> str:
        for sym, fut in self._cached_futures.items():
            if fut["tradingsymbol"] == tradingsymbol:
                return sym
        return tradingsymbol

    def _seed_spread_history(self) -> None:
        """
        Bootstrap the rolling spread series from cached bhavcopy front-month STF
        closes for both legs. If bhavcopy isn't available the strategy starts
        with an empty buffer and accumulates intraday observations until z-score
        becomes computable (~20 ticks).
        """
        try:
            from screen_pairs import load_front_month_panel
            panel = load_front_month_panel(
                [self.symbol_a, self.symbol_b],
                min_coverage=0.5,
            )
        except Exception as e:
            logger.warning(
                "Could not seed spread history from bhavcopy: %s — "
                "z-score will be unavailable until ~20 intraday ticks accumulate.",
                e,
            )
            return

        if self.symbol_a not in panel.columns or self.symbol_b not in panel.columns:
            logger.warning("Bhavcopy panel missing one or both pair legs; spread seed empty")
            return

        seed = (panel[self.symbol_a] - self.hedge_ratio * panel[self.symbol_b]).dropna().tolist()
        # Keep at most lookback_days*3 so the warm-up doesn't dominate the rolling window
        self._spread_history = seed[-self.lookback_days * 3:]
        logger.info(
            "Seeded %d historical spread observations for %s/%s",
            len(self._spread_history), self.symbol_a, self.symbol_b,
        )

    @staticmethod
    def _top_screener_pair() -> Tuple[str, str, float]:
        if not PAIR_CANDIDATES_PATH.exists():
            raise FileNotFoundError(
                f"{PAIR_CANDIDATES_PATH} not found — "
                "run `python screen_pairs.py` to generate pair candidates first."
            )
        df = pd.read_csv(PAIR_CANDIDATES_PATH).sort_values("rank_score")
        if df.empty:
            raise RuntimeError(f"{PAIR_CANDIDATES_PATH} has no candidates")
        # Filter to pairs the strategy can actually trade. The screener has no
        # β-range filter, so its row 0 (best rank_score) can be a pair the
        # __init__ guard would reject — see 2026-05-08 LT/MARUTI β=0.077 incident.
        beta_abs = df["hedge_ratio"].abs()
        tradeable = df[(beta_abs >= HEDGE_RATIO_MIN) & (beta_abs <= HEDGE_RATIO_MAX)]
        if tradeable.empty:
            raise RuntimeError(
                f"{PAIR_CANDIDATES_PATH} has {len(df)} candidates but none with "
                f"|β| in [{HEDGE_RATIO_MIN}, {HEDGE_RATIO_MAX}] — re-run "
                "`python screen_pairs.py` or set pair_trading.symbol_a/b in config."
            )
        row = tradeable.iloc[0]
        return str(row["symbol_a"]), str(row["symbol_b"]), float(row["hedge_ratio"])

    # ══════════════════════════════════════════════════════════
    # EXECUTION (paper / live)
    # ══════════════════════════════════════════════════════════

    def _paper_execute(self, prop: TradeProposal) -> Dict:
        logger.info(
            "[PAPER] %s %d lots %s @ %.2f — %s",
            prop.transaction_type, prop.quantity, prop.tradingsymbol,
            prop.price, prop.rationale,
        )
        return {"order_id": f"PAPER-{int(time.time())}", "status": "COMPLETE", "mode": "paper"}

    def _live_execute(self, prop: TradeProposal) -> Dict:
        try:
            validate_order(prop)
        except OrderValidationError as e:
            logger.error("Order rejected pre-submit: %s — %s", e, prop)
            return {"order_id": None, "status": "REJECTED", "error": str(e), "mode": "live"}
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR, exchange="NFO",
                tradingsymbol=prop.tradingsymbol,
                transaction_type=(
                    self.kite.TRANSACTION_TYPE_BUY if prop.transaction_type == "BUY"
                    else self.kite.TRANSACTION_TYPE_SELL
                ),
                quantity=abs(prop.quantity) * prop.lot_size,
                product=self.kite.PRODUCT_NRML,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=prop.price,
                validity=self.kite.VALIDITY_DAY,
            )
            return {"order_id": order_id, "status": "PENDING", "mode": "live"}
        except Exception as e:
            return {"order_id": None, "status": "FAILED", "error": str(e), "mode": "live"}
