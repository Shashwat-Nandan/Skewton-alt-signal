"""Smoke test for the backtest harness."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from datetime import date, timedelta
from research import backtest
from research.backtest import (
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

    def test_quote_serves_the_synthetic_futures_placeholder(self):
        """instruments() hands out a FUTMOCK contract on synthetic data, so
        quote() must price it. Returning nothing makes the strategy refuse
        its hard delta hedge on every tick, which silently turns every
        synthetic backtest — including the autoresearch hold-out validation
        — into an unhedged book."""
        data = generate_synthetic_data(days=1, ticks_per_day=2)
        kite = MockKite(data, "NIFTY")
        futs = [i for i in kite.instruments("NFO")
                if i["instrument_type"] == "FUT"]
        assert futs, "synthetic instruments must expose a FUT contract"
        sym = futs[0]["tradingsymbol"]

        q = kite.quote([f"NFO:{sym}"])
        assert f"NFO:{sym}" in q
        fut = q[f"NFO:{sym}"]["last_price"]
        assert fut > 0

        # Priced off spot + a real basis, not equal to spot: the hedge is
        # entered, marked and flattened on this series, so a zero basis
        # would hide exactly the regression class this models.
        spot = kite.quote(["NSE:NIFTY"])["NSE:NIFTY"]["last_price"]
        assert fut > spot
        assert 0.0005 < (fut / spot - 1) < 0.003

    def test_hard_delta_hedge_is_proposed_on_synthetic_data(self):
        """End-to-end guard for the same regression: with a short delta the
        strategy must actually emit a futures hedge on a synthetic tape,
        priced off the futures series rather than spot."""
        from unittest.mock import MagicMock
        from strategies.taleb_karpathy import TalebKarpathyStrategy, HedgeState

        data = generate_synthetic_data(days=1, ticks_per_day=3, lot_size=65)
        kite = MockKite(data, "NIFTY")
        h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
        h.kite, h.state = kite, HedgeState()
        h.underlying, h.exchange = "NIFTY", "NFO"
        h._cached_lot_size, h._cached_futures_symbol = 65, None
        spot = kite.quote(["NSE:NIFTY"])["NSE:NIFTY"]["last_price"]

        greeks = MagicMock(net_discrete_delta=-260.0, net_delta=-260.0)
        props = h._generate_hard_delta_proposals(greeks, spot)
        assert len(props) == 1, "synthetic backtests must still hedge delta"
        assert props[0].option_type == "FUT"
        assert props[0].transaction_type == "BUY"
        assert props[0].price > spot   # futures level, not the index


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
        # 40 days, not 5: the IV percentile ranks against one observation per
        # SESSION and needs 30 prior sessions to leave warmup, so a 5-day
        # tape can never fire an entry regardless of the band.
        data = generate_synthetic_data(days=40, ticks_per_day=12)
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
        # 40 days: see test_backtest_produces_trades — 30 prior sessions are
        # needed before the IV-percentile gate can leave warmup.
        data = generate_synthetic_data(days=40, ticks_per_day=12)
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
        # 40 days: see test_backtest_produces_trades — with fewer sessions the
        # IV-percentile pool never leaves warmup, no trade fires, and the
        # skip guard below silently stops exercising the flatten path.
        data = generate_synthetic_data(days=40, ticks_per_day=12)
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

    def test_todays_in_progress_capture_is_excluded(self, tmp_path, monkeypatch):
        """WHY: during market hours today's JSONL is still being appended —
        replaying it races the writer, biases sweeps, and by mid-session it
        is tens of millions of rows (this exact test class OOM-killed a
        16 GB pytest on 2026-07-06 by loading sessions[-1] == today). Every
        replay consumer (autoresearch, sweeps, these tests) goes through
        list_captured_sessions, so the guard lives there. 'Today' is the
        IST trading date (matching tick filename stamps), NOT host-local —
        on a CEST host, 20:30-00:00 local is already the next IST day, and
        a host-local check would discard the just-COMPLETED session."""
        from datetime import timedelta

        from research.backtest import ist_today
        ticks = tmp_path / "data_cache" / "ticks"
        ticks.mkdir(parents=True)
        today = ist_today().isoformat()
        yesterday = (ist_today() - timedelta(days=1)).isoformat()
        (ticks / f"ticks-{today}.jsonl").write_text("{}\n")
        (ticks / f"ticks-{yesterday}.jsonl.zst").write_bytes(b"")
        monkeypatch.chdir(tmp_path)
        assert list_captured_sessions() == [yesterday]
        assert list_captured_sessions(include_today=True) == [yesterday, today]

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


def _write_tape_session(cache_dir, date_iso, ce_ticks,
                        spot_price=23000.0, header=True):
    """Write the smallest replayable session: instruments master + tape
    with (optional) header, one spot tick, and `ce_ticks` as a list of
    (ts_suffix, price) for the CE token. Shared by the archive and
    parse-semantics test classes so the master schema lives ONCE."""
    import json as _json
    ticks_dir = cache_dir / "ticks"
    ticks_dir.mkdir(parents=True, exist_ok=True)
    ymd = date_iso.replace("-", "")
    (cache_dir / f"instruments_NIFTY_{ymd}.csv").write_text(
        "instrument_token,exchange_token,tradingsymbol,name,last_price,"
        "expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
        "111,1,NIFTY26JAN23000CE,NIFTY,0.0,2026-01-27,23000.0,0.05,65,"
        "CE,NFO-OPT,NFO\n"
    )
    lines = []
    if header:
        lines.append(_json.dumps({"instruments": [
            {"token": 999, "tradingsymbol": "NIFTY 50"},
            {"token": 111, "tradingsymbol": "NIFTY26JAN23000CE"},
        ]}))
    lines.append(_json.dumps({
        "instrument_token": 999,
        "exchange_timestamp": f"{date_iso} 09:15:00",
        "last_price": spot_price,
    }))
    for ts_suffix, price in ce_ticks:
        lines.append(_json.dumps({
            "instrument_token": 111,
            "exchange_timestamp": f"{date_iso} {ts_suffix}",
            "last_price": price,
        }))
    raw = ticks_dir / f"ticks-{date_iso}.jsonl"
    raw.write_text("\n".join(lines) + "\n")
    return raw


class TestZstTapeArchives:
    """2026-07-02: tick-retention.sh keeps only the newest 8 sessions as
    raw .jsonl and zstd-compresses the rest. list_captured_sessions used
    to glob *.jsonl only, capping the autoresearch replay window at ~a
    week — the 06-20/06-27 flat-plateau sweeps. Archives must be listed
    and streamable, and a corrupt archive must fail loud (Rule 12), not
    truncate a replay into a fake 0-trade session."""

    @pytest.fixture
    def ticks_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "data_cache" / "ticks"
        d.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        return d

    @pytest.fixture
    def zstd_bin(self):
        import shutil
        path = shutil.which("zstd")
        if not path:
            pytest.skip("system zstd binary not available")
        return path

    def test_listing_includes_archives_and_dedupes(self, ticks_dir):
        (ticks_dir / "ticks-2026-01-05.jsonl").write_text("{}\n")
        (ticks_dir / "ticks-2026-01-06.jsonl.zst").write_bytes(b"")
        # Same date in both forms must count once.
        (ticks_dir / "ticks-2026-01-07.jsonl").write_text("{}\n")
        (ticks_dir / "ticks-2026-01-07.jsonl.zst").write_bytes(b"")
        assert list_captured_sessions() == [
            "2026-01-05", "2026-01-06", "2026-01-07",
        ]

    def _write_minimal_session(self, ticks_dir, date_iso):
        return _write_tape_session(ticks_dir.parent, date_iso,
                                   ce_ticks=[("09:15:01", 101.5)])

    def test_tape_path_prefers_raw_over_archive(self, ticks_dir):
        (ticks_dir / "ticks-2026-01-07.jsonl").write_text('{"src": "raw"}\n')
        (ticks_dir / "ticks-2026-01-07.jsonl.zst").write_bytes(b"not zstd")
        assert backtest._tape_path("2026-01-07").suffix == ".jsonl"

    def test_archive_is_replayable(self, ticks_dir, zstd_bin):
        """An archived-only session must load end-to-end — DuckDB's ndjson
        reader decompresses the .zst directly (no zstd subprocess)."""
        import subprocess
        raw = self._write_minimal_session(ticks_dir, "2026-01-06")
        subprocess.run(
            [zstd_bin, "-q", str(raw), "-o", f"{raw}.zst"], check=True,
        )
        raw.unlink()
        df = load_captured_tape("2026-01-06")
        assert df[df["symbol"] == "NIFTY26JAN23000CE"]["last_price"].tolist() == [101.5]

    def test_corrupt_archive_fails_loud(self, ticks_dir, zstd_bin):
        """A corrupt archive must raise, not truncate a replay into a fake
        0-trade session (Rule 12)."""
        self._write_minimal_session(ticks_dir, "2026-01-06")
        (ticks_dir / "ticks-2026-01-06.jsonl").unlink()
        (ticks_dir / "ticks-2026-01-06.jsonl.zst").write_bytes(b"garbage")
        with pytest.raises(Exception,
                           match="(?i)zst|compress|invalid|malformed|frame"):
            load_captured_tape("2026-01-06")

    def test_truncated_archive_fails_loud(self, ticks_dir, zstd_bin):
        """2026-07-12 review finding: a TRUNCATED (partially decompressible)
        archive is the case DuckDB's ignore_errors silently PARTIAL-READS —
        94,932 of 200,000 rows with no error. The zstd -t gate must catch
        it before the scan, or a silently short session biases every sweep
        it enters (Rule 12)."""
        import subprocess
        raw = self._write_minimal_session(ticks_dir, "2026-01-06")
        # Pad the session so truncation lands mid-stream, then cut ~40%.
        with raw.open("a") as f:
            for i in range(5000):
                f.write('{"instrument_token": 111, "exchange_timestamp": '
                        f'"2026-01-06 09:{16 + i // 60 % 40:02d}:{i % 60:02d}", '
                        '"last_price": 100.0}\n')
        subprocess.run([zstd_bin, "-q", str(raw), "-o", f"{raw}.zst"], check=True)
        raw.unlink()
        zst = ticks_dir / "ticks-2026-01-06.jsonl.zst"
        blob = zst.read_bytes()
        zst.write_bytes(blob[: int(len(blob) * 0.6)])
        with pytest.raises(RuntimeError, match="zstd -t"):
            load_captured_tape("2026-01-06")

    def test_missing_header_fails_loud(self, ticks_dir):
        """2026-07-12 review finding: a tape whose header line is corrupt
        or absent used to load with an empty token→symbol map, silently
        dropping the spot stream (all-NaN underlying_price downstream).
        Both failure shapes must raise at the load site: a MISSING header
        (first line is a valid tick — the old json.loads accepted it
        silently) and a CORRUPT first line."""
        import json
        _write_tape_session(ticks_dir.parent, "2026-01-06",
                            ce_ticks=[("09:15:01", 101.5)], header=False)
        with pytest.raises(ValueError, match="not a session header"):
            load_captured_tape("2026-01-06")

        tape = ticks_dir / "ticks-2026-01-06.jsonl"
        tape.write_text('{"instruments": [TRUNCATED\n' + tape.read_text())
        with pytest.raises(json.JSONDecodeError):
            load_captured_tape("2026-01-06")

    def test_tape_path_missing_raises(self, ticks_dir):
        with pytest.raises(FileNotFoundError):
            backtest._tape_path("2099-01-01")


# ── Fix B (2026-05-31): IV/skew seeding so autoresearch tunables can bind ──
# Diagnosis: a captured-tape replay fires one entry scan per session; without
# a primed IV history _compute_iv_percentile can't rank a percentile (<30 obs
# → warmup), so the IV-percentile / regime tunables were inert in the weekly
# sweep. (Pre-#75 that warmup path returned a neutral 50.0; it now returns
# None and the scan skips — the seed lets it leave warmup and produce a real
# percentile either way.)

class TestLoadIVSkewSeed:
    def test_truncation_drops_most_recent_k(self, tmp_path, monkeypatch):
        """drop_recent must trim the TAIL (most-recent) of each series — the
        coarse guard against a replay ranking against its own/future IV."""
        import json
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
    them ALL (the prior code left _skew_history loaded from the live JSON —
    a silent, asymmetric look-ahead leak, and later the daily ATM-IV pool
    leaked from the on-disk EOD archive the same way).

    Note which window does what: `_compute_iv_percentile` ranks against the
    DAILY pool, so that is the seed which decides whether the IV gate opens
    and the scan reaches `_compute_skew_percentile` at all. The tick-level
    `_atm_iv_history` still feeds vol-of-vol and the current-IV reading, so
    it is seeded and asserted separately.
    """

    @staticmethod
    def _daily_pool(n=40, iv=0.15, end=date(2026, 2, 28)):
        """Dated daily ATM-IV pool ending BEFORE generate_synthetic_data's
        first session (2026-03-01), so the look-ahead guard keeps all of it."""
        return [(end - timedelta(days=n - 1 - i), iv + 0.001 * i)
                for i in range(n)]

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
        # Neutralize the IV-percentile band so the scan deterministically
        # reaches _compute_skew_percentile: generate_synthetic_data draws
        # unseeded np.random noise, so the seeded ATM IV's percentile lands
        # in/out of the promoted best_params band [8,43] at random — without
        # this override the skew observation (and thus the test) is flaky.
        run_backtest(data, underlying="NIFTY",
                     tunable_params={"entry_iv_percentile_min": 0.0,
                                     "entry_iv_percentile_max": 100.0},
                     seed_iv_history=[0.15] * 40,
                     seed_skew_history=[0.01] * 35,
                     seed_daily_iv=self._daily_pool())
        # The single entry scan saw the full seeded prefix → can leave the
        # 30-obs warmup, so the IV-percentile / regime tunables can bind.
        assert seen["iv"] == 40
        assert seen["skew"] == 35

    def test_default_wipes_both_no_skew_leak(self, monkeypatch):
        data = generate_synthetic_data(days=2, ticks_per_day=12)
        band = {"entry_iv_percentile_min": 0.0, "entry_iv_percentile_max": 100.0}

        # (1) IV default-wipe: with no seed, _atm_iv_history must START empty
        # even though __init__ loaded the live persisted history, and the
        # daily pool must fall back to this 2-session tape rather than the
        # on-disk archive. The first _compute_iv_percentile call sees length
        # 0 (recorded before its own append) and returns None — a 2-session
        # pool cannot leave the 30-session warmup — so the scan short-circuits
        # BEFORE _compute_skew_percentile, which is why the skew-leak guard
        # below needs its own run.
        seen_iv = self._spy_first_call_lengths(monkeypatch)
        run_backtest(data, underlying="NIFTY", tunable_params=dict(band))
        assert seen_iv["iv"] == 0
        assert "skew" not in seen_iv  # None IV short-circuits before skew

        # (2) Skew default-wipe (the leak this test is named for): seed the
        # DAILY pool (≥30 sessions) so the gate passes and the scan reaches
        # _compute_skew_percentile with NO skew seed. Pre-fix, _skew_history
        # started non-empty (leaked from the live JSON) — pin that it's empty.
        seen_skew = self._spy_first_call_lengths(monkeypatch)
        run_backtest(data, underlying="NIFTY", tunable_params=dict(band),
                     seed_iv_history=[0.15] * 40,
                     seed_daily_iv=self._daily_pool())
        assert seen_skew["skew"] == 0


