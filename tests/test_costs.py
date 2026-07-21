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


class TestEquityCost:
    """core.costs.estimate_equity_cost — the shared equity-cash model the
    varsity-swing and buy-on-gap strategies migrated onto (§4.1)."""

    def test_delivery_charges_stt_on_both_sides(self):
        """Delivery STT is 0.1% on BUY and SELL — the asymmetry vs intraday
        (sell-only) is exactly why a flat round-trip % mis-grades a swing
        book. On ₹1L: buy carries STT ₹100 + stamp ₹15; sell carries STT
        ₹100 and no stamp."""
        buy = costs.estimate_equity_cost(100.0, 1000, "BUY", "delivery")
        sell = costs.estimate_equity_cost(100.0, 1000, "SELL", "delivery")
        # STT dominates and is present on both sides (>= ₹100 each).
        assert buy > 100.0 and sell > 100.0
        # Buy is dearer than sell by the buy-side stamp duty (₹15 on ₹1L).
        assert round(buy - sell, 2) == 15.0

    def test_intraday_stt_sell_side_only(self):
        """Intraday STT (0.025%) is charged on the SELL leg only; the BUY
        leg pays none. Booking STT on the intraday buy would roughly double
        the modelled tax and understate the edge."""
        buy = costs.estimate_equity_cost(100.0, 1000, "BUY", "intraday")
        sell = costs.estimate_equity_cost(100.0, 1000, "SELL", "intraday")
        assert sell - buy > 100_000 * 0.00025 - 5  # ~₹25 STT shows up on sell
        # No 0.1% delivery-STT leaking in: intraday buy stays cheap (<₹40 on ₹1L).
        assert buy < 40.0

    def test_intraday_brokerage_capped_at_20(self):
        """₹20-or-0.03%-whichever-lower: a large intraday order caps
        brokerage at ₹20/side, not 0.03% unbounded."""
        # 0.03% of ₹10L = ₹300 > ₹20 → capped at ₹20.
        big = costs.estimate_equity_cost(1000.0, 1000, "BUY", "intraday")
        # brokerage ₹20 + exchange/SEBI/GST/stamp; no STT on buy.
        assert big < 100.0  # would be ~₹300+ if the cap were missing

    def test_delivery_is_brokerage_free(self):
        """Zerodha delivery is brokerage-free; a tiny delivery order's cost
        is dominated by STT+stamp, not a flat fee (contrast the F&O ₹20)."""
        small = costs.estimate_equity_cost(10.0, 1, "BUY", "delivery")
        assert small < 0.05  # ~₹0.026 (STT 0.1% of ₹10 + stamp), no ₹20 floor

    def test_slippage_is_additive_and_side_scoped(self):
        base = costs.estimate_equity_cost(100.0, 1000, "BUY", "delivery")
        slipped = costs.estimate_equity_cost(100.0, 1000, "BUY", "delivery",
                                             slippage_bps=5.0)
        assert round(slipped - base, 2) == round(100_000 * 5.0 / 1e4, 2)  # ₹50

    def test_unknown_product_raises(self):
        """A typo'd product must fail loud, not silently pick a cost model —
        a swing booked as intraday would understate STT 4x."""
        import pytest
        with pytest.raises(ValueError):
            costs.estimate_equity_cost(100.0, 10, "BUY", "swing")

    def test_zero_and_negative_turnover_cost_nothing(self):
        assert costs.estimate_equity_cost(0.0, 100, "BUY", "delivery") == 0.0
        assert costs.estimate_equity_cost(100.0, 0, "SELL", "intraday") == 0.0
        assert costs.estimate_equity_cost(-100.0, 100, "BUY", "delivery") == 0.0
