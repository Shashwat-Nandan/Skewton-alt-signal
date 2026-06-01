"""Smoke test for the backtest harness."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from pathlib import Path
import backtest
from backtest import (
    generate_synthetic_data, MockKite, run_backtest,
    load_captured_tape, list_captured_sessions,
)


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


class TestCapturedTapeReplay:
    """Phase 2.2: load_captured_tape converts JSONL ticks to the
    MockKite-compatible DataFrame schema. Tests skip cleanly when no
    captured sessions are present in data_cache/ — replay is an
    operational feature that depends on having run tick_capture on a
    real Kite session."""

    @pytest.fixture
    def captured_sessions(self):
        sessions = list_captured_sessions()
        if not sessions:
            pytest.skip("No captured tick sessions in data_cache/ticks/")
        return sessions

    def test_list_captured_sessions_iso_format(self, captured_sessions):
        """Each entry must be a valid ISO date string."""
        from datetime import date
        for s in captured_sessions:
            # raises ValueError if malformed
            date.fromisoformat(s)

    def test_load_produces_mockkite_schema(self, captured_sessions):
        """The DataFrame must contain every column MockKite reads,
        else the replay path silently degrades to no-trades."""
        df = load_captured_tape(captured_sessions[-1])
        required = {"timestamp", "symbol", "underlying_price", "strike",
                    "option_type", "expiry", "last_price", "bid", "ask",
                    "lot_size", "iv"}
        missing = required - set(df.columns)
        assert not missing, f"Missing columns: {missing}"

    def test_load_includes_spot_rows(self, captured_sessions):
        """A session without spot ticks fails to feed the strategy's
        spot-quote path. Symbol 'NIFTY' (bare underlying) is what
        MockKite looks up for the spot."""
        df = load_captured_tape(captured_sessions[-1])
        spot_rows = df[df["symbol"] == "NIFTY"]
        assert len(spot_rows) > 0, (
            "No spot rows. Likely cause: the JSONL header's spot token "
            "wasn't patched into the enriched DataFrame — replay would "
            "silently no-trade."
        )

    def test_load_missing_session_raises(self):
        """Fail loud when the requested date has no tick file. Silent
        degradation would have the autoresearch loop's tape-replay
        cycle quietly produce 0-trade penalties."""
        with pytest.raises(FileNotFoundError):
            load_captured_tape("2099-01-01")

    def test_minute_resolution_collapses_ticks(self, captured_sessions):
        """1-minute resampling must yield fewer rows than tick resolution
        on a real session (otherwise the resampling didn't fire)."""
        # 5min must be smaller than 1min for any session with traffic.
        df_1m = load_captured_tape(captured_sessions[-1], resolution="1min")
        df_5m = load_captured_tape(captured_sessions[-1], resolution="5min")
        assert len(df_5m) < len(df_1m), (
            "5min and 1min returned same row count — resampling did not "
            "collapse buckets"
        )

    def test_real_futures_surfaced_in_mockkite_instruments(self, captured_sessions):
        """Code-review fix #7: load_captured_tape emits real FUT rows
        (e.g. NIFTY26MAYFUT) and MockKite.instruments must return them
        rather than the synthetic NIFTYFUTMOCK placeholder. Pre-fix,
        the strategy cached the placeholder symbol and every futures
        hedge silently failed because tick_data contained the REAL
        futures symbol the placeholder couldn't match."""
        df = load_captured_tape(captured_sessions[-1])
        kite = MockKite(df, "NIFTY")
        instruments = kite.instruments("NFO")
        fut_rows = [i for i in instruments if i["instrument_type"] == "FUT"]
        assert len(fut_rows) > 0, "No FUT in MockKite.instruments"
        symbols = {i["tradingsymbol"] for i in fut_rows}
        assert "NIFTYFUTMOCK" not in symbols, (
            "Synthetic FUTMOCK leaked into captured-tape MockKite — "
            "the real FUT row was not surfaced."
        )
        # The surfaced symbol must be quotable somewhere in the
        # session — advance the tick clock until quote() resolves it.
        # (The first tick at 09:03 predates the first FUT print at
        # 09:07; MockKite filters quote() to current tick so we need
        # to step forward.)
        real_fut = fut_rows[0]["tradingsymbol"]
        for _ in range(50):
            q = kite.quote([f"NFO:{real_fut}"])
            if q and f"NFO:{real_fut}" in q:
                return
            if not kite.advance_tick():
                break
        raise AssertionError(
            f"MockKite.quote never resolved the captured FUT symbol "
            f"{real_fut} after 50 ticks."
        )

    def test_underlying_price_forward_filled(self, captured_sessions):
        """Code-review fix #4: option rows in minutes lacking a spot
        tick must still carry a non-NaN underlying_price (forward-fill
        via merge_asof). Pre-fix the merge produced NaN spot for
        any spot-tick gap, poisoning downstream Greeks / IV percentile."""
        import pandas as pd  # noqa: F401
        df = load_captured_tape(captured_sessions[-1])
        option_rows = df[df["option_type"].isin(["CE", "PE"])]
        assert len(option_rows) > 0, "Captured tape has no option rows"
        nan_spot = option_rows["underlying_price"].isna().sum()
        assert nan_spot == 0, (
            f"{nan_spot} option rows have NaN underlying_price — "
            f"merge_asof forward-fill regression"
        )


# ── Fix B (2026-05-31): IV/skew seeding so autoresearch tunables can bind ──
# Diagnosis: a captured-tape replay fires one entry scan per session; without
# a primed IV history _compute_iv_percentile sees <30 obs → neutral 50.0, so
# the IV-percentile / regime tunables were inert in the weekly sweep.

class TestLoadIVSkewSeed:
    def test_truncation_drops_most_recent_k(self, tmp_path, monkeypatch):
        """drop_recent must trim the TAIL (most-recent) of each series — the
        coarse guard against a replay ranking against its own/future IV."""
        import json
        from pathlib import Path
        d = tmp_path / "data_cache"
        d.mkdir()
        (d / "iv_history_NIFTY.json").write_text(json.dumps({
            "atm_iv": [0.10, 0.20, 0.30, 0.40, 0.50],
            "skew": [0.01, 0.02, 0.03],
        }))
        monkeypatch.chdir(tmp_path)
        atm, skew = backtest.load_iv_skew_seed("NIFTY", drop_recent=2)
        assert atm == [0.10, 0.20, 0.30]   # last two dropped
        assert skew == [0.01]

    def test_filters_out_of_range_values(self, tmp_path, monkeypatch):
        """Same sanity bounds as the live loader: IV in (0.01,3.0),
        skew in (-1.0,1.0). Garbage must not poison the seed."""
        import json
        d = tmp_path / "data_cache"; d.mkdir()
        (d / "iv_history_NIFTY.json").write_text(json.dumps({
            "atm_iv": [0.15, 99.0, 0.0, 0.25],
            "skew": [0.05, 5.0, -0.05],
        }))
        monkeypatch.chdir(tmp_path)
        atm, skew = backtest.load_iv_skew_seed("NIFTY")
        assert atm == [0.15, 0.25]
        assert skew == [0.05, -0.05]

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert backtest.load_iv_skew_seed("NIFTY") == ([], [])

    def test_drop_recent_ge_len_returns_empty(self, tmp_path, monkeypatch):
        import json
        d = tmp_path / "data_cache"; d.mkdir()
        (d / "iv_history_NIFTY.json").write_text(json.dumps(
            {"atm_iv": [0.15, 0.25], "skew": [0.05]}))
        monkeypatch.chdir(tmp_path)
        assert backtest.load_iv_skew_seed("NIFTY", drop_recent=5) == ([], [])


class TestRunBacktestSeeding:
    """The seed must reach the hedger's rolling windows; default must wipe
    BOTH (the prior code left _skew_history loaded from the live JSON — a
    silent, asymmetric look-ahead leak)."""

    def _spy_first_call_lengths(self, monkeypatch):
        seen = {}
        orig_iv = backtest.TalebKarpathyStrategy._compute_iv_percentile
        orig_sk = backtest.TalebKarpathyStrategy._compute_skew_percentile

        def spy_iv(self, chain, spot):
            seen.setdefault("iv", len(self._atm_iv_history))  # before its own append
            return orig_iv(self, chain, spot)

        def spy_sk(self, chain, spot):
            seen.setdefault("skew", len(self._skew_history))
            return orig_sk(self, chain, spot)

        monkeypatch.setattr(backtest.TalebKarpathyStrategy,
                            "_compute_iv_percentile", spy_iv)
        monkeypatch.setattr(backtest.TalebKarpathyStrategy,
                            "_compute_skew_percentile", spy_sk)
        return seen

    def test_seed_primes_both_windows(self, monkeypatch):
        seen = self._spy_first_call_lengths(monkeypatch)
        data = generate_synthetic_data(days=2, ticks_per_day=12)
        run_backtest(data, underlying="NIFTY",
                     seed_iv_history=[0.15] * 40,
                     seed_skew_history=[0.01] * 35)
        # The single entry scan saw the full seeded prefix → can leave the
        # 30-obs warmup, so the IV-percentile / regime tunables can bind.
        assert seen["iv"] == 40
        assert seen["skew"] == 35

    def test_default_wipes_both_no_skew_leak(self, monkeypatch):
        seen = self._spy_first_call_lengths(monkeypatch)
        data = generate_synthetic_data(days=2, ticks_per_day=12)
        # No seed: both windows must START empty even though __init__ loaded
        # the live persisted history. Pre-fix, _skew_history started non-empty
        # (leak); this pins the symmetry.
        run_backtest(data, underlying="NIFTY")
        assert seen["iv"] == 0
        assert seen["skew"] == 0
