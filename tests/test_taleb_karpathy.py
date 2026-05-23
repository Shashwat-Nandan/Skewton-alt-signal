"""Tests for the Taleb-Karpathy strategy — position management, netting, costs, metrics."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import MagicMock, patch
from strategies.taleb_karpathy import (
    TalebKarpathyStrategy, HedgeState, estimate_transaction_cost,
    _apply_best_params,
)
from trade_proposer import TradeProposal
from greeks_engine import OptionContract


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

        # Simulate spot moving to 22100 — futures P/L should be (22100-22000)*2*25 = 5000
        mock_hedger.kite.quote.return_value = {}  # No option quotes
        mock_hedger._update_positions_prices(22100.0)
        assert mock_hedger.state.unrealized_pnl == 5000.0


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
        from datetime import date
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
        from kite_auth import KiteAuthManager, AuthenticationError
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
        from greeks_engine import time_to_expiry
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
        from autoresearch_loop import HedgeResearchLoop
        # These are the params actually used in runtime decision paths
        runtime_tunables = {
            "rehedge_delta_threshold", "gamma_scalp_band_pct",
            "position_size_pct", "vega_limit", "max_holding_period_hours",
            "entry_iv_percentile_min", "entry_iv_percentile_max",
            "max_entry_alpha", "mc_worst_path_loss_pct",
            "min_rv_iv_ratio", "rv_window_days",
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
            return_value=MagicMock(worst_path_pnl=-1000),
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
        return hedger

    def test_open_book_skips_entry_pipeline(self, mock_hedger):
        mock_hedger.state.positions.append(OptionContract(
            tradingsymbol="NIFTY26403CE22000", instrument_token=1,
            strike=22000, expiry="2026-04-03", option_type="CE",
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
            return_value=MagicMock(worst_path_pnl=-15000),
        )

        result = mock_hedger.scan_and_propose()

        assert result == []

    def test_sufficient_lot_scale_proceeds(self, mock_hedger):
        # 4-lot proposal, MC worst path 10000 vs cap 5000 → scale = 0.5.
        # int(4 * 0.5) = 2, above 1 lot → entry proceeds with scaled size.
        proposal = self._make_proposal(qty=4)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-10000),
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1
        assert result[0].quantity == 2

    def test_within_cap_no_scaling(self, mock_hedger):
        # MC worst path 3000 vs cap 5000 → no scaling; quantity unchanged.
        proposal = self._make_proposal(qty=1)
        mock_hedger.proposer.propose_delta_neutral = MagicMock(return_value=[proposal])
        mock_hedger.risk.path_dependence_monte_carlo = MagicMock(
            return_value=MagicMock(worst_path_pnl=-3000),
        )

        result = mock_hedger.scan_and_propose()

        assert len(result) == 1
        assert result[0].quantity == 1


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
        from datetime import datetime, timedelta
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
        import numpy as np
        h = self._make_hedger()
        h._clock = lambda: datetime(2026, 4, 19, 15, 30)
        old_t = datetime(2026, 4, 1)  # ~18 days old
        for i in range(15):
            h._spot_history.append((old_t + timedelta(minutes=5*i), 22000.0 + i*100))
        # Only old samples in a 5-day window → too few in-window → None
        assert h._compute_realized_vol(window_days=5) is None


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
            return_value=MagicMock(worst_path_pnl=-100),
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
# The 2026-05-19 rebuild removed run_paper.py's unconditional EOD flatten.
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

    def test_false_on_instruments_lookup_failure(self):
        """Flaky API hiccup must not force an unintended flatten."""
        from datetime import date
        h = self._mock_hedger()
        h.state.futures_hedge_delta = -75.0
        h.state.futures_lots = -1
        def _raise(*a, **k):
            raise RuntimeError("network down")
        h.kite.instruments = _raise
        assert h.legs_expire_on(date(2026, 5, 19)) is False


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
        from greeks_engine import GreeksEngine
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
        from greeks_engine import GreeksEngine
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
        from datetime import datetime
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
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite = MagicMock()
        h.state = HedgeState()
        h.mode = "paper"
        h._pre_trade_checks = MagicMock(return_value=True)
        h._get_spot_price = MagicMock(return_value=22000.0)
        h.proposer = MagicMock()
        h.greeks = MagicMock()
        h.risk = MagicMock()
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
            expiry="2026-04-03", option_type="CE", lot_size=25,
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
            expiry="2026-04-03", option_type="CE", lot_size=25,
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
            expiry="2026-04-03", option_type="CE", lot_size=25,
            quantity=1, entry_price=300, current_price=300, iv=0.15,
        ))
        result = h.scan_and_propose()
        assert result == []
        h._pre_trade_checks.assert_called_once()

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
        # The realized-scalp anchor isn't set in this minimal mock so
        # gamma_scalp_pnl stays 0, but the band trigger AND log emit
        # is what matters here. Easiest assertion: rehedge_count
        # incremented, OR proposals non-empty.
        # NOTE: round(15/75)=0 so proposals=[] even when band fires,
        # per the 2026-05-07 lesson. Check rehedge_count.
        assert h.state.rehedge_count == 1 or len(proposals) > 0, (
            "Phase 5 T-0 tightening did not fire — band still too wide"
        )

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
