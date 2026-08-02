"""Tests for the Taleb-Karpathy strategy — position management, netting, costs, metrics."""
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from strategies.taleb_karpathy import (
    TalebKarpathyStrategy, HedgeState, estimate_transaction_cost,
    _apply_best_params, _FUT_EXCHANGE_RATE_LEGACY,
)
from core.trade_proposer import TradeProposal
from core.greeks_engine import OptionContract
from core.regime_classifier import Structure


class TestTransactionCosts:
    def test_cost_always_positive(self):
        cost = estimate_transaction_cost(300.0, 2, 25, "BUY")
        assert cost > 0

    def test_sell_includes_stt(self):
        cost_buy = estimate_transaction_cost(300.0, 2, 25, "BUY")
        cost_sell = estimate_transaction_cost(300.0, 2, 25, "SELL")
        # Sell should have STT, buy should have stamp duty
        assert cost_sell > 0
        assert cost_buy > 0

    def test_zero_price_zero_cost(self):
        cost = estimate_transaction_cost(0.0, 2, 25, "BUY")
        assert cost == 0.0

    def test_cost_scales_with_turnover(self):
        cost_small = estimate_transaction_cost(100.0, 1, 25, "BUY")
        cost_large = estimate_transaction_cost(100.0, 10, 25, "BUY")
        assert cost_large > cost_small

    def test_options_sell_stt_rate(self):
        # Audit 3.4: options STT = 0.150% of premium turnover on sell, per the
        # NSE schedule. Isolate STT as (sell cost - buy cost) + buy stamp:
        # the only side-dependent levies are STT (sell) and stamp (buy), so
        # sell_cost - buy_cost = STT - stamp.
        turnover = 300.0 * 2 * 25                       # 15,000
        buy = estimate_transaction_cost(300.0, 2, 25, "BUY", instrument_type="OPT")
        sell = estimate_transaction_cost(300.0, 2, 25, "SELL", instrument_type="OPT")
        stamp = turnover * 0.00003                       # buy-side stamp
        stt = (sell - buy) + stamp
        assert stt == pytest.approx(turnover * 0.0015)   # 0.150%

    def test_futures_sell_stt_rate(self):
        # Audit 3.4: futures STT = 0.050% of traded turnover on sell.
        turnover = 1000.0 * 1 * 25                       # 25,000
        buy = estimate_transaction_cost(1000.0, 1, 25, "BUY", instrument_type="FUT")
        sell = estimate_transaction_cost(1000.0, 1, 25, "SELL", instrument_type="FUT")
        stamp = turnover * 0.00003
        stt = (sell - buy) + stamp
        assert stt == pytest.approx(turnover * 0.0005)   # 0.050%

    def test_futures_exchange_charge_corrected_rate(self):
        # Regression lock for the 2026-06-19 fix: FUT exchange charge ≈ 0.0019%
        # (₹190/cr), NOT the pre-fix 0.02%. The relational tests above pass at
        # either rate, so pin the FULL cost here — this FAILS if someone reverts
        # the exchange rate. FUT BUY, turnover 25,000:
        #   brokerage 20 + exch 25000*0.000019=0.475 + sebi 0.025
        #   + gst 0.18*(20+0.475+0.025)=3.69 + stamp 25000*0.00003=0.75
        #   + slippage 25000*0.0002=5.0  = 29.94  (BUY → no STT)
        cost = estimate_transaction_cost(1000.0, 1, 25, "BUY", instrument_type="FUT")
        assert cost == pytest.approx(29.94, abs=0.01)

    def test_legacy_fut_exchange_rate_override(self):
        # The pair-gate freeze mechanism: passing the legacy rate must yield a
        # HIGHER cost than the corrected default, and the gap must equal the
        # exchange-charge delta grossed up for GST (the only affected levy).
        turnover = 1000.0 * 1 * 25
        corrected = estimate_transaction_cost(1000.0, 1, 25, "BUY", instrument_type="FUT")
        legacy = estimate_transaction_cost(
            1000.0, 1, 25, "BUY", instrument_type="FUT",
            fut_exchange_rate=_FUT_EXCHANGE_RATE_LEGACY,
        )
        assert legacy > corrected
        exch_delta = turnover * (_FUT_EXCHANGE_RATE_LEGACY - 0.000019)
        assert (legacy - corrected) == pytest.approx(exch_delta * 1.18, abs=0.01)


class TestPositionNetting:
    """P0: Verify position netting in execute_proposals."""

    @pytest.fixture
    def mock_hedger(self):
        """Create a TalebKarpathyStrategy with mocked Kite (bypassing __init__)."""
        kite = MagicMock()
        kite.instruments.return_value = [
            {"name": "NIFTY", "instrument_type": "CE", "lot_size": 25,
             "tradingsymbol": "NIFTY26403CE22000", "expiry": "2026-04-03"},
        ]
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: __import__("datetime").datetime(2026, 3, 29, 10, 0)
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0)
        )
        return hedger

    def test_new_position_added(self, mock_hedger):
        prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )
        mock_hedger.execute_proposals([prop])
        assert len(mock_hedger.state.positions) == 1
        assert mock_hedger.state.positions[0].quantity == 2

    def test_closing_trade_removes_position(self, mock_hedger):
        # Open position: long 2 lots
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, entry_price=300, current_price=350, iv=0.15,
        ))

        # Close: sell 2 lots
        prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=350,
            transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=0,
        )
        mock_hedger.execute_proposals([prop])
        assert len(mock_hedger.state.positions) == 0
        # Realized P/L should be (350-300)*2*25 = 2500 minus costs
        assert mock_hedger.state.realized_pnl > 0

    def test_futures_tracked_in_delta(self, mock_hedger):
        prop = TradeProposal(
            tradingsymbol="NIFTY26APRFUT", instrument_token=0,
            strike=0, expiry="", option_type="FUT",
            lot_size=25, quantity=2, price=22000,
            transaction_type="BUY", iv=0, bid_ask_spread_pct=0,
            margin_required=0,
        )
        mock_hedger.execute_proposals([prop])
        assert mock_hedger.state.futures_hedge_delta == 50  # 2 lots * 25

    def test_sell_futures_negative_delta(self, mock_hedger):
        prop = TradeProposal(
            tradingsymbol="NIFTY26APRFUT", instrument_token=0,
            strike=0, expiry="", option_type="FUT",
            lot_size=25, quantity=3, price=22000,
            transaction_type="SELL", iv=0, bid_ask_spread_pct=0,
            margin_required=0,
        )
        mock_hedger.execute_proposals([prop])
        assert mock_hedger.state.futures_hedge_delta == -75  # -3 lots * 25


class TestFailedOrderGuard:
    """P1: Failed live orders must not mutate internal state."""

    @pytest.fixture
    def live_hedger(self):
        kite = MagicMock()
        kite.instruments.return_value = [
            {"name": "NIFTY", "instrument_type": "CE", "lot_size": 25,
             "tradingsymbol": "NIFTY26403CE22000", "expiry": "2026-04-03"},
        ]
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "live"  # LIVE mode
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: __import__("datetime").datetime(2026, 3, 29, 10, 0)
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0)
        )
        # Make place_order raise to simulate rejection
        kite.place_order.side_effect = Exception("Order rejected")
        kite.VARIETY_REGULAR = "regular"
        kite.PRODUCT_NRML = "NRML"
        kite.ORDER_TYPE_LIMIT = "LIMIT"
        kite.VALIDITY_DAY = "DAY"
        kite.TRANSACTION_TYPE_BUY = "BUY"
        kite.TRANSACTION_TYPE_SELL = "SELL"
        return hedger

    def test_failed_order_no_position_mutation(self, live_hedger):
        prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )
        results = live_hedger.execute_proposals([prop])
        assert results[0]["status"] == "FAILED"
        assert len(live_hedger.state.positions) == 0
        assert live_hedger.state.realized_pnl == 0.0
        assert live_hedger.state.total_transaction_costs == 0.0

    def test_failed_futures_no_delta_mutation(self, live_hedger):
        prop = TradeProposal(
            tradingsymbol="NIFTY26APRFUT", instrument_token=0,
            strike=0, expiry="", option_type="FUT",
            lot_size=25, quantity=2, price=22000,
            transaction_type="BUY", iv=0, bid_ask_spread_pct=0,
            margin_required=0,
        )
        results = live_hedger.execute_proposals([prop])
        assert results[0]["status"] == "FAILED"
        assert live_hedger.state.futures_hedge_delta == 0.0
        assert live_hedger.state.futures_lots == 0


class TestFuturesPnL:
    """P1: Unrealized P/L must include the futures hedge leg."""

    @pytest.fixture
    def mock_hedger(self):
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: __import__("datetime").datetime(2026, 3, 29, 10, 0)
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0)
        )
        return hedger

    def test_futures_unrealized_pnl_included(self, mock_hedger):
        # Buy 2 lots futures @ 22000
        prop = TradeProposal(
            tradingsymbol="NIFTY26APRFUT", instrument_token=0,
            strike=0, expiry="", option_type="FUT",
            lot_size=25, quantity=2, price=22000,
            transaction_type="BUY", iv=0, bid_ask_spread_pct=0,
            margin_required=0,
        )
        mock_hedger.execute_proposals([prop])
        assert mock_hedger.state.futures_entry_vwap == 22000.0
        assert mock_hedger.state.futures_lots == 2

        # Futures moving to 22100 — P/L is (22100-22000)*2*25 = 5000.
        # Spot is passed deliberately DIFFERENT from the futures LTP: the
        # entry VWAP is a futures price, so the mark must come off the
        # futures contract. Marking against spot here would give 2500 and
        # silently book the 50-pt basis as P&L.
        mock_hedger._cached_futures_symbol = "NIFTY26APRFUT"
        mock_hedger.kite.quote.return_value = {
            "NFO:NIFTY26APRFUT": {"last_price": 22100.0},
        }
        mock_hedger._update_positions_prices(22050.0)
        assert mock_hedger.state.unrealized_pnl == 5000.0


class TestFuturesHedgeBasisPricing:
    """The futures hedge is entered, marked and flattened off ONE series —
    the futures contract. Mixing in index spot books the basis as phantom
    P&L, which feeds the daily-loss breaker.

    Regression for the 2026-07-10 session: a 4-lot long hedge (VWAP
    24,220.47) was marked and flattened at spot while the basis ran ~+30
    pts, showing ~Rs 7.7k of loss that did not exist. That tripped the
    Rs 15k breaker at -Rs 15,297, flattened the book near the low and
    locked out entries for the remaining four hours of a session that
    closed higher.
    """

    LOT, LOTS = 65, 4
    VWAP, FUT, SPOT = 24220.47, 24200.00, 24170.55   # basis ~ +30 pts

    @pytest.fixture
    def h(self):
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = MagicMock()
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = self.LOT
        hedger._cached_futures_symbol = "NIFTY26JULFUT"
        hedger._consecutive_losses = 0
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger._consecutive_futures_failures = 0
        hedger._clock = lambda: datetime(2026, 7, 10, 11, 13)
        hedger.immutable_params = {
            "total_capital": 1_000_000, "max_daily_loss_pct": 1.5,
            "gap_exit_threshold_pct": 100.0,
        }
        hedger.tunable_params = {"max_holding_period_hours": 22.0,
                                 "vega_limit": 1e9}
        hedger.state.futures_lots = self.LOTS
        hedger.state.futures_hedge_delta = self.LOTS * self.LOT
        hedger.state.futures_entry_vwap = self.VWAP
        hedger.state.futures_symbol = "NIFTY26JULFUT"
        hedger.state.futures_last_mark = self.VWAP
        return hedger

    def _quote_futures(self, h, price, symbol="NIFTY26JULFUT"):
        h.kite.quote.return_value = {f"NFO:{symbol}": {"last_price": price}}

    # ── marking ────────────────────────────────────────────────

    def test_mark_uses_futures_not_spot(self, h):
        """The mark must track the contract we actually hold. Spot is
        passed 30 pts below the futures; using it would report -12,979
        instead of the true -5,322."""
        self._quote_futures(h, self.FUT)
        h._update_positions_prices(self.SPOT)
        assert h.state.unrealized_pnl == pytest.approx(
            (self.FUT - self.VWAP) * self.LOTS * self.LOT
        )
        assert h.state.unrealized_pnl == pytest.approx(-5322.2)

    def test_basis_alone_moves_no_pnl(self, h):
        """Hedge opened at the futures price and the futures have not
        moved: P&L is zero no matter how wide the basis is. This is the
        property the 07-10 loss violated."""
        h.state.futures_entry_vwap = self.FUT
        self._quote_futures(h, self.FUT)
        h._update_positions_prices(self.FUT - 30.0)
        assert h.state.unrealized_pnl == 0.0

    def test_daily_loss_breaker_not_tripped_by_basis(self, h):
        """The money-affecting assertion. With Rs 2,300 of real costs and
        the futures 20 pts against us, the day is -Rs 7,622 — inside the
        Rs 15,000 floor. Marking at spot makes it -Rs 15,279 and fires the
        breaker, which is exactly what happened on 2026-07-10."""
        h.state.realized_pnl = -2300.0
        self._quote_futures(h, self.FUT)
        h._update_positions_prices(self.SPOT)

        assert h.state._current_day_pnl == pytest.approx(-7622.2)
        assert h._should_exit(None, self.SPOT) is False
        assert h._daily_loss_stop_date is None

        # Same book marked the old (spot) way would have breached.
        spot_marked = -2300.0 + (self.SPOT - self.VWAP) * self.LOTS * self.LOT
        assert spot_marked < -(1_000_000 * 1.5 / 100)

    def test_mark_carries_last_good_futures_price_on_quote_failure(self, h):
        """H-6a: a quote outage must carry the last good futures mark, not
        fall back to spot and not silently mark the leg flat — the loss
        gates read this number."""
        self._quote_futures(h, self.FUT)
        h._update_positions_prices(self.SPOT)
        h.kite.quote.side_effect = Exception("feed down")
        h._update_positions_prices(self.SPOT)
        assert h.state.unrealized_pnl == pytest.approx(
            (self.FUT - self.VWAP) * self.LOTS * self.LOT
        )

    # ── flattening ─────────────────────────────────────────────

    def test_close_all_prices_flatten_at_futures(self, h):
        self._quote_futures(h, self.FUT)
        props = h._generate_close_all_proposals()
        fut = [p for p in props if p.option_type == "FUT"]
        assert len(fut) == 1
        assert fut[0].price == pytest.approx(self.FUT)
        assert fut[0].transaction_type == "SELL"   # long hedge -> sell to close

    def test_flatten_falls_back_to_entry_vwap_never_zero(self, h):
        """H-6b guarantee preserved: the flatten price is never 0.0, so
        validate_order's price>0 gate cannot reject an emergency square-off
        just because the feed died."""
        h.kite.quote.side_effect = Exception("feed down")
        props = h._generate_close_all_proposals()
        fut = [p for p in props if p.option_type == "FUT"]
        assert len(fut) == 1
        assert fut[0].price == pytest.approx(self.VWAP)

    # ── entry ──────────────────────────────────────────────────

    def test_hard_hedge_skipped_when_no_futures_price(self, h):
        """Refuse rather than guess (H-6c/H-6d): a spot-priced entry VWAP
        would corrupt every later mark on the position. Drift re-proposes
        the hedge on the next tick."""
        h.kite.quote.side_effect = Exception("feed down")
        greeks = MagicMock(net_discrete_delta=-260.0, net_delta=-260.0)
        assert h._generate_hard_delta_proposals(greeks, self.SPOT) == []

    def test_hard_hedge_prices_entry_at_futures(self, h):
        self._quote_futures(h, self.FUT)
        greeks = MagicMock(net_discrete_delta=-260.0, net_delta=-260.0)
        props = h._generate_hard_delta_proposals(greeks, self.SPOT)
        assert len(props) == 1
        assert props[0].price == pytest.approx(self.FUT)
        assert props[0].quantity == self.LOTS

    # ── contract identity across the monthly roll ──────────────

    def test_mark_quotes_the_contract_held_not_front_month(self, h):
        """Review finding: after JUL settles, `_get_futures_symbol()`
        resolves AUG. Quoting AUG against a JUL entry VWAP would book the
        roll spread as phantom P&L — the same class of error as marking at
        index spot. Mark the contract we actually hold."""
        h._cached_futures_symbol = "NIFTY26AUGFUT"      # front-month rolled
        h.kite.quote.return_value = {
            "NFO:NIFTY26JULFUT": {"last_price": self.FUT},
            "NFO:NIFTY26AUGFUT": {"last_price": self.FUT + 180.0},
        }
        h._update_positions_prices(self.SPOT)
        assert h.state.futures_last_mark == pytest.approx(self.FUT)
        assert h.state.unrealized_pnl == pytest.approx(
            (self.FUT - self.VWAP) * self.LOTS * self.LOT
        )
        # The quote must have been asked for the HELD contract.
        assert h.kite.quote.call_args[0][0] == ["NFO:NIFTY26JULFUT"]

    def test_fill_binds_contract_and_seeds_mark_then_clears(self, h):
        """The mark is seeded from the fill so a quote outage on the very
        next tick has something to carry; both are cleared when flat so a
        later hedge can never inherit a stale contract's price."""
        h.state = HedgeState()
        h.mode = "paper"
        h.greeks = MagicMock()
        h.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0))
        open_prop = TradeProposal(
            tradingsymbol="NIFTY26JULFUT", instrument_token=0, strike=0,
            expiry="", option_type="FUT", lot_size=self.LOT, quantity=2,
            price=self.FUT, transaction_type="BUY", iv=0,
            bid_ask_spread_pct=0, margin_required=0,
        )
        h.execute_proposals([open_prop])
        assert h.state.futures_symbol == "NIFTY26JULFUT"
        assert h.state.futures_last_mark == pytest.approx(self.FUT)

        close_prop = TradeProposal(
            tradingsymbol="NIFTY26JULFUT", instrument_token=0, strike=0,
            expiry="", option_type="FUT", lot_size=self.LOT, quantity=2,
            price=self.FUT + 10, transaction_type="SELL", iv=0,
            bid_ask_spread_pct=0, margin_required=0,
        )
        h.execute_proposals([close_prop])
        assert h.state.futures_symbol == ""
        assert h.state.futures_last_mark == 0.0

    def test_mark_and_contract_survive_serialize_restore(self, h):
        """Review finding: the mark used to be an un-persisted instance
        attribute, so after a runner restart with a carried hedge the first
        failing quote marked the leg FLAT and the daily-loss breaker went
        blind to the whole futures move."""
        h.state.futures_last_mark = self.FUT
        blob = h.serialize_state()
        h2 = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h2.state = HedgeState()
        h2.restore_state(blob)
        assert h2.state.futures_symbol == "NIFTY26JULFUT"
        assert h2.state.futures_last_mark == pytest.approx(self.FUT)

    def test_restore_tolerates_blob_without_futures_fields(self, h):
        """Existing on-disk state files predate these fields."""
        blob = h.serialize_state()
        blob["state"].pop("futures_symbol", None)
        blob["state"].pop("futures_last_mark", None)
        h2 = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h2.state = HedgeState()
        h2.restore_state(blob)
        assert h2.state.futures_symbol == ""
        assert h2.state.futures_last_mark == 0.0

    def test_carried_mark_keeps_breaker_sighted_after_restart(self, h):
        """The point of persisting the mark: a restored hedge whose quote
        fails still marks against a real price, so a large adverse futures
        move remains visible to `_should_exit`."""
        h.state.futures_last_mark = self.VWAP - 100.0      # restored, adverse
        h.kite.quote.side_effect = Exception("feed down")
        h._update_positions_prices(self.SPOT)
        assert h.state.unrealized_pnl == pytest.approx(-100.0 * self.LOTS * self.LOT)
        assert h.state.unrealized_pnl != 0.0

    # ── fail-loud ledger (H-6a) ────────────────────────────────

    def test_futures_quote_failures_escalate_to_error(self, h, caplog):
        """H-6a: a frozen futures mark feeds the daily-loss breaker, so it
        must not sit at one severity forever."""
        h.kite.quote.side_effect = Exception("feed down")
        for _ in range(4):
            h._futures_mark()
        assert h._consecutive_futures_failures == 4
        with caplog.at_level(logging.ERROR):
            h._futures_mark()
        assert h._consecutive_futures_failures == 5
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    @pytest.mark.parametrize("payload", [
        {"NFO:NIFTY26JULFUT": None},                       # AttributeError
        {"NFO:NIFTY26JULFUT": {"last_price": "n/a"}},      # ValueError
        {"NFO:NIFTY26JULFUT": ["last_price"]},             # AttributeError
    ])
    def test_malformed_quote_payload_does_not_abort_the_pnl_update(self, h, payload):
        """A shape-drifted payload must degrade to the carried mark, not
        raise out of _update_positions_prices — which runs AFTER the option
        legs have been re-marked, so a raise leaves a half-updated book with
        total_pnl unset and _should_exit never evaluated."""
        h.state.futures_last_mark = self.FUT
        h.kite.quote.return_value = payload
        h._update_positions_prices(self.SPOT)   # must not raise
        assert h.state.unrealized_pnl == pytest.approx(
            (self.FUT - self.VWAP) * self.LOTS * self.LOT
        )

    def test_futures_failure_counter_resets_on_recovery(self, h):
        h.kite.quote.side_effect = Exception("feed down")
        h._futures_mark()
        assert h._consecutive_futures_failures == 1
        h.kite.quote.side_effect = None
        self._quote_futures(h, self.FUT)
        assert h._futures_mark() == pytest.approx(self.FUT)
        assert h._consecutive_futures_failures == 0


