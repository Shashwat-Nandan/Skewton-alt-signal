"""Tests for the delivery-accum Phase-D surface: delivery_* DB helpers,
/api/delivery/* endpoints, and the runner's queue→fill lifecycle.

The runner's whole reason to exist is fill-model parity with the backtest
(next-open queue, gap-skip, max-age), so the lifecycle tests drive the real
runner functions against a seeded DB rather than re-implementing the rules.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import db, run_manager as rm
from backend.main import create_app

log = logging.getLogger("test_delivery_runner_db")


@pytest.fixture
def tmp_db(tmp_path):
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    db.init_schema()
    yield
    db.reset_for_tests(None)


@pytest.fixture
def client(tmp_db, tmp_path, monkeypatch):
    from backend.routers import delivery_accum as da_router
    from tests._helpers import login_client
    monkeypatch.setattr(da_router, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    app = create_app()
    with TestClient(app) as c:
        login_client(c)
        yield c


def _pos_dict(symbol="RELIANCE", **overrides):
    d = {
        "symbol": symbol, "side": "LONG",
        "entry_dt": "2026-07-21T18:45:00",
        "entry_px": 1300.0, "qty": 10,
        "initial_sl": 1260.0, "current_sl": 1260.0,
        "target": 1400.0, "atr_at_entry": 13.0,
        "rationale": "deliv pctile=0.95 hits5d=3; rangePos=0.22",
        "last_mtm_dt": "2026-07-21T18:45:00",
        "last_mtm_px": 1300.0,
        "high_watermark": 1300.0,
    }
    d.update(overrides)
    return d


class TestDbHelpers:

    def test_position_roundtrip(self, tmp_db):
        pid = db.insert_delivery_position(_pos_dict(), opened_by_scan="close")
        rows = db.list_delivery_positions(status="OPEN")
        assert len(rows) == 1 and rows[0]["id"] == pid
        db.close_delivery_position(pid, exit_dt="2026-07-22T18:45:00",
                                   exit_px=1350.0, exit_reason="TARGET_HIT", pnl=450.0)
        assert db.list_delivery_positions(status="OPEN") == []
        closed = db.list_delivery_positions(status="CLOSED")
        assert closed[0]["pnl"] == pytest.approx(450.0)
        assert closed[0]["exit_reason"] == "TARGET_HIT"

    def test_tables_isolated_from_equity(self, tmp_db):
        """A delivery insert must never appear in the swing's book — the whole
        point of per-strategy tables is that this paper book can't pollute
        the deployed swing's P&L or vice versa."""
        db.insert_delivery_position(_pos_dict(), opened_by_scan="close")
        assert db.list_equity_positions() == []

    def test_fill_pending_is_atomic(self, tmp_db):
        pending_id = db.insert_delivery_pending_entry(
            signal_dt="2026-07-21", symbol="RELIANCE", side="LONG",
            signal_close=1295.0, sl_distance=39.0, target_distance=97.5,
            atr=13.0, qty=10, rationale="test",
        )
        pid = db.fill_delivery_pending_entry(
            _pos_dict(), opened_by_scan="close",
            pending_id=pending_id, fill_px=1300.0,
        )
        assert db.list_delivery_positions(status="OPEN")[0]["id"] == pid
        filled = db.list_delivery_pending_entries(status="FILLED")
        assert len(filled) == 1
        assert f"id={pid}" in filled[0]["resolution_note"]

    def test_pending_dedupe_flag(self, tmp_db):
        assert not db.has_delivery_pending_entry_for_symbol("RELIANCE")
        db.insert_delivery_pending_entry(
            signal_dt="2026-07-21", symbol="RELIANCE", side="LONG",
            signal_close=1295.0, sl_distance=39.0, target_distance=97.5,
            atr=13.0, qty=10, rationale=None,
        )
        assert db.has_delivery_pending_entry_for_symbol("RELIANCE")


