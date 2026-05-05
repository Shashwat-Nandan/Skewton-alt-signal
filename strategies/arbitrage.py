"""
Arbitrage Strategy — Cash–Futures Basis & Calendar (Term-Structure)
===================================================================
Two related mispricings on Indian single-stock F&O:

  1. Cash–Futures basis
     Theoretical fair futures price under cost-of-carry:
         F* = S · exp((r - q) · T)
     where S is spot, r is the risk-free rate, q is the dividend yield,
     and T is time-to-expiry in years.
     Annualized basis = (F - F*) / S · 365 / days_to_expiry.

       * F too rich vs F*  → cash-and-carry: SELL fut + BUY spot
       * F too cheap vs F* → reverse arb:    BUY fut + SELL spot

     The cash leg requires equity capital (cash-and-carry) or pre-existing
     inventory / SLB (reverse). For retail F&O accounts neither is generally
     executable, so this variant is **signals-only**: we log the
     opportunity but don't take it.

  2. Calendar / term-structure spread
     Same-underlying near-month (F1) vs next-month (F2). Implied carry:
         carry_implied = ln(F2 / F1) · 365 / (T2 - T1)   [annualized]
     Fair carry ≈ r - q. When the implied carry diverges by more than an
     entry threshold, sell the rich leg and buy the cheap leg in equal
     lot count (per-leg notional roughly equal because F1≈F2). Both legs
     are F&O — fully tradable in **paper** and **live**.

The strategy refreshes its universe + carry table on each scan tick.
There is no rolling history to seed (unlike pair_trading); the basis is
a point-in-time mispricing and the entry rule is threshold-based.

Mode dispatch:
  - signals: emit JSONL via base._emit_signal (no state mutation)
  - paper:   simulated fills + state update
  - live:    place real kite orders (only calendar spreads — cash-side
             is never lifted to a live order)
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Literal, Optional, Tuple

from trade_proposer import TradeProposal

from .base import BaseStrategy, ExecutionMode, OrderValidationError, validate_order

logger = logging.getLogger(__name__)

CalendarPosition = Literal["FLAT", "LONG_CALENDAR", "SHORT_CALENDAR"]


@dataclass
class CalendarLeg:
    """One leg of an open calendar spread."""
    symbol: str               # underlying ticker (e.g. "RELIANCE")
    tradingsymbol: str        # NFO trading symbol (e.g. "RELIANCE26APRFUT")
    expiry: str
    lot_size: int
    quantity: int             # signed lots: +N long, -N short
    entry_price: float
    current_price: float = 0.0


@dataclass
class CalendarTrade:
    """One open calendar spread on a single underlying."""
    symbol: str
    position: CalendarPosition
    entry_time: datetime
    entry_carry_diff: float   # implied carry minus fair carry (annualized, fraction)
    legs: List[CalendarLeg] = field(default_factory=list)


@dataclass
class ArbitrageState:
    open_calendars: Dict[str, CalendarTrade] = field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_transaction_costs: float = 0.0
    closed_trades: List[dict] = field(default_factory=list)
    # Most recent basis snapshot per symbol; published in EOD reports.
    last_basis_snapshot: List[dict] = field(default_factory=list)
    # carry_diff observed at proposal time, keyed by symbol. Carries the
    # number from scan_and_propose into _apply_fill so closed_trades record
    # the entry conditions accurately.
    pending_entry_diff: Dict[str, float] = field(default_factory=dict)


class ArbitrageStrategy(BaseStrategy):
    """
    Cash-futures basis (signals-only) + calendar/term-structure spreads (tradable).

    Reads universe + carry rates from config. Each scan tick pulls quotes for
    spot + near + next month futures per symbol, computes mispricings, and
    proposes entries above threshold. Open calendar trades are managed by
    check_and_rehedge — exit on convergence, max-hold, or near-expiry.
    """

    name = "arbitrage"

    def __init__(
        self,
        kite,
        config_path: str = "config.ini",
        mode: Optional[ExecutionMode] = None,
        universe: Optional[List[str]] = None,
    ):
        super().__init__(kite, config_path=config_path, mode=mode)

        cfg = (
            dict(self.config["arbitrage"])
            if self.config.has_section("arbitrage")
            else {}
        )

        # Universe: explicit arg > config csv > NIFTY-50 default
        if universe:
            self.universe = list(universe)
        elif cfg.get("universe"):
            self.universe = [s.strip() for s in cfg["universe"].split(",") if s.strip()]
        else:
            from screen_pairs import NIFTY_50
            self.universe = list(NIFTY_50)

        # Carry assumptions — hardcoded per config (no per-stock dividend yield).
        # Stocks with high dividend yield (q > 0) will appear as "cash rich"
        # under the q=0 default; we expose `dividend_yield_default` so the
        # operator can shift the whole curve up if needed.
        self.risk_free_rate = float(cfg.get("risk_free_rate", 0.07))
        self.dividend_yield = float(cfg.get("dividend_yield_default", 0.0))

        # Cash-futures basis — always emitted as a signal (never traded).
        self.basis_entry_annual = float(cfg.get("basis_entry_annual", 0.015))   # 1.5%
        self.basis_min_dte = int(cfg.get("basis_min_dte", 3))                  # ignore final 3 days
        # Set true to run as a pure basis-monitoring service: no calendar
        # entries are proposed regardless of carry diff, but basis signals
        # still emit. Per the 6-month full-archive backtest the calendar
        # leg's gross edge doesn't cover retail F&O costs, so this is the
        # default-honest deployment mode.
        self.disable_calendar = (
            str(cfg.get("disable_calendar", "false")).strip().lower() in ("true", "1", "yes")
        )

        # Calendar spread — tradable
        self.calendar_entry_annual = float(cfg.get("calendar_entry_annual", 0.020))
        self.calendar_exit_annual = float(cfg.get("calendar_exit_annual", 0.005))
        self.calendar_max_holding_days = int(cfg.get("calendar_max_holding_days", 15))
        self.calendar_min_dte_near = int(cfg.get("calendar_min_dte_near", 4))
        # Cleanliness gate: skip the calendar if either leg has a large
        # standalone cash-futures basis (likely a discrete dividend pricing
        # artifact, not a calendar mispricing). Without this, full-archive
        # backtests pile into one-sided LONG_CALENDAR trades on RVNL /
        # MUTHOOTFIN / PGEL that bleed even pre-cost.
        self.calendar_max_leg_basis = float(cfg.get("calendar_max_leg_basis", 0.10))
        self.lots_per_leg = int(cfg.get("lots_per_leg", 1))
        self.max_open_calendars = int(cfg.get("max_open_calendars", 5))
        # Per-leg notional cap so a 1-lot RELIANCE+ITC pair doesn't deploy ₹50L silently.
        mln = cfg.get("max_leg_notional", "").strip()
        self.max_leg_notional: Optional[float] = float(mln) if mln else None

        # Shared sizing
        self.total_capital = self.config.getfloat("strategy", "total_capital", fallback=500000)

        # State
        self.state = ArbitrageState()
        self._instrument_cache: Optional[List[dict]] = None
        self._clock = datetime.now

    # ══════════════════════════════════════════════════════════
    # PUBLIC API (BaseStrategy interface)
    # ══════════════════════════════════════════════════════════

    def scan_and_propose(self) -> List[TradeProposal]:
        proposals: List[TradeProposal] = []
        snapshots = self._observe_universe()
        self.state.last_basis_snapshot = snapshots

        for snap in snapshots:
            # Cash-futures basis is ALWAYS emitted as a signal regardless of
            # execution mode — the strategy doubles as a basis-monitoring
            # service. The proposals are routed to _emit_signal in
            # execute_proposals so paper/live state is never touched.
            if (
                snap["near"] is not None
                and snap["dte_near"] >= self.basis_min_dte
                and abs(snap["basis_annual"]) >= self.basis_entry_annual
            ):
                proposals.extend(self._build_basis_signal(snap))

            # Calendar spread: tradable in any mode (subject to disable_calendar).
            #
            # Cleanliness gate: skip when either leg's standalone cash-futures
            # basis is large in absolute terms. A "calendar mispricing" where
            # one leg is also far from cash-and-carry fair is almost always a
            # discrete dividend pricing artifact, not a true term-structure
            # arb. Without this gate, the strategy piled into one-sided
            # LONG_CALENDAR trades on dividend-rich names across a 6-month
            # backtest and lost ~₹61k pre-cost.
            if self.disable_calendar:
                continue
            if (
                snap["near"] is not None
                and snap["next"] is not None
                and snap["dte_near"] >= self.calendar_min_dte_near
                and snap["carry_diff"] is not None
                and abs(snap["carry_diff"]) >= self.calendar_entry_annual
                and snap["symbol"] not in self.state.open_calendars
                and len(self.state.open_calendars) < self.max_open_calendars
                and abs(snap["basis_annual"]) <= self.calendar_max_leg_basis
                and snap.get("basis_annual_next") is not None
                and abs(snap["basis_annual_next"]) <= self.calendar_max_leg_basis
            ):
                cal_proposals = self._build_calendar_entry(snap)
                if cal_proposals:
                    self.state.pending_entry_diff[snap["symbol"]] = snap["carry_diff"]
                proposals.extend(cal_proposals)

        return proposals

    def check_and_rehedge(self) -> List[TradeProposal]:
        if not self.state.open_calendars:
            return []

        snapshots = {s["symbol"]: s for s in self._observe_universe()}
        proposals: List[TradeProposal] = []
        self._update_unrealized(snapshots)

        for symbol, trade in list(self.state.open_calendars.items()):
            snap = snapshots.get(symbol)
            if snap is None:
                continue

            held_days = (self._clock() - trade.entry_time).total_seconds() / 86400.0

            # Force-exit if near-month is about to expire (cash settlement risk).
            if snap["near"] is not None and snap["dte_near"] <= 1:
                proposals.extend(self._build_calendar_exit(trade, snap, "EXPIRY"))
                continue

            if held_days >= self.calendar_max_holding_days:
                proposals.extend(self._build_calendar_exit(trade, snap, "MAX_HOLD"))
                continue

            # Mean-revert: implied carry has converged toward fair.
            cd = snap.get("carry_diff")
            if cd is not None and abs(cd) <= self.calendar_exit_annual:
                proposals.extend(self._build_calendar_exit(trade, snap, "CONVERGE"))
                continue

        return proposals

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[Dict]:
        if self.is_signals_mode:
            return [self._emit_signal(p) for p in proposals]

        results: List[Dict] = []
        for prop in proposals:
            # Basis-side proposals (BOTH legs) are signals-only by policy —
            # cash because retail can't reliably execute it, and the futures
            # leg because the trade only makes sense paired with the cash
            # leg. Route them through _emit_signal regardless of execution
            # mode so paper/live state is never touched.
            if prop.option_type in ("CASH", "FUT_BASIS"):
                results.append(self._emit_signal(prop))
                continue

            result = self._paper_execute(prop) if self.is_paper_mode else self._live_execute(prop)
            results.append(result)
            if result.get("status") == "FAILED":
                logger.warning("Order FAILED for %s: %s", prop.tradingsymbol, result.get("error"))
                continue
            self._apply_fill(prop)

        return results

    def generate_eod_report(self) -> Dict:
        return {
            "strategy": self.name,
            "open_calendars": [
                {
                    "symbol": t.symbol,
                    "position": t.position,
                    "entry_carry_diff": t.entry_carry_diff,
                    "legs": [
                        {"tradingsymbol": l.tradingsymbol, "qty": l.quantity,
                         "entry": l.entry_price, "current": l.current_price}
                        for l in t.legs
                    ],
                }
                for t in self.state.open_calendars.values()
            ],
            "realized_pnl": self.state.realized_pnl,
            "unrealized_pnl": self.state.unrealized_pnl,
            "transaction_costs": self.state.total_transaction_costs,
            "n_closed_trades": len(self.state.closed_trades),
            "last_basis_snapshot": self.state.last_basis_snapshot[:20],
            "universe_size": len(self.universe),
        }

    # ══════════════════════════════════════════════════════════
    # CARRY MATH
    # ══════════════════════════════════════════════════════════

    def _fair_future(self, spot: float, dte_days: int) -> float:
        T = max(dte_days, 0) / 365.0
        return spot * math.exp((self.risk_free_rate - self.dividend_yield) * T)

    def _annualized_basis(self, spot: float, fut: float, dte_days: int) -> float:
        """(F - F*)/S · 365 / dte. Positive = futures rich."""
        if spot <= 0 or dte_days <= 0:
            return 0.0
        fair = self._fair_future(spot, dte_days)
        return (fut - fair) / spot * (365.0 / dte_days)

    def _implied_carry(self, near: float, nxt: float, dte_near: int, dte_next: int) -> Optional[float]:
        """ln(F2/F1) · 365 / (T2 - T1). None if degenerate inputs."""
        if near <= 0 or nxt <= 0 or dte_next <= dte_near:
            return None
        return math.log(nxt / near) * 365.0 / (dte_next - dte_near)

    # ══════════════════════════════════════════════════════════
    # OBSERVE UNIVERSE
    # ══════════════════════════════════════════════════════════

    def _observe_universe(self) -> List[dict]:
        """Snapshot spot + near + next future for every symbol in the universe.

        Memoized per `_clock()` tick: scan/check/EOD all call this within the
        same bar and the result is identical. Without memoization the
        backtester does ~3× the work per day across 200+ symbols.
        """
        cache_key = self._clock()
        cached = getattr(self, "_obs_cache", None)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        snapshots = self._observe_universe_uncached()
        self._obs_cache = (cache_key, snapshots)
        return snapshots

    def _observe_universe_uncached(self) -> List[dict]:
        instruments = self._load_instruments()
        if not instruments:
            return []

        today = self._clock().date()
        snapshots: List[dict] = []

        for sym in self.universe:
            futures = self._symbol_futures_sorted(instruments, sym, today)
            if not futures:
                continue
            near = futures[0]
            nxt = futures[1] if len(futures) > 1 else None

            # Quote both sides in one call when possible.
            quote_keys = [f"NFO:{near['tradingsymbol']}"]
            if nxt:
                quote_keys.append(f"NFO:{nxt['tradingsymbol']}")
            quotes = self._safe_quote(quote_keys)
            if not quotes:
                continue

            near_q = quotes.get(f"NFO:{near['tradingsymbol']}")
            next_q = quotes.get(f"NFO:{nxt['tradingsymbol']}") if nxt else None

            # Spot is published on every futures quote as `last_price` for the
            # underlying via Kite's `ohlc` field — simplest is to read from a
            # parallel cash-segment quote, falling back to the near future's
            # implied spot if the cash quote isn't available.
            spot = self._safe_spot(sym)
            if spot is None and near_q is not None:
                # Fallback: discount near future back to spot at fair carry. Inexact
                # but better than skipping the symbol entirely.
                dte = (self._exp_date(near["expiry"]) - today).days
                if dte > 0:
                    T = dte / 365.0
                    spot = float(near_q["last_price"]) * math.exp(
                        -(self.risk_free_rate - self.dividend_yield) * T
                    )

            if near_q is None or spot is None:
                continue

            dte_near = (self._exp_date(near["expiry"]) - today).days
            near_px = float(near_q["last_price"])

            basis_ann = self._annualized_basis(spot, near_px, dte_near)

            next_px = None
            dte_next = None
            carry_implied = None
            carry_diff = None
            basis_ann_next = None
            if nxt and next_q is not None:
                dte_next = (self._exp_date(nxt["expiry"]) - today).days
                next_px = float(next_q["last_price"])
                carry_implied = self._implied_carry(near_px, next_px, dte_near, dte_next)
                if carry_implied is not None:
                    carry_diff = carry_implied - (self.risk_free_rate - self.dividend_yield)
                basis_ann_next = self._annualized_basis(spot, next_px, dte_next)

            snapshots.append({
                "symbol": sym,
                "spot": spot,
                "near": near,
                "near_price": near_px,
                "dte_near": dte_near,
                "next": nxt,
                "next_price": next_px,
                "dte_next": dte_next,
                "basis_annual": basis_ann,
                "basis_annual_next": basis_ann_next,
                "carry_implied": carry_implied,
                "carry_diff": carry_diff,
            })

        return snapshots

    def _load_instruments(self) -> List[dict]:
        if self._instrument_cache is not None:
            return self._instrument_cache
        try:
            self._instrument_cache = self.kite.instruments("NFO") or []
        except Exception as e:
            logger.warning("instruments('NFO') failed: %s", e)
            self._instrument_cache = []
        return self._instrument_cache

    @staticmethod
    def _exp_date(exp) -> date:
        if isinstance(exp, str):
            return datetime.strptime(exp[:10], "%Y-%m-%d").date()
        if hasattr(exp, "date"):
            return exp.date()
        return exp

    def _build_fut_index(self, instruments: List[dict], today: date) -> Dict[str, List[dict]]:
        """Group FUT contracts by underlying name once per `instruments` reload.

        Backtester replays ~200+ symbols per day, so re-scanning the full NFO
        list inside `_symbol_futures_sorted` is O(symbols × instruments) per
        bar. Indexing once collapses that to a dict lookup.
        """
        idx: Dict[str, List[dict]] = {}
        for r in instruments:
            if r.get("instrument_type") != "FUT":
                continue
            try:
                exp = self._exp_date(r.get("expiry"))
            except Exception:
                continue
            if exp < today:
                continue
            idx.setdefault(r.get("name"), []).append({
                "tradingsymbol": r["tradingsymbol"],
                "lot_size": int(r.get("lot_size", 0) or 0),
                "expiry": exp.isoformat(),
                "instrument_token": int(r.get("instrument_token", 0) or 0),
            })
        for rows in idx.values():
            rows.sort(key=lambda r: r["expiry"])
        return idx

    def _symbol_futures_sorted(
        self, instruments: List[dict], symbol: str, today: date,
    ) -> List[dict]:
        """All FUT contracts for `symbol` with expiry >= today, sorted ascending."""
        # Reuse a per-instruments-list index. The cache key is the id() of the
        # instruments list — when the underlying mock rotates the list (each
        # backtest day) the id changes and we rebuild.
        cache = getattr(self, "_fut_index_cache", None)
        if cache is None or cache[0] is not id(instruments) or cache[1] != today:
            self._fut_index_cache = (id(instruments), today, self._build_fut_index(instruments, today))
        return self._fut_index_cache[2].get(symbol, [])

    def _safe_quote(self, keys: List[str]) -> Dict[str, dict]:
        try:
            return self.kite.quote(keys) or {}
        except Exception as e:
            logger.warning("quote(%s) failed: %s", keys, e)
            return {}

    def _safe_spot(self, symbol: str) -> Optional[float]:
        try:
            q = self.kite.quote([f"NSE:{symbol}"]) or {}
            row = q.get(f"NSE:{symbol}")
            if row and row.get("last_price"):
                return float(row["last_price"])
        except Exception:
            pass
        return None

    # ══════════════════════════════════════════════════════════
    # PROPOSAL BUILDERS
    # ══════════════════════════════════════════════════════════

    def _build_basis_signal(self, snap: dict) -> List[TradeProposal]:
        """
        Cash-futures basis — emitted as a paired signal:
          * SELL near future + BUY spot   (when fut rich)
          * BUY near future  + SELL spot  (when fut cheap)
        Both legs are flagged option_type="CASH" / "FUT_BASIS" so the executor
        knows never to send the cash leg to a real order.
        """
        near = snap["near"]
        spot = snap["spot"]
        fut_px = snap["near_price"]
        basis = snap["basis_annual"]
        side_fut = "SELL" if basis > 0 else "BUY"
        side_cash = "BUY" if basis > 0 else "SELL"

        rationale = (
            f"BASIS {basis*100:.2f}% ann. on {snap['symbol']} "
            f"(F={fut_px:.2f}, S={spot:.2f}, dte={snap['dte_near']}d, "
            f"r-q={self.risk_free_rate - self.dividend_yield:.3f})"
        )
        lot_size = int(near["lot_size"])
        qty = self.lots_per_leg

        fut_prop = TradeProposal(
            tradingsymbol=near["tradingsymbol"],
            instrument_token=int(near["instrument_token"]),
            strike=0.0,
            expiry=near["expiry"],
            option_type="FUT_BASIS",
            lot_size=lot_size,
            quantity=qty,
            price=fut_px,
            transaction_type=side_fut,
            iv=0.0,
            bid_ask_spread_pct=0.0,
            margin_required=fut_px * lot_size * qty * 0.20,
            rationale=rationale,
        )
        cash_prop = TradeProposal(
            tradingsymbol=f"NSE:{snap['symbol']}",
            instrument_token=0,
            strike=0.0,
            expiry="",
            option_type="CASH",
            lot_size=lot_size,
            quantity=qty,
            price=spot,
            transaction_type=side_cash,
            iv=0.0,
            bid_ask_spread_pct=0.0,
            margin_required=spot * lot_size * qty,
            rationale=rationale + " (cash leg — signals-only)",
        )
        return [fut_prop, cash_prop]

    def _build_calendar_entry(self, snap: dict) -> List[TradeProposal]:
        """
        Calendar spread:
          carry_diff > 0 (next is rich vs near for the implied carry):
              SHORT_CALENDAR → SELL F2 + BUY F1
          carry_diff < 0 (next is cheap):
              LONG_CALENDAR  → BUY  F2 + SELL F1
        Both legs use the same `lots_per_leg` since F1 ≈ F2 in absolute price.
        """
        near = snap["near"]
        nxt = snap["next"]
        cd = snap["carry_diff"]
        symbol = snap["symbol"]

        # Refuse if even 1 lot busts the cap (we can't go fractional).
        if self.max_leg_notional:
            one_lot_near = snap["near_price"] * int(near["lot_size"])
            one_lot_next = snap["next_price"] * int(nxt["lot_size"])
            if max(one_lot_near, one_lot_next) > self.max_leg_notional:
                logger.warning(
                    "%s calendar: 1-lot leg ₹%.0f exceeds cap ₹%.0f — skipping",
                    symbol, max(one_lot_near, one_lot_next), self.max_leg_notional,
                )
                return []

        qty = self.lots_per_leg
        if cd > 0:
            side_near, side_next = "BUY", "SELL"
            position: CalendarPosition = "SHORT_CALENDAR"
        else:
            side_near, side_next = "SELL", "BUY"
            position = "LONG_CALENDAR"

        rationale = (
            f"{position} on {symbol}: implied carry={snap['carry_implied']:.3f} "
            f"vs fair={self.risk_free_rate - self.dividend_yield:.3f} "
            f"(diff={cd*100:.2f}% ann., near {snap['dte_near']}d / next {snap['dte_next']}d)"
        )
        return [
            self._make_fut_proposal(near, qty, snap["near_price"], side_near, rationale),
            self._make_fut_proposal(nxt, qty, snap["next_price"], side_next, rationale),
        ]

    def _build_calendar_exit(
        self, trade: CalendarTrade, snap: dict, reason: str,
    ) -> List[TradeProposal]:
        rationale = (
            f"EXIT_{reason} on {trade.symbol} (entry_diff={trade.entry_carry_diff:.3f}, "
            f"now_diff={(snap.get('carry_diff') or 0):.3f})"
        )
        proposals: List[TradeProposal] = []
        for leg in trade.legs:
            # Match the leg back to a future in the snapshot for current price.
            current_px = leg.current_price
            if snap.get("near") and snap["near"]["tradingsymbol"] == leg.tradingsymbol:
                current_px = snap["near_price"]
            elif snap.get("next") and snap["next"] and snap["next"]["tradingsymbol"] == leg.tradingsymbol:
                current_px = snap["next_price"]
            side = "SELL" if leg.quantity > 0 else "BUY"
            proposals.append(self._make_fut_proposal(
                {
                    "tradingsymbol": leg.tradingsymbol,
                    "lot_size": leg.lot_size,
                    "expiry": leg.expiry,
                    "instrument_token": 0,
                },
                abs(leg.quantity),
                current_px or leg.entry_price,
                side,
                rationale,
            ))
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
            margin_required=notional * 0.20,
            rationale=rationale,
        )

    # ══════════════════════════════════════════════════════════
    # FILL HANDLING / STATE UPDATES
    # ══════════════════════════════════════════════════════════

    def _apply_fill(self, prop: TradeProposal) -> None:
        from strategies.taleb_karpathy import estimate_transaction_cost
        cost = estimate_transaction_cost(
            prop.price, prop.quantity, prop.lot_size, prop.transaction_type,
            instrument_type="FUT",
        )
        self.state.total_transaction_costs += cost
        self.state.realized_pnl -= cost

        symbol = self._symbol_from_tradingsymbol(prop.tradingsymbol)
        signed_qty = prop.quantity if prop.transaction_type == "BUY" else -prop.quantity

        trade = self.state.open_calendars.get(symbol)
        if trade is None:
            # Opening leg — create the trade record on the first fill.
            trade = CalendarTrade(
                symbol=symbol,
                position="LONG_CALENDAR",   # finalized after both legs in
                entry_time=self._clock(),
                entry_carry_diff=self.state.pending_entry_diff.pop(symbol, 0.0),
                legs=[],
            )
            self.state.open_calendars[symbol] = trade

        existing = next((l for l in trade.legs if l.tradingsymbol == prop.tradingsymbol), None)
        if existing is None:
            trade.legs.append(CalendarLeg(
                symbol=symbol,
                tradingsymbol=prop.tradingsymbol,
                expiry=prop.expiry,
                lot_size=prop.lot_size,
                quantity=signed_qty,
                entry_price=prop.price,
                current_price=prop.price,
            ))
        else:
            old_qty = existing.quantity
            new_qty = old_qty + signed_qty
            if new_qty == 0:
                realized = (prop.price - existing.entry_price) * old_qty * existing.lot_size
                self.state.realized_pnl += realized
                trade.legs.remove(existing)
            elif old_qty * signed_qty < 0:
                closed_qty = min(abs(old_qty), abs(signed_qty)) * (1 if old_qty > 0 else -1)
                realized = (prop.price - existing.entry_price) * closed_qty * existing.lot_size
                self.state.realized_pnl += realized
                existing.quantity = new_qty
            else:
                existing.entry_price = (
                    existing.entry_price * old_qty + prop.price * signed_qty
                ) / new_qty
                existing.quantity = new_qty

        # If both legs are present and signed opposite, finalize position direction.
        # Standard convention: LONG_CALENDAR  = SELL near + BUY  far
        #                      SHORT_CALENDAR = BUY  near + SELL far
        if len(trade.legs) == 2 and trade.legs[0].quantity * trade.legs[1].quantity < 0:
            near_leg = min(trade.legs, key=lambda l: l.expiry)
            trade.position = "SHORT_CALENDAR" if near_leg.quantity > 0 else "LONG_CALENDAR"

        # If the trade is now empty, archive and remove.
        if not trade.legs:
            self.state.closed_trades.append({
                "symbol": symbol,
                "exit_time": self._clock(),
                "entry_time": trade.entry_time,
                "entry_carry_diff": trade.entry_carry_diff,
                "realized_pnl": self.state.realized_pnl,
                "transaction_costs": self.state.total_transaction_costs,
                "position": trade.position,
            })
            del self.state.open_calendars[symbol]

    def _update_unrealized(self, snapshots: Dict[str, dict]) -> None:
        unrealized = 0.0
        for symbol, trade in self.state.open_calendars.items():
            snap = snapshots.get(symbol)
            for leg in trade.legs:
                cur = leg.current_price
                if snap:
                    if snap.get("near") and snap["near"]["tradingsymbol"] == leg.tradingsymbol:
                        cur = snap["near_price"]
                    elif snap.get("next") and snap["next"] and snap["next"]["tradingsymbol"] == leg.tradingsymbol:
                        cur = snap["next_price"]
                leg.current_price = cur
                unrealized += (cur - leg.entry_price) * leg.quantity * leg.lot_size
        self.state.unrealized_pnl = unrealized

    def _symbol_from_tradingsymbol(self, tradingsymbol: str) -> str:
        for sym in self.universe:
            # NFO trading symbols start with the underlying name (RELIANCE26APRFUT etc.)
            if tradingsymbol.startswith(sym):
                return sym
        return tradingsymbol

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