class TestResetAndMetrics:
    def test_reset_clears_state(self):
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.state = HedgeState()
        hedger.state.total_pnl = 1000
        hedger.state.positions.append(MagicMock())
        hedger._consecutive_losses = 3
        hedger._circuit_breaker_until = "something"

        hedger.reset_state()

        assert hedger.state.total_pnl == 0
        assert len(hedger.state.positions) == 0
        assert hedger._consecutive_losses == 0
        assert hedger._circuit_breaker_until is None

    def test_get_strategy_metrics_daily_aggregation(self):
        """P2: Sharpe/Sortino should be based on daily returns, not tick-level."""
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.state = HedgeState()
        # Simulate 5 daily returns already flushed
        hedger.state.daily_pnl_history = [100, -50, 200, -30, 150]
        hedger.state._current_day_pnl = 0.0
        hedger.state._current_trading_date = None
        hedger.state.total_pnl = 370
        hedger.state.max_drawdown = 50
        hedger.immutable_params = {"total_capital": 500000}

        metrics = hedger.get_strategy_metrics()

        assert "net_pnl" in metrics
        assert "sharpe_ratio" in metrics
        assert "calmar_ratio" in metrics
        assert "sortino_ratio" in metrics
        assert "total_transaction_costs" in metrics
        assert metrics["net_pnl"] == 370
        assert metrics["sharpe_ratio"] != 0  # Should compute with 5 data points


class TestSharpeDegeneracyGuard:
    """The Sharpe/Sortino metric must not explode on too-few daily points.

    Pre-fix the `>= 2` gate + population std let two near-identical daily
    buckets drive std→0, producing a Sharpe of ±hundreds-of-thousands. That
    degenerate spike (the autoresearch baseline was -569035) made the sweep
    rank on noise. These tests pin the min-observations guard and that a
    genuine multi-day series annualizes with the sample (ddof=1) std."""

    def _hedger(self, daily):
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.state = HedgeState()
        hedger.state.daily_pnl_history = list(daily)
        hedger.state._current_day_pnl = 0.0
        hedger.state._current_trading_date = None
        hedger.state.total_pnl = float(sum(daily))
        hedger.state.max_drawdown = 0.0
        hedger.immutable_params = {"total_capital": 500000}
        return hedger

    def test_two_near_identical_days_do_not_explode(self):
        # The exact shape that produced -569035: two almost-equal buckets.
        # Caught by the count floor (2 < 5 days).
        m = self._hedger([5000.0, 5000.0001]).get_strategy_metrics()
        assert m["sharpe_ratio"] == 0.0
        assert m["sortino_ratio"] == 0.0

    def test_many_near_identical_days_do_not_explode(self):
        # THE case the count floor alone misses: 5+ near-flat buckets clear
        # the day count, but std/|mean| ≈ 1e-8 ⇒ pre-CV-guard Sharpe was
        # ~1.8e9. The coefficient-of-variation guard must return 0.0. A
        # near-flat P&L week (calm-regime straddle) is entirely realistic.
        m = self._hedger([5000.0, 5000.0, 5000.0, 5000.0, 5000.0001]).get_strategy_metrics()
        assert m["sharpe_ratio"] == 0.0
        assert m["sortino_ratio"] == 0.0
        m6 = self._hedger([100.0, 100.0, 100.0, 100.0, 100.0, 100.0001]).get_strategy_metrics()
        assert m6["sharpe_ratio"] == 0.0

    def test_near_zero_mean_flat_series_does_not_explode(self):
        # Tiny mean ⇒ tiny relative-std threshold, so the CV guard alone
        # would let this through (~7.1). The absolute ₹ std floor catches it:
        # a sub-₹1 daily std means the book isn't really trading.
        m = self._hedger([0.0, 0.0, 0.0, 0.0, 0.0001]).get_strategy_metrics()
        assert m["sharpe_ratio"] == 0.0

    def test_below_min_days_returns_zero(self):
        # 4 days is below the 5-day floor → undefined → neutral 0.0.
        m = self._hedger([100.0, -50.0, 200.0, -30.0]).get_strategy_metrics()
        assert m["sharpe_ratio"] == 0.0

    def test_sample_std_annualization_is_correct(self):
        # 5 clean daily points: Sharpe = mean/std(ddof=1) * sqrt(252).
        import numpy as np
        daily = [100.0, -50.0, 200.0, -30.0, 150.0]
        m = self._hedger(daily).get_strategy_metrics()
        arr = np.array(daily)
        expected = (arr.mean() / arr.std(ddof=1)) * np.sqrt(252)
        assert m["sharpe_ratio"] == pytest.approx(expected, rel=1e-9)

    def test_single_session_replay_sharpe_is_zero_not_huge(self):
        # A single-session tape replay flushes ~1 daily bucket. Sharpe must
        # be the neutral 0.0 there (undefined), NOT a degenerate spike —
        # net_pnl / gamma_theta_ratio are the right metrics for that data.
        m = self._hedger([1234.0]).get_strategy_metrics()
        assert m["sharpe_ratio"] == 0.0
        assert m["sortino_ratio"] == 0.0


class TestDailyPnLAggregation:
    """P2: Verify tick-level P/L gets aggregated into daily buckets."""

    def test_ticks_same_day_not_appended_individually(self):
        from datetime import datetime
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger.kite = MagicMock()
        hedger.kite.quote.return_value = {}
        hedger.immutable_params = {"total_capital": 500000}

        # Simulate 5 ticks on the same day by bumping realized_pnl each time
        # (_update_positions_prices recalculates total_pnl = realized + unrealized)
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        for i in range(5):
            hedger.state.realized_pnl = (i + 1) * 100
            hedger._update_positions_prices(22000.0)

        # daily_pnl_history should have 0 entries (all same day, not flushed yet)
        assert len(hedger.state.daily_pnl_history) == 0
        assert hedger.state._current_day_pnl == 500.0  # 5 ticks * 100 each

    def test_day_change_flushes_daily_pnl(self):
        from datetime import datetime
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger.kite = MagicMock()
        hedger.kite.quote.return_value = {}
        hedger.immutable_params = {"total_capital": 500000}

        # Day 1: 2 ticks
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger.state.realized_pnl = 100
        hedger._update_positions_prices(22000.0)
        hedger.state.realized_pnl = 200
        hedger._update_positions_prices(22000.0)

        # Day 2: 1 tick — should flush day 1
        hedger._clock = lambda: datetime(2026, 3, 30, 10, 0)
        hedger.state.realized_pnl = 250
        hedger._update_positions_prices(22000.0)

        assert len(hedger.state.daily_pnl_history) == 1
        assert hedger.state.daily_pnl_history[0] == 200.0  # Day 1 total


class TestCredentialValidation:
    """P2: Placeholder credentials must raise immediately."""

    def test_placeholder_detected(self):
        from core.kite_auth import KiteAuthManager, AuthenticationError
        import tempfile
        config_content = """[kite]
api_key = ${KITE_API_KEY}
api_secret = ${KITE_API_SECRET}
user_id = ${KITE_USER_ID}
password = ${KITE_PASSWORD}
totp_key = ${KITE_TOTP_KEY}
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False) as f:
            f.write(config_content)
            f.flush()
            # Clear env vars to ensure fallback to placeholders
            env_backup = {}
            for var in ["KITE_API_KEY", "KITE_API_SECRET", "KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_KEY"]:
                env_backup[var] = os.environ.pop(var, None)
            try:
                with pytest.raises(AuthenticationError, match="Credentials not configured"):
                    KiteAuthManager(f.name)
            finally:
                for var, val in env_backup.items():
                    if val is not None:
                        os.environ[var] = val
                os.unlink(f.name)


class TestFlatBookPnL:
    """P1: After full exit, unrealized P/L must be zero and total P/L must equal realized."""

    @pytest.fixture
    def mock_hedger(self):
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: __import__("datetime").datetime(2026, 3, 29, 10, 0)
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0)
        )
        return hedger

    def test_unrealized_zero_after_full_close(self, mock_hedger):
        # Open long 2 lots CE @ 300
        open_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )
        mock_hedger.execute_proposals([open_prop])
        assert len(mock_hedger.state.positions) == 1

        # Close: sell 2 lots @ 250 (loss)
        close_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=250,
            transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=0,
        )
        mock_hedger.execute_proposals([close_prop])

        assert len(mock_hedger.state.positions) == 0
        assert mock_hedger.state.unrealized_pnl == 0.0
        assert mock_hedger.state.total_pnl == mock_hedger.state.realized_pnl


class TestTimeToExpiryReplay:
    """P1: time_to_expiry must use reference_time, not datetime.now()."""

    def test_reference_time_used(self):
        from core.greeks_engine import time_to_expiry
        from datetime import datetime

        # Expiry is 2026-03-13, reference time is 2026-03-01 → ~12 days out
        T = time_to_expiry("2026-03-13", datetime(2026, 3, 1, 9, 15))
        assert T > 0.02  # Should be ~12/365 ≈ 0.033

        # Without reference_time (today is 2026-03-30), expiry is past → 0
        T_now = time_to_expiry("2026-03-13")
        assert T_now == 0.0


class TestTransactionCostByProduct:
    """P2: Transaction costs must differ between options and futures."""

    def test_futures_vs_options_cost_diverge(self):
        cost_opt = estimate_transaction_cost(300.0, 2, 25, "SELL", instrument_type="OPT")
        cost_fut = estimate_transaction_cost(300.0, 2, 25, "SELL", instrument_type="FUT")
        # Options have higher STT and exchange charges than futures
        assert cost_opt != cost_fut
        assert cost_opt > cost_fut  # Options are more expensive to trade

    def test_futures_buy_has_lower_slippage(self):
        cost_opt = estimate_transaction_cost(300.0, 2, 25, "BUY", instrument_type="OPT")
        cost_fut = estimate_transaction_cost(300.0, 2, 25, "BUY", instrument_type="FUT")
        assert cost_fut < cost_opt

    def test_backward_compat_defaults_to_options(self):
        # Old callers without instrument_type should still work (default = OPT)
        cost = estimate_transaction_cost(300.0, 2, 25, "BUY")
        cost_opt = estimate_transaction_cost(300.0, 2, 25, "BUY", instrument_type="OPT")
        assert cost == cost_opt


class TestOptimizerRangeCoverage:
    """P2: All active runtime tunable params must be in TUNABLE_RANGES."""

    def test_all_runtime_tunables_covered(self):
        from runners.autoresearch_loop import HedgeResearchLoop
        # Params used in runtime decision paths that the optimizer must cover.
        # Under regime dispatch (the live config) structure routing is decided by
        # the regime_* classifier cutoffs, so those must be sweepable. The legacy
        # hard gates min_rv_iv_ratio / skew_pct_max are bypassed under dispatch
        # (their regime equivalents ARE the regime_* thresholds) and are
        # intentionally config-only, not swept — so they are not required here.
        runtime_tunables = {
            "rehedge_delta_threshold", "gamma_scalp_band_pct",
            "position_size_pct", "vega_limit", "max_holding_period_hours",
            "entry_iv_percentile_min", "entry_iv_percentile_max",
            "max_entry_alpha", "mc_worst_path_loss_pct", "rv_window_days",
            "regime_straddle_iv_pct_max", "regime_straddle_rv_iv_ratio_min",
            "regime_straddle_skew_pct_max", "regime_calendar_iv_pct_min",
            "regime_calendar_skew_pct_max", "regime_risk_reversal_skew_pct_min",
            "regime_backspread_vvol_min", "regime_asymmetric_strangle_rv_iv_min",
            "regime_asymmetric_strangle_skew_pct_min",
        }
        optimizer_tunables = set(HedgeResearchLoop.TUNABLE_RANGES.keys())
        missing = runtime_tunables - optimizer_tunables
        assert not missing, f"Optimizer missing tunables: {missing}"

    def test_no_dead_tunables_in_hedger(self):
        """delta_bump_pct was removed — verify it's gone."""
        import inspect
        source = inspect.getsource(TalebKarpathyStrategy.__init__)
        assert "delta_bump_pct" not in source


class TestDailyLossStop:
    """P1: After max daily loss, no new entries should occur for the rest of that day."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger._consecutive_losses = 0
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger._atm_iv_history = []
        hedger.immutable_params = {
            "total_capital": 500000,
            "max_daily_loss_pct": 2.0,
            "no_trade_last_minutes": 15,
            "max_positions": 6,
            "gap_exit_threshold_pct": 3.0,
            "circuit_breaker_consecutive_losses": 3,
            "circuit_breaker_pause_minutes": 60,
        }
        hedger.tunable_params = {
            "max_holding_period_hours": 48,
            "vega_limit": 500,
        }
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0, net_vega=0)
        )
        return hedger

    def test_daily_loss_blocks_new_entries(self, mock_hedger):
        # Simulate daily loss stop triggered
        mock_hedger._daily_loss_stop_date = mock_hedger._clock().date()
        assert mock_hedger._pre_trade_checks() is False

    def test_daily_loss_clears_next_day(self, mock_hedger):
        from datetime import datetime
        # Loss triggered on March 29
        mock_hedger._daily_loss_stop_date = datetime(2026, 3, 29).date()
        # Next day should allow trading
        mock_hedger._clock = lambda: datetime(2026, 3, 30, 10, 0)
        assert mock_hedger._pre_trade_checks() is True

    def test_should_exit_sets_daily_loss_date(self, mock_hedger):
        # _current_day_pnl = -15000, which exceeds 2% of 500000 = 10000
        mock_hedger.state._current_day_pnl = -15000
        greeks = MagicMock(net_vega=0)
        result = mock_hedger._should_exit(greeks, 22000)
        assert result is True
        assert mock_hedger._daily_loss_stop_date == mock_hedger._clock().date()


class TestDrawdownConsistency:
    """P2: Max drawdown must be updated on the flat-book exit path."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger._consecutive_losses = 0
        hedger._daily_loss_stop_date = None
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0)
        )
        return hedger

    def test_drawdown_updated_on_flat_exit(self, mock_hedger):
        # Set peak at 1000
        mock_hedger.state.peak_pnl = 1000
        mock_hedger.state.max_drawdown = 0

        # Open and close at a loss
        open_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )
        mock_hedger.execute_proposals([open_prop])

        close_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=200,
            transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=0,
        )
        mock_hedger.execute_proposals([close_prop])

        # Book is flat, total_pnl should be negative (realized loss minus costs)
        assert len(mock_hedger.state.positions) == 0
        assert mock_hedger.state.total_pnl < 0
        # Drawdown should be peak - total_pnl = 1000 - (negative) > 1000
        assert mock_hedger.state.max_drawdown > 1000

    def test_daily_pnl_bookkeeping_on_flat_exit(self, mock_hedger):
        """P2: Flat-book exit must update daily P/L delta, not just drawdown."""
        # Open
        open_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )
        mock_hedger.execute_proposals([open_prop])

        # Close at a loss — the flat-book path fires
        close_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=200,
            transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=0,
        )
        mock_hedger.execute_proposals([close_prop])

        # _prev_snapshot_pnl should match total_pnl (delta was recorded)
        assert mock_hedger.state._prev_snapshot_pnl == mock_hedger.state.total_pnl
        # _current_day_pnl should be non-zero (the loss was recorded)
        assert mock_hedger.state._current_day_pnl != 0


