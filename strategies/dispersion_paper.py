"""
Hedged hold-to-expiry Nifty dispersion — PAPER/SIGNALS ONLY.

This is Book A from the Bloch replay (research/backtest_dispersion.py):
sell the Nifty ATM straddle, buy constituent ATM straddles of the same
monthly expiry, and hedge each straddle with the future at the close.
The book is held to the expiry settlement. The flatten-at-two-days path
and Book B's entry gate are not this strategy.

The daily sign check on equal weight, February–September 2026, showed a
positive sum on this one variant (seven expiries, futures hedge, hold to
expiry). That sample cannot clear the promotion bar of 36 expiries with
12 held out. ``mode="live"`` raises. This class is not in STRATEGIES, so
the dashboard cannot start it.

Weights are equal across the Nifty 50 list published 2026-10-01. There
is no free-float file. Dropped names stay in the weight denominator.
Index lots scale until covered weight reaches 30%, the same rule as the
replay, capped at ``max_index_lots``.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence

from core.trade_proposer import TradeProposal
from research.backtest_dispersion import (
    INDEX,
    MAX_INDEX_LOTS,
    NIFTY50_2026_10_01,
    choose_lots,
    equal_weights,
    exercise_stt,
    futures_hedge_lots,
    future_order_cost,
    option_order_cost,
    straddle_iv,
    atm_strike,
)
from strategies.base import BaseStrategy, validate_order

logger = logging.getLogger(__name__)


@dataclass
class NameSurface:
    """One underlying on the session the close is decided."""
    symbol: str
    spot: float
    quotes: Sequence[tuple] = ()
    future_price: Optional[float] = None
    future_lot: int = 0
    future_symbol: str = ""
    option_symbols: Mapping[float, Mapping[str, str]] = field(default_factory=dict)
    option_lot: int = 0


@dataclass
class SessionView:
    """What the runner knows at the close. The strategy does not fetch."""
    session: date
    previous_front: Optional[date]
    front_expiry: Optional[date]
    names: Dict[str, NameSurface]

    @property
    def is_roll(self) -> bool:
        return (
            self.previous_front is not None
            and self.front_expiry is not None
            and self.front_expiry != self.previous_front
        )


@dataclass
class OptionLeg:
    symbol: str
    strike: float
    lots: int
    lot_size: int
    side: int
    ce: float
    pe: float
    iv: float
    weight: float
    ce_symbol: str
    pe_symbol: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "strike": self.strike, "lots": self.lots,
            "lot_size": self.lot_size, "side": self.side, "ce": self.ce,
            "pe": self.pe, "iv": self.iv, "weight": self.weight,
            "ce_symbol": self.ce_symbol, "pe_symbol": self.pe_symbol,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "OptionLeg":
        return cls(
            symbol=raw["symbol"], strike=float(raw["strike"]), lots=int(raw["lots"]),
            lot_size=int(raw["lot_size"]), side=int(raw["side"]),
            ce=float(raw["ce"]), pe=float(raw["pe"]), iv=float(raw["iv"]),
            weight=float(raw["weight"]), ce_symbol=raw["ce_symbol"],
            pe_symbol=raw["pe_symbol"],
        )


@dataclass
class FuturePos:
    symbol: str
    lots: int
    lot_size: int
    last_px: float
    tradingsymbol: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "lots": self.lots, "lot_size": self.lot_size,
            "last_px": self.last_px, "tradingsymbol": self.tradingsymbol,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "FuturePos":
        return cls(
            symbol=raw["symbol"], lots=int(raw["lots"]), lot_size=int(raw["lot_size"]),
            last_px=float(raw["last_px"]), tradingsymbol=raw["tradingsymbol"],
        )


@dataclass
class OpenBook:
    expiry: date
    entry: date
    index_lots: int
    covered_weight: float
    weighting: str
    legs: List[OptionLeg]
    futures: Dict[str, FuturePos]
    costs: float
    futures_pnl: float
    last_hedge_session: Optional[date]

    def to_dict(self) -> dict:
        return {
            "expiry": self.expiry.isoformat(),
            "entry": self.entry.isoformat(),
            "index_lots": self.index_lots,
            "covered_weight": self.covered_weight,
            "weighting": self.weighting,
            "legs": [leg.to_dict() for leg in self.legs],
            "futures": {k: v.to_dict() for k, v in self.futures.items()},
            "costs": self.costs,
            "futures_pnl": self.futures_pnl,
            "last_hedge_session": (
                self.last_hedge_session.isoformat() if self.last_hedge_session else None
            ),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "OpenBook":
        last = raw.get("last_hedge_session")
        return cls(
            expiry=date.fromisoformat(raw["expiry"]),
            entry=date.fromisoformat(raw["entry"]),
            index_lots=int(raw["index_lots"]),
            covered_weight=float(raw["covered_weight"]),
            weighting=raw["weighting"],
            legs=[OptionLeg.from_dict(x) for x in raw["legs"]],
            futures={k: FuturePos.from_dict(v) for k, v in raw["futures"].items()},
            costs=float(raw["costs"]),
            futures_pnl=float(raw["futures_pnl"]),
            last_hedge_session=date.fromisoformat(last) if last else None,
        )


class DispersionPaperStrategy(BaseStrategy):
    """Paper book for the hedged hold-to-expiry dispersion."""

    name = "dispersion_paper"

    def __init__(self, client, config_path: str = "config.ini", mode: Optional[str] = None,
                 max_index_lots: int = MAX_INDEX_LOTS):
        super().__init__(client, config_path, mode)
        if self.mode == "live":
            raise NotImplementedError(
                "dispersion_paper is PAPER/SIGNALS ONLY. The hedged hold-to-expiry "
                "replay is a seven-expiry daily sign check on equal weight, not a "
                "promoted book. Live orders are refused (safety rule 3)."
            )
        if max_index_lots < 1:
            raise ValueError("max_index_lots must be >= 1")
        self.max_index_lots = int(max_index_lots)
        self.weights = equal_weights(NIFTY50_2026_10_01)
        self.view: Optional[SessionView] = None
        self.book: Optional[OpenBook] = None
        self.closed: List[dict] = []
        self.traded_expiries: set[str] = set()

    def set_view(self, view: SessionView) -> None:
        self.view = view

    def on_close(self, view: SessionView) -> List[dict]:
        """One close. A second call the same session does not trade again.

        A missed expiry is settled and labelled on this close. The next
        expiry is left for a later session: this close already spent its
        decision on the book that should have been flat yesterday.
        """
        self.set_view(view)
        results: List[dict] = []
        hedge = self.check_and_rehedge()
        if hedge:
            done = self.execute_proposals(hedge)
            results.extend(done)
            if any(row.get("cycle_status") == "missed_settlement" for row in done):
                logger.error(
                    "missed settlement on %s — leaving the next expiry unopened",
                    view.session,
                )
                return results
        entries = self.scan_and_propose()
        if entries:
            results.extend(self.execute_proposals(entries))
        return results

    def scan_and_propose(self) -> List[TradeProposal]:
        view = self.view
        if view is None or self.book is not None:
            return []
        if not view.is_roll or view.front_expiry is None:
            return []
        if view.front_expiry.isoformat() in self.traded_expiries:
            logger.info("expiry %s already traded — no second entry", view.front_expiry)
            return []
        if (view.front_expiry - view.session).days < 1:
            logger.info("front expiry %s is not ahead of %s — no entry",
                        view.front_expiry, view.session)
            return []
        built = self._entry_package(view)
        if built is None:
            return []
        legs, index_lots, covered = built
        proposals = []
        for leg in legs:
            for opt, px, sym in (("CE", leg.ce, leg.ce_symbol), ("PE", leg.pe, leg.pe_symbol)):
                proposals.append(TradeProposal(
                    tradingsymbol=sym, instrument_token=0, strike=leg.strike,
                    expiry=view.front_expiry.isoformat(), option_type=opt,
                    lot_size=leg.lot_size, quantity=leg.lots, price=px,
                    transaction_type="SELL" if leg.side < 0 else "BUY",
                    iv=leg.iv, bid_ask_spread_pct=0.0, margin_required=0.0,
                    rationale=(
                        f"dispersion open {leg.symbol} {opt} "
                        f"covered={covered:.2f} index_lots={index_lots}"
                    ),
                    greeks_snapshot={
                        "action": "open",
                        "symbol": leg.symbol,
                        "side": leg.side,
                        "ce": leg.ce,
                        "pe": leg.pe,
                        "iv": leg.iv,
                        "weight": leg.weight,
                        "ce_symbol": leg.ce_symbol,
                        "pe_symbol": leg.pe_symbol,
                        "index_lots": index_lots,
                        "covered_weight": covered,
                        "weighting": "equal",
                    },
                ))
        return proposals

    def check_and_rehedge(self) -> List[TradeProposal]:
        view = self.view
        book = self.book
        if view is None or book is None:
            return []
        dte = (book.expiry - view.session).days
        if dte < 0:
            logger.error(
                "dispersion book expiry %s is already past %s — settling on "
                "this close and labelling it missed_settlement",
                book.expiry, view.session,
            )
            return self._settlement_proposals(view, missed=True)
        if dte == 0:
            return self._settlement_proposals(view, missed=False)
        if book.last_hedge_session == view.session:
            return []
        return self._hedge_proposals(view, flatten=False) or []

    def execute_proposals(self, proposals: List[TradeProposal]) -> List[dict]:
        if not proposals:
            return []
        action = (proposals[0].greeks_snapshot or {}).get("action")
        if any((p.greeks_snapshot or {}).get("action") != action for p in proposals):
            logger.error("refusing a mixed dispersion batch")
            return [{"status": "REJECTED", "error": "mixed batch"}]
        for p in proposals:
            # A mark with no trade (the hedge is already at the target, including
            # a flat expiry) has quantity 0. validate_order rejects that, and
            # rejecting it would leave the option book open through settlement.
            snap = p.greeks_snapshot or {}
            mark_only = p.quantity <= 0 and (
                action == "hedge" or (action == "settle" and snap.get("kind") == "future")
            )
            if mark_only:
                continue
            try:
                validate_order(p)
            except Exception as e:                            # noqa: BLE001
                logger.warning("validate_order rejected %s: %s", p.tradingsymbol, e)
                return [{"status": "REJECTED", "error": str(e),
                         "tradingsymbol": p.tradingsymbol}]
        if self.is_signals_mode:
            return [self._emit_signal(p) for p in proposals]
        if action == "open":
            return self._paper_open(proposals)
        if action == "hedge":
            return self._paper_hedge(proposals, flatten=False)
        if action == "settle":
            return self._paper_settle(proposals)
        logger.error("unknown dispersion action %r", action)
        return [{"status": "REJECTED", "error": f"unknown action {action!r}"}]

    def generate_eod_report(self) -> dict:
        closed_net = sum(row["net"] for row in self.closed)
        book = self.book.to_dict() if self.book else None
        return {
            "strategy": self.name,
            "mode": self.mode,
            "open": book,
            "closed": len(self.closed),
            "cumulative_net": closed_net,
            "cumulative_costs": sum(row["costs"] for row in self.closed),
        }

    def to_dict(self) -> dict:
        return {
            "book": self.book.to_dict() if self.book else None,
            "closed": self.closed,
            "traded_expiries": sorted(self.traded_expiries),
            "max_index_lots": self.max_index_lots,
        }

    def load_dict(self, raw: dict) -> None:
        book = raw.get("book")
        self.book = OpenBook.from_dict(book) if book else None
        self.closed = list(raw.get("closed") or [])
        self.traded_expiries = set(raw.get("traded_expiries") or [])
        if raw.get("max_index_lots"):
            self.max_index_lots = int(raw["max_index_lots"])
        if self.book is not None:
            self.traded_expiries.add(self.book.expiry.isoformat())

    def _entry_package(self, view: SessionView):
        index = view.names.get(INDEX)
        if index is None or index.spot <= 0 or index.option_lot <= 0:
            logger.info("no Nifty surface — no entry")
            return None
        dte = (view.front_expiry - view.session).days
        spots: Dict[str, float] = {}
        lot_sizes: Dict[str, int] = {}
        picked: Dict[str, tuple] = {}
        strike = atm_strike(index.spot, index.quotes)
        iv = None
        if strike is not None:
            row = _quote_row(index.quotes, strike)
            if row is not None:
                iv = straddle_iv(index.spot, strike, row[1], row[2], dte)
        if strike is None or iv is None:
            logger.info("Nifty ATM straddle is not traded — no entry")
            return None
        symbols = _option_symbols(index, strike)
        if symbols is None:
            logger.info("Nifty ATM symbols missing — no entry")
            return None
        picked[INDEX] = (strike, row[1], row[2], iv, symbols, index.option_lot, -1, 0.0)
        for sym, weight in self.weights.items():
            surface = view.names.get(sym)
            if surface is None or surface.spot <= 0 or surface.option_lot <= 0:
                continue
            k = atm_strike(surface.spot, surface.quotes)
            if k is None:
                continue
            q = _quote_row(surface.quotes, k)
            if q is None:
                continue
            siv = straddle_iv(surface.spot, k, q[1], q[2], dte)
            if siv is None:
                continue
            syms = _option_symbols(surface, k)
            if syms is None:
                continue
            spots[sym] = surface.spot
            lot_sizes[sym] = surface.option_lot
            picked[sym] = (k, q[1], q[2], siv, syms, surface.option_lot, 1, weight)
        sized = choose_lots(
            index.spot, index.option_lot, self.weights, spots, lot_sizes,
            max_index_lots=self.max_index_lots,
        )
        if sized is None:
            logger.info(
                "covered weight stayed under 30%% at %d index lots — no entry",
                self.max_index_lots,
            )
            return None
        index_lots, stock_lots, covered = sized
        legs = [_leg_from_pick(INDEX, picked[INDEX], index_lots)]
        for sym, lots in stock_lots.items():
            legs.append(_leg_from_pick(sym, picked[sym], lots))
        logger.info(
            "dispersion package expiry %s index_lots=%d names=%d covered=%.2f",
            view.front_expiry, index_lots, len(stock_lots), covered,
        )
        return legs, index_lots, covered

    def _hedge_proposals(self, view: SessionView, flatten: bool) -> Optional[List[TradeProposal]]:
        book = self.book
        assert book is not None
        dte = max((book.expiry - view.session).days, 0)
        proposals = []
        for leg in book.legs:
            surface = view.names.get(leg.symbol)
            pos = book.futures.get(leg.symbol)
            held = pos.lots if pos else 0
            if surface is None or not surface.future_price or surface.future_lot <= 0:
                if held != 0 or flatten:
                    logger.error(
                        "no future for %s while the hedge is %s — not marking this close",
                        leg.symbol, held,
                    )
                    return None
                logger.info("no future for %s — that straddle stays unhedged this close", leg.symbol)
                continue
            target = 0 if flatten else futures_hedge_lots(
                surface.spot, leg.strike, dte, leg.iv, leg.lots, leg.lot_size,
                leg.side, surface.future_lot,
            )
            delta = target - held
            proposals.append(TradeProposal(
                tradingsymbol=surface.future_symbol or f"{leg.symbol}FUT",
                instrument_token=0, strike=0.0, expiry=book.expiry.isoformat(),
                option_type="FUT", lot_size=surface.future_lot,
                quantity=abs(delta), price=float(surface.future_price),
                transaction_type="BUY" if delta > 0 else "SELL",
                iv=leg.iv, bid_ask_spread_pct=0.0, margin_required=0.0,
                rationale=f"dispersion hedge {leg.symbol} {held} -> {target}",
                greeks_snapshot={
                    "action": "hedge",
                    "symbol": leg.symbol,
                    "target_lots": target,
                    "prev_lots": held,
                    "prev_px": None if pos is None else pos.last_px,
                    "price": float(surface.future_price),
                    "lot_size": surface.future_lot,
                    "tradingsymbol": surface.future_symbol,
                },
            ))
        return proposals

    def _settlement_proposals(self, view: SessionView, missed: bool) -> List[TradeProposal]:
        book = self.book
        assert book is not None
        proposals = []
        for leg in book.legs:
            surface = view.names.get(leg.symbol)
            if surface is None or surface.spot <= 0:
                logger.error("no settlement spot for %s — book stays open", leg.symbol)
                return []
            intrinsic = abs(surface.spot - leg.strike)
            proposals.append(TradeProposal(
                tradingsymbol=leg.ce_symbol, instrument_token=0, strike=leg.strike,
                expiry=book.expiry.isoformat(), option_type="CE",
                lot_size=leg.lot_size, quantity=leg.lots, price=max(intrinsic, 0.01),
                transaction_type="BUY" if leg.side < 0 else "SELL",
                iv=leg.iv, bid_ask_spread_pct=0.0, margin_required=0.0,
                rationale=f"dispersion settle {leg.symbol}",
                greeks_snapshot={
                    "action": "settle",
                    "kind": "option",
                    "symbol": leg.symbol,
                    "intrinsic": intrinsic,
                    "missed": missed,
                },
            ))
        hedges = self._hedge_proposals(view, flatten=True)
        if not hedges:
            logger.error("not settling — the futures hedge has no mark")
            return []
        for hedge in hedges:
            snap = dict(hedge.greeks_snapshot or {})
            snap["action"] = "settle"
            snap["kind"] = "future"
            snap["missed"] = missed
            hedge.greeks_snapshot = snap
        return proposals + hedges

    def _paper_open(self, proposals: List[TradeProposal]) -> List[dict]:
        view = self.view
        if view is None or view.front_expiry is None or self.book is not None:
            return [{"status": "REJECTED", "error": "open without a roll view"}]
        by_symbol: Dict[str, Dict[str, TradeProposal]] = {}
        for p in proposals:
            by_symbol.setdefault(p.greeks_snapshot["symbol"], {})[p.option_type] = p
        if any(set(legs) != {"CE", "PE"} for legs in by_symbol.values()):
            logger.error("open batch is missing a straddle leg — nothing filled")
            return [{"status": "REJECTED", "error": "incomplete straddle"}]
        first = proposals[0].greeks_snapshot
        legs = []
        costs = 0.0
        for sym, pair in by_symbol.items():
            ce, pe = pair["CE"], pair["PE"]
            side = int(ce.greeks_snapshot["side"])
            side_name = "SELL" if side < 0 else "BUY"
            costs += option_order_cost(ce.price, ce.quantity, ce.lot_size, side_name)
            costs += option_order_cost(pe.price, pe.quantity, pe.lot_size, side_name)
            legs.append(OptionLeg(
                symbol=sym, strike=ce.strike, lots=ce.quantity, lot_size=ce.lot_size,
                side=side, ce=float(ce.greeks_snapshot["ce"]),
                pe=float(ce.greeks_snapshot["pe"]),
                iv=float(ce.greeks_snapshot["iv"]),
                weight=float(ce.greeks_snapshot["weight"]),
                ce_symbol=ce.greeks_snapshot["ce_symbol"],
                pe_symbol=ce.greeks_snapshot["pe_symbol"],
            ))
        self.book = OpenBook(
            expiry=view.front_expiry, entry=view.session,
            index_lots=int(first["index_lots"]),
            covered_weight=float(first["covered_weight"]),
            weighting=str(first["weighting"]),
            legs=legs, futures={}, costs=costs, futures_pnl=0.0,
            last_hedge_session=None,
        )
        self.traded_expiries.add(view.front_expiry.isoformat())
        hedge = self._hedge_proposals(view, flatten=False) or []
        hedge_results = self._paper_hedge(hedge, flatten=False) if hedge else []
        notional = 0.0
        for leg in self.book.legs:
            surface = view.names.get(leg.symbol)
            if surface is not None and surface.spot > 0:
                notional += leg.lots * leg.lot_size * surface.spot
        logger.info(
            "[PAPER OPEN] dispersion expiry %s index_lots=%d names=%d covered=%.2f "
            "costs=₹%.0f gross_notional=₹%.0f",
            view.front_expiry, self.book.index_lots, len(legs) - 1,
            self.book.covered_weight, self.book.costs, notional,
        )
        return [{"status": "PAPER_OPEN", "expiry": view.front_expiry.isoformat(),
                 "index_lots": self.book.index_lots,
                 "covered_weight": self.book.covered_weight}] + hedge_results

    def _paper_hedge(self, proposals: List[TradeProposal], flatten: bool) -> List[dict]:
        book = self.book
        view = self.view
        if book is None or view is None:
            return [{"status": "REJECTED", "error": "hedge without a book"}]
        results = []
        for p in proposals:
            snap = p.greeks_snapshot or {}
            prev_lots = int(snap["prev_lots"])
            prev_px = snap["prev_px"]
            price = float(snap["price"])
            lot_size = int(snap["lot_size"])
            target = int(snap["target_lots"])
            if prev_px is not None and prev_lots != 0:
                book.futures_pnl += prev_lots * lot_size * (price - float(prev_px))
            delta = target - prev_lots
            if delta != 0:
                side = "BUY" if delta > 0 else "SELL"
                book.costs += future_order_cost(price, abs(delta), lot_size, side)
            book.futures[snap["symbol"]] = FuturePos(
                symbol=snap["symbol"], lots=target, lot_size=lot_size,
                last_px=price, tradingsymbol=snap.get("tradingsymbol") or p.tradingsymbol,
            )
            results.append({
                "status": "PAPER_HEDGE", "symbol": snap["symbol"],
                "lots": target, "price": price,
            })
        if not flatten:
            book.last_hedge_session = view.session
        return results

    def _paper_settle(self, proposals: List[TradeProposal]) -> List[dict]:
        book = self.book
        view = self.view
        if book is None or view is None:
            return [{"status": "REJECTED", "error": "settle without a book"}]
        options = [p for p in proposals if (p.greeks_snapshot or {}).get("kind") == "option"]
        hedges = [p for p in proposals if (p.greeks_snapshot or {}).get("kind") == "future"]
        if {p.greeks_snapshot["symbol"] for p in options} != {leg.symbol for leg in book.legs}:
            logger.error("settlement is missing a leg — book stays open")
            return [{"status": "REJECTED", "error": "incomplete settlement"}]
        self._paper_hedge(hedges, flatten=True)
        by_symbol = {p.greeks_snapshot["symbol"]: p for p in options}
        premium = 0.0
        stt = 0.0
        missed = any((p.greeks_snapshot or {}).get("missed") for p in proposals)
        for leg in book.legs:
            intrinsic = float(by_symbol[leg.symbol].greeks_snapshot["intrinsic"])
            entry_px = (leg.ce + leg.pe) * leg.lots * leg.lot_size
            exit_px = intrinsic * leg.lots * leg.lot_size
            premium += leg.side * (exit_px - entry_px)
            surface = view.names[leg.symbol]
            stt += exercise_stt(surface.spot, leg.strike, leg.lots, leg.lot_size, leg.side)
        costs = book.costs + stt
        net = premium + book.futures_pnl - costs
        row = {
            "expiry": book.expiry.isoformat(),
            "entry": book.entry.isoformat(),
            "exit": view.session.isoformat(),
            "status": "missed_settlement" if missed else "ok",
            "index_lots": book.index_lots,
            "covered_weight": book.covered_weight,
            "weighting": book.weighting,
            "premium_pnl": premium,
            "futures_pnl": book.futures_pnl,
            "costs": costs,
            "exercise_stt": stt,
            "net": net,
        }
        self.closed.append(row)
        self.book = None
        logger.info(
            "[PAPER SETTLE] dispersion expiry %s status=%s premium=₹%.0f "
            "futures=₹%.0f costs=₹%.0f net=₹%.0f",
            row["expiry"], row["status"], premium, row["futures_pnl"], costs, net,
        )
        # `status` on the closed row is the cycle outcome. The fill status
        # stays PAPER_SETTLE so a caller can tell a fill from a rejection.
        payload = {k: v for k, v in row.items() if k != "status"}
        payload["status"] = "PAPER_SETTLE"
        payload["cycle_status"] = row["status"]
        return [payload]


def _quote_row(quotes, strike: float):
    for row in quotes:
        if math.isclose(float(row[0]), float(strike), abs_tol=0.01):
            return row
    return None


def _option_symbols(surface: NameSurface, strike: float):
    for listed, pair in surface.option_symbols.items():
        if math.isclose(float(listed), float(strike), abs_tol=0.01):
            if "CE" in pair and "PE" in pair:
                return pair["CE"], pair["PE"]
    return None


def _leg_from_pick(symbol: str, pick: tuple, lots: int) -> OptionLeg:
    strike, ce, pe, iv, symbols, lot_size, side, weight = pick
    return OptionLeg(
        symbol=symbol, strike=float(strike), lots=int(lots), lot_size=int(lot_size),
        side=int(side), ce=float(ce), pe=float(pe), iv=float(iv), weight=float(weight),
        ce_symbol=symbols[0], pe_symbol=symbols[1],
    )
