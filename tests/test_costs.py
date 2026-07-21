"""core/costs.py — canonical cost model + the taleb_karpathy re-export shim.

The component-rate tests live in tests/test_taleb_karpathy.py (they predate
the move and now exercise the shim path end-to-end). This file guards what
the 2026-07-21 move itself could break: the re-export contract and the
zero-turnover guard.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core import costs
from strategies import taleb_karpathy as tk


class TestReExportContract:
    def test_shim_is_the_same_object(self):
        """Strategies (arbitrage, pair_trading, kalman pairs, risk_analyzer)
        import estimate_transaction_cost from strategies.taleb_karpathy at
        call time, and tests monkeypatch that module attribute to fake costs.
        If the shim ever became a wrapper instead of the same function object,
        core.costs callers and taleb_karpathy callers could silently price the
        same order differently (the exact per-harness drift core/costs.py
        exists to kill)."""
        assert tk.estimate_transaction_cost is costs.estimate_transaction_cost

    def test_pinned_legacy_rate_survives_the_move(self):
        """pair_trading._has_sufficient_edge pins _FUT_EXCHANGE_RATE_LEGACY to
        keep the LIVE entry gate's hurdle unchanged; the corrected default
        must stay ~10x lower. If either constant drifts in the move, the live
        pair gate's economics change without anyone deciding that."""
        assert tk._FUT_EXCHANGE_RATE == costs._FUT_EXCHANGE_RATE == 0.000019
        assert tk._FUT_EXCHANGE_RATE_LEGACY == costs._FUT_EXCHANGE_RATE_LEGACY == 0.0002

    def test_zero_and_negative_turnover_cost_nothing(self):
        """A zero- or negative-turnover order must cost exactly 0 — not the
        ₹20 flat brokerage (rehedge no-ops probe candidate sizes including
        zero; a phantom fee per probe biases hedge_decision toward never
        hedging) and not a NEGATIVE cost (a bad caller passing a negative
        price would otherwise book negative costs that reduce booked totals).
        The zero-PRICE case is covered by test_taleb_karpathy's
        test_zero_price_zero_cost — same function object via the shim."""
        assert costs.estimate_transaction_cost(100.0, 0, 50, "SELL") == 0.0
        assert costs.estimate_transaction_cost(-100.0, 5, 50, "BUY") == 0.0
        assert costs.estimate_transaction_cost(100.0, -5, 50, "SELL") == 0.0