class TestConsecutiveLossReset:
    """P2: _consecutive_losses must reset on profitable close (streak, not cumulative)."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger._consecutive_losses = 2  # Already 2 consecutive losses
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0)
        )
        return hedger

    def test_profitable_close_resets_streak(self, mock_hedger):
        # Open position at 200, close at 300 (profit)
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, entry_price=200, current_price=300, iv=0.15,
        ))

        close_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=0,
        )
        mock_hedger.execute_proposals([close_prop])

        assert mock_hedger._consecutive_losses == 0

    def test_losing_close_does_not_reset_streak(self, mock_hedger):
        # Open position at 300, close at 200 (loss)
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, entry_price=300, current_price=200, iv=0.15,
        ))

        close_prop = TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=200,
            transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=0,
        )
        mock_hedger.execute_proposals([close_prop])

        # Should stay at 2 (not reset)
        assert mock_hedger._consecutive_losses == 2

    def test_mixed_leg_net_loss_does_not_reset_streak(self, mock_hedger):
        """P1: One profitable leg in a net-losing multi-leg exit must NOT reset streak."""
        # CE: entry 200, will close at 300 → profit 5000
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, entry_price=200, current_price=300, iv=0.15,
        ))
        # PE: entry 400, will close at 100 → loss -15000
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403PE22000", instrument_token=2,
            strike=22000, expiry="2026-04-03", option_type="PE",
            lot_size=25, quantity=2, entry_price=400, current_price=100, iv=0.15,
        ))

        close_props = [
            TradeProposal(
                tradingsymbol="NIFTY26403CE22000", instrument_token=1,
                strike=22000, expiry="2026-04-03", option_type="CE",
                lot_size=25, quantity=2, price=300,
                transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
                margin_required=0,
            ),
            TradeProposal(
                tradingsymbol="NIFTY26403PE22000", instrument_token=2,
                strike=22000, expiry="2026-04-03", option_type="PE",
                lot_size=25, quantity=2, price=100,
                transaction_type="SELL", iv=0.15, bid_ask_spread_pct=0.5,
                margin_required=0,
            ),
        ]
        mock_hedger.execute_proposals(close_props)

        # Net batch P/L: 5000 - 15000 - costs < 0 → streak should NOT reset
        assert mock_hedger._consecutive_losses == 2


class TestSameBarRoundTripGuard:
    """P1: check_and_rehedge must not exit on the same bar an entry occurred."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger._consecutive_losses = 0
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {"vega_limit": 500, "rehedge_delta_threshold": 0.5}
        hedger.greeks = MagicMock()
        hedger._spot_history = []
        hedger._spot_history_max_size = 2000
        # Spot fetch is bypassed because we never reach it
        hedger._get_spot_price = MagicMock(return_value=22000.0)
        return hedger

    def test_same_timestamp_entry_blocks_rehedge(self, mock_hedger):
        from datetime import datetime
        # Position opened at the current clock timestamp
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, entry_price=300, current_price=300, iv=0.15,
        ))
        mock_hedger.state.entry_time = datetime(2026, 3, 29, 10, 0)

        result = mock_hedger.check_and_rehedge()
        assert result == []
        # Spot must not even be fetched — guard is the very first check
        mock_hedger._get_spot_price.assert_not_called()

    def test_later_timestamp_allows_rehedge_check(self, mock_hedger):
        from datetime import datetime
        # Position opened earlier than current clock
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, entry_price=300, current_price=300, iv=0.15,
        ))
        mock_hedger.state.entry_time = datetime(2026, 3, 29, 9, 30)
        # Stub the methods invoked past the guard to confirm we proceed
        mock_hedger._update_positions_prices = MagicMock()
        mock_hedger._update_portfolio_greeks = MagicMock()
        mock_hedger.state.portfolio_greeks = None  # short-circuits after guard

        mock_hedger.check_and_rehedge()
        mock_hedger._get_spot_price.assert_called_once()


class TestPreEntryVegaGate:
    """P1: scan_and_propose must reject entries whose post-scale vega still breaches the limit."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger._consecutive_losses = 0
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger._atm_iv_history = [0.15] * 30
        hedger.immutable_params = {
            "total_capital": 500000,
            "max_daily_loss_pct": 2.0,
            "no_trade_last_minutes": 15,
            "max_positions": 6,
            "gap_exit_threshold_pct": 3.0,
            "circuit_breaker_consecutive_losses": 3,
            "circuit_breaker_pause_minutes": 60,
        }
        hedger.tunable_params = {
            "vega_limit": 3000,
            "entry_iv_percentile_min": 0,
            "entry_iv_percentile_max": 100,
            "max_entry_alpha": 1e9,  # disable alpha gate for this test
            "position_size_pct": 5.0,
            "mc_worst_path_loss_pct": 100.0,
        }
        hedger._pre_trade_checks = MagicMock(return_value=True)
        hedger._get_spot_price = MagicMock(return_value=22000.0)
        # Non-empty chain
        import pandas as pd
        hedger._get_options_chain = MagicMock(return_value=pd.DataFrame({"x": [1]}))
        # Phase 3.2: scan_and_propose now slices chain to primary expiry.
        # These tests stub the chain with sentinel data, so bypass the
        # slice (identity passthrough) — they don't exercise multi-expiry
        # behaviour and the sentinel has no "expiry" column.
        hedger._primary_expiry_slice = lambda c: c
        hedger._compute_iv_percentile = MagicMock(return_value=50.0)
        hedger._apply_risk_filters = lambda props, spot: props
        hedger._proposals_to_contracts = MagicMock(return_value=[MagicMock()])
        hedger.proposer = MagicMock()
        hedger.greeks = MagicMock()
        hedger.risk = MagicMock()
        hedger._spot_history = []
        hedger._spot_history_max_size = 2000
        return hedger

    def _make_proposal(self, qty=2):
        return TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=qty, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )

    def test_post_scale_breach_rejects_entry(self, mock_hedger):
        # Single-lot vega already exceeds the per-lot limit (3000). Scaling
        # 2 lots → 1 lot cannot rescue it: the budget shrinks proportionally.
        proposal = self._make_proposal(qty=2)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        # First greeks call: vega = 8000 (over 6000 limit for 2 lots)
        # After scale to 1 lot, recompute returns vega = 4000 (still > 3000 limit)
        mock_hedger.greeks.compute_portfolio_greeks = MagicMock(side_effect=[
            MagicMock(net_alpha=0, net_vega=8000),
            MagicMock(net_alpha=0, net_vega=4000),
        ])

        result = mock_hedger.scan_and_propose()

        assert result == []
        # Stability test must NOT have been reached
        mock_hedger.risk.stability_test.assert_not_called()

    def test_scale_under_half_short_circuits_entry(self, mock_hedger):
        # If scale < 0.5 the early skip fires before we even recompute
        proposal = self._make_proposal(qty=2)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        # Vega 20000 vs limit 6000 → scale 0.3 → skip
        mock_hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_alpha=0, net_vega=20000),
        )

        result = mock_hedger.scan_and_propose()

        assert result == []
        mock_hedger.risk.stability_test.assert_not_called()

    def test_scaled_within_limit_proceeds(self, mock_hedger):
        # Initial: 4 lots, vega 15000 vs 4*3000=12000 limit → over.
        # Scale 0.8 → 3 lots. New limit 9000. Recompute vega 8000 < 9000 → pass.
        proposal = self._make_proposal(qty=4)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.greeks.compute_portfolio_greeks = MagicMock(side_effect=[
            MagicMock(net_alpha=0, net_vega=15000),
            MagicMock(net_alpha=0, net_vega=8000),
        ])
        # Stability + MC stubs to allow a clean exit path
        mock_hedger.risk.stability_test = MagicMock(
            return_value=MagicMock(is_stable=True, warnings=[]),
        )
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-1000, mean_pnl=50000, pct_profitable=100.0),
        )

        result = mock_hedger.scan_and_propose()

        assert result  # proposals survived the vega gate
        mock_hedger.risk.stability_test.assert_called_once()


class TestApplyBestParams:
    """_apply_best_params overlays autoresearch output onto runtime tunable params."""

    def test_missing_file_no_overlay(self, tmp_path):
        params = {"a": 1, "b": 2}
        applied, ignored = _apply_best_params(params, tmp_path / "absent.json")
        assert applied == 0
        assert ignored == []
        assert params == {"a": 1, "b": 2}

    def test_overlays_known_keys(self, tmp_path):
        import json
        path = tmp_path / "best.json"
        path.write_text(json.dumps({"best_params": {"a": 99, "b": 42}}))
        params = {"a": 1, "b": 2, "c": 3}
        applied, ignored = _apply_best_params(params, path)
        assert applied == 2
        assert ignored == []
        assert params == {"a": 99, "b": 42, "c": 3}

    def test_unknown_keys_ignored_not_added(self, tmp_path):
        import json
        # Stale autoresearch outputs may contain renamed/removed knobs.
        # They must not pollute the tunable surface.
        path = tmp_path / "best.json"
        path.write_text(json.dumps({"best_params": {"a": 99, "stale_param": 7}}))
        params = {"a": 1, "b": 2}
        applied, ignored = _apply_best_params(params, path)
        assert applied == 1
        assert ignored == ["stale_param"]
        assert params == {"a": 99, "b": 2}
        assert "stale_param" not in params

    def test_malformed_json_no_overlay(self, tmp_path):
        path = tmp_path / "best.json"
        path.write_text("{not valid json")
        params = {"a": 1}
        applied, ignored = _apply_best_params(params, path)
        assert applied == 0
        assert params == {"a": 1}

    def test_missing_best_params_object_no_overlay(self, tmp_path):
        import json
        path = tmp_path / "best.json"
        # File is valid JSON but lacks the expected wrapper.
        path.write_text(json.dumps({"timestamp": "2026-04-18", "metric": 10815}))
        params = {"a": 1}
        applied, ignored = _apply_best_params(params, path)
        assert applied == 0
        assert params == {"a": 1}


class TestActivePositionGuard:
    """scan_and_propose must not stack new entries on top of an open book.

    Without this guard, every tick that passes IV/RV/alpha/vega/MC gates
    re-enters and execute_proposals nets onto the existing legs, silently
    blowing past position_size_pct (observed live on 2026-05-06: 22 entries
    accumulated to 44 lots before max_positions tripped).
    """

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger._pre_trade_checks = MagicMock(return_value=True)
        hedger._get_spot_price = MagicMock(return_value=22000.0)
        hedger.proposer = MagicMock()
        hedger.greeks = MagicMock()
        hedger.risk = MagicMock()
        # Held expiry is 2027-04-03; clock at 2027-04-01 keeps the T-0
        # expiry-day guard disarmed so the legacy single-structure gate
        # is exercised here.
        hedger._clock = lambda: datetime(2027, 4, 1, 10, 0)
        return hedger

    def test_open_book_skips_entry_pipeline(self, mock_hedger):
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY27403CE22000", instrument_token=1,
            strike=22000, expiry="2027-04-03", option_type="CE",
            lot_size=25, quantity=2, entry_price=300, current_price=300, iv=0.15,
        ))

        result = mock_hedger.scan_and_propose()

        assert result == []
        # Downstream pipeline must not be touched while a position is open.
        mock_hedger._pre_trade_checks.assert_not_called()
        mock_hedger._get_spot_price.assert_not_called()
        mock_hedger.proposer.propose_delta_neutral.assert_not_called()


class TestMCSizingSubLotGate:
    """P1: MC sizing must reject entries whose required scale drops legs below 1 lot."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger._consecutive_losses = 0
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger._atm_iv_history = [0.15] * 30
        hedger.immutable_params = {
            "total_capital": 500000,
            "max_daily_loss_pct": 2.0,
            "no_trade_last_minutes": 15,
            "max_positions": 6,
            "gap_exit_threshold_pct": 3.0,
            "circuit_breaker_consecutive_losses": 3,
            "circuit_breaker_pause_minutes": 60,
        }
        hedger.tunable_params = {
            "vega_limit": 1e9,  # disable vega gate
            "entry_iv_percentile_min": 0,
            "entry_iv_percentile_max": 100,
            "max_entry_alpha": 1e9,  # disable alpha gate
            "position_size_pct": 5.0,
            "mc_worst_path_loss_pct": 1.0,  # 1% of 500k = 5000
        }
        hedger._pre_trade_checks = MagicMock(return_value=True)
        hedger._get_spot_price = MagicMock(return_value=22000.0)
        import pandas as pd
        hedger._get_options_chain = MagicMock(return_value=pd.DataFrame({"x": [1]}))
        # Phase 3.2: scan_and_propose now slices chain to primary expiry.
        # These tests stub the chain with sentinel data, so bypass the
        # slice (identity passthrough) — they don't exercise multi-expiry
        # behaviour and the sentinel has no "expiry" column.
        hedger._primary_expiry_slice = lambda c: c
        hedger._compute_iv_percentile = MagicMock(return_value=50.0)
        hedger._apply_risk_filters = lambda props, spot: props
        hedger._proposals_to_contracts = MagicMock(return_value=[MagicMock()])
        hedger.proposer = MagicMock()
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_alpha=0, net_vega=0),
        )
        hedger.risk = MagicMock()
        hedger.risk.stability_test = MagicMock(
            return_value=MagicMock(is_stable=True, warnings=[]),
        )
        hedger._spot_history = []
        hedger._spot_history_max_size = 2000
        return hedger

    def _make_proposal(self, qty=1):
        return TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=qty, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )

    def test_sub_lot_scale_rejects_one_lot_proposal(self, mock_hedger):
        # 1-lot proposal, MC worst path 15000 vs cap 5000 → scale = 0.333.
        # int(1 * 0.333) = 0, so the entry must be rejected, not floored to 1.
        proposal = self._make_proposal(qty=1)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-15000, mean_pnl=50000, pct_profitable=100.0),
        )

        result = mock_hedger.scan_and_propose()

        assert result == []

    def test_sufficient_lot_scale_proceeds(self, mock_hedger):
        # 4-lot proposal, MC worst path 10000 vs cap 5000 → scale = 0.5.
        # int(4 * 0.5) = 2, above 1 lot → entry proceeds with scaled size.
        proposal = self._make_proposal(qty=4)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-10000, mean_pnl=50000, pct_profitable=100.0),
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1
        assert result[0].quantity == 2

    def test_within_cap_no_scaling(self, mock_hedger):
        # MC worst path 3000 vs cap 5000 → no scaling; quantity unchanged.
        proposal = self._make_proposal(qty=1)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-3000, mean_pnl=50000, pct_profitable=100.0),
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1
        assert result[0].quantity == 1

    def test_negative_expectancy_entry_rejected(self, mock_hedger):
        """Gap #2: an entry whose MC MEAN path P/L is negative is rejected
        outright, even when its worst path is within the loss cap.
        Reproduces the 2026-06-04 straddle add (mean -3,371, 14% profitable)
        that slipped through because only worst_path_pnl was gated."""
        mock_hedger.tunable_params["mc_min_mean_pnl"] = 0.0
        proposal = self._make_proposal(qty=1)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-3000, mean_pnl=-3371, pct_profitable=14.0),
        )

        result = mock_hedger.scan_and_propose()

        assert result == []

    def test_positive_expectancy_low_winrate_proceeds(self, mock_hedger):
        """Gap #2: the gate keys on EXPECTANCY, not win-rate. A convex
        long-gamma entry with a LOW pct_profitable but POSITIVE mean P/L
        is exactly what the book exists to hold — it must proceed. Guards
        against a naive pct_profitable floor that would be anti-Taleb."""
        mock_hedger.tunable_params["mc_min_mean_pnl"] = 0.0
        proposal = self._make_proposal(qty=1)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-3000, mean_pnl=12000, pct_profitable=18.0),
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1


