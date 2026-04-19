"""Tests for trade_proposer.py — sign convention and proposal generation."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import pandas as pd
from unittest.mock import MagicMock
from trade_proposer import TradeProposer, TradeProposal


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
        proposer.kite = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()

        # Mock _get_quote to return consistent data
        def mock_quote(symbol):
            return {"last_price": 200, "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
        proposer._get_quote = mock_quote

        from greeks_engine import GreeksEngine
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
        proposer.kite = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()

        def mock_quote(symbol):
            return {"last_price": 200, "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
        proposer._get_quote = mock_quote

        from greeks_engine import GreeksEngine
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
        proposer.kite = mock_kite
        proposer.underlying = "NIFTY"
        proposer.config = MagicMock()

        def mock_quote(symbol):
            return {"last_price": 200, "depth": {"buy": [{"price": 199}], "sell": [{"price": 201}]}}
        proposer._get_quote = mock_quote

        from greeks_engine import GreeksEngine
        engine = GreeksEngine()
        proposals = proposer.propose_delta_neutral(
            chain=sample_chain, spot=22000, capital=500000,
            position_size_pct=15, greeks_engine=engine,
        )

        assert len(proposals) == 2, f"Expected 2 legs (straddle), got {len(proposals)}"
        for p in proposals:
            assert p.transaction_type == "BUY", f"All legs must be BUY (long convexity), got {p.transaction_type}"
        assert {p.option_type for p in proposals} == {"CE", "PE"}