class TestRunnerLifecycle:
    """Drive the real runner fill/queue functions against a seeded DB."""

    def _strategy_with_panel(self, open_px=1300.0, day="2026-07-22"):
        from strategies.delivery_accumulation import DeliveryAccumulationStrategy

        class _NullKite:
            pass
        s = DeliveryAccumulationStrategy(_NullKite(), config_path="/dev/null", mode="paper")
        dates = pd.to_datetime([day])
        panel = pd.DataFrame({
            "date": dates, "symbol": "RELIANCE",
            "open": open_px, "high": open_px + 10, "low": open_px - 10,
            "close": open_px + 5, "volume": 1_000_000,
        })
        s.set_panel(panel)
        s.set_delivery_panel(pd.DataFrame(
            columns=["date", "symbol", "traded_qty", "deliv_qty", "deliv_per"]))
        s._ensure_features()
        return s

    def test_pending_fills_at_next_open(self, tmp_db):
        """The fill must land at the NEXT session's official open with SL/target
        re-anchored to it — the backtested fill model (Phase-D precondition)."""
        from runners.run_delivery_accum import _fill_pending_entries
        db.insert_delivery_pending_entry(
            signal_dt="2026-07-21", symbol="RELIANCE", side="LONG",
            signal_close=1295.0, sl_distance=39.0, target_distance=97.5,
            atr=13.0, qty=10, rationale="test",
        )
        s = self._strategy_with_panel(open_px=1300.0)
        filled, g, st, op = _fill_pending_entries(s, date(2026, 7, 22), "close", log)
        assert (filled, g, st, op) == (1, 0, 0, 0)
        pos = s.positions["RELIANCE"]
        assert pos.entry_px == pytest.approx(1300.0)
        assert pos.initial_sl == pytest.approx(1300.0 - 39.0)
        assert pos.target == pytest.approx(1300.0 + 97.5)
        assert db.list_delivery_positions(status="OPEN")

    def test_gap_skip(self, tmp_db):
        """A >1.5x-ATR overnight gap is a different setup than the screened
        one — the backtest skips it, so paper must too (EQ-FU-2)."""
        from runners.run_delivery_accum import _fill_pending_entries
        db.insert_delivery_pending_entry(
            signal_dt="2026-07-21", symbol="RELIANCE", side="LONG",
            signal_close=1295.0, sl_distance=39.0, target_distance=97.5,
            atr=13.0, qty=10, rationale="test",
        )
        s = self._strategy_with_panel(open_px=1295.0 + 13.0 * 1.6)
        filled, g, st, op = _fill_pending_entries(s, date(2026, 7, 22), "close", log)
        assert (filled, g) == (0, 1)
        assert not s.positions
        assert db.list_delivery_pending_entries(status="SKIPPED_GAP")

    def test_stale_skip(self, tmp_db):
        from runners.run_delivery_accum import _fill_pending_entries
        db.insert_delivery_pending_entry(
            signal_dt="2026-07-10", symbol="RELIANCE", side="LONG",
            signal_close=1295.0, sl_distance=39.0, target_distance=97.5,
            atr=13.0, qty=10, rationale="test",
        )
        s = self._strategy_with_panel()
        filled, g, st, op = _fill_pending_entries(s, date(2026, 7, 22), "close", log)
        assert (filled, st) == (0, 1)
        assert db.list_delivery_pending_entries(status="SKIPPED_STALE")

    def test_db_error_leaves_no_phantom_position(self, tmp_db, monkeypatch):
        """If the transactional DB fill fails, NOTHING may survive in the
        in-memory book — pre-fix the position was added first, so
        _persist_proposals later inserted an OPEN row whose own audit row
        said SKIPPED (code-review 2026-07-22)."""
        from runners.run_delivery_accum import _fill_pending_entries
        db.insert_delivery_pending_entry(
            signal_dt="2026-07-21", symbol="RELIANCE", side="LONG",
            signal_close=1295.0, sl_distance=39.0, target_distance=97.5,
            atr=13.0, qty=10, rationale="test",
        )
        s = self._strategy_with_panel()

        def _boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(db, "fill_delivery_pending_entry", _boom)
        filled, g, st, op = _fill_pending_entries(s, date(2026, 7, 22), "close", log)
        assert filled == 0
        assert "RELIANCE" not in s.positions          # the actual invariant
        assert db.list_delivery_positions() == []
        assert db.list_delivery_pending_entries(status="SKIPPED_STALE")

    def test_queue_isolates_malformed_proposal(self, tmp_db):
        """One contract-violating proposal must not drop the rest of the
        day's entries (pre-fix a KeyError aborted the loop); it is counted
        so the caller can exit non-zero (code-review 2026-07-22)."""
        from core.trade_proposer import TradeProposal
        from runners.run_delivery_accum import _queue_pending_entries

        def _prop(sym, snap):
            return TradeProposal(
                tradingsymbol=sym, instrument_token=0, strike=0.0, expiry="",
                option_type="EQ", lot_size=1, quantity=10, price=100.0,
                transaction_type="BUY", iv=0.0, bid_ask_spread_pct=0.0,
                margin_required=1000.0, rationale="t", greeks_snapshot=snap)

        bad = _prop("BADSYM", {"atr": 4.0})  # missing entry/sl/target
        good = _prop("GOODSYM", {"atr": 4.0, "entry": 100.0, "sl": 88.0, "target": 130.0})
        n, n_malformed = _queue_pending_entries([bad, good], date(2026, 7, 22), log)
        assert (n, n_malformed) == (1, 1)
        pending = db.list_delivery_pending_entries(status="PENDING")
        assert [p["symbol"] for p in pending] == ["GOODSYM"]

    def test_resume_counts_unparseable_rows(self, tmp_db):
        """A corrupt OPEN row must be counted (→ non-zero exit) — it is a
        phantom position that frees its symbol slot for double exposure
        (code-review 2026-07-22)."""
        from runners.run_delivery_accum import _load_open_positions_into_strategy
        db.insert_delivery_position(_pos_dict(symbol="OKSYM"), opened_by_scan="close")
        bad_id = db.insert_delivery_position(_pos_dict(symbol="BADSYM"), opened_by_scan="close")
        db.get_conn().execute(
            "UPDATE delivery_positions SET current_sl = 'garbage' WHERE id = ?",
            (bad_id,))
        s = self._strategy_with_panel()
        n, n_bad = _load_open_positions_into_strategy(s, log)
        assert (n, n_bad) == (1, 1)
        assert "OKSYM" in s.positions and "BADSYM" not in s.positions

    def test_stale_check_never_raises(self, tmp_path, monkeypatch):
        """The staleness guard must never abort the run — empty dir, junk
        filenames, or a missing dir all degrade to a log line
        (code-review 2026-07-22: a NaT crashed the old parquet-sampling
        version daily until the file was hand-deleted)."""
        import runners.run_delivery_accum as runner
        monkeypatch.setattr(runner, "RAW_DELIV_DIR", tmp_path / "missing")
        runner._warn_if_delivery_stale(date(2026, 7, 22), log)  # missing dir
        raw = tmp_path / "raw"
        raw.mkdir()
        (raw / "deliv_garbage.parquet").write_text("junk")
        (raw / "deliv_20260721.parquet").write_text("junk")  # only the NAME is read
        monkeypatch.setattr(runner, "RAW_DELIV_DIR", raw)
        runner._warn_if_delivery_stale(date(2026, 7, 22), log)  # no raise