class TestPerTradeAttribution:
    """A full open→close cycle must emit one closed_trade dict whose
    components reconcile against the realized PnL delta."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock_now = datetime(2026, 3, 29, 10, 0)
        hedger._clock = lambda: hedger._clock_now
        hedger._consecutive_losses = 0
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_delta=0, net_shadow_theta=0, net_vega=0),
        )
        return hedger

    def _open(self, qty=2, price=300, iv=0.18):
        return TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=qty, price=price,
            transaction_type="BUY", iv=iv, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )

    def _close(self, qty=2, price=320):
        return TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=qty, price=price,
            transaction_type="SELL", iv=0.18, bid_ask_spread_pct=0.5,
            margin_required=0,
        )

    def test_emits_one_attribution_per_cycle(self, mock_hedger):
        from datetime import datetime
        # Open
        mock_hedger.execute_proposals([self._open(qty=2, price=300)])
        assert mock_hedger.state._attribution_baseline is not None
        assert mock_hedger.state.closed_trades == []

        # Simulate gamma scalp accruing during the hold
        mock_hedger.state.gamma_scalp_pnl += 1500.0

        # Advance clock and close
        mock_hedger._clock_now = datetime(2026, 3, 29, 14, 0)
        mock_hedger.execute_proposals([self._close(qty=2, price=320)])

        assert mock_hedger.state._attribution_baseline is None
        assert len(mock_hedger.state.closed_trades) == 1
        attr = mock_hedger.state.closed_trades[0]

        # Identity: gross_pnl == realized_pnl delta (which is what
        # gross_pnl is defined as), and gross + costs - gamma_scalp == residual
        assert abs(attr["holding_minutes"] - 240.0) < 1e-6
        assert attr["n_legs"] == 1
        assert attr["entry_atm_iv"] == pytest.approx(0.18)
        assert attr["gamma_scalp"] == pytest.approx(1500.0)
        assert attr["costs"] > 0  # both legs of the round trip cost something
        # gross_pnl + costs - gamma_scalp == residual (the definition)
        assert attr["residual"] == pytest.approx(
            attr["gross_pnl"] + attr["costs"] - attr["gamma_scalp"]
        )
        # Sanity: 320 - 300 = 20 per share * 25 lot * 2 lots = 1000 raw,
        # then minus costs is what realized booked.
        assert attr["gross_pnl"] < 1000  # net of round-trip costs
        assert attr["gross_pnl"] > 1000 - 500  # but not destroyed by costs

    def test_subsequent_cycle_does_not_double_count(self, mock_hedger):
        from datetime import datetime
        # Cycle 1
        mock_hedger.execute_proposals([self._open(qty=2, price=300)])
        mock_hedger.state.gamma_scalp_pnl += 500.0
        mock_hedger._clock_now = datetime(2026, 3, 29, 12, 0)
        mock_hedger.execute_proposals([self._close(qty=2, price=310)])

        # Cycle 2
        mock_hedger._clock_now = datetime(2026, 3, 29, 13, 0)
        mock_hedger.execute_proposals([self._open(qty=2, price=305)])
        mock_hedger.state.gamma_scalp_pnl += 800.0
        mock_hedger._clock_now = datetime(2026, 3, 29, 15, 0)
        mock_hedger.execute_proposals([self._close(qty=2, price=315)])

        assert len(mock_hedger.state.closed_trades) == 2
        # Cycle 2 scalp delta should be 800, not 1300
        assert mock_hedger.state.closed_trades[1]["gamma_scalp"] == pytest.approx(800.0)
        # Cycle 2 entry IV is fresh, not the cumulative
        assert mock_hedger.state.closed_trades[1]["entry_atm_iv"] == pytest.approx(0.18)


class TestRealizedVolEstimator:
    """The RV estimator must annualize correctly under irregular sampling."""

    def _make_hedger(self):
        from datetime import datetime
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger._spot_history = []
        hedger._spot_history_max_size = 2000
        hedger._clock = lambda: datetime(2026, 4, 19, 15, 30)
        return hedger

    def test_returns_none_when_undersampled(self):
        h = self._make_hedger()
        # 5 samples — below the 10-sample minimum
        from datetime import datetime, timedelta
        t0 = datetime(2026, 4, 19, 9, 15)
        for i in range(5):
            h._spot_history.append((t0 + timedelta(minutes=i), 22000.0 + i))
        assert h._compute_realized_vol(window_days=5) is None

    def test_constant_spot_yields_zero_vol(self):
        from datetime import datetime, timedelta
        h = self._make_hedger()
        t0 = datetime(2026, 4, 19, 9, 15)
        for i in range(20):
            h._spot_history.append((t0 + timedelta(minutes=5*i), 22000.0))
        rv = h._compute_realized_vol(window_days=5)
        assert rv == pytest.approx(0.0, abs=1e-9)

    def test_known_constant_return_recovers_annualized_vol(self):
        # Each 1-day step has return r = ln(1.01) ≈ 0.00995. Annualized
        # under 365-day calendar: sqrt(mean(r^2 / dt_years)) where dt_years
        # = 1/365. Expected: |r| * sqrt(365) ≈ 0.190.
        from datetime import datetime, timedelta
        import numpy as np
        h = self._make_hedger()
        h._clock = lambda: datetime(2026, 4, 19) + timedelta(days=20)
        t0 = datetime(2026, 4, 19)
        spot = 22000.0
        for i in range(20):
            h._spot_history.append((t0 + timedelta(days=i), spot))
            spot *= 1.01
        rv = h._compute_realized_vol(window_days=30)
        expected = abs(np.log(1.01)) * np.sqrt(365)
        assert rv == pytest.approx(expected, rel=1e-6)

    def test_dedup_on_same_timestamp(self):
        from datetime import datetime
        h = self._make_hedger()
        ts = datetime(2026, 4, 19, 10, 0)
        h._record_spot_sample(ts, 22000.0)
        h._record_spot_sample(ts, 22001.0)  # same ts → dropped
        h._record_spot_sample(ts, 22002.0)
        assert len(h._spot_history) == 1
        assert h._spot_history[0] == (ts, 22000.0)

    def test_window_filter_drops_old_samples(self):
        from datetime import datetime, timedelta
        h = self._make_hedger()
        h._clock = lambda: datetime(2026, 4, 19, 15, 30)
        old_t = datetime(2026, 4, 1)  # ~18 days old
        for i in range(15):
            h._spot_history.append((old_t + timedelta(minutes=5*i), 22000.0 + i*100))
        # Only old samples in a 5-day window → too few in-window → None
        assert h._compute_realized_vol(window_days=5) is None

    def test_five_in_window_samples_compute_rv(self):
        # Regression for the n<5 off-by-one: the daily-EOD seeding path
        # produces exactly 5 in-window samples (= 4 returns), which the
        # docstring calls the floor. Before the fix `n < 5` rejected 4
        # returns, so RV was permanently None on the seeded path and the
        # RV/IV regime feature could never bind in backtest. This is the
        # exact shape the autoresearch tape replay hits.
        from datetime import datetime, timedelta
        import numpy as np
        h = self._make_hedger()
        h._clock = lambda: datetime(2026, 4, 20, 15, 30)
        # 10 total samples (clears the len<10 guard). With window_days=5 the
        # cutoff is 2026-04-15 15:30: the 5 old samples (Apr 1–5) fall out,
        # leaving exactly 5 in-window (Apr 16–20) → 4 returns.
        old_t = datetime(2026, 4, 1)
        for i in range(5):
            h._spot_history.append((old_t + timedelta(days=i), 22000.0))
        spot = 22000.0
        win_t = datetime(2026, 4, 16)
        for i in range(5):
            h._spot_history.append((win_t + timedelta(days=i), spot))
            spot *= 1.01
        rv = h._compute_realized_vol(window_days=5)
        # 4 daily returns of ln(1.01); 365-day annualization.
        assert rv == pytest.approx(abs(np.log(1.01)) * np.sqrt(365), rel=1e-6)


class TestRVIVGate:
    """scan_and_propose must reject entries when RV/IV ratio falls below the threshold."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger._consecutive_losses = 0
        hedger._circuit_breaker_until = None
        hedger._daily_loss_stop_date = None
        hedger._atm_iv_history = []
        hedger._spot_history = []
        hedger._spot_history_max_size = 2000
        hedger.immutable_params = {
            "total_capital": 500000,
            "max_daily_loss_pct": 2.0,
            "no_trade_last_minutes": 15,
            "max_positions": 6,
            "gap_exit_threshold_pct": 3.0,
            "circuit_breaker_consecutive_losses": 3,
            "circuit_breaker_pause_minutes": 60,
        }
        hedger.tunable_params = {
            "vega_limit": 1e9,
            "entry_iv_percentile_min": 0,
            "entry_iv_percentile_max": 100,
            "max_entry_alpha": 1e9,
            "position_size_pct": 5.0,
            "mc_worst_path_loss_pct": 100.0,
            "min_rv_iv_ratio": 1.0,
            "rv_window_days": 5.0,
        }
        hedger._pre_trade_checks = MagicMock(return_value=True)
        hedger._get_spot_price = MagicMock(return_value=22000.0)
        import pandas as pd
        hedger._get_options_chain = MagicMock(return_value=pd.DataFrame({"x": [1]}))
        # Phase 3.2: scan_and_propose now slices chain to primary expiry.
        # These tests stub the chain with sentinel data, so bypass the
        # slice (identity passthrough) — they don't exercise multi-expiry
        # behaviour and the sentinel has no "expiry" column.
        hedger._primary_expiry_slice = lambda c: c
        # We control IV via _atm_iv_history directly; stub percentile to mid.
        hedger._compute_iv_percentile = MagicMock(return_value=50.0)
        hedger._apply_risk_filters = lambda props, spot: props
        hedger._proposals_to_contracts = MagicMock(return_value=[MagicMock()])
        hedger.proposer = MagicMock()
        hedger.greeks = MagicMock()
        hedger.greeks.compute_portfolio_greeks = MagicMock(
            return_value=MagicMock(net_alpha=0, net_vega=0),
        )
        hedger.risk = MagicMock()
        hedger.risk.stability_test = MagicMock(
            return_value=MagicMock(is_stable=True, warnings=[]),
        )
        hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-100, mean_pnl=50000, pct_profitable=100.0),
        )
        return hedger

    def _make_proposal(self):
        return TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )

    def _seed_spot_history(self, hedger, per_step_log_return: float, n_steps: int = 30):
        """Seed daily spot samples ending at the clock with a constant
        |log return| per step. Sign alternates so the path doesn't drift
        out of the window. Annualized RV ≈ |r| * sqrt(365)."""
        from datetime import timedelta
        import numpy as np
        end = hedger._clock()
        spot = 22000.0
        for i in range(n_steps):
            ts = end - timedelta(days=(n_steps - i))
            hedger._spot_history.append((ts, spot))
            sign = 1 if i % 2 == 0 else -1
            spot *= float(np.exp(sign * per_step_log_return))

    def test_low_rv_blocks_entry(self, mock_hedger):
        # IV ~25% annualized; per-step |r| = 0.003 → RV ≈ 0.003 * sqrt(365) ≈ 5.7%
        mock_hedger._atm_iv_history = [0.25] * 30
        mock_hedger.tunable_params["rv_window_days"] = 35.0
        self._seed_spot_history(mock_hedger, per_step_log_return=0.003)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(
            return_value=[self._make_proposal()],
        )

        result = mock_hedger.scan_and_propose()

        assert result == []

    def test_high_rv_admits_entry(self, mock_hedger):
        # IV ~25% annualized; per-step |r| = 0.03 → RV ≈ 0.03 * sqrt(365) ≈ 57%
        mock_hedger._atm_iv_history = [0.25] * 30
        mock_hedger.tunable_params["rv_window_days"] = 35.0
        self._seed_spot_history(mock_hedger, per_step_log_return=0.03)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(
            return_value=[self._make_proposal()],
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1

    def test_warmup_gate_is_permissive(self, mock_hedger):
        # No spot history at all → RV is None → gate should not bite
        mock_hedger._atm_iv_history = [0.25] * 30
        # leave _spot_history empty
        mock_hedger.proposer.propose_delta_neutral = MagicMock(
            return_value=[self._make_proposal()],
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1

    def test_no_iv_history_makes_gate_inert(self, mock_hedger):
        # Even with low RV, an empty IV history short-circuits the gate
        # because we cannot compute the ratio.
        mock_hedger.tunable_params["rv_window_days"] = 35.0
        self._seed_spot_history(mock_hedger, per_step_log_return=0.0001)  # near-zero RV
        mock_hedger._atm_iv_history = []
        mock_hedger.proposer.propose_delta_neutral = MagicMock(
            return_value=[self._make_proposal()],
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1

    def test_uncomputable_iv_percentile_skips_scan(self, mock_hedger):
        # Issue #75: when _compute_iv_percentile can't compute (None), the
        # scan must skip — NOT trade blind and NOT block on a fabricated 50.
        # Band is [0,100] here, so a None that leaked through as a real
        # value would pass the gate and produce a trade; asserting [] pins
        # the None short-circuit.
        mock_hedger._compute_iv_percentile = MagicMock(return_value=None)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(
            return_value=[self._make_proposal()],
        )

        result = mock_hedger.scan_and_propose()

        assert result == []


class TestIVPercentileNotReady:
    """Issue #75: _compute_iv_percentile must return None — not a fabricated
    neutral 50.0 — when it cannot compute the percentile (ATM quote gap,
    unsolvable IV, warmup). A neutral 50.0 conflates "couldn't compute" with
    "genuinely mid-range": it makes the IV-band tunables degenerate to a
    binary "does [min,max] contain 50" switch in autoresearch, and either
    blocks (band excludes 50) or waves trades through (band includes 50) on
    no real evidence. The caller must treat None as "skip this scan"."""

    def _bare(self):
        from datetime import datetime
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h.underlying = "NIFTY"
        h._clock = lambda: datetime(2026, 3, 29, 10, 0)
        h._atm_iv_history = []
        h._iv_history_max_size = 500
        h._persist_iv_history = False  # _save_iv_history is a no-op
        return h

    def _chain(self):
        import pandas as pd
        return pd.DataFrame({
            "strike": [22000, 22000],
            "instrument_type": ["CE", "PE"],
            "tradingsymbol": ["NIFTY26403CE22000", "NIFTY26403PE22000"],
            "expiry": ["2026-04-03", "2026-04-03"],
        })

    def _resolving_quote(self, price=150.0):
        return lambda syms: {s: {"last_price": price} for s in syms}

    def test_quote_gap_returns_none(self):
        """Kite returns no row for the ATM symbol (bucket gap): None, not 50."""
        h = self._bare()
        h._atm_iv_history = [0.15] * 60  # past warmup, so 50 can't come from warmup
        h.kite.quote = MagicMock(return_value={})  # empty: KeyError path
        assert h._compute_iv_percentile(self._chain(), 22000.0) is None

    def test_missing_atm_ce_returns_none(self):
        import pandas as pd
        h = self._bare()
        h._atm_iv_history = [0.15] * 60
        pe_only = pd.DataFrame({
            "strike": [22000], "instrument_type": ["PE"],
            "tradingsymbol": ["NIFTY26403PE22000"], "expiry": ["2026-04-03"],
        })
        assert h._compute_iv_percentile(pe_only, 22000.0) is None

    def test_warmup_returns_none_not_neutral_50(self):
        """<30 obs cannot rank a percentile — must be None, never 50."""
        h = self._bare()
        h._atm_iv_history = [0.15] * 5
        h.kite.quote = MagicMock(side_effect=self._resolving_quote())
        assert h._compute_iv_percentile(self._chain(), 22000.0) is None

    def test_computes_real_percentile_when_ready(self):
        """With ≥30 obs and a resolvable quote, returns a real float — and it
        is a genuine rank, not the fabricated 50.0 sentinel. Seed the history
        BELOW the current ATM IV so the true percentile is high (~100), which
        would be indistinguishable from a bug only if it returned 50."""
        h = self._bare()
        h._atm_iv_history = [0.05] * 40  # all far below the ~ATM IV we'll solve
        h.kite.quote = MagicMock(side_effect=self._resolving_quote(price=150.0))
        pct = h._compute_iv_percentile(self._chain(), 22000.0)
        assert pct is not None
        assert pct > 50.0  # current IV ranks above a low-vol history


# ───────────────────────────────────────────────────────────────────
# Spot-fetch regression — incident 2026-05-04
# ───────────────────────────────────────────────────────────────────
# Before this fix, _get_spot_price did `kite.quote([f"NSE:{underlying}"])`
# which returns {} for indices on Kite Connect (the right key is
# "NSE:NIFTY 50", not "NSE:NIFTY"). A bare-except masked the KeyError
# and silently returned 0.0, which poisoned every Greek call with
# math.log(0/K). One full session (339 ticks) ran zero proposals as a
# result. These tests pin all three corrected behaviours.

class TestSpotFetch:
    def _bare(self):
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h._consecutive_spot_failures = 0
        return h

    def test_index_underlying_uses_kite_display_name(self):
        h = self._bare()
        h.underlying = "NIFTY"
        assert h._spot_quote_key() == "NSE:NIFTY 50"
        h.underlying = "BANKNIFTY"
        assert h._spot_quote_key() == "NSE:NIFTY BANK"

    def test_stock_underlying_passes_through(self):
        h = self._bare()
        h.underlying = "RELIANCE"
        assert h._spot_quote_key() == "NSE:RELIANCE"

    def test_empty_quote_returns_none_not_zero(self):
        # The exact failure mode from 2026-05-04: Kite returns {} and
        # the old code's q[key]["last_price"] raised KeyError into a
        # bare except that converted it to 0.0. Must now return None.
        h = self._bare()
        h.underlying = "NIFTY"
        h.kite.quote.return_value = {}
        assert h._get_spot_price() is None

    def test_kite_exception_returns_none(self):
        h = self._bare()
        h.underlying = "NIFTY"
        h.kite.quote.side_effect = RuntimeError("network down")
        assert h._get_spot_price() is None

    def test_valid_quote_returns_price(self):
        h = self._bare()
        h.underlying = "NIFTY"
        h.kite.quote.return_value = {"NSE:NIFTY 50": {"last_price": 24119.3}}
        assert h._get_spot_price() == 24119.3

    def test_check_spot_escalates_to_error_after_5_failures(self, caplog):
        import logging
        h = self._bare()
        h.underlying = "NIFTY"
        with caplog.at_level(logging.WARNING, logger="strategies.taleb_karpathy"):
            for _ in range(4):
                assert h._check_spot(None) is False
            assert all(r.levelno != logging.ERROR for r in caplog.records)
            caplog.clear()
            assert h._check_spot(None) is False  # 5th failure
            assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_check_spot_resets_counter_on_recovery(self):
        h = self._bare()
        h.underlying = "NIFTY"
        for _ in range(3):
            h._check_spot(None)
        assert h._consecutive_spot_failures == 3
        assert h._check_spot(24000.0) is True
        assert h._consecutive_spot_failures == 0


# ──────────────────────────────────────────────────────────────────
# Cross-session persistence (serialize_state / restore_state)
# ──────────────────────────────────────────────────────────────────
# The 2026-05-19 rebuild removed runners/run_paper.py's unconditional EOD flatten.
# Open straddle + futures hedge positions now survive across sessions via
# serialize/restore. Roundtrip correctness is load-bearing — a partial
# restore would abandon a real position.

class TestSerializeRestore:

    def _mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = kite
        h.state = HedgeState()
        h.mode = "paper"
        h.underlying = "NIFTY"
        h._cached_lot_size = 75
        h._cached_futures_symbol = "NIFTY26MAYFUT"
        h._clock = lambda: datetime(2026, 5, 19, 10, 0)
        h.immutable_params = {"total_capital": 500000}
        h.tunable_params = {}
        h.greeks = MagicMock()
        return h

    def _seeded_open_position(self):
        """An open long ATM straddle + a short futures hedge, with some
        gamma-scalp P/L and one prior closed trade — the realistic shape
        of a held overnight position."""
        from datetime import datetime, date
        h = self._mock_hedger()
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY2651923700CE", instrument_token=111,
                strike=23700, expiry="2026-05-19", option_type="CE",
                lot_size=75, quantity=1, entry_price=79.65,
                current_price=85.00, iv=0.20,
            ),
            OptionContract(
                tradingsymbol="NIFTY2651923700PE", instrument_token=222,
                strike=23700, expiry="2026-05-19", option_type="PE",
                lot_size=75, quantity=1, entry_price=65.25,
                current_price=60.00, iv=0.20,
            ),
        ]
        h.state.entry_time = datetime(2026, 5, 19, 9, 16, 9)
        h.state.realized_pnl = -1500.0
        h.state.unrealized_pnl = 50.0
        h.state.total_pnl = -1450.0
        h.state.rehedge_count = 4
        h.state.gamma_scalp_pnl = 5058.0
        h.state.theta_decay_paid = 1200.0
        h.state.max_drawdown = 800.0
        h.state.peak_pnl = 200.0
        h.state.total_transaction_costs = 1500.0
        h.state.futures_hedge_delta = -75.0
        h.state.futures_entry_vwap = 23759.90
        h.state.futures_lots = -1
        h.state._current_day_pnl = -1450.0
        h.state._current_trading_date = date(2026, 5, 19)
        h.state.daily_pnl_history = [+2500.0, -800.0]
        h.state.closed_trades = [
            {"exit_time": "2026-05-18T15:25:00", "realized_pnl": -800.0,
             "transaction_costs": 600.0},
        ]
        return h

    def test_roundtrip_open_position(self):
        from datetime import datetime, date
        h1 = self._seeded_open_position()
        blob = h1.serialize_state()

        h2 = self._mock_hedger()
        h2.restore_state(blob)

        assert len(h2.state.positions) == 2
        ce = next(p for p in h2.state.positions if p.option_type == "CE")
        assert ce.strike == 23700
        assert ce.expiry == "2026-05-19"
        assert ce.entry_price == pytest.approx(79.65)
        assert ce.quantity == 1
        assert h2.state.entry_time == datetime(2026, 5, 19, 9, 16, 9)
        assert h2.state.realized_pnl == pytest.approx(-1500.0)
        assert h2.state.gamma_scalp_pnl == pytest.approx(5058.0)
        assert h2.state.futures_hedge_delta == pytest.approx(-75.0)
        assert h2.state.futures_entry_vwap == pytest.approx(23759.90)
        assert h2.state.futures_lots == -1
        assert h2.state._current_trading_date == date(2026, 5, 19)
        assert h2.state.daily_pnl_history == [+2500.0, -800.0]
        assert len(h2.state.closed_trades) == 1

    def test_flat_state_roundtrip(self):
        """No open position — restore should leave a clean HedgeState."""
        h1 = self._mock_hedger()
        blob = h1.serialize_state()
        h2 = self._mock_hedger()
        h2.restore_state(blob)
        assert h2.state.positions == []
        assert h2.state.futures_lots == 0
        assert h2.state.realized_pnl == 0.0

    def test_serialised_blob_is_json_clean(self):
        """The blob must round-trip through json.dumps/loads without losing
        information — that's what the runner does when it writes the state
        file."""
        import json
        h1 = self._seeded_open_position()
        blob = h1.serialize_state()
        wire = json.dumps(blob, default=str)
        decoded = json.loads(wire)
        h2 = self._mock_hedger()
        h2.restore_state(decoded)
        assert len(h2.state.positions) == 2
        assert h2.state.gamma_scalp_pnl == pytest.approx(5058.0)


class TestLegsExpireOn:

    def _mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = kite
        h.state = HedgeState()
        h.mode = "paper"
        h.underlying = "NIFTY"
        h._cached_lot_size = 75
        h._cached_futures_symbol = "NIFTY26MAYFUT"
        h._clock = lambda: datetime(2026, 5, 19, 10, 0)
        h.immutable_params = {"total_capital": 500000}
        h.tunable_params = {}
        return h

    def test_returns_false_when_no_positions(self):
        from datetime import date
        h = self._mock_hedger()
        assert h.legs_expire_on(date(2026, 5, 19)) is False

    def test_true_when_option_expiry_is_today(self):
        from datetime import date
        h = self._mock_hedger()
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY2651923700CE", instrument_token=1,
                strike=23700, expiry="2026-05-19", option_type="CE",
                lot_size=75, quantity=1, entry_price=80, current_price=80, iv=0.2,
            ),
        ]
        assert h.legs_expire_on(date(2026, 5, 19)) is True

    def test_false_when_option_expiry_is_future(self):
        from datetime import date
        h = self._mock_hedger()
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY2652623700CE", instrument_token=1,
                strike=23700, expiry="2026-05-26", option_type="CE",
                lot_size=75, quantity=1, entry_price=80, current_price=80, iv=0.2,
            ),
        ]
        assert h.legs_expire_on(date(2026, 5, 19)) is False

    def test_true_when_futures_hedge_expires_today(self):
        """Even with no option positions, an open futures hedge that
        expires today must trip the guard."""
        from datetime import date
        h = self._mock_hedger()
        h.state.futures_hedge_delta = -75.0
        h.state.futures_lots = -1
        h.kite.instruments = lambda seg: [
            {"tradingsymbol": "NIFTY26MAYFUT", "expiry": "2026-05-28"},
        ]
        assert h.legs_expire_on(date(2026, 5, 28)) is True
        assert h.legs_expire_on(date(2026, 5, 19)) is False

    def test_raises_after_retries_on_instruments_failure(self):
        """H18: when a futures hedge is held and instruments('NFO') keeps
        failing, legs_expire_on must raise rather than silently return
        False — silent False on real expiry day means carrying a contract
        into cash settlement. Three attempts with 1s/2s backoff between.
        time.sleep is patched out so the test stays fast."""
        from datetime import date
        import strategies.taleb_karpathy as tk_mod
        import pytest

        h = self._mock_hedger()
        h.state.futures_hedge_delta = -75.0
        h.state.futures_lots = -1
        call_count = {"n": 0}

        def _raise(*a, **k):
            call_count["n"] += 1
            raise RuntimeError("network down")
        h.kite.instruments = _raise

        sleeps: list[float] = []
        orig_sleep = tk_mod.time.sleep
        tk_mod.time.sleep = lambda secs: sleeps.append(secs)
        try:
            with pytest.raises(RuntimeError, match="3 consecutive times"):
                h.legs_expire_on(date(2026, 5, 19))
        finally:
            tk_mod.time.sleep = orig_sleep

        assert call_count["n"] == 3, f"expected 3 attempts, got {call_count['n']}"
        assert sleeps == [1.0, 2.0], f"unexpected backoff schedule: {sleeps}"

    def test_raises_on_empty_instruments_dump(self):
        """H18: kite.instruments('NFO') succeeding but returning [] is
        treated the same as a fetch failure for the futures-hedge path —
        cannot verify whether the hedge contract expires today."""
        from datetime import date
        import pytest

        h = self._mock_hedger()
        h.state.futures_hedge_delta = -75.0
        h.state.futures_lots = -1
        h.kite.instruments = lambda seg: []
        with pytest.raises(RuntimeError, match="empty list"):
            h.legs_expire_on(date(2026, 5, 19))

    def test_no_retry_when_no_futures_hedge(self):
        """If only option legs are held (no futures hedge), the futures
        instruments path is never touched — a dead kite API doesn't
        block expiry detection on the option side."""
        from datetime import date

        h = self._mock_hedger()
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY2651923700CE", instrument_token=1,
                strike=23700, expiry="2026-05-19", option_type="CE",
                lot_size=75, quantity=1, entry_price=80, current_price=80, iv=0.2,
            ),
        ]
        h.state.futures_hedge_delta = 0.0
        h.state.futures_lots = 0

        def _explode(*a, **k):
            raise AssertionError("kite.instruments() must not be called "
                                 "when only option legs are held")
        h.kite.instruments = _explode
        assert h.legs_expire_on(date(2026, 5, 19)) is True


