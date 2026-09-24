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

    # Phase 3.2: structure builders the proposer can emit. Names match
    # regime_classifier.Structure values one-to-one so dispatch is a
    # simple lookup.
    STRATEGY_TYPES = [
        "long_straddle",
        "calendar_short_front",
        "risk_reversal_long_put",
        "backspread",
        "asymmetric_strangle",
    ]

    # Structures whose legs MUST share one expiry. Everything except the
    # calendar, whose entire thesis is the term-structure spread between two.
    # Phase 3.2 widened the chain handed to `propose_for_structure` to span
    # two expiries so the calendar builder could work — but the delta-based
    # builders below pick each leg independently off that chain via
    # `_pick_strike_by_delta`, with nothing pinning them to the same expiry.
    # They routinely straddled two (2026-08-09 review: a "backspread" of
    # short NIFTY2680424300CE against long NIFTY2681124850CE is a diagonal
    # ratio spread, not a backspread). Two consequences, both bad:
    #   * risk — the short leg carries near-expiry gamma/theta the long legs
    #     don't offset, so the structure the classifier picked is not the
    #     structure the book holds;
    #   * margin — `_structure_margin` can only expiry-scan a single-expiry
    #     book. A mixed-expiry, net-CREDIT structure (which a properly built
    #     backspread always is) misses both the scan and the net-debit
    #     branch, falls through to `return gross` = the naked per-leg sum,
    #     and gets rejected by the 30%-of-capital cap. On the 15-session
    #     replay window that killed EVERY backspread entry: same-expiry
    #     structures margin Rs 107k-123k, the mixed-expiry ones Rs 717k-5.0M
    #     against a Rs 300k cap.
    _MULTI_EXPIRY_STRUCTURES = frozenset({"calendar_short_front"})

    def __init__(self, client, config_path: str = "config.ini"):
        self.client = client
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
        from core.greeks_engine import implied_volatility_bisect, time_to_expiry
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

    # ══════════════════════════════════════════════════════════════
    # Phase 3.2 / 3.4: structure builders + off-ATM strike picking
    # ══════════════════════════════════════════════════════════════

    def _pick_strike_by_delta(
        self, chain: pd.DataFrame, spot: float, target_delta: float,
        option_type: str, greeks_engine,
    ) -> Optional[dict]:
        """Find the chain row whose computed delta is closest to
        `target_delta`. Returns a dict {row, iv, delta, T} for the
        builders to consume, or None if no strike fits.

        Iterates the chain, prices each strike's option, backs out IV,
        computes delta. O(n_strikes) per call — typically ~20-40 strikes
        for a weekly expiry. The strike pick is what differentiates
        risk reversal (target ±25Δ), strangle (target ±20-30Δ wings),
        and backspread (one ATM + two ~10Δ OTM) from the ATM straddle.
        """
        from core.greeks_engine import implied_volatility_bisect, time_to_expiry

        sub = chain[chain["instrument_type"] == option_type]
        if sub.empty:
            return None
        clock = getattr(self, "_clock", None)
        ref = clock() if clock is not None else None
        # T is per-EXPIRY, not one value for the whole chain. The chain can span
        # two expiries (primary + back month), and on an expiry day the primary's
        # T is 0 (day-resolution time_to_expiry). The old code read T from the
        # first row and returned None when T<=0, so every delta-based builder
        # (backspread, risk reversal, asymmetric strangle) failed on expiry day.
        # Now each row uses its own expiry's T and rows with T<=0 are skipped, so
        # the picker falls through to the next live expiry instead of nulling the
        # whole structure. (Also fixes back-month rows being priced with the
        # front-month T.)
        T_by_expiry = {}

        best = None
        best_dist = float("inf")
        for _, row in sub.iterrows():
            expiry_str = str(row["expiry"])
            if expiry_str not in T_by_expiry:
                T_by_expiry[expiry_str] = time_to_expiry(expiry_str, ref)
            T = T_by_expiry[expiry_str]
            if T <= 0:
                continue
            strike = float(row["strike"])
            symbol = row["tradingsymbol"]
            q = self._get_quote(symbol)
            if not q:
                continue
            price = q.get("last_price", 0)
            if price <= 0:
                continue
            try:
                iv = implied_volatility_bisect(
                    price, spot, strike, T, 0.065, option_type,
                )
                if not (0.03 < iv < 3.0):
                    continue
                d = greeks_engine.delta(spot, strike, T, iv, option_type)
            except Exception:
                continue
            dist = abs(d - target_delta)
            if dist < best_dist:
                best_dist = dist
                best = {
                    "row": row, "iv": iv, "delta": d, "T": T,
                    "price": price, "quote": q,
                }
        return best

    # NSE SPAN+ELM for a naked short NIFTY option is typically 12-15% of
    # underlying notional (= strike × lot_size × qty). The old
    # `margin_required = price × lot × qty` formula was right for longs
    # (cash outlay = premium) but wrong for shorts: it treated premium
    # as posted margin when in reality SPAN is far larger than premium
    # for OTM shorts. 0.15 is the conservative end of the SPAN+ELM band,
    # erring toward the 30% portfolio cap rejecting rather than admitting
    # an over-leveraged book. Compare strategies/pair_trading.py and
    # strategies/arbitrage.py which use 0.20 for FUT notional.
    _SHORT_OPTION_MARGIN_PCT = 0.15

    def _build_proposal(
        self, row, price, iv, quantity, transaction_type, rationale,
        quote=None,
    ) -> TradeProposal:
        """Helper: pack one chain row + price + IV into a TradeProposal.
        Shared by every builder so the boilerplate doesn't repeat."""
        lot_size = int(row["lot_size"])
        spread = self._compute_spread(quote) if quote else 0.0
        strike = float(row["strike"])
        if transaction_type == "BUY":
            margin_required = price * lot_size * quantity
        else:
            # Short option: SPAN+ELM ≈ 15% of strike-notional. Use max with
            # 2× premium as a floor for deep-ITM shorts where premium can
            # exceed the notional-percentage estimate.
            notional_margin = strike * lot_size * quantity * self._SHORT_OPTION_MARGIN_PCT
            premium_floor = 2.0 * price * lot_size * quantity
            margin_required = max(notional_margin, premium_floor)
        return TradeProposal(
            tradingsymbol=row["tradingsymbol"],
            instrument_token=int(row["instrument_token"]),
            strike=strike,
            expiry=str(row["expiry"]),
            option_type=row["instrument_type"],
            lot_size=lot_size,
            quantity=quantity,
            price=price,
            transaction_type=transaction_type,
            iv=iv,
            bid_ask_spread_pct=spread,
            margin_required=margin_required,
            rationale=rationale,
        )

    def propose_risk_reversal_long_put(
        self, chain: pd.DataFrame, spot: float, capital: float,
        position_size_pct: float, greeks_engine,
    ) -> List[TradeProposal]:
        """Long 25Δ put + short 25Δ call. Pays no net premium (the call
        finances the put), benefits from rich put-skew, has bullish-side
        capped P&L but downside-friendly convexity. Use when skew
        percentile is rich.
        """
        if chain.empty:
            return []
        put = self._pick_strike_by_delta(chain, spot, -0.25, "PE", greeks_engine)
        call = self._pick_strike_by_delta(chain, spot, +0.25, "CE", greeks_engine)
        if put is None or call is None:
            logger.warning("Risk reversal: could not find both 25Δ strikes")
            return []

        # Size against the structure's MARGIN, which the short call dominates
        # (SPAN ≈ 15% of its strike-notional). The long put is a cash premium
        # that does NOT offset the call's upside risk, so sizing off the put
        # price alone (the old behaviour) requested lots whose short-call margin
        # dwarfed the capital allocation. Mirror both legs at the same lots.
        lot_size = int(put["row"]["lot_size"])
        risk_capital = capital * position_size_pct / 100.0
        short_call_margin_per_lot = max(
            float(call["row"]["strike"]) * lot_size * self._SHORT_OPTION_MARGIN_PCT,
            2.0 * call["price"] * lot_size,
        )
        margin_per_lot = short_call_margin_per_lot + put["price"] * lot_size
        max_lots = max(int(risk_capital / margin_per_lot), 1)

        proposals = [
            self._build_proposal(
                put["row"], put["price"], put["iv"], max_lots, "BUY",
                f"Risk reversal: long {max_lots} × 25Δ PE @ "
                f"{put['row']['strike']:.0f} | IV: {put['iv']*100:.1f}% | "
                f"financed by short call @ {call['row']['strike']:.0f}",
                put["quote"],
            ),
            self._build_proposal(
                call["row"], call["price"], call["iv"], max_lots, "SELL",
                f"Risk reversal: short {max_lots} × 25Δ CE @ "
                f"{call['row']['strike']:.0f} | IV: {call['iv']*100:.1f}% | "
                f"finances the put",
                call["quote"],
            ),
        ]
        return proposals

    def propose_calendar_short_front(
        self, chain: pd.DataFrame, spot: float, capital: float,
        position_size_pct: float, greeks_engine,
    ) -> List[TradeProposal]:
        """Long back-month ATM, short front-month ATM. Net long gamma at
        spot, net short vega. Use when front IV is rich and back is
        cheaper — captures the term-structure premium.

        Implementation note: requires the chain to span TWO expiries.
        The strategy's _get_options_chain selects one expiry today; for
        this builder to fire we'd need a two-expiry chain. For now we
        return [] when only one expiry is present, with a log
        explaining the gap. Phase 3.3 (per-leg T) makes the multi-expiry
        portfolio greeks workable; the chain-fetcher upgrade is a
        follow-up.
        """
        if chain.empty:
            return []
        expiries = sorted(chain["expiry"].unique())
        if len(expiries) < 2:
            logger.info(
                "Calendar builder needs 2 expiries; chain has %d. "
                "Chain fetcher to be extended in a follow-up — for "
                "now this regime falls back to NO_TRADE upstream.",
                len(expiries),
            )
            return []
        # ATM strike
        strikes = chain["strike"].unique()
        atm = strikes[np.argmin(np.abs(strikes - spot))]
        front, back = expiries[0], expiries[1]
        front_ce = chain[(chain["expiry"] == front) & (chain["strike"] == atm)
                         & (chain["instrument_type"] == "CE")]
        back_ce = chain[(chain["expiry"] == back) & (chain["strike"] == atm)
                        & (chain["instrument_type"] == "CE")]
        if front_ce.empty or back_ce.empty:
            return []

        fq = self._get_quote(front_ce.iloc[0]["tradingsymbol"])
        bq = self._get_quote(back_ce.iloc[0]["tradingsymbol"])
        if not fq or not bq:
            return []

        lot_size = int(back_ce.iloc[0]["lot_size"])
        # Net debit = back - front (back is more expensive than front in
        # normal contango). Size so net debit respects capital budget.
        net_debit = (bq["last_price"] - fq["last_price"]) * lot_size
        if net_debit <= 0:
            logger.info("Calendar: front IV not rich enough; net credit would imply normal regime not calendar")
            return []
        risk_capital = capital * position_size_pct / 100.0
        max_lots = max(int(risk_capital / net_debit), 1)

        # Back out per-leg IV from market prices so downstream
        # compute_option_greeks does not silently substitute the 0.20
        # fallback — the front-vs-back IV gap is the entire premise of
        # the calendar; a flat-0.20 view would defeat term-structure
        # alpha gates and vega caps.
        from core.greeks_engine import implied_volatility_bisect, time_to_expiry
        clock = getattr(self, "_clock", None)
        ref = clock() if clock is not None else None
        front_T = time_to_expiry(str(front), ref)
        back_T = time_to_expiry(str(back), ref)
        try:
            front_iv = (
                implied_volatility_bisect(fq["last_price"], spot, atm, front_T, 0.065, "CE")
                if front_T > 0 else 0.20
            )
            back_iv = (
                implied_volatility_bisect(bq["last_price"], spot, atm, back_T, 0.065, "CE")
                if back_T > 0 else 0.20
            )
        except Exception as e:
            logger.warning(
                "Calendar IV bisection failed (%s) — falling back to "
                "0.20 across legs; greeks will be approximate.", e,
            )
            front_iv = back_iv = 0.20
        if not (0.03 < front_iv < 3.0):
            front_iv = 0.20
        if not (0.03 < back_iv < 3.0):
            back_iv = 0.20

        proposals = [
            self._build_proposal(
                back_ce.iloc[0], bq["last_price"], back_iv, max_lots, "BUY",
                f"Calendar: long {max_lots} × back-month ATM CE "
                f"@ {atm:.0f} ({back}) IV={back_iv*100:.1f}%",
                bq,
            ),
            self._build_proposal(
                front_ce.iloc[0], fq["last_price"], front_iv, max_lots, "SELL",
                f"Calendar: short {max_lots} × front-month ATM CE "
                f"@ {atm:.0f} ({front}) IV={front_iv*100:.1f}%",
                fq,
            ),
        ]
        return proposals

    def propose_backspread(
        self, chain: pd.DataFrame, spot: float, capital: float,
        position_size_pct: float, greeks_engine,
    ) -> List[TradeProposal]:
        """1×ATM short + 2×OTM long (call backspread or put backspread).
        Long fourth moment (long vol-of-vol). Net credit at entry,
        unlimited upside in the OTM direction. Use when vol-of-vol is
        elevated.

        We default to call-side backspread; the directional choice
        could be a refinement (long-put backspread for biased-asset
        downside-vvol). For this Phase 3 cut we pick whichever side
        has more skew benefit: if put skew is rich, build a put
        backspread; else call.
        """
        if chain.empty:
            return []
        atm_short = self._pick_strike_by_delta(chain, spot, 0.5, "CE", greeks_engine)
        otm_long = self._pick_strike_by_delta(chain, spot, 0.10, "CE", greeks_engine)
        if atm_short is None or otm_long is None:
            logger.warning("Backspread: could not find both ATM and 10Δ OTM call strikes")
            return []
        if atm_short["row"]["tradingsymbol"] == otm_long["row"]["tradingsymbol"]:
            logger.warning("Backspread: ATM and OTM resolved to same strike")
            return []

        lot_size = int(atm_short["row"]["lot_size"])
        # Size against max-loss-at-expiry, which occurs when the underlying
        # pins at the OTM strike: the ATM short is ITM by (OTM - ATM) and
        # the OTM longs expire worthless. Entry net credit (+) or debit (−)
        # offsets that. Max loss per (1×ATM short, 2×OTM long) unit:
        #     max_loss = (OTM_strike − ATM_strike) − (ATM_price − 2·OTM_price)
        # The old "per_unit_cost = max(2·OTM − ATM, 1.0)" sizing was
        # meaningless for a properly-built (net-credit) backspread —
        # it clamped to 1.0 and blew max_lots to hundreds of lots.
        strike_width = float(otm_long["row"]["strike"]) - float(atm_short["row"]["strike"])
        net_credit = atm_short["price"] - 2 * otm_long["price"]
        max_loss_per_unit = (strike_width - net_credit) * lot_size
        if max_loss_per_unit <= 0:
            logger.warning(
                "Backspread: non-positive max loss (width=%.0f credit=%.2f) — "
                "structure is free money or pricing is off; skipping",
                strike_width, net_credit,
            )
            return []
        risk_capital = capital * position_size_pct / 100.0
        max_lots = max(int(risk_capital / max_loss_per_unit), 1)

        return [
            self._build_proposal(
                atm_short["row"], atm_short["price"], atm_short["iv"],
                max_lots, "SELL",
                f"Backspread: short {max_lots} × ATM CE @ "
                f"{atm_short['row']['strike']:.0f}",
                atm_short["quote"],
            ),
            self._build_proposal(
                otm_long["row"], otm_long["price"], otm_long["iv"],
                2 * max_lots, "BUY",
                f"Backspread: long {2*max_lots} × 10Δ OTM CE @ "
                f"{otm_long['row']['strike']:.0f} (vvol bet)",
                otm_long["quote"],
            ),
        ]

    def propose_asymmetric_strangle(
        self, chain: pd.DataFrame, spot: float, capital: float,
        position_size_pct: float, greeks_engine,
    ) -> List[TradeProposal]:
        """Long ~30Δ put + long ~15Δ call. Tilted to capture downside
        moves; the call is for cheap upside tail protection. Use in
        biased-asset post-event regimes where RV ≫ IV and skew is
        rich.
        """
        if chain.empty:
            return []
        put = self._pick_strike_by_delta(chain, spot, -0.30, "PE", greeks_engine)
        call = self._pick_strike_by_delta(chain, spot, +0.15, "CE", greeks_engine)
        if put is None or call is None:
            logger.warning("Asymmetric strangle: missing put or call leg")
            return []

        lot_size = int(put["row"]["lot_size"])
        cost_per_lot = (put["price"] + call["price"]) * lot_size
        risk_capital = capital * position_size_pct / 100.0
        max_lots = max(int(risk_capital / cost_per_lot), 1)

        return [
            self._build_proposal(
                put["row"], put["price"], put["iv"], max_lots, "BUY",
                f"Asymmetric strangle: long {max_lots} × 30Δ PE @ "
                f"{put['row']['strike']:.0f} (downside emphasis)",
                put["quote"],
            ),
            self._build_proposal(
                call["row"], call["price"], call["iv"], max_lots, "BUY",
                f"Asymmetric strangle: long {max_lots} × 15Δ CE @ "
                f"{call['row']['strike']:.0f} (cheap upside tail)",
                call["quote"],
            ),
        ]

    def propose_for_structure(
        self, structure: str, chain: pd.DataFrame, spot: float,
        capital: float, position_size_pct: float, greeks_engine,
    ) -> List[TradeProposal]:
        """Dispatch: convert a regime_classifier.Structure label to the
        appropriate builder call. Unknown / NO_TRADE labels return [].

        Keeping dispatch in the proposer keeps `scan_and_propose` free
        of regime-knowledge — the strategy passes a label and gets back
        proposals.
        """
        builders = {
            "straddle": self.propose_delta_neutral,
            "risk_reversal_long_put": self.propose_risk_reversal_long_put,
            "calendar_short_front": self.propose_calendar_short_front,
            "backspread": self.propose_backspread,
            "asymmetric_strangle": self.propose_asymmetric_strangle,
        }
        builder = builders.get(structure)
        if builder is None:
            return []
        # Pin every single-expiry structure to ONE expiry before the builder
        # sees the chain (see _MULTI_EXPIRY_STRUCTURES). Done here rather than
        # in each builder so a future builder cannot forget it.
        if structure not in self._MULTI_EXPIRY_STRUCTURES:
            chain = self._single_expiry_slice(chain)
            if chain.empty:
                logger.warning(
                    "%s: no expiry with time remaining in the chain — "
                    "skipping (structure needs one live expiry)", structure,
                )
                return []
        # propose_delta_neutral (the straddle builder) doesn't take
        # greeks_engine positionally — it has its own signature. Branch.
        if structure == "straddle":
            return builder(
                chain=chain, spot=spot, capital=capital,
                position_size_pct=position_size_pct,
                greeks_engine=greeks_engine,
            )
        return builder(
            chain=chain, spot=spot, capital=capital,
            position_size_pct=position_size_pct,
            greeks_engine=greeks_engine,
        )

    def _single_expiry_slice(self, chain: pd.DataFrame) -> pd.DataFrame:
        """Restrict `chain` to the NEAREST expiry that still has time on it.

        Deliberately "nearest LIVE", not "chain.attrs['primary_expiry']":
        on expiry day the primary's `time_to_expiry` is 0 (day-resolution),
        and `_pick_strike_by_delta` already skips those rows so the picker
        falls through to the next live expiry rather than nulling the whole
        structure. Selecting the primary blindly would re-break exactly that
        case; selecting the nearest live expiry keeps the fallthrough AND
        guarantees both legs land in the same one.

        Returns an empty frame when no expiry has time remaining — the
        caller skips, it must not silently trade the expiring series.
        """
        from core.greeks_engine import time_to_expiry

        if chain.empty or "expiry" not in chain:
            return chain
        clock = getattr(self, "_clock", None)
        ref = clock() if clock is not None else None
        # Rank by remaining time, not by string sort: expiry formatting has
        # varied (date objects vs 'YYYY-MM-DD') and 26JUL-style trading
        # symbols do not sort chronologically either.
        live = sorted(
            ((time_to_expiry(str(e), ref), str(e))
             for e in chain["expiry"].unique()),
            key=lambda t: t[0],
        )
        live = [e for T, e in live if T > 0]
        if not live:
            return chain.iloc[0:0]
        return chain[chain["expiry"].astype(str) == live[0]]

    # ══════════════════════════════════════════════════════════════

    def _get_quote(self, tradingsymbol: str) -> Optional[dict]:
        """Fetch live quote for a symbol."""
        try:
            quote = self.client.quote([f"NFO:{tradingsymbol}"])
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
