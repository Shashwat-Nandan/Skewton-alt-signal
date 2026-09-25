"""Tests for core/trade_proposer.py — sign convention and proposal generation."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import pandas as pd
from unittest.mock import MagicMock
from core.trade_proposer import TradeProposer


@pytest.fixture
def mock_kite():
    kite = MagicMock()
    kite.quote.return_value = {
        "NFO:NIFTY2640322000CE": {"last_price": 300, "depth": {"buy": [{"price": 299}], "sell": [{"price": 301}]}},
        "NFO:NIFTY2640322000PE": {"last_price": 280, "depth": {"buy": [{"price": 279}], "sell": [{"price": 281}]}},
        "NFO:NIFTY2640323100CE": {"last_price": 50, "depth": {"buy": [{"price": 49}], "sell": [{"price": 51}]}},
        "NFO:NIFTY2640320900PE": {"last_price": 45, "depth": {"buy": [{"price": 44}], "sell": [{"price": 46}]}},
    }
    return kite


@pytest.fixture
def sample_chain():
    """Create a minimal options chain DataFrame."""
    strikes = [20900, 21000, 21500, 22000, 22500, 23000, 23100]
    rows = []
    for s in strikes:
        for otype in ["CE", "PE"]:
            rows.append({
                "tradingsymbol": f"NIFTY26403{s}{otype}",
                "instrument_token": hash(f"{s}{otype}") % 100000,
                "strike": s,
                "expiry": "2026-04-03",
                "instrument_type": otype,
                "lot_size": 25,
                "name": "NIFTY",
            })
    return pd.DataFrame(rows)


class TestSignConvention:
    """P0: Verify TradeProposal.quantity is always positive."""

    def test_all_proposals_have_positive_quantity(self, mock_kite, sample_chain):
        proposer = TradeProposer.__new__(TradeProposer)
        proposer.client = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()

        # Mock _get_quote to return consistent data
        def mock_quote(symbol):
            return {"last_price": 200, "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
        proposer._get_quote = mock_quote

        from core.greeks_engine import GreeksEngine
        engine = GreeksEngine()

        proposals = proposer.propose_delta_neutral(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=15, greeks_engine=engine,
        )

        for p in proposals:
            assert p.quantity > 0, (
                f"Proposal for {p.tradingsymbol} has non-positive quantity {p.quantity}. "
                f"Direction should be in transaction_type='{p.transaction_type}', not quantity sign."
            )

    def test_buy_proposals_have_buy_type(self, mock_kite, sample_chain):
        proposer = TradeProposer.__new__(TradeProposer)
        proposer.client = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()

        def mock_quote(symbol):
            return {"last_price": 200, "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
        proposer._get_quote = mock_quote

        from core.greeks_engine import GreeksEngine
        engine = GreeksEngine()

        proposals = proposer.propose_delta_neutral(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=15, greeks_engine=engine,
        )

        for p in proposals:
            assert p.transaction_type in ("BUY", "SELL"), f"Invalid transaction_type: {p.transaction_type}"


class TestStraddleOnly:
    def test_proposer_generates_only_long_straddle(self, mock_kite, sample_chain):
        """No wings: structure must be exactly 2 long legs (ATM CE + ATM PE)."""
        proposer = TradeProposer.__new__(TradeProposer)
        proposer.client = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()

        def mock_quote(symbol):
            return {"last_price": 200, "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
        proposer._get_quote = mock_quote

        from core.greeks_engine import GreeksEngine
        engine = GreeksEngine()
        proposals = proposer.propose_delta_neutral(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=15, greeks_engine=engine,
        )

        assert len(proposals) == 2, f"Expected 2 legs (straddle), got {len(proposals)}"
        for p in proposals:
            assert p.transaction_type == "BUY", f"All legs must be BUY (long convexity), got {p.transaction_type}"
        assert {p.option_type for p in proposals} == {"CE", "PE"}


@pytest.fixture
def two_expiry_chain():
    """Phase 3.2: two-expiry chain so calendar builder can construct
    front-vs-back legs. ATM strike at 22000 for both."""
    strikes = [21800, 21900, 22000, 22100, 22200]
    rows = []
    for expiry in ("2026-04-03", "2026-05-29"):
        for s in strikes:
            for otype in ("CE", "PE"):
                rows.append({
                    "tradingsymbol": f"NIFTY{expiry.replace('-','')}{s}{otype}",
                    "instrument_token": hash((expiry, s, otype)) % 100000,
                    "strike": float(s),
                    "expiry": expiry,
                    "instrument_type": otype,
                    "lot_size": 25,
                    "name": "NIFTY",
                })
    return pd.DataFrame(rows)


class TestCalendarShortFront:
    """Phase 3.2: calendar builder must fire when chain has two
    expiries — pre-patch it returned [] on every call because
    `_get_options_chain` only ever returned one."""

    def test_two_expiry_chain_unblocks_calendar(self, mock_kite, two_expiry_chain):
        proposer = TradeProposer.__new__(TradeProposer)
        proposer.client = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()
        # Front IV rich → calendar prefers to short the front.
        # Back ATM CE worth more than front ATM CE → net debit > 0.
        def mock_quote(symbol):
            # Front month (Apr): cheaper ATM CE
            if "20260403" in symbol:
                return {"last_price": 200,
                        "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
            # Back month (May): more expensive ATM CE
            return {"last_price": 350,
                    "depth": {"buy": [{"price": 349}], "sell": [{"price": 351}]}}
        proposer._get_quote = mock_quote

        from core.greeks_engine import GreeksEngine
        engine = GreeksEngine()
        proposals = proposer.propose_calendar_short_front(
            chain=two_expiry_chain, spot=22000, capital=500000,
            position_size_pct=15, greeks_engine=engine,
        )
        assert len(proposals) == 2, (
            f"Calendar expected 2 legs (long-back + short-front), got "
            f"{len(proposals)}: {proposals}"
        )
        types = {p.transaction_type for p in proposals}
        assert types == {"BUY", "SELL"}, (
            f"Calendar must have one BUY and one SELL leg; got {types}"
        )
        expiries = {p.expiry for p in proposals}
        assert expiries == {"2026-04-03", "2026-05-29"}, (
            f"Calendar legs must span both expiries; got {expiries}"
        )
        # Long leg sits on back month (more expensive), short on front
        long_leg = next(p for p in proposals if p.transaction_type == "BUY")
        short_leg = next(p for p in proposals if p.transaction_type == "SELL")
        assert long_leg.expiry == "2026-05-29"
        assert short_leg.expiry == "2026-04-03"

    def test_single_expiry_chain_returns_empty_calendar(self, mock_kite, sample_chain):
        """Backward-compat sanity: with only one expiry the builder
        still correctly returns [] (the single-expiry guard is the
        path that used to fire universally before Phase 3.2)."""
        proposer = TradeProposer.__new__(TradeProposer)
        proposer.client = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()
        proposer._get_quote = lambda sym: {"last_price": 200,
            "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
        from core.greeks_engine import GreeksEngine
        proposals = proposer.propose_calendar_short_front(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=15, greeks_engine=GreeksEngine(),
        )
        assert proposals == []


class TestBackspreadSizing:
    """Phase 3.2 regression: the net-credit backspread used to clamp
    `per_unit_cost` to 1.0 INR when 2·OTM_price ≤ ATM_price (the
    typical, well-structured case), exploding max_lots into the
    hundreds and tripping the 30% margin gate on every fire. Sizing
    is now against max-loss-at-expiry, which is bounded by strike
    width regardless of how rich the ATM short is."""

    def _build_proposer(self, mock_kite, atm_price, otm_price):
        from datetime import datetime
        proposer = TradeProposer.__new__(TradeProposer)
        proposer.client = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()
        # Anchor _clock before the fixture's 2026-04-03 expiry so
        # _pick_strike_by_delta sees positive T and computes real deltas.
        proposer._clock = lambda: datetime(2026, 3, 20, 10, 30)
        proposer._get_quote = lambda symbol: {
            "last_price": atm_price if "22000CE" in symbol else otm_price,
            "depth": {
                "buy": [{"price": (atm_price if "22000CE" in symbol else otm_price) - 1}],
                "sell": [{"price": (atm_price if "22000CE" in symbol else otm_price) + 1}],
            },
        }
        return proposer

    def test_net_credit_backspread_does_not_explode_max_lots(self, mock_kite, sample_chain):
        """ATM short ₹300, OTM long ₹50 → net credit ₹200, width 1100.
        Max loss per unit = (1100 − 200) × 25 = ₹22,500. With
        risk_capital = 500k × 12% = ₹60k, max_lots ≈ 2. Old code
        clamped per_unit_cost to 25 INR and produced ~2,400 lots."""
        from core.greeks_engine import GreeksEngine
        proposer = self._build_proposer(mock_kite, atm_price=300, otm_price=50)
        proposals = proposer.propose_backspread(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=12.0, greeks_engine=GreeksEngine(),
        )
        assert len(proposals) == 2, f"Expected 2 legs, got {proposals}"
        short_leg = next(p for p in proposals if p.transaction_type == "SELL")
        long_leg = next(p for p in proposals if p.transaction_type == "BUY")
        # ATM short = 1× max_lots; OTM long = 2× max_lots
        assert long_leg.quantity == 2 * short_leg.quantity
        # Sanity: cumulative notional (treating premium × lot × qty as a
        # proxy for the legacy margin formula) must fit in the 30% cap.
        cumulative = sum(p.price * p.lot_size * p.quantity for p in proposals)
        assert cumulative < 0.30 * 500000, (
            f"Backspread sizing should fit under 30% margin cap; got "
            f"₹{cumulative:.0f} (= {cumulative/500000*100:.1f}% of capital)"
        )

    def test_short_leg_uses_span_estimate_not_premium(self, mock_kite, sample_chain):
        """Backspread ATM short: margin_required must be ≈15% of strike
        notional, not premium × notional. With ATM ₹300 strike 22000
        lot 25 qty 2: premium-only would be 300×25×2 = ₹15,000;
        SPAN approx is 0.15×22000×25×2 = ₹165,000."""
        from core.greeks_engine import GreeksEngine
        proposer = self._build_proposer(mock_kite, atm_price=300, otm_price=50)
        proposals = proposer.propose_backspread(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=12.0, greeks_engine=GreeksEngine(),
        )
        short_leg = next(p for p in proposals if p.transaction_type == "SELL")
        # Expected SPAN approx: 0.15 × 22000 × 25 × qty
        expected_span = 0.15 * short_leg.strike * short_leg.lot_size * short_leg.quantity
        # Premium floor: 2 × 300 × 25 × qty
        expected_floor = 2.0 * 300 * short_leg.lot_size * short_leg.quantity
        assert short_leg.margin_required == max(expected_span, expected_floor), (
            f"Short margin should be max(SPAN={expected_span:.0f}, "
            f"floor={expected_floor:.0f}); got {short_leg.margin_required:.0f}"
        )
        # Sanity: cannot equal the legacy premium-only formula
        legacy = 300 * short_leg.lot_size * short_leg.quantity
        assert short_leg.margin_required > legacy, (
            "Short margin should exceed the legacy premium-only estimate"
        )

    def test_long_leg_margin_unchanged_uses_premium(self, mock_kite, sample_chain):
        """BUY legs still post premium × notional as 'margin' — that's
        the cash outlay, which is the correct meaning for longs."""
        from core.greeks_engine import GreeksEngine
        proposer = self._build_proposer(mock_kite, atm_price=300, otm_price=50)
        proposals = proposer.propose_backspread(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=12.0, greeks_engine=GreeksEngine(),
        )
        long_leg = next(p for p in proposals if p.transaction_type == "BUY")
        expected = long_leg.price * long_leg.lot_size * long_leg.quantity
        assert long_leg.margin_required == expected

    def test_net_debit_backspread_still_sizes_sensibly(self, mock_kite, sample_chain):
        """ATM short ₹50, OTM long ₹40 → net debit 30 (2·40 − 50).
        Max loss per unit = (1100 − (−30)) × 25 = ₹28,250. Still
        bounded by strike width even in the unusual debit case."""
        from core.greeks_engine import GreeksEngine
        proposer = self._build_proposer(mock_kite, atm_price=50, otm_price=40)
        proposals = proposer.propose_backspread(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=12.0, greeks_engine=GreeksEngine(),
        )
        assert len(proposals) == 2
        short_leg = next(p for p in proposals if p.transaction_type == "SELL")
        assert short_leg.quantity >= 1
        cumulative = sum(p.price * p.lot_size * p.quantity for p in proposals)
        assert cumulative < 0.30 * 500000


# ── single-expiry pinning (2026-08-09 review) ──────────────

@pytest.fixture
def wide_two_expiry_chain():
    """Two expiries shaped like a real NIFTY chain: a fine-stepped weekly
    front clustered around ATM, and a coarse-stepped monthly back that runs
    much further out.

    That asymmetry is the point. `_pick_strike_by_delta` scans the WHOLE
    frame and keeps the single closest delta match, so the ~0.5Δ ATM target
    lands on the finely-stepped front while the ~0.10Δ / ~0.15Δ tail targets
    land on the back — which is exactly how the live chain produced
    "backspreads" with one leg in each expiry. A uniform ladder lets both
    picks land in the same expiry by luck and hides the defect."""
    rows = []
    ladders = {
        "2026-04-03": range(21600, 22401, 50),     # weekly: fine, near ATM only
        "2026-05-29": range(17000, 27001, 250),    # monthly: coarse, wide tails
    }
    for expiry, strikes in ladders.items():
        for s in strikes:
            for otype in ("CE", "PE"):
                rows.append({
                    "tradingsymbol": f"NIFTY{expiry.replace('-', '')}{s}{otype}",
                    "instrument_token": hash((expiry, s, otype)) % 100000,
                    "strike": float(s),
                    "expiry": expiry,
                    "instrument_type": otype,
                    "lot_size": 25,
                    "name": "NIFTY",
                })
    return pd.DataFrame(rows)


class TestSingleExpiryPinning:
    """2026-08-09 review: Phase 3.2 widened the chain handed to
    `propose_for_structure` to two expiries so the CALENDAR builder could
    construct front-vs-back legs. The delta-based builders pick each leg
    independently off that same chain, and nothing pinned them to one
    expiry — so a "backspread" routinely came out as short near-expiry ATM
    against long far-expiry OTM: a diagonal ratio spread, not a backspread.

    WHY it matters beyond naming: `_structure_margin` can only expiry-scan
    a single-expiry book. A mixed-expiry, net-CREDIT structure (which a
    properly built backspread always is) misses the scan AND the net-debit
    branch and falls through to `return gross` — the naked per-leg sum. On
    the 15-session tape replay that rejected EVERY backspread entry at the
    30%-of-capital cap: same-expiry structures margined ~Rs 110k, the
    mixed-expiry ones Rs 717k-5.0M against a Rs 300k cap. The strategy was
    silently unable to enter its own vol-of-vol regime."""

    VOL = {"2026-04-03": 0.16, "2026-05-29": 0.18}

    def _proposer(self, mock_kite, chain, now):
        """Quotes priced with Black-Scholes at a per-expiry vol, so
        `_pick_strike_by_delta`'s IV bisect solves and the delta targets
        resolve to real strikes on either expiry."""
        from core.greeks_engine import GreeksEngine, time_to_expiry

        engine = GreeksEngine()
        by_symbol = {
            r["tradingsymbol"]: (float(r["strike"]), str(r["expiry"]),
                                 r["instrument_type"])
            for _, r in chain.iterrows()
        }

        def quote(symbol):
            strike, expiry, otype = by_symbol[symbol]
            T = time_to_expiry(expiry, now)
            if T <= 0:
                return None
            px = engine.bs_price(22000.0, strike, T, self.VOL[expiry], otype)
            px = max(px, 0.05)
            return {"last_price": px,
                    "depth": {"buy": [{"price": px * 0.995}],
                              "sell": [{"price": px * 1.005}]}}

        proposer = TradeProposer.__new__(TradeProposer)
        proposer.client = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()
        proposer._clock = lambda: now
        proposer._get_quote = quote
        return proposer

    def _propose(self, mock_kite, chain, structure, now=None):
        from datetime import datetime
        from core.greeks_engine import GreeksEngine
        now = now or datetime(2026, 3, 20, 10, 30)
        return self._proposer(mock_kite, chain, now).propose_for_structure(
            structure=structure, chain=chain, spot=22000.0,
            capital=1000000, position_size_pct=12.0,
            greeks_engine=GreeksEngine(),
        )

    @pytest.mark.parametrize("structure", [
        "backspread", "risk_reversal_long_put", "asymmetric_strangle", "straddle",
    ])
    def test_single_expiry_structures_never_straddle_two_expiries(
            self, mock_kite, wide_two_expiry_chain, structure):
        # The three delta-based builders each fail this without the pinning
        # in propose_for_structure. `straddle` passes either way — it reads
        # `atm_ce.iloc[0]` / `atm_pe.iloc[0]`, which happen to land in the
        # same expiry — so it is here as a forward guard on the invariant,
        # not as a reproduction of the 2026-08-09 defect.
        proposals = self._propose(mock_kite, wide_two_expiry_chain, structure)
        assert proposals, f"{structure} produced no legs — fixture is wrong"
        expiries = {str(p.expiry) for p in proposals}
        assert len(expiries) == 1, (
            f"{structure} spans {expiries} — a single-expiry structure whose "
            f"legs sit in different expiries is a diagonal, and "
            f"_structure_margin degrades it to the naked per-leg sum"
        )

    def test_calendar_still_gets_both_expiries(self, mock_kite, wide_two_expiry_chain):
        # The exemption must stay exact: pinning the calendar to one expiry
        # would silently disable the structure Phase 3.2 widened the chain for.
        proposals = self._propose(
            mock_kite, wide_two_expiry_chain, "calendar_short_front")
        assert proposals, "calendar produced no legs"
        assert len({str(p.expiry) for p in proposals}) == 2, (
            "calendar_short_front IS the term-structure spread — it must "
            "keep both expiries"
        )

    def test_expiry_day_falls_through_to_next_live_expiry(
            self, mock_kite, wide_two_expiry_chain):
        # `_pick_strike_by_delta` skips rows whose T <= 0 so builders don't
        # null out on expiry day. Pinning must preserve that: with the front
        # expiry already expired, the structure builds on the BACK month
        # rather than returning [].
        from datetime import datetime
        proposals = self._propose(
            mock_kite, wide_two_expiry_chain, "backspread",
            now=datetime(2026, 4, 3, 10, 30))
        assert proposals, "expiry day must fall through to the back month"
        assert {str(p.expiry) for p in proposals} == {"2026-05-29"}

    def test_no_live_expiry_returns_no_proposals(self, mock_kite, wide_two_expiry_chain):
        # Rule 12: past every expiry there is nothing tradable. Skip, never
        # fall back to an expired series.
        from datetime import datetime
        assert self._propose(
            mock_kite, wide_two_expiry_chain, "backspread",
            now=datetime(2026, 6, 30, 10, 30)) == []