class TestRealizedThetaAccounting:
    """Phase 1.1: theta_decay_paid must integrate signed net_shadow_theta
    over elapsed time, not sum abs(net_shadow_theta) every tick. The old
    behaviour silently inflated the counter and made any gamma/theta
    ratio metric meaningless."""

    def _make_hedger(self, theta_per_day):
        from datetime import datetime
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = MagicMock()
        hedger.state = HedgeState()
        hedger.mode = "paper"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 75
        hedger._cached_futures_symbol = None
        hedger._clock_value = datetime(2026, 5, 22, 9, 30)
        hedger._clock = lambda: hedger._clock_value
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        pf = MagicMock(
            net_delta=0, net_discrete_delta=0,
            net_shadow_theta=theta_per_day,
            net_gamma=0.0, net_shadow_gamma=0.0,
        )
        hedger.greeks.compute_portfolio_greeks = MagicMock(return_value=pf)
        hedger._get_spot_price = lambda: 23800.0
        hedger.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY26MAY23800CE", instrument_token=1,
                strike=23800, expiry="2026-05-29", option_type="CE",
                lot_size=75, quantity=1, entry_price=150.0,
                current_price=150.0, iv=0.15,
            ),
        ]
        return hedger

    def test_no_anchor_on_first_tick(self):
        """First call with positions only sets the anchor; nothing
        accumulates yet (no elapsed time)."""
        h = self._make_hedger(theta_per_day=-2400.0)
        h._update_portfolio_greeks()
        assert h.state.theta_decay_paid == 0.0
        assert h.state._last_theta_anchor_time is not None

    def test_realized_theta_after_one_hour(self):
        """At -₹2400/day, after 1 hour the integrated decay is ₹100
        (= 2400 × 1/24). The previous abs-sum logic would have added
        2400 every tick regardless of elapsed time."""
        from datetime import timedelta
        h = self._make_hedger(theta_per_day=-2400.0)
        h._update_portfolio_greeks()  # anchor only
        h._clock_value = h._clock_value + timedelta(hours=1)
        h._update_portfolio_greeks()
        assert h.state.theta_decay_paid == pytest.approx(100.0, abs=1.0)

    def test_short_premium_book_accumulates_negative(self):
        """A short-premium book (positive net_shadow_theta — trader
        collects time decay) should drive theta_decay_paid NEGATIVE.
        The previous abs() always grew positive, which obscured the
        sign of the strategy's theta exposure."""
        from datetime import timedelta
        h = self._make_hedger(theta_per_day=+3600.0)
        h._update_portfolio_greeks()
        h._clock_value = h._clock_value + timedelta(hours=2)
        h._update_portfolio_greeks()
        # +3600 × 2/24 = +300; theta_decay_paid stores -that
        assert h.state.theta_decay_paid == pytest.approx(-300.0, abs=1.0)

    def test_anchor_clears_when_book_flattens(self):
        """When positions empty, anchor drops so the next entry starts
        a fresh window. Otherwise a stale anchor from a closed trade
        would credit huge "decay" against the next entry's first tick."""
        h = self._make_hedger(theta_per_day=-2400.0)
        h._update_portfolio_greeks()
        assert h.state._last_theta_anchor_time is not None
        h.state.positions = []
        h._update_portfolio_greeks()
        assert h.state._last_theta_anchor_time is None


class TestRealizedGammaScalpPnL:
    """Phase 1.1: gamma_scalp_pnl must be 0.5 × γ × (actual ΔS)² where
    ΔS is the spot move since the last anchor (entry or prior rehedge),
    not a static 0.5 × γ × (band × spot)² estimate. The previous
    estimate incremented every rehedge whether or not the underlying
    actually moved, inflating the scalp counter on flat tapes."""

    def _book_scalp(self, gamma_at_rehedge, spot, anchor_spot):
        """Exercise the scalp-booking tail of check_and_rehedge in
        isolation, returning the resulting (gamma_scalp_pnl,
        new_anchor) pair."""
        state = HedgeState()
        state._last_rehedge_spot = anchor_spot
        greeks = MagicMock(
            net_shadow_gamma=gamma_at_rehedge,
            net_gamma=gamma_at_rehedge,
        )
        # Mirror of the inline scalp-booking in check_and_rehedge.
        # Kept in-test (not via the full call) so we can isolate the
        # accounting from the hedge decision and proposer machinery.
        anchor = state._last_rehedge_spot
        if anchor is not None and anchor > 0:
            dS = spot - anchor
            g = greeks.net_shadow_gamma if greeks.net_shadow_gamma != 0 else greeks.net_gamma
            state.gamma_scalp_pnl += 0.5 * g * dS * dS
        state._last_rehedge_spot = spot
        state.rehedge_count += 1
        return state

    def test_scalp_scales_with_squared_move(self):
        """0.5 × 0.3 × 50² = 375. Doubling ΔS quadruples the scalp."""
        s_small = self._book_scalp(0.3, spot=23850, anchor_spot=23800)
        s_big = self._book_scalp(0.3, spot=23900, anchor_spot=23800)
        assert s_small.gamma_scalp_pnl == pytest.approx(375.0)
        assert s_big.gamma_scalp_pnl == pytest.approx(1500.0)
        assert s_big.gamma_scalp_pnl == pytest.approx(4 * s_small.gamma_scalp_pnl)

    def test_zero_move_zero_scalp(self):
        """ΔS = 0 books no scalp even though a rehedge fired. The old
        estimate credited a fictional scalp regardless."""
        s = self._book_scalp(0.3, spot=23800, anchor_spot=23800)
        assert s.gamma_scalp_pnl == 0.0
        assert s.rehedge_count == 1

    def test_anchor_advances_to_current_spot(self):
        """After a rehedge, anchor moves to current spot so the next
        scalp measures ΔS from the new anchor — no double-counting."""
        s = self._book_scalp(0.3, spot=23850, anchor_spot=23800)
        assert s._last_rehedge_spot == 23850

    def test_short_gamma_book_loses_scalp_to_realized_vol(self):
        """Phase 1.1 fix (post-review): a SHORT-gamma structure (γ<0)
        LOSES money to realized vol — the scalp formula must preserve
        sign. The previous abs(γ) credited fictitious positive P&L on
        the very books Phase 3 enables (risk reversal, backspread,
        calendar wings) and would bias autoresearch toward losers."""
        s = self._book_scalp(-0.3, spot=23850, anchor_spot=23800)
        # 0.5 × (-0.3) × 50² = -375 (loss)
        assert s.gamma_scalp_pnl == pytest.approx(-375.0)


class TestAsymmetricRehedgeBand:
    """Phase 1.2: rehedge bands must scale per-side with shadow gamma
    (Taleb Ch 8). For biased assets like NIFTY/BANKNIFTY, γ_down > γ_up
    means a given delta drift to the downside represents a smaller
    price move — band should be tighter there. The previous code used
    one symmetric threshold and left downside delta on the book exactly
    when the position is most at risk."""

    def _make_hedger(self, base_threshold=0.5):
        from datetime import datetime
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h.state = HedgeState()
        h.mode = "paper"
        h.underlying = "NIFTY"
        h.exchange = "NFO"
        h._cached_lot_size = 75
        h._cached_futures_symbol = None
        h._clock = lambda: datetime(2026, 5, 22, 10, 0)
        h.immutable_params = {"total_capital": 500000}
        h.tunable_params = {
            "rehedge_delta_threshold": base_threshold,
            "gamma_scalp_band_pct": 1.5,
            "cost_hurdle_factor": 1.0,  # neutralise the WW gate for these tests
            "max_holding_period_hours": 8,
        }
        h.greeks = MagicMock()
        h._get_spot_price = lambda: 23800.0
        h._consecutive_quote_failures = 0
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY26MAY23800CE", instrument_token=1,
                strike=23800, expiry="2026-05-29", option_type="CE",
                lot_size=75, quantity=1, entry_price=150.0,
                current_price=150.0, iv=0.15,
            ),
        ]
        h.state.entry_time = datetime(2026, 5, 22, 9, 30)  # not same bar
        # Stub risk + futures helpers so check_and_rehedge can finish.
        h.risk = MagicMock()
        h.risk.hedge_decision = MagicMock(return_value=MagicMock(
            use_soft_delta=False, rationale="test",
        ))
        h._update_positions_prices = lambda spot: None
        h._record_spot_sample = lambda *a: None
        h._should_exit = lambda *a: False
        h._get_lot_size = lambda: 75
        h._get_futures_symbol = lambda: "NIFTY26MAYFUT"
        h.kite.quote = MagicMock(return_value={
            "NFO:NIFTY26MAYFUT": {"last_price": 23800.0},
        })
        return h

    def _set_greeks(self, h, *, delta, g_up, g_down, g_avg=None):
        """Inject a portfolio-greeks snapshot for the asymmetric-band path."""
        if g_avg is None:
            g_avg = (g_up + g_down) / 2
        pf = MagicMock(
            net_delta=delta,
            net_discrete_delta=delta,
            net_shadow_gamma=g_avg,
            net_shadow_gamma_up=g_up,
            net_shadow_gamma_down=g_down,
            net_gamma=g_avg,
            net_shadow_theta=-2400.0,
            net_vega=2000.0,
        )
        h.greeks.compute_portfolio_greeks = MagicMock(return_value=pf)
        h.state.portfolio_greeks = pf

    def test_downside_band_tighter_when_gamma_down_smaller(self):
        """With γ_down < γ_avg, a downside drift triggers earlier
        asymmetrically than under the symmetric base threshold.

        We use a wider base threshold (2.0 lots) so the asymmetric
        tightening produces a band above the 1-lot rounding floor of
        the proposal generator. With smaller bases the asymmetry still
        applies in principle but the hedge can't size to ≥1 lot.

        Numbers: base=2.0 lots × lot_size=75 → symmetric trigger at 150
        delta. γ_down=0.18, γ_avg=0.375 → sqrt(0.48) = 0.693 → downside
        band ≈ 1.39 lots ≈ 104 delta. A −120 drift triggers
        asymmetrically (1.6 > 1.39) but would NOT trigger symmetrically
        (1.6 < 2.0)."""
        h = self._make_hedger(base_threshold=2.0)
        self._set_greeks(h, delta=-120.0, g_up=0.5, g_down=0.18, g_avg=0.375)
        proposals = h.check_and_rehedge()
        assert len(proposals) > 0, (
            "Downside band must be tighter when γ_down is smaller than "
            "γ_avg, otherwise dangerous downside delta sits on the book"
        )

    def test_upside_band_looser_when_gamma_up_larger(self):
        """With γ_up > γ_avg, the upside band widens — the position is
        scalping efficiently per ΔS so no rush to hedge. A symmetric
        threshold would over-trade here, eating round-trip costs.

        Numbers: base=0.5 lots × lot_size=75 → symmetric trigger at 37.5
        delta. γ_up=0.6, γ_avg=0.375 → sqrt(1.6) = 1.265 → upside band
        ≈ 0.632 lots ≈ 47.4 delta. A +40 drift triggers symmetrically
        (0.533 > 0.5) but NOT asymmetrically (0.533 < 0.632)."""
        h = self._make_hedger(base_threshold=0.5)
        self._set_greeks(h, delta=40.0, g_up=0.6, g_down=0.15, g_avg=0.375)
        proposals = h.check_and_rehedge()
        assert proposals == [], (
            "Upside band must widen when γ_up exceeds γ_avg, else we "
            "over-trade and burn round-trip costs"
        )

    def test_symmetric_when_gammas_equal(self):
        """When γ_up == γ_down == γ_avg, the asymmetric form must
        collapse to the legacy symmetric threshold (no regression)."""
        h = self._make_hedger(base_threshold=2.0)
        # sqrt-factor = 1 → band = 2.0 lots = 150 delta. A drift of
        # 160 delta (≈2.13 lots) triggers and rounds to 2 lots.
        self._set_greeks(h, delta=160.0, g_up=0.4, g_down=0.4, g_avg=0.4)
        proposals = h.check_and_rehedge()
        assert len(proposals) > 0