class TestEndpoints:

    def test_positions_empty_then_seeded(self, client):
        assert client.get("/api/delivery/positions").json() == {"positions": []}
        db.insert_delivery_position(_pos_dict(), opened_by_scan="close")
        body = client.get("/api/delivery/positions").json()
        assert len(body["positions"]) == 1
        assert body["positions"][0]["symbol"] == "RELIANCE"

    def test_pending_endpoint_defaults_to_pending(self, client):
        db.insert_delivery_pending_entry(
            signal_dt="2026-07-21", symbol="RELIANCE", side="LONG",
            signal_close=1295.0, sl_distance=39.0, target_distance=97.5,
            atr=13.0, qty=10, rationale=None,
        )
        body = client.get("/api/delivery/pending-entries").json()
        assert len(body["pending"]) == 1
        assert body["pending"][0]["status"] == "PENDING"

    def test_scans_endpoint(self, client):
        db.insert_delivery_scan(
            scan_dt="2026-07-22T18:45:00", scan_kind="close", mode="paper",
            n_signals=2, n_trades=1, n_open_positions=1, n_closed_today=0,
            notes="queued 2 pending entry(ies) for next session",
        )
        body = client.get("/api/delivery/scans").json()
        assert body["scans"][0]["scan_kind"] == "close"

    def test_bad_status_400(self, client):
        assert client.get("/api/delivery/positions?status=bogus").status_code == 400
