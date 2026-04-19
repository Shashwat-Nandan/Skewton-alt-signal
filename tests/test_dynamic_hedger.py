"""Tests for dynamic_hedger.py — position management, netting, costs, metrics."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import MagicMock, patch
from dynamic_hedger import TalebHedger, HedgeState, estimate_transaction_cost
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
        """Create a TalebHedger with mocked Kite."""
        kite = MagicMock()
        kite.instruments.return_value = [
            {"name": "NIFTY", "instrument_type": "CE", "lot_size": 25,
             "tradingsymbol": "NIFTY26403CE22000", "expiry": "2026-04-03"},
        ]
        with patch("dynamic_hedger.configparser.ConfigParser") as mock_cfg:
            cfg = MagicMock()
            cfg.__getitem__ = MagicMock(return_value={
                "underlying": "NIFTY", "exchange": "NFO", "trading_mode": "paper",
            })
            cfg.getfloat = MagicMock(return_value=0.15)
            cfg.getboolean = MagicMock(return_value=True)
            cfg.getint = MagicMock(return_value=6)
            cfg.read = MagicMock()
            mock_cfg.return_value = cfg

            hedger = TalebHedger.__new__(TalebHedger)
            hedger.kite = kite
            hedger.state = HedgeState()
            hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = False  # LIVE mode
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
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
        hedger = TalebHedger.__new__(TalebHedger)
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        from dynamic_hedger import TalebHedger
        source = inspect.getsource(TalebHedger.__init__)
        assert "delta_bump_pct" not in source


class TestDailyLossStop:
    """P1: After max daily loss, no new entries should occur for the rest of that day."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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


class TestMCSizingSubLotGate:
    """P1: MC sizing must reject entries whose required scale drops legs below 1 lot."""

    @pytest.fixture
    def mock_hedger(self):
        from datetime import datetime
        kite = MagicMock()
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
        hedger = TalebHedger.__new__(TalebHedger)
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
        hedger = TalebHedger.__new__(TalebHedger)
        hedger.kite = kite
        hedger.state = HedgeState()
        hedger._is_paper_mode = True
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