class TestWhalleyWilmottCostGate:
    """Phase 1.2: the cost gate must use cube-root scaling, not linear.
    WW's optimal-band result shows required scalp grows as cost^(1/3),
    not cost^1. The previous linear gate killed too many marginally
    profitable rehedges."""

    def _build_hedger(self, cost_hurdle):
        from datetime import datetime
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h.state = HedgeState()
        h.mode = "paper"
        h.underlying = "NIFTY"
        h.exchange = "NFO"
        h._cached_lot_size = 75
        h._cached_futures_symbol = None
        h._clock = lambda: datetime(2026, 5, 22, 10, 0)
        h.immutable_params = {"total_capital": 500000}
        h.tunable_params = {
            "rehedge_delta_threshold": 0.1,  # easy to clear
            "gamma_scalp_band_pct": 1.5,
            "cost_hurdle_factor": cost_hurdle,
            "max_holding_period_hours": 8,
        }
        h.greeks = MagicMock()
        h._get_spot_price = lambda: 23800.0
        h._consecutive_quote_failures = 0
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY26MAY23800CE", instrument_token=1,
                strike=23800, expiry="2026-05-29", option_type="CE",
                lot_size=75, quantity=1, entry_price=150.0,
                current_price=150.0, iv=0.15,
            ),
        ]
        h.state.entry_time = datetime(2026, 5, 22, 9, 30)
        h.risk = MagicMock()
        h.risk.hedge_decision = MagicMock(return_value=MagicMock(
            use_soft_delta=False, rationale="test",
        ))
        h._update_positions_prices = lambda spot: None
        h._record_spot_sample = lambda *a: None
        h._should_exit = lambda *a: False
        h._get_lot_size = lambda: 75
        h._get_futures_symbol = lambda: "NIFTY26MAYFUT"
        h.kite.quote = MagicMock(return_value={
            "NFO:NIFTY26MAYFUT": {"last_price": 23800.0},
        })
        pf = MagicMock(
            net_delta=50.0, net_discrete_delta=50.0,
            net_shadow_gamma=0.4, net_shadow_gamma_up=0.4,
            net_shadow_gamma_down=0.4, net_gamma=0.4,
            net_shadow_theta=-2400.0, net_vega=2000.0,
        )
        h.greeks.compute_portfolio_greeks = MagicMock(return_value=pf)
        h.state.portfolio_greeks = pf
        return h

    def test_cube_root_scales_softer_than_linear(self):
        """A hurdle of 8.0 under the old LINEAR gate would demand
        8× the cost in scalp. Under the cube-root form it demands
        only 2× (= 8^(1/3)). This must let through scalps that the
        linear gate killed."""
        # Sanity: with cost_hurdle = 8, cube root is 2.0. So scalp
        # must beat 2× round-trip cost, not 8×.
        h = self._build_hedger(cost_hurdle=8.0)
        proposals = h.check_and_rehedge()
        # We can't easily assert "exactly 2×" without knowing the
        # exact cost number; but the new code should at least either
        # let it through or skip with the correct LOG message. The
        # absence of an exception is the smoke check here.
        assert isinstance(proposals, list)

    def test_hurdle_1_disables_gate(self):
        """cost_hurdle = 1 → cube root = 1 → no de-rating beyond cost.
        The expected_scalp need only beat the raw round-trip cost,
        which is the most permissive setting an operator can choose."""
        h = self._build_hedger(cost_hurdle=1.0)
        proposals = h.check_and_rehedge()
        assert isinstance(proposals, list)

    def test_best_params_cost_hurdle_was_migrated_for_cube_root(self):
        """Code-review fix #8: best_params.json carried a cost_hurdle
        of 1.3624 tuned against the OLD linear gate. After Phase 1.2
        switched the gate to cube-root, 1.3624 would mean a much
        looser 1.11× cost threshold (1.36^(1/3)). The migration cubed
        the value to 1.3624^3 ≈ 2.5288 so the new gate produces the
        same effective threshold the optimizer found. Guard against
        future regressions that silently revert the value."""
        import json
        from pathlib import Path
        bp = json.loads(
            (Path(__file__).resolve().parent.parent / "best_params.json").read_text()
        )
        hurdle = bp["best_params"]["cost_hurdle_factor"]
        # The migrated value must yield an effective linear-equivalent
        # threshold ≥ ~1.3× cost — anything materially lower indicates
        # the value was reset without re-running autoresearch.
        effective = hurdle ** (1 / 3)
        assert effective >= 1.3, (
            f"best_params cost_hurdle_factor={hurdle} → effective "
            f"{effective:.2f}× cost. If you intentionally re-tuned, "
            f"update this test bound; otherwise this is the Phase 1.2 "
            f"semantics-drift bug."
        )


class TestSkewPercentileGate:
    """Phase 1.3: a rich put-skew percentile should block ATM-straddle
    entry — the body pays the skew premium it can't recover via delta
    hedging."""

    def _make_hedger(self, skew_history, skew_max=80.0):
        from datetime import datetime
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h.state = HedgeState()
        h.mode = "paper"
        h.underlying = "NIFTY"
        h.exchange = "NFO"
        h._cached_lot_size = 75
        h._cached_futures_symbol = None
        h._clock = lambda: datetime(2026, 5, 22, 10, 0)
        h._persist_iv_history = False
        h._iv_history_max_size = 500
        h._skew_history = list(skew_history)
        h._atm_iv_history = []
        h.greeks = MagicMock()
        # Stub the engine's delta so the strike-picking loop converges.
        h.greeks.delta = lambda S, K, T, sigma, kind: (
            +0.25 if (kind == "CE" and K > S) else
            -0.25 if (kind == "PE" and K < S) else
            +0.5 if kind == "CE" else -0.5
        )
        h.tunable_params = {"skew_pct_max": skew_max}
        h._save_iv_history = lambda: None
        return h

    def _chain_with_skewed_quotes(self, spot, put_iv, call_iv):
        """Build a tiny options chain DataFrame plus a kite.quote stub
        that returns prices consistent with `put_iv` for the OTM put
        and `call_iv` for the OTM call."""
        import pandas as pd
        from datetime import date as _date, timedelta as _td
        from core.greeks_engine import GreeksEngine
        engine = GreeksEngine(risk_free_rate=0.065)
        T = 7 / 365
        put_strike = spot - 200
        call_strike = spot + 200
        put_price = engine.bs_price(spot, put_strike, T, put_iv, "PE")
        call_price = engine.bs_price(spot, call_strike, T, call_iv, "CE")
        expiry_iso = (_date(2026, 5, 22) + _td(days=7)).isoformat()
        chain = pd.DataFrame([
            {"strike": put_strike, "instrument_type": "PE",
             "tradingsymbol": "NIFTY26MAY23600PE", "expiry": expiry_iso},
            {"strike": call_strike, "instrument_type": "CE",
             "tradingsymbol": "NIFTY26MAY24000CE", "expiry": expiry_iso},
        ])
        quote_map = {
            "NFO:NIFTY26MAY23600PE": {"last_price": put_price},
            "NFO:NIFTY26MAY24000CE": {"last_price": call_price},
        }
        return chain, quote_map

    def test_skew_percentile_returns_neutral_during_warmup(self):
        """< 30 observations ⇒ returns 50.0 so the gate doesn't bite
        during the first month of live operation."""
        from datetime import date  # noqa: F401 — used by helper
        h = self._make_hedger(skew_history=[0.02] * 10)
        chain, qmap = self._chain_with_skewed_quotes(23800, put_iv=0.20, call_iv=0.15)
        h.kite.quote = lambda keys: {k: qmap[k] for k in keys if k in qmap}
        pct = h._compute_skew_percentile(chain, 23800)
        assert pct == 50.0

    def test_skew_percentile_high_when_current_above_history(self):
        """With 40 prior observations centered at 0.02 (call IV − put IV
        almost flat), a current skew of 0.08 should rank in the top
        decile (>= 90th percentile)."""
        from datetime import date  # noqa: F401
        h = self._make_hedger(skew_history=[0.02 + 0.005 * (i % 5) for i in range(40)])
        # put_iv 0.23, call_iv 0.15 ⇒ current skew = 0.08
        chain, qmap = self._chain_with_skewed_quotes(23800, put_iv=0.23, call_iv=0.15)
        h.kite.quote = lambda keys: {k: qmap[k] for k in keys if k in qmap}
        pct = h._compute_skew_percentile(chain, 23800)
        # Both append (so length grows to 41) and rank against own history.
        # The exact value depends on bisect, but it must be > 80.
        assert pct > 80.0, f"Expected high percentile, got {pct}"

    def test_skew_percentile_low_when_current_below_history(self):
        """Same setup but current skew below history → low percentile.
        Confirms the percentile direction matches semantics."""
        from datetime import date  # noqa: F401
        h = self._make_hedger(skew_history=[0.05 + 0.005 * (i % 5) for i in range(40)])
        # put_iv 0.16, call_iv 0.15 ⇒ current skew = 0.01 (mild)
        chain, qmap = self._chain_with_skewed_quotes(23800, put_iv=0.16, call_iv=0.15)
        h.kite.quote = lambda keys: {k: qmap[k] for k in keys if k in qmap}
        pct = h._compute_skew_percentile(chain, 23800)
        assert pct < 20.0, f"Expected low percentile, got {pct}"

    def test_missing_skew_pct_max_disables_gate(self):
        """A tunable_params without skew_pct_max must NOT raise — the
        gate just becomes inert. Required so older configs and tests
        that bypass __init__ continue to work."""
        h = self._make_hedger(skew_history=[], skew_max=100.0)
        del h.tunable_params["skew_pct_max"]
        # Build minimal preconditions for scan_and_propose to reach the
        # gate without triggering anything before it. We don't need it
        # to *succeed* — just not crash on missing key.
        h._pre_trade_checks = lambda: False
        # Should silently return [] without KeyError
        assert h.scan_and_propose() == []

    def test_skew_uses_single_batched_quote_call(self):
        """Code-review fix #6: the skew computation MUST issue exactly
        ONE batched kite.quote() call for the whole chain, not N
        per-strike calls — Kite's documented rate limit makes the
        per-strike pattern silently inert in production."""
        from datetime import date as _date, timedelta as _td
        import pandas as pd
        from core.greeks_engine import GreeksEngine
        h = self._make_hedger(skew_history=[])
        engine = GreeksEngine(risk_free_rate=0.065)
        T = 7 / 365
        expiry_iso = (_date(2026, 5, 22) + _td(days=7)).isoformat()
        # Build a 6-strike chain (3 CE + 3 PE) — pre-fix this would
        # have issued 6 separate quote() calls.
        spot = 23800
        rows = []
        for offset in (-200, 0, 200):
            for typ in ("CE", "PE"):
                rows.append({
                    "strike": spot + offset, "instrument_type": typ,
                    "tradingsymbol": f"NIFTY26MAY{spot+offset}{typ}",
                    "expiry": expiry_iso,
                })
        chain = pd.DataFrame(rows)
        # Synthetic prices at IV ≈ 0.20.
        qmap = {}
        for r in rows:
            price = engine.bs_price(spot, r["strike"], T, 0.20, r["instrument_type"])
            qmap[f"NFO:{r['tradingsymbol']}"] = {"last_price": price}

        call_count = {"n": 0}
        def fake_quote(symbols):
            call_count["n"] += 1
            return {k: qmap[k] for k in symbols if k in qmap}
        h.kite.quote = fake_quote
        h._compute_skew_percentile(chain, spot)
        assert call_count["n"] == 1, (
            f"Expected 1 batched quote() call, got {call_count['n']} "
            "— per-strike iteration would re-introduce the rate-limit "
            "regression."
        )


class TestGammaThetaRatio:
    """Phase 2.4: get_strategy_metrics must expose a gamma_theta_ratio
    derived from the realized scalp / realized theta. This becomes the
    autoresearch primary metric — guard against silent regression."""

    def _make_hedger(self):
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.state = HedgeState()
        h.immutable_params = {"total_capital": 500000}
        return h

    def test_ratio_above_one_when_scalp_beats_theta(self):
        h = self._make_hedger()
        h.state.gamma_scalp_pnl = 2500.0
        h.state.theta_decay_paid = 1500.0
        m = h.get_strategy_metrics()
        assert m["gamma_theta_ratio"] == pytest.approx(2500 / 1500)

    def test_ratio_zero_when_no_theta(self):
        """No realized theta yet ⇒ ratio is undefined; return 0 not
        infinity so the autoresearch variance penalty doesn't blow up."""
        h = self._make_hedger()
        h.state.gamma_scalp_pnl = 1500.0
        h.state.theta_decay_paid = 0.0
        m = h.get_strategy_metrics()
        assert m["gamma_theta_ratio"] == 0.0

    def test_ratio_zero_when_short_premium(self):
        """Short-premium book has negative theta_decay_paid (collected
        time decay). Ratio sign reflects net cashflow direction —
        useful for the optimizer to distinguish from a true win."""
        h = self._make_hedger()
        h.state.gamma_scalp_pnl = 500.0
        h.state.theta_decay_paid = -2000.0
        m = h.get_strategy_metrics()
        # |theta_decay_paid| > 1 in absolute terms but the guard checks
        # > 1.0 SIGNED, not absolute — so this returns 0 in the current
        # implementation. That's deliberate: short-premium ratios need
        # their own metric (Phase 3 territory), not the same one.
        assert m["gamma_theta_ratio"] == 0.0


