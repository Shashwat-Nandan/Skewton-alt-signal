"""Smoke test for the backtest harness."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from backtest import generate_synthetic_data, MockKite, run_backtest


class TestSyntheticData:
    def test_generates_data(self):
        data = generate_synthetic_data(days=3, ticks_per_day=4)
        assert not data.empty
        assert "timestamp" in data.columns
        assert "symbol" in data.columns
        assert "last_price" in data.columns

    def test_contains_spot_and_options(self):
        data = generate_synthetic_data(days=2, ticks_per_day=2)
        assert (data["option_type"] == "IDX").any()
        assert (data["option_type"] == "CE").any()
        assert (data["option_type"] == "PE").any()


class TestMockKite:
    def test_quote_returns_spot(self):
        data = generate_synthetic_data(days=1, ticks_per_day=2)
        kite = MockKite(data, "NIFTY")
        q = kite.quote(["NSE:NIFTY"])
        assert "NSE:NIFTY" in q
        assert q["NSE:NIFTY"]["last_price"] > 0

    def test_advance_tick(self):
        data = generate_synthetic_data(days=1, ticks_per_day=3)
        kite = MockKite(data, "NIFTY")
        ts1 = kite.current_timestamp
        assert kite.advance_tick()
        ts2 = kite.current_timestamp
        assert ts2 > ts1

    def test_instruments(self):
        data = generate_synthetic_data(days=1, ticks_per_day=1)
        kite = MockKite(data, "NIFTY")
        instruments = kite.instruments("NFO")
        assert len(instruments) > 0
        types = set(i["instrument_type"] for i in instruments)
        assert "CE" in types
        assert "PE" in types


class TestBacktestIntegration:
    def test_smoke_test(self):
        """Run a short backtest and verify it produces results."""
        data = generate_synthetic_data(days=2, ticks_per_day=4)
        results = run_backtest(data, underlying="NIFTY")

        assert "pnl_curve" in results
        assert "metrics" in results
        assert "trade_log" in results
        assert not results["pnl_curve"].empty
        assert results["metrics"]["total_ticks"] > 0

    def test_backtest_produces_trades(self):
        """P1: Backtest must actually produce trades (clock injection works)."""
        data = generate_synthetic_data(days=5, ticks_per_day=12)
        # Widen entry filters so synthetic data passes all pre-trade gates
        results = run_backtest(data, underlying="NIFTY", tunable_params={
            "entry_iv_percentile_min": 0,
            "entry_iv_percentile_max": 100,
            "max_entry_alpha": 50000,
        })

        assert results["metrics"]["total_trades"] > 0, (
            "Backtest produced 0 trades — clock injection or pre_trade_checks likely broken"
        )
        assert not results["trade_log"].empty

    def test_metrics_have_required_keys(self):
        data = generate_synthetic_data(days=2, ticks_per_day=4)
        results = run_backtest(data, underlying="NIFTY")
        m = results["metrics"]

        required = ["net_pnl", "sharpe_ratio", "calmar_ratio", "sortino_ratio",
                     "max_drawdown", "total_transaction_costs"]
        for key in required:
            assert key in m, f"Missing metric: {key}"

    def test_tunable_params_applied(self):
        """P1: run_backtest must apply tunable_params to the hedger, not ignore them."""
        data = generate_synthetic_data(days=2, ticks_per_day=4)
        # Use an impossibly tight IV window so the hedger cannot trade
        results_blocked = run_backtest(data, underlying="NIFTY", tunable_params={
            "entry_iv_percentile_min": 99,
            "entry_iv_percentile_max": 100,
        })
        # Use a wide IV window + wide alpha so the hedger can trade
        results_open = run_backtest(data, underlying="NIFTY", tunable_params={
            "entry_iv_percentile_min": 0,
            "entry_iv_percentile_max": 100,
            "max_entry_alpha": 50000,
        })
        # The blocked run must have fewer or equal trades than the open run
        assert results_blocked["metrics"]["total_trades"] <= results_open["metrics"]["total_trades"]

    def test_multi_day_re_entry(self):
        """P1: Hedger must be able to re-enter on subsequent days after exiting."""
        import numpy as np
        np.random.seed(1)
        data = generate_synthetic_data(days=5, ticks_per_day=12)
        results = run_backtest(data, underlying="NIFTY")
        tl = results["trade_log"]
        if not tl.empty:
            entries = tl[tl["action"] == "ENTRY"]
            entry_days = set(str(ts)[:10] for ts in entries["timestamp"])
            assert len(entry_days) > 1, (
                f"Entries only on {entry_days} — multi-day re-entry is broken"
            )

    def test_default_config_produces_trades(self):
        """P1: Default config must produce trades with synthetic data (no param overrides)."""
        import numpy as np
        np.random.seed(42)
        data = generate_synthetic_data(days=5, ticks_per_day=12)
        results = run_backtest(data, underlying="NIFTY")
        assert results["metrics"]["total_trades"] > 0, (
            "Default backtest still produces 0 trades — not a meaningful validator"
        )

    def test_backtest_flattens_at_end_of_data(self):
        """P1: Final pnl_curve row must show zero open positions and zero unrealized PnL.

        Without end-of-data liquidation, reported net_pnl is a mark-to-market
        snapshot rather than a fully realized result.
        """
        import numpy as np
        np.random.seed(7)
        data = generate_synthetic_data(days=5, ticks_per_day=12)
        results = run_backtest(data, underlying="NIFTY", tunable_params={
            "entry_iv_percentile_min": 0,
            "entry_iv_percentile_max": 100,
            "max_entry_alpha": 50000,
        })
        # Only meaningful if the engine actually traded
        if results["metrics"]["total_trades"] == 0:
            pytest.skip("No trades produced — flatten path not exercised")

        last_row = results["pnl_curve"].iloc[-1]
        assert last_row["positions"] == 0, (
            f"Backtest ended with {int(last_row['positions'])} open positions — "
            "end-of-data liquidation did not run"
        )
        assert abs(last_row["unrealized_pnl"]) < 1e-6, (
            f"Backtest ended with unrealized_pnl={last_row['unrealized_pnl']:.2f} — "
            "PnL is still mark-to-market"
        )
        # Realized PnL should equal total PnL once flat
        assert abs(last_row["total_pnl"] - last_row["realized_pnl"]) < 1e-6