class TestTapeParseSemantics:
    """load_captured_tape's parse phase (DuckDB read_ndjson since the
    2026-07-12 storage increment 2; chunked json.loads before that) must
    keep the resample's contract: last tick IN FILE ORDER per bucket,
    'tick' resolution returns every line, out-of-session timestamps drop
    loudly. File order is what DuckDB's insertion-order preservation
    provides — a parallel-reordering regression in the parse query would
    silently rewrite session prices and reshuffle every sweep ranking."""

    SESSION = "2026-01-05"

    @pytest.fixture
    def synthetic_session(self, tmp_path, monkeypatch):
        # Seven CE ticks inside ONE 1-min bucket. Prices are chosen so
        # the correct answer (3.0, last in file order) differs from
        # first-seen (5.0), max (9.0), and min (1.0) — a parse that
        # reorders and keeps any of those is caught, not just an unlucky
        # reorder. The final two ticks share the SAME second: Kite's
        # exchange_timestamp is second-granular, so same-second ticks are
        # routine, and only FILE order (not timestamp order) can break
        # that tie the way the pre-DuckDB parser did.
        _write_tape_session(
            tmp_path / "data_cache", self.SESSION,
            ce_ticks=[(f"09:15:{s:02d}", p) for s, p in
                      [(1, 5.0), (2, 2.0), (3, 9.0), (4, 1.0),
                       (5, 4.0), (7, 8.0), (7, 3.0)]],
        )
        monkeypatch.chdir(tmp_path)
        return self.SESSION

    def test_bucket_keeps_file_order_last(self, synthetic_session):
        """WHY: the 1-min bucket must resolve to the last tick in FILE
        order — including across the same-second tie in the fixture
        (8.0 then 3.0 at :07). Keeping first-seen/max/min/tie-reordered
        instead would silently rewrite session prices and reshuffle
        every sweep ranking."""
        df = load_captured_tape(self.SESSION)
        ce = df[df["symbol"] == "NIFTY26JAN23000CE"]
        assert ce["last_price"].tolist() == [3.0]

    def test_tick_resolution_returns_every_line(self, synthetic_session):
        """WHY: resolution='tick' must return every line — bucket-last
        is a resampling concern and must NOT deduplicate same-bucket
        (or same-second) tick rows when no resampling was requested."""
        df = load_captured_tape(self.SESSION, resolution="tick")
        ce = df[df["symbol"] == "NIFTY26JAN23000CE"]
        assert len(ce) == 7

    def test_epoch_zero_ticks_dropped(
        self, synthetic_session, tmp_path, caplog,
    ):
        """WHY: Kite full-mode sends exchange_timestamp 1970-01-01 for a
        token's pre-first-trade snapshot. ONE such tick makes resample()
        materialize per-token minute bins from 1970 to the session date
        (~30M bins) — 164 of them in ticks-2026-07-06 ballooned
        load_captured_tape to 16 GB and OOM-killed the 2026-07-11 weekly
        autoresearch sweep. Out-of-session timestamps must be dropped
        BEFORE resampling, and loudly (Rule 12)."""
        import json
        import logging
        tape = tmp_path / "data_cache" / "ticks" / f"ticks-{self.SESSION}.jsonl"
        with tape.open("a") as f:
            f.write(json.dumps({
                "instrument_token": 111,
                "exchange_timestamp": "1970-01-01T05:30:00",
                "last_price": 6.5,
            }) + "\n")
        with caplog.at_level(logging.WARNING, logger="backtest"):
            df = load_captured_tape(self.SESSION)
        assert df["timestamp"].dt.strftime("%Y-%m-%d").eq(self.SESSION).all(), (
            "Out-of-session timestamps leaked into the replay frame — "
            "resample() will span decades of minute bins and OOM."
        )
        assert any("dropped 1 ticks" in r.message for r in caplog.records), (
            "Silent drop: the out-of-session filter must warn (Rule 12)."
        )

    def test_unparseable_timestamp_counted_in_drop_warning(
        self, synthetic_session, tmp_path, caplog,
    ):
        """WHY (2026-07-12 review finding): a tick whose exchange_timestamp
        fails the TIMESTAMP cast is NULLed by ignore_errors; filtering it
        in SQL made it vanish from the 'dropped N ticks' count the old
        loader reported. A capture-format regression that mangles
        timestamps must stay operator-visible in that warning."""
        import json
        import logging
        tape = tmp_path / "data_cache" / "ticks" / f"ticks-{self.SESSION}.jsonl"
        with tape.open("a") as f:
            f.write(json.dumps({
                "instrument_token": 111,
                "exchange_timestamp": "not-a-timestamp",
                "last_price": 6.5,
            }) + "\n")
        with caplog.at_level(logging.WARNING, logger="backtest"):
            df = load_captured_tape(self.SESSION)
        ce = df[df["symbol"] == "NIFTY26JAN23000CE"]
        assert ce["last_price"].tolist() == [3.0]  # bucket-last unaffected
        assert any("dropped 1 ticks" in r.message for r in caplog.records), (
            "Unparseable-timestamp tick dropped without being counted "
            "in the warning (Rule 12)."
        )


class TestEmptyDataFailsLoud:
    def test_run_backtest_refuses_empty_frame(self):
        """2026-07-12: an empty (fully-filtered stillborn) tape used to die
        deep in MockKite as 'single positional indexer is out-of-bounds',
        which autoresearch's per-cycle except scored as -999999 — flattening
        the whole sweep. Empty input must be a clear, immediate error."""
        import pandas as pd
        with pytest.raises(ValueError, match="EMPTY data frame"):
            run_backtest(pd.DataFrame())

    def test_list_captured_sessions_ignores_quarantined_tapes(
        self, tmp_path, monkeypatch,
    ):
        """Quarantine convention: renaming a stillborn tape to *.stillborn
        must remove it from the replay universe (and from tick-retention's
        globs) without deleting the forensic evidence."""
        ticks = tmp_path / "data_cache" / "ticks"
        ticks.mkdir(parents=True)
        (ticks / "ticks-2026-06-25.jsonl.zst").write_bytes(b"x")
        (ticks / "ticks-2026-06-26.jsonl.zst.stillborn").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)
        assert list_captured_sessions() == ["2026-06-25"]