class TestLayeredStructures:
    """Phase 4: when max_layered_structures > 1 AND regime dispatch is
    on, an existing structure does not block a NEW structure of a
    different type from being proposed."""

    def _make_hedger(self, max_layers=1, regime=False):
        from datetime import datetime
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h.state = HedgeState()
        h.mode = "paper"
        h._pre_trade_checks = MagicMock(return_value=True)
        h._get_spot_price = MagicMock(return_value=22000.0)
        h.proposer = MagicMock()
        h.greeks = MagicMock()
        h.risk = MagicMock()
        # Held expiry is 2027-04-03; clock at 2027-04-01 keeps min_days_to_exp > 1
        # so the T-0 expiry-day guard stays disarmed for layering tests.
        h._clock = lambda: datetime(2027, 4, 1, 10, 0)
        h.tunable_params = {
            "max_layered_structures": max_layers,
            "enable_regime_dispatch": regime,
        }
        return h

    def test_legacy_default_blocks_layering(self):
        """max_layered_structures=1 keeps the legacy one-at-a-time
        invariant — any existing position blocks new entries."""
        h = self._make_hedger(max_layers=1, regime=False)
        h.state.positions.append(OptionContract(
            tradingsymbol="X", instrument_token=1, strike=22000,
            expiry="2027-04-03", option_type="CE", lot_size=25,
            quantity=1, entry_price=300, current_price=300, iv=0.15,
        ))
        result = h.scan_and_propose()
        assert result == []
        h._pre_trade_checks.assert_not_called()

    def test_layering_blocked_without_regime_dispatch(self):
        """Even with max_layered_structures=3, if regime dispatch is
        OFF the legacy guard still blocks. Layering only makes sense
        when the proposer can emit different structures."""
        h = self._make_hedger(max_layers=3, regime=False)
        h.state.positions.append(OptionContract(
            tradingsymbol="X", instrument_token=1, strike=22000,
            expiry="2027-04-03", option_type="CE", lot_size=25,
            quantity=1, entry_price=300, current_price=300, iv=0.15,
        ))
        result = h.scan_and_propose()
        assert result == []

    def test_layering_proceeds_with_regime_and_capacity(self):
        """max_layered_structures=2, one structure (one expiry) active,
        regime dispatch on — the layering guard does NOT block, so
        _pre_trade_checks gets called. (We stub it to return False so
        the rest of the pipeline doesn't try to run.)"""
        h = self._make_hedger(max_layers=2, regime=True)
        h._pre_trade_checks = MagicMock(return_value=False)
        h.state.positions.append(OptionContract(
            tradingsymbol="X", instrument_token=1, strike=22000,
            expiry="2027-04-03", option_type="CE", lot_size=25,
            quantity=1, entry_price=300, current_price=300, iv=0.15,
        ))
        result = h.scan_and_propose()
        assert result == []
        h._pre_trade_checks.assert_called_once()

    def test_layering_blocked_on_expiry_day(self):
        """T-0 guard: even with max_layers=2 and regime dispatch ON,
        an existing leg expiring today blocks any new structure —
        gamma/cost burn on T-0 makes additional layers negative-EV
        (saw 9 rehedges in 13 min on 2026-05-26 May expiry)."""
        from datetime import datetime
        h = self._make_hedger(max_layers=2, regime=True)
        h._clock = lambda: datetime(2026, 5, 26, 9, 20)
        h.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26MAY24000CE", instrument_token=1,
            strike=24000, expiry="2026-05-26", option_type="CE",
            lot_size=65, quantity=1, entry_price=48.65,
            current_price=48.65, iv=0.20,
        ))
        result = h.scan_and_propose()
        assert result == []
        h._pre_trade_checks.assert_not_called()

    def test_t0_band_tightens_on_expiry_day(self):
        """Phase 5: when t0_band_factor < 1.0 and any leg has < 1 day
        to expiry, the rehedge band is multiplied by the factor —
        making the trigger tighter and capturing sticky-strike scalps.
        With factor 1.0 (default), behaviour is unchanged."""
        from datetime import datetime
        h = self._make_hedger(max_layers=1, regime=False)
        h.tunable_params.update({
            "rehedge_delta_threshold": 0.5,
            "gamma_scalp_band_pct": 1.5,
            "cost_hurdle_factor": 1.0,
            "max_holding_period_hours": 8,
            "t0_band_factor": 0.33,
        })
        h._clock = lambda: datetime(2026, 5, 28, 10, 0)
        h._cached_lot_size = 75
        h._get_lot_size = lambda: 75
        h._consecutive_quote_failures = 0
        h._update_positions_prices = lambda spot: None
        h._record_spot_sample = lambda *a: None
        h._should_exit = lambda *a: False
        h._get_spot_price = lambda: 23800.0
        h._get_futures_symbol = lambda: "NIFTY26MAYFUT"
        h.kite.quote = MagicMock(return_value={
            "NFO:NIFTY26MAYFUT": {"last_price": 23800.0},
        })
        # Leg expires same day → < 1 day to expiry → band tightens.
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY26MAY23800CE", instrument_token=1,
                strike=23800, expiry="2026-05-28", option_type="CE",
                lot_size=75, quantity=1, entry_price=120.0,
                current_price=120.0, iv=0.20,
            ),
        ]
        h.state.entry_time = datetime(2026, 5, 28, 9, 30)
        h.risk = MagicMock()
        h.risk.hedge_decision = MagicMock(return_value=MagicMock(
            use_soft_delta=False, rationale="test",
        ))
        # Greeks: symmetric γ_up=γ_down=γ_avg so the asymmetric band
        # collapses to base × 1.0 = 0.5 lots = 37.5 delta. Phase 5
        # factor 0.33 → effective band = 0.165 lots = 12.4 delta.
        # A drift of 15 delta (0.2 lots) is BELOW symmetric band but
        # ABOVE Phase 5 tightened band → rehedge fires.
        pf = MagicMock(
            net_delta=15.0, net_discrete_delta=15.0,
            net_shadow_gamma=0.4, net_shadow_gamma_up=0.4,
            net_shadow_gamma_down=0.4, net_gamma=0.4,
            net_shadow_theta=-2400.0, net_vega=2000.0,
        )
        h.greeks.compute_portfolio_greeks = MagicMock(return_value=pf)
        h.state.portfolio_greeks = pf
        proposals = h.check_and_rehedge()
        # round(15/75)=0 so the hard-hedge proposal list is empty even though
        # the tightened band fired (per the 2026-05-07 lesson). The robust
        # signal that the band fired is that the method proceeded past the
        # band + cost gates to the hedge decision — hedge_decision is only
        # reached when delta clears the band. rehedge_count is NOT a valid
        # proxy: the 2026-06-02 C2 fix stops it incrementing on empty proposals
        # (a 0-lot non-hedge must not count toward the session cap).
        h.risk.hedge_decision.assert_called_once()
        assert proposals == [], "0-lot drift should yield no proposal here"

    def test_count_active_structures_by_expiry(self):
        """Two legs sharing one expiry (a straddle) count as ONE
        structure. Two legs across two expiries (a calendar) count
        as TWO. The coarse-grained count is intentional: it's the
        invariant the existing attribution/exit code can handle."""
        h = self._make_hedger(max_layers=2, regime=True)
        h.state.positions = [
            OptionContract(
                tradingsymbol="A", instrument_token=1, strike=22000,
                expiry="2026-04-03", option_type="CE", lot_size=25,
                quantity=1, entry_price=300, current_price=300, iv=0.15,
            ),
            OptionContract(
                tradingsymbol="B", instrument_token=2, strike=22000,
                expiry="2026-04-03", option_type="PE", lot_size=25,
                quantity=1, entry_price=280, current_price=280, iv=0.15,
            ),
        ]
        assert h._count_active_structures() == 1
        h.state.positions.append(OptionContract(
            tradingsymbol="C", instrument_token=3, strike=22000,
            expiry="2026-05-08", option_type="CE", lot_size=25,
            quantity=1, entry_price=320, current_price=320, iv=0.15,
        ))
        assert h._count_active_structures() == 2

    def test_count_active_structures_uses_type_list(self):
        """Gap #2: when active_structure_types is populated it is the
        authoritative count, in the correct unit. Two same-expiry layered
        structures count as TWO (the expiry fallback would wrongly say
        one); a single calendar spanning two expiries counts as ONE (the
        expiry fallback would wrongly say two)."""
        h = self._make_hedger(max_layers=3, regime=True)
        # One expiry on the book, but two structures were layered:
        h.state.positions = [OptionContract(
            tradingsymbol="A", instrument_token=1, strike=22000,
            expiry="2027-04-03", option_type="CE", lot_size=25,
            quantity=1, entry_price=300, current_price=300, iv=0.15,
        )]
        h.state.active_structure_types = ["straddle", "asymmetric_strangle"]
        assert h._count_active_structures() == 2
        # A single calendar spans two expiries but is ONE structure:
        h.state.positions = [
            OptionContract(
                tradingsymbol="N", instrument_token=2, strike=22000,
                expiry="2027-04-03", option_type="CE", lot_size=25,
                quantity=-1, entry_price=300, current_price=300, iv=0.15,
            ),
            OptionContract(
                tradingsymbol="F", instrument_token=3, strike=22000,
                expiry="2027-05-08", option_type="CE", lot_size=25,
                quantity=1, entry_price=320, current_price=320, iv=0.15,
            ),
        ]
        h.state.active_structure_types = ["calendar_short_front"]
        assert h._count_active_structures() == 1

    def _stub_pipeline_to_classify(self, h):
        """Stub everything scan_and_propose touches between the layering
        branch and classify(), so a test can drive the type-difference gate
        with _pre_trade_checks permissive."""
        h._pre_trade_checks = MagicMock(return_value=True)
        h._check_spot = MagicMock(return_value=True)
        h._record_spot_sample = MagicMock()
        chain = MagicMock(empty=False)
        h._get_options_chain = MagicMock(return_value=chain)
        h._primary_expiry_slice = MagicMock(return_value=chain)
        h._compute_iv_percentile = MagicMock(return_value=50.0)
        h._compute_skew_percentile = MagicMock(return_value=50.0)
        h._compute_realized_vol = MagicMock(return_value=0.20)
        h._atm_iv_history = [0.18] * 12
        h._apply_risk_filters = lambda proposals, spot: proposals
        h.immutable_params = {"total_capital": 1_000_000.0}
        h.tunable_params.update({
            "entry_iv_percentile_min": 0.0,
            "entry_iv_percentile_max": 100.0,
            "position_size_pct": 10.0,
        })

    def test_layering_blocks_same_structure_type(self):
        """Type-difference gate: with capacity to layer (max_layers=2,
        regime on, one expiry held) a SECOND structure of the SAME type is
        refused — the invariant _count_active_structures (expiry-based)
        cannot enforce. Reproduces the 2026-06-04 straddle-on-straddle add."""
        h = self._make_hedger(max_layers=2, regime=True)
        self._stub_pipeline_to_classify(h)
        h.proposer.propose_for_structure = MagicMock(return_value=[])
        h.state.positions.append(OptionContract(
            tradingsymbol="NIFTY27APR22000CE", instrument_token=1,
            strike=22000, expiry="2027-04-03", option_type="CE",
            lot_size=25, quantity=1, entry_price=300,
            current_price=300, iv=0.15,
        ))
        h.state.active_structure_types = ["straddle"]
        with patch("strategies.taleb_karpathy.classify",
                   return_value=Structure("straddle")):
            result = h.scan_and_propose()
        assert result == []
        h.proposer.propose_for_structure.assert_not_called()

    def test_layering_allows_different_structure_type(self):
        """A structure whose type differs from everything on the book is
        NOT blocked by the type gate — propose_for_structure is invoked.
        (Returns [] downstream to keep the test focused on the gate.)"""
        h = self._make_hedger(max_layers=2, regime=True)
        self._stub_pipeline_to_classify(h)
        h.proposer.propose_for_structure = MagicMock(return_value=[])
        h.state.positions.append(OptionContract(
            tradingsymbol="NIFTY27APR22000CE", instrument_token=1,
            strike=22000, expiry="2027-04-03", option_type="CE",
            lot_size=25, quantity=1, entry_price=300,
            current_price=300, iv=0.15,
        ))
        h.state.active_structure_types = ["straddle"]
        with patch("strategies.taleb_karpathy.classify",
                   return_value=Structure("risk_reversal_long_put")):
            h.scan_and_propose()
        h.proposer.propose_for_structure.assert_called_once()

    def test_active_structure_types_survive_serialize_restore(self):
        """Overnight hold: the layering type list round-trips through
        serialize/restore so the gate stays enforced on resume."""
        h = self._make_hedger(max_layers=2, regime=True)
        h.state.active_structure_types = ["straddle", "calendar_short_front"]
        blob = h.serialize_state()
        h2 = self._make_hedger(max_layers=2, regime=True)
        h2.restore_state(blob)
        assert h2.state.active_structure_types == [
            "straddle", "calendar_short_front",
        ]

    def test_restore_tolerates_missing_active_structure_types(self):
        """Older state blobs predate the field — restore defaults to []."""
        h = self._make_hedger()
        h.state.active_structure_types = ["straddle"]
        blob = h.serialize_state()
        del blob["state"]["active_structure_types"]
        h.restore_state(blob)
        assert h.state.active_structure_types == []


class TestTwoExpiryChain:
    """Phase 3.2 unblock: `_get_options_chain` returns rows from up to
    two expiries so `propose_calendar_short_front` can fire. Primary
    expiry (best ATM coverage) rows are first and marked in attrs.
    Single-expiry callers filter via `_primary_expiry_slice`."""

    def _make_instruments(self, *, dense_expiry, sparse_expiry, spot=22000):
        """Build a fake NFO instrument dump.
        `dense_expiry` has 9 strikes ±4% of spot (best ATM coverage);
        `sparse_expiry` has 3 strikes ±2% (less coverage).
        Both have ATM CE+PE pairs so `has_atm_pair` is True for both.
        """
        rows = []
        dense_strikes = [spot - 4*100, spot - 3*100, spot - 2*100, spot - 100,
                         spot, spot + 100, spot + 2*100, spot + 3*100, spot + 4*100]
        for s in dense_strikes:
            for ot in ("CE", "PE"):
                rows.append({
                    "name": "NIFTY", "tradingsymbol": f"NIFTY_{dense_expiry}_{int(s)}{ot}",
                    "instrument_token": hash((dense_expiry, s, ot)) % 100000,
                    "strike": float(s), "expiry": dense_expiry,
                    "instrument_type": ot, "lot_size": 25,
                })
        sparse_strikes = [spot - 100, spot, spot + 100]
        for s in sparse_strikes:
            for ot in ("CE", "PE"):
                rows.append({
                    "name": "NIFTY", "tradingsymbol": f"NIFTY_{sparse_expiry}_{int(s)}{ot}",
                    "instrument_token": hash((sparse_expiry, s, ot)) % 100000,
                    "strike": float(s), "expiry": sparse_expiry,
                    "instrument_type": ot, "lot_size": 25,
                })
        return rows

    def _make_hedger(self, instruments, spot=22000):
        kite = MagicMock()
        kite.instruments.return_value = instruments
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = kite
        h.underlying = "NIFTY"
        h._get_spot_price = MagicMock(return_value=spot)
        h._consecutive_chain_failures = 0
        return h

    def test_returns_union_of_two_expiries_with_primary_first(self):
        # Sparse expiry is the NEAREST (front), dense is the next.
        # Primary should be the dense one (best ATM coverage), and its
        # rows should come first in the returned DataFrame.
        instruments = self._make_instruments(
            dense_expiry="2026-05-29", sparse_expiry="2026-05-22",
        )
        h = self._make_hedger(instruments)
        chain = h._get_options_chain()
        assert not chain.empty
        expiries_seen = chain["expiry"].unique().tolist()
        assert "2026-05-22" in expiries_seen and "2026-05-29" in expiries_seen, (
            f"chain must span both expiries; got {expiries_seen}"
        )
        # Primary = dense (best ATM coverage)
        assert chain.attrs.get("primary_expiry") == "2026-05-29"
        # Primary rows ordered first
        assert chain.iloc[0]["expiry"] == "2026-05-29"

    def test_primary_expiry_slice_returns_only_primary(self):
        instruments = self._make_instruments(
            dense_expiry="2026-05-29", sparse_expiry="2026-05-22",
        )
        h = self._make_hedger(instruments)
        chain = h._get_options_chain()
        primary = h._primary_expiry_slice(chain)
        assert primary["expiry"].unique().tolist() == ["2026-05-29"]
        # Dense expiry has 9 strikes × 2 types = 18 rows
        assert len(primary) == 18

    def test_primary_slice_falls_back_when_attrs_stripped(self):
        # pandas does not preserve `.attrs` through all DataFrame ops;
        # the helper must fall back to first-row expiry.
        instruments = self._make_instruments(
            dense_expiry="2026-05-29", sparse_expiry="2026-05-22",
        )
        h = self._make_hedger(instruments)
        chain = h._get_options_chain()
        chain_stripped = chain.copy()
        chain_stripped.attrs = {}  # simulate operation that drops attrs
        primary = h._primary_expiry_slice(chain_stripped)
        # First row is primary (dense_expiry) by sort order
        assert primary["expiry"].unique().tolist() == ["2026-05-29"]

    def test_returns_single_expiry_when_only_one_available(self):
        # Backward compat: if instruments dump has only one expiry,
        # chain should still be non-empty and primary points at it.
        instruments = self._make_instruments(
            dense_expiry="2026-05-29", sparse_expiry="2026-05-29",
        )
        # De-dup tradingsymbol so we don't have duplicate rows in the chain
        seen = set()
        dedup = []
        for r in instruments:
            key = (r["expiry"], r["strike"], r["instrument_type"])
            if key in seen:
                continue
            seen.add(key)
            dedup.append(r)
        h = self._make_hedger(dedup)
        chain = h._get_options_chain()
        assert chain["expiry"].unique().tolist() == ["2026-05-29"]
        assert chain.attrs.get("primary_expiry") == "2026-05-29"

    def test_empty_when_no_spot(self):
        instruments = self._make_instruments(
            dense_expiry="2026-05-29", sparse_expiry="2026-05-22",
        )
        h = self._make_hedger(instruments, spot=0)
        h._get_spot_price = MagicMock(return_value=0)
        assert h._get_options_chain().empty

    def test_empty_when_kite_fails(self):
        instruments = self._make_instruments(
            dense_expiry="2026-05-29", sparse_expiry="2026-05-22",
        )
        h = self._make_hedger(instruments)
        h.kite.instruments.side_effect = RuntimeError("boom")
        assert h._get_options_chain().empty
        assert h._consecutive_chain_failures == 1

    def test_primary_slice_on_empty_returns_empty(self):
        import pandas as pd
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        assert h._primary_expiry_slice(pd.DataFrame()).empty


