"""
Trade Proposer — Risk-Managed Trade Signal Generation
=====================================================
Generates delta-neutral position proposals based on:
  - Options chain analysis
  - IV filtering and strike selection
  - Risk budget allocation
  - Taleb's preference for long gamma / short theta positions

This module proposes; the hedger decides; the user confirms.
"""

import logging
import configparser
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class TradeProposal:
    """A single proposed trade with full context for decision-making."""
    tradingsymbol: str
    instrument_token: int
    strike: float
    expiry: str
    option_type: str  # "CE", "PE", or "FUT"
    lot_size: int
    quantity: int  # In lots, always positive. Direction is in transaction_type.
    price: float
    transaction_type: str  # "BUY" or "SELL"
    iv: float
    bid_ask_spread_pct: float
    margin_required: float
    rationale: str = ""
    greeks_snapshot: Optional[dict] = None


class TradeProposer:
    """
    Generates long ATM straddle proposals.

    Structure: Buy ATM CE + ATM PE → long gamma, pay theta, unlimited convexity.
    This is Taleb's preferred base structure for gamma scalping.
    """

    STRATEGY_TYPES = ["long_straddle"]

    def __init__(self, kite, config_path: str = "config.ini"):
        self.kite = kite
        self.config = configparser.ConfigParser()
        self.config.read(config_path)
        self.underlying = self.config["strategy"]["underlying"]
        self._clock = None  # Set by hedger for replay mode

    def propose_delta_neutral(
        self, chain: pd.DataFrame, spot: float, capital: float,
        position_size_pct: float, greeks_engine=None,
    ) -> List[TradeProposal]:
        """
        Main entry point: propose a delta-neutral position.

        Default strategy: Long ATM Straddle
        - Buy 1 ATM CE + 1 ATM PE
        - Net delta ≈ 0 (CE delta ~0.5, PE delta ~-0.5)
        - Long gamma: profit from realized vol > implied vol
        - Short theta: pay time decay as cost of carrying gamma

        This is Taleb's preferred base structure for gamma scalping.
        """
        if chain.empty:
            return []

        # Find ATM strike (nearest to spot, rounded to strike interval)
        strikes = chain["strike"].unique()
        atm_strike = strikes[np.argmin(np.abs(strikes - spot))]

        # Get ATM CE and PE
        atm_ce = chain[(chain["strike"] == atm_strike) & (chain["instrument_type"] == "CE")]
        atm_pe = chain[(chain["strike"] == atm_strike) & (chain["instrument_type"] == "PE")]

        if atm_ce.empty or atm_pe.empty:
            logger.warning("Could not find ATM CE/PE at strike %.0f", atm_strike)
            return []

        ce_row = atm_ce.iloc[0]
        pe_row = atm_pe.iloc[0]

        # Fetch live quotes for pricing
        ce_quote = self._get_quote(ce_row["tradingsymbol"])
        pe_quote = self._get_quote(pe_row["tradingsymbol"])

        if not ce_quote or not pe_quote:
            return []

        lot_size = int(ce_row["lot_size"])
        ce_price = ce_quote["last_price"]
        pe_price = pe_quote["last_price"]

        # Position sizing: how many lots can we afford?
        cost_per_lot = (ce_price + pe_price) * lot_size
        risk_capital = capital * position_size_pct / 100.0
        max_lots = max(int(risk_capital / cost_per_lot), 1)

        # Compute bid-ask spreads
        ce_spread = self._compute_spread(ce_quote)
        pe_spread = self._compute_spread(pe_quote)

        # Compute IVs
        from greeks_engine import implied_volatility_bisect, time_to_expiry
        expiry_str = str(ce_row["expiry"])
        clock = getattr(self, "_clock", None)
        ref_time = clock() if clock is not None else None
        T = time_to_expiry(expiry_str, ref_time)
        ce_iv = implied_volatility_bisect(ce_price, spot, atm_strike, T, 0.065, "CE") if T > 0 else 0.2
        pe_iv = implied_volatility_bisect(pe_price, spot, atm_strike, T, 0.065, "PE") if T > 0 else 0.2

        # Build proposals
        proposals = [
            TradeProposal(
                tradingsymbol=ce_row["tradingsymbol"],
                instrument_token=int(ce_row["instrument_token"]),
                strike=atm_strike,
                expiry=expiry_str,
                option_type="CE",
                lot_size=lot_size,
                quantity=max_lots,
                price=ce_price,
                transaction_type="BUY",
                iv=ce_iv,
                bid_ask_spread_pct=ce_spread,
                margin_required=ce_price * lot_size * max_lots,
                rationale=f"Long ATM CE @ {atm_strike} | IV: {ce_iv*100:.1f}% | "
                          f"Cost: ₹{ce_price * lot_size * max_lots:,.0f} | "
                          f"Part of delta-neutral straddle for gamma scalping",
            ),
            TradeProposal(
                tradingsymbol=pe_row["tradingsymbol"],
                instrument_token=int(pe_row["instrument_token"]),
                strike=atm_strike,
                expiry=expiry_str,
                option_type="PE",
                lot_size=lot_size,
                quantity=max_lots,
                price=pe_price,
                transaction_type="BUY",
                iv=pe_iv,
                bid_ask_spread_pct=pe_spread,
                margin_required=pe_price * lot_size * max_lots,
                rationale=f"Long ATM PE @ {atm_strike} | IV: {pe_iv*100:.1f}% | "
                          f"Cost: ₹{pe_price * lot_size * max_lots:,.0f} | "
                          f"Part of delta-neutral straddle for gamma scalping",
            ),
        ]

        total_cost = sum(p.price * p.lot_size * abs(p.quantity) for p in proposals)
        logger.info(
            "Proposed long ATM straddle @ %d | Cost: ₹%.0f | Capital used: %.1f%%",
            atm_strike, total_cost, (total_cost / capital) * 100
        )

        return proposals

    def _get_quote(self, tradingsymbol: str) -> Optional[dict]:
        """Fetch live quote for a symbol."""
        try:
            quote = self.kite.quote([f"NFO:{tradingsymbol}"])
            return quote[f"NFO:{tradingsymbol}"]
        except Exception as e:
            logger.warning("Quote fetch failed for %s: %s", tradingsymbol, e)
            return None

    @staticmethod
    def _compute_spread(quote: dict) -> float:
        """Compute bid-ask spread as percentage of mid price."""
        bid = quote.get("depth", {}).get("buy", [{}])[0].get("price", 0)
        ask = quote.get("depth", {}).get("sell", [{}])[0].get("price", 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            return ((ask - bid) / mid) * 100
        return 99.0  # No liquidity

    def format_proposals_table(self, proposals: List[TradeProposal]) -> str:
        """Format proposals as a readable table for user review."""
        if not proposals:
            return "No trades proposed."

        lines = [
            f"{'Action':<6} {'Symbol':<25} {'Strike':>8} {'Type':>4} "
            f"{'Lots':>5} {'Price':>10} {'IV':>8} {'Spread':>8} {'Rationale'}",
            "─" * 120,
        ]

        total_buy = 0
        total_sell = 0

        for p in proposals:
            action = p.transaction_type
            cost = p.price * p.lot_size * abs(p.quantity)
            if action == "BUY":
                total_buy += cost
            else:
                total_sell += cost

            lines.append(
                f"{action:<6} {p.tradingsymbol:<25} {p.strike:>8.0f} {p.option_type:>4} "
                f"{p.quantity:>5} {p.price:>10.2f} {p.iv*100:>7.1f}% {p.bid_ask_spread_pct:>7.2f}% "
                f"{p.rationale[:50]}"
            )

        lines.append("─" * 120)
        lines.append(f"Net cost: ₹{total_buy - total_sell:,.0f}  "
                      f"(Buy: ₹{total_buy:,.0f}  Sell credit: ₹{total_sell:,.0f})")

        return "\n".join(lines)