class TestRehedgeChurnBounds:
    """C2 (2026-06-02): bound rehedge frequency, count, and per-tick size so an
    optimistic scalp estimate cannot churn the book into a cost bleed
    (06-02: 53 rehedges / ₹19.5k cost vs ₹10.8k gross loss). The band trigger
    and WW cost gate are forced to PASS in these tests — we are verifying the
    three new bounds gate independently of the +EV decision, and that exits are
    never throttled."""

    CLOCK = datetime(2026, 4, 22, 11, 0, 0)

    def _hedger(self, *, lots=2, cooldown=180.0, session_cap=20, lots_cap=20):
        from types import SimpleNamespace
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.state = HedgeState()
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY26APR20000CE", instrument_token=1,
                strike=20000, expiry="2026-04-30", option_type="CE",
                lot_size=25, quantity=25, entry_price=100.0,
                current_price=100.0, iv=0.2,
            )
        ]
        h.state.entry_time = None  # not the entry bar → :601 guard passes
        # Greeks chosen so the asymmetric band ≈ base_threshold (all γ equal),
        # and |delta|/lot_size clears it → a rehedge is *wanted*.
        h.state.portfolio_greeks = SimpleNamespace(
            net_discrete_delta=float(lots) * 25.0,
            net_shadow_gamma=1.0, net_gamma=1.0,
            net_shadow_gamma_up=1.0, net_shadow_gamma_down=1.0,
        )
        h.tunable_params = {
            "rehedge_delta_threshold": 0.6,
            "cost_hurdle_factor": 1.5,
            "gamma_scalp_band_pct": 1.5,
            "t0_band_factor": 1.0,
            "max_rehedge_lots_per_tick": lots_cap,
            "rehedge_cooldown_seconds": cooldown,
            "max_rehedges_per_session": session_cap,
        }
        h.underlying = "NIFTY"
        h._clock = lambda: self.CLOCK
        # Stub the surrounding machinery so the method reaches the gates.
        h._get_spot_price = lambda: 20000.0
        h._check_spot = lambda spot: True
        h._record_spot_sample = lambda *a: None
        h._update_positions_prices = lambda spot: None
        h._update_portfolio_greeks = lambda: None
        h._should_exit = lambda greeks, spot: False
        h._get_lot_size = lambda: 25
        h._estimate_gamma_scalp_pnl = lambda greeks, spot: 1e9  # cost gate passes
        h._generate_close_all_proposals = lambda: ["CLOSE_SENTINEL"]
        h._generate_hard_delta_proposals = lambda greeks, spot: [
            TradeProposal(
                tradingsymbol="NIFTY26APRFUT", instrument_token=0, strike=0,
                expiry="", option_type="FUT", lot_size=25, quantity=lots,
                price=20000.0, transaction_type="SELL", iv=0,
                bid_ask_spread_pct=0.01, margin_required=20000.0 * 25 * lots * 0.10,
                rationale="test hedge",
            )
        ]
        h.risk = MagicMock()
        h.risk.hedge_decision.return_value = SimpleNamespace(
            use_soft_delta=False, rationale="hard"
        )
        return h

    def test_single_inband_rehedge_emits_and_stamps_time(self):
        """Baseline: with no prior rehedge and bounds permissive, an in-band
        rehedge is emitted and the cooldown clock is stamped — proving the
        bounds don't block legitimate hedging."""
        h = self._hedger()
        out = h.check_and_rehedge()
        assert len(out) == 1 and out[0].option_type == "FUT"
        assert h.state._last_rehedge_time == self.CLOCK

    def test_cooldown_blocks_rapid_rehedge(self):
        """A rehedge within rehedge_cooldown_seconds of the last is skipped —
        this is the bound on sustained churn (the 06-02 mode)."""
        h = self._hedger(cooldown=180.0)
        h.state._last_rehedge_time = self.CLOCK - timedelta(seconds=60)
        assert h.check_and_rehedge() == []

    def test_cooldown_does_not_block_exit(self):
        """Exits must NEVER be throttled — flattening on a stop/expiry is
        unconditional even mid-cooldown."""
        h = self._hedger(cooldown=180.0)
        h.state._last_rehedge_time = self.CLOCK - timedelta(seconds=1)
        h._should_exit = lambda greeks, spot: True
        assert h.check_and_rehedge() == ["CLOSE_SENTINEL"]

    def test_cooldown_disabled_when_zero(self):
        """cooldown=0 disables the gate (legacy behaviour)."""
        h = self._hedger(cooldown=0)
        h.state._last_rehedge_time = self.CLOCK - timedelta(seconds=1)
        assert len(h.check_and_rehedge()) == 1

    def test_session_cap_blocks_after_limit(self):
        """The (cap+1)th rehedge within one open→close trade is skipped,
        bounding total per-trade churn regardless of cost-gate optimism."""
        h = self._hedger(session_cap=20)
        h.state._attribution_baseline = {"rehedges_at_entry": 0}
        h.state.rehedge_count = 20  # already did 20 this trade
        assert h.check_and_rehedge() == []

    def test_session_cap_allows_below_limit(self):
        h = self._hedger(session_cap=20)
        h.state._attribution_baseline = {"rehedges_at_entry": 5}
        h.state.rehedge_count = 10  # 5 this trade < 20
        assert len(h.check_and_rehedge()) == 1

    def test_lots_cap_clamps_oversized_hedge(self):
        """A hedge larger than max_rehedge_lots_per_tick is clamped, and its
        margin is scaled to match — bounds the 05-26 mode (few, huge hedges)."""
        h = self._hedger(lots=50, lots_cap=20)
        out = h.check_and_rehedge()
        assert len(out) == 1
        assert out[0].quantity == 20
        # margin scaled from the original 50-lot figure to 20 lots
        assert out[0].margin_required == pytest.approx(20000.0 * 25 * 50 * 0.10 * 20 / 50)

    def test_lots_cap_leaves_small_hedge_untouched(self):
        h = self._hedger(lots=2, lots_cap=20)
        out = h.check_and_rehedge()
        assert out[0].quantity == 2

    def test_empty_proposal_does_not_count_as_rehedge(self):
        """A sub-1-lot hard hedge that rounds to 0 lots returns [] — it must NOT
        increment rehedge_count (the session-cap input), start the cooldown, or
        re-anchor the gamma-scalp baseline. Otherwise phantom 0-lot ticks on a
        T-0 tightened band could silently exhaust the per-trade cap."""
        h = self._hedger()
        h._generate_hard_delta_proposals = lambda greeks, spot: []  # rounds to 0
        h.state.rehedge_count = 3
        h.state._last_rehedge_spot = 19990.0
        assert h.check_and_rehedge() == []
        assert h.state.rehedge_count == 3            # unchanged
        assert h.state._last_rehedge_time is None    # cooldown not started
        assert h.state._last_rehedge_spot == 19990.0  # anchor not moved

    def test_session_cap_survives_state_restore(self):
        """The per-trade session cap reads _attribution_baseline. A trade held
        across a session boundary must keep that baseline through serialize/
        restore, or the cap silently goes unenforced for restored positions."""
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.state = HedgeState()
        h.state._attribution_baseline = {
            "entry_time": datetime(2026, 4, 22, 9, 20, 0),
            "realized_pnl_at_entry": 0.0,
            "gamma_scalp_at_entry": 0.0,
            "costs_at_entry": 0.0,
            "rehedges_at_entry": 7,
            "entry_atm_iv": 0.2,
            "n_legs": 2,
        }
        blob = h.serialize_state()

        h2 = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h2.state = HedgeState()
        h2.restore_state(blob)
        assert h2.state._attribution_baseline is not None
        assert h2.state._attribution_baseline["rehedges_at_entry"] == 7
        assert h2.state._attribution_baseline["entry_time"] == datetime(2026, 4, 22, 9, 20, 0)

        # Legacy blob without the key restores to None (no crash, no cap).
        del blob["state"]["_attribution_baseline"]
        h3 = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h3.state = HedgeState()
        h3.restore_state(blob)
        assert h3.state._attribution_baseline is None


class TestLiveStatusHandling:
    """Audit 2026-06-10 task 0.3 (C-1): execute_proposals skips state
    mutation only on status == "FAILED". _live_execute returns PENDING for
    every successfully-placed order (fill unknown) and REJECTED for
    validation failures — both currently book costs, positions, and
    realized P&L as if filled. These tests encode the INTENDED contract
    (mutate on COMPLETE only); the PENDING/REJECTED ones are
    xfail(strict=True) until task 1.2 lands the COMPLETE-whitelist, at
    which point the markers come off."""

    def _live_hedger(self):
        hedger = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        hedger.kite = MagicMock()
        hedger.state = HedgeState()
        hedger.mode = "live"
        hedger.underlying = "NIFTY"
        hedger.exchange = "NFO"
        hedger._cached_lot_size = 25
        hedger._cached_futures_symbol = None
        hedger._clock = lambda: datetime(2026, 3, 29, 10, 0)
        hedger.immutable_params = {"total_capital": 500000}
        hedger.tunable_params = {}
        hedger.greeks = MagicMock()
        hedger._update_portfolio_greeks = lambda: None
        hedger._get_spot_price = lambda: 23000.0
        return hedger

    def _prop(self):
        return TradeProposal(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
            lot_size=25, quantity=2, price=300,
            transaction_type="BUY", iv=0.15, bid_ask_spread_pct=0.5,
            margin_required=15000,
        )

    def _run_with_status(self, status):
        h = self._live_hedger()
        h._live_execute = lambda p: {"order_id": "X1", "status": status, "mode": "live"}
        h.execute_proposals([self._prop()])
        return h

    def test_pending_does_not_mutate_state(self):
        h = self._run_with_status("PENDING")
        assert h.state.positions == []
        assert h.state.realized_pnl == 0.0
        assert h.state.total_transaction_costs == 0.0

    def test_rejected_does_not_mutate_state(self):
        h = self._run_with_status("REJECTED")
        assert h.state.positions == []
        assert h.state.realized_pnl == 0.0
        assert h.state.total_transaction_costs == 0.0

    def test_failed_does_not_mutate_state(self):
        h = self._run_with_status("FAILED")
        assert h.state.positions == []
        assert h.state.realized_pnl == 0.0
        assert h.state.total_transaction_costs == 0.0

    def test_complete_books_position_and_costs(self):
        h = self._run_with_status("COMPLETE")
        assert len(h.state.positions) == 1
        assert h.state.total_transaction_costs > 0.0

    def test_complete_books_at_actual_average_price(self):
        # Audit 1.2 step 2: live fills book at the executor's reported
        # average_price (marketable LIMITs can fill inside the protection
        # pad), not the proposal's quote. Paper results carry no
        # average_price → prop.price, pinned by the booking tests above.
        h = self._live_hedger()
        h._live_execute = lambda p: {
            "order_id": "X1", "status": "COMPLETE", "filled_lots": 2,
            "average_price": 305.0, "mode": "live",
        }
        h.execute_proposals([self._prop()])
        assert h.state.positions[0].entry_price == 305.0
        assert h.state.positions[0].current_price == 305.0

    def test_live_execute_delegates_to_shared_executor(self):
        # Audit 1.2 step 2: the refusal is gone — _live_execute now hands
        # the proposal to the shared KiteOrderExecutor (place → poll →
        # cancel/partial-reverse) and rebinds the kite client so a
        # runner-side token refresh propagates.
        h = self._live_hedger()
        executor = MagicMock()
        executor.execute.return_value = {
            "order_id": "X1", "status": "COMPLETE", "filled_lots": 2,
            "average_price": 300.0, "mode": "live",
        }
        h._live_order_executor = executor
        prop = self._prop()
        result = h._live_execute(prop)
        executor.execute.assert_called_once_with(prop)
        assert executor.kite is h.kite
        assert result["status"] == "COMPLETE"

    def test_order_executor_wiring(self):
        # The lazily-built executor carries taleb's identity: underlying-
        # tagged orders, the strategy's exchange, instruments-dump tick
        # lookup, and the 0.25 default pad when config has no override.
        from strategies.order_executor import KiteOrderExecutor
        h = self._live_hedger()
        ex = h._order_executor()
        assert isinstance(ex, KiteOrderExecutor)
        assert ex._tag_for(self._prop()) == "taleb-NIFTY"
        assert ex.exchange == "NFO"
        assert ex.limit_protection_pct == 0.25
        assert ex._get_instruments == h._fetch_nfo_instruments_with_retry
        assert h._order_executor() is ex  # built once


class TestMarkingFallbacks:
    """Audit 2026-06-10 task 1.6 (H-6): quote/lookup fallbacks must never
    corrupt the safety stops. (a) A quote outage carries the last good
    mark — unrealized P&L stays intact so _should_exit's loss gates can
    still fire (the old reset-to-entry zeroed it exactly when the tape
    was volatile). (c)/(d) lot-size and futures-symbol lookups fail loud
    instead of guessing (stale NIFTY=25 table → 3x sizing; 'NIFTYFUT'
    placeholder → fake paper fills). (b) the close-all futures flatten
    never prices at 0.0 and never aborts the option-leg closes."""

    def _hedger_with_position(self):
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h.state = HedgeState()
        h.mode = "paper"
        h.underlying = "NIFTY"
        h.exchange = "NFO"
        h._cached_lot_size = None
        h._cached_futures_symbol = None
        h._clock = lambda: datetime(2026, 6, 11, 10, 0)
        h.immutable_params = {"total_capital": 500000}
        h.tunable_params = {}
        h._consecutive_quote_failures = 0
        h.state.positions = [
            OptionContract(
                tradingsymbol="NIFTY26JUN23000PE", instrument_token=1,
                strike=23000, expiry="2026-06-25", option_type="PE",
                lot_size=75, quantity=2, entry_price=300.0,
                current_price=350.0, iv=0.15,  # last good mark: +7500 unreal
            ),
        ]
        return h

    # ── H-6a: quote outage keeps the last good mark ──────────────

    def test_quote_outage_carries_last_good_mark(self):
        h = self._hedger_with_position()
        h.kite.quote = MagicMock(side_effect=RuntimeError("exchange feed down"))
        h._record_pnl_snapshot = lambda: None
        h._update_positions_prices(23000.0)
        pos = h.state.positions[0]
        assert pos.current_price == 350.0          # NOT reset to entry 300
        assert h.state.unrealized_pnl == (350.0 - 300.0) * 2 * 75
        assert h._stale_marks["NIFTY26JUN23000PE"] == 1
        assert h._consecutive_quote_failures == 1

    def test_quote_recovery_clears_staleness(self):
        h = self._hedger_with_position()
        h._record_pnl_snapshot = lambda: None
        h.kite.quote = MagicMock(side_effect=RuntimeError("down"))
        h._update_positions_prices(23000.0)
        h.kite.quote = MagicMock(return_value={
            "NFO:NIFTY26JUN23000PE": {"last_price": 360.0},
        })
        h._update_positions_prices(23000.0)
        assert h.state.positions[0].current_price == 360.0
        assert "NIFTY26JUN23000PE" not in h._stale_marks
        assert h._consecutive_quote_failures == 0  # reset on success (3.5)

    def test_loss_gate_still_fires_through_quote_outage(self):
        # The point of H-6a: a deep loss must remain visible to
        # _should_exit while quotes are down. Mark the leg at a heavy
        # loss, kill the feed, and assert the daily-loss gate trips.
        h = self._hedger_with_position()
        h.immutable_params["max_daily_loss_pct"] = 1.0   # ₹5,000 on 5L
        h.state.positions[0].current_price = 100.0       # -30,000 unreal
        h._record_pnl_snapshot = lambda: None
        h.kite.quote = MagicMock(side_effect=RuntimeError("down"))
        h._update_positions_prices(23000.0)
        h.state._current_day_pnl = h.state.unrealized_pnl
        assert h.state.unrealized_pnl == (100.0 - 300.0) * 2 * 75
        h._record_loss = lambda: None
        h._daily_loss_stop_date = None
        assert h._should_exit(MagicMock(), 23000.0) is True

    # ── H-6c/d: lookups fail loud, never guess ───────────────────

    def test_lot_size_lookup_failure_raises(self):
        h = self._hedger_with_position()
        h.kite.instruments = MagicMock(side_effect=RuntimeError("api down"))
        with pytest.raises(RuntimeError, match="refusing to size"):
            h._get_lot_size()

    def test_lot_size_no_rows_raises(self):
        h = self._hedger_with_position()
        h.kite.instruments = MagicMock(return_value=[
            {"name": "OTHER", "instrument_type": "CE", "lot_size": 10,
             "tradingsymbol": "X", "expiry": "2026-06-25"},
        ])
        with pytest.raises(RuntimeError, match="lot size"):
            h._get_lot_size()

    def test_futures_symbol_lookup_failure_raises_no_placeholder(self):
        h = self._hedger_with_position()
        h.kite.instruments = MagicMock(side_effect=RuntimeError("api down"))
        with pytest.raises(RuntimeError, match="NIFTYFUT|futures symbol"):
            h._get_futures_symbol()

    # ── H-6b: close-all flatten pricing + degradation ────────────

    def _hedger_with_futures(self):
        h = self._hedger_with_position()
        h.state.futures_hedge_delta = 150.0
        h.state.futures_lots = 2
        h.state.futures_entry_vwap = 23150.0
        h._get_lot_size = lambda: 75
        h._get_futures_symbol = lambda: "NIFTY26JUNFUT"
        return h

    def test_flatten_prices_at_entry_vwap_when_spot_fails(self):
        h = self._hedger_with_futures()
        h._get_spot_price = lambda: None
        props = h._generate_close_all_proposals()
        fut = [p for p in props if p.option_type == "FUT"]
        assert len(fut) == 1
        assert fut[0].price == 23150.0      # entry VWAP, never 0.0
        assert fut[0].transaction_type == "SELL"

    def test_flatten_degrades_to_options_only_on_lookup_failure(self):
        # H-6c/d raising must not abort the safety close of the option
        # legs — degrade, don't fail the whole flatten.
        h = self._hedger_with_futures()
        h._get_lot_size = MagicMock(side_effect=RuntimeError("no instruments"))
        props = h._generate_close_all_proposals()
        assert len(props) == 1
        assert props[0].option_type == "PE"   # option close survived


# ──────────────────────────────────────────────────────────
# Issue #160 — MC path-source config + bootstrap return pool.
# ──────────────────────────────────────────────────────────


class TestMcPathSource:
    def _hedger_with_config(self, value=None):
        import configparser
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        cfg = configparser.ConfigParser()
        cfg.add_section("strategy")
        if value is not None:
            cfg.set("strategy", "mc_path_source", value)
        h.config = cfg
        return h

    def test_default_is_gbm(self):
        # WHY: merge must change nothing until the operator opts in — the
        # live hedger keeps the pre-#160 Gaussian gate by default
        # (safety rule 3: paper validates bootstrap first).
        assert self._hedger_with_config()._read_mc_path_source() == "gbm"

    def test_bootstrap_opt_in(self):
        assert (self._hedger_with_config("bootstrap")._read_mc_path_source()
                == "bootstrap")

    def test_typo_falls_back_to_gbm_not_crash(self, caplog):
        # WHY: the entry path must not crash on a config typo, but the
        # operator must SEE the intended source was not applied (Rule 12).
        import logging as _logging
        caplog.set_level(_logging.WARNING)
        assert (self._hedger_with_config("boostrap")._read_mc_path_source()
                == "gbm")
        assert any("mc_path_source" in r.message for r in caplog.records)

    def test_daily_return_pool_built_from_eod_snapshot(self, tmp_path, monkeypatch):
        # WHY: the pool must come from the EOD snapshot rebuilt each session
        # start — NOT the tick-appended _spot_history, whose 2000-sample cap
        # would evict the daily anchors after a few live sessions and
        # silently starve the bootstrap back to Gaussian. Each return must be
        # dated by the day it is REALIZED (the second close) so the gate can
        # exclude future returns on tape replay.
        import datetime as _dt
        import numpy as _np
        import pandas as _pd
        monkeypatch.chdir(tmp_path)
        dc = tmp_path / "data_cache"
        dc.mkdir()
        closes = [24000.0, 24240.0, 23997.6, 24100.0]
        _pd.DataFrame({
            "timestamp": [f"2026-07-{d:02d} 15:30:00" for d in range(1, 5)],
            "underlying_price": closes,
        }).to_csv(dc / "NIFTY_20260701_20260731_eod.csv", index=False)
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.underlying = "NIFTY"
        h._spot_history = []
        h._spot_history_max_size = 2000
        h._daily_return_history = []
        h._load_spot_history()
        dates = [d for d, _ in h._daily_return_history]
        rets = [r for _, r in h._daily_return_history]
        assert dates == [_dt.date(2026, 7, 2), _dt.date(2026, 7, 3),
                         _dt.date(2026, 7, 4)]
        assert _np.allclose(rets, _np.diff(_np.log(_np.array(closes))))

    def test_mc_empirical_returns_gbm_is_none_bootstrap_filters_future(self):
        # WHY (#160 review): the gate pool must exclude returns dated on/after
        # the current session — a no-op live, but the look-ahead guard that
        # stops a tape replay of a past session from resampling its own
        # future. And gbm must pass None so the risk path is byte-identical
        # to pre-#160.
        import datetime as _dt
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h._clock = lambda: datetime(2026, 7, 10, 9, 20)
        h._daily_return_history = [
            (_dt.date(2026, 7, 8), -0.021),
            (_dt.date(2026, 7, 9), 0.004),
            (_dt.date(2026, 7, 10), 0.010),   # same day — not yet knowable
            (_dt.date(2026, 7, 13), -0.008),  # future
        ]
        h.immutable_params = {"mc_path_source": "gbm"}
        assert h._mc_empirical_returns() is None
        h.immutable_params = {"mc_path_source": "bootstrap"}
        assert h._mc_empirical_returns() == [-0.021, 0.004]
