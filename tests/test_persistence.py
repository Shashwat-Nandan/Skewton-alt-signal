"""Tests for the SQLite persistence layer (backend/db.py) and restart hydration."""
from __future__ import annotations

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend import db
from backend.run_manager import Run
from trade_proposer import TradeProposal


@pytest.fixture
def fresh_db(tmp_path):
    db.reset_for_tests(tmp_path / "test.db")
    db.init_schema()
    yield
    db.reset_for_tests(None)


def _make_run(strategy="taleb_karpathy", mode="signals", params=None) -> Run:
    return Run(
        id="run-abc-123",
        strategy_name=strategy,
        mode=mode,
        params=params or {"foo": 1, "bar": "baz"},
        created_at=datetime(2026, 5, 2, 10, 0),
    )


def _make_proposal(symbol="NIFTY26APR22000CE", side="BUY", qty=1, price=150.0) -> TradeProposal:
    return TradeProposal(
        tradingsymbol=symbol, instrument_token=1, strike=22000,
        expiry="2026-04-28", option_type="CE", lot_size=25, quantity=qty,
        price=price, transaction_type=side, iv=0.15, bid_ask_spread_pct=0.5,
        margin_required=10000, rationale="test",
    )


# ──────────────────────────────────────────────────────────
# Schema + run lifecycle
# ──────────────────────────────────────────────────────────

class TestRunPersistence:
    def test_insert_then_get(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        row = db.get_run(run.id)
        assert row is not None
        assert row["strategy_name"] == "taleb_karpathy"
        assert row["mode"] == "signals"
        assert row["params"] == {"foo": 1, "bar": "baz"}
        assert row["status"] == "RUNNING"
        assert row["tick_count"] == 0
        assert row["n_signals"] == 0
        assert row["n_trades"] == 0

    def test_update_status_sets_stopped_at_once(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        ts = datetime(2026, 5, 2, 11, 0)
        db.update_run_status(run.id, "STOPPED", stopped_at=ts)
        row = db.get_run(run.id)
        assert row["status"] == "STOPPED"
        assert row["stopped_at"] == ts.isoformat()

        # Subsequent COALESCE-protected update preserves first stopped_at
        db.update_run_status(run.id, "ERRORED", error="oops")
        row = db.get_run(run.id)
        assert row["status"] == "ERRORED"
        assert row["error"] == "oops"
        assert row["stopped_at"] == ts.isoformat()  # unchanged

    def test_update_run_tick(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        db.update_run_tick(run.id, 7, datetime(2026, 5, 2, 12, 0), {"strategy": "X", "realized_pnl": 1234.5})
        row = db.get_run(run.id)
        assert row["tick_count"] == 7
        assert row["last_tick_at"] == "2026-05-02T12:00:00"
        assert row["last_eod_report"]["realized_pnl"] == 1234.5

    def test_list_runs_orders_newest_first(self, fresh_db):
        a = _make_run()
        a.id = "run-old"
        a.created_at = datetime(2026, 5, 1, 9, 0)
        b = _make_run()
        b.id = "run-new"
        b.created_at = datetime(2026, 5, 2, 9, 0)
        db.insert_run(a)
        db.insert_run(b)
        rows = db.list_runs()
        assert [r["id"] for r in rows] == ["run-new", "run-old"]


# ──────────────────────────────────────────────────────────
# Proposals
# ──────────────────────────────────────────────────────────

class TestProposalPersistence:
    def test_append_increments_counter(self, fresh_db):
        run = _make_run(mode="paper")
        db.insert_run(run)
        prop = _make_proposal()
        result = {"status": "COMPLETE", "order_id": "PAPER-1", "mode": "paper"}
        db.append_proposal(run.id, "ENTRY", "trade", prop, result)
        row = db.get_run(run.id)
        assert row["n_trades"] == 1
        assert row["n_signals"] == 0

    def test_signal_vs_trade_counters(self, fresh_db):
        run = _make_run(mode="signals")
        db.insert_run(run)
        for _ in range(3):
            db.append_proposal(run.id, "ENTRY", "signal", _make_proposal(), {"status": "SIGNAL_LOGGED"})
        for _ in range(2):
            db.append_proposal(run.id, "REHEDGE", "trade", _make_proposal(), {"status": "COMPLETE"})
        row = db.get_run(run.id)
        assert row["n_signals"] == 3
        assert row["n_trades"] == 2

    def test_get_proposals_filters_by_source(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        db.append_proposal(run.id, "ENTRY", "signal", _make_proposal("AAA"), {"status": "S"})
        db.append_proposal(run.id, "ENTRY", "trade", _make_proposal("BBB"), {"status": "T"})

        signals = db.get_proposals(run.id, source="signal")
        trades = db.get_proposals(run.id, source="trade")
        assert len(signals) == 1
        assert len(trades) == 1
        assert signals[0]["tradingsymbol"] == "AAA"
        assert trades[0]["tradingsymbol"] == "BBB"

    def test_get_proposals_orders_chronologically(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        db.append_proposal(run.id, "ENTRY", "signal", _make_proposal("FIRST"), {"status": "S"})
        db.append_proposal(run.id, "ENTRY", "signal", _make_proposal("SECOND"), {"status": "S"})
        rows = db.get_proposals(run.id)
        assert [r["tradingsymbol"] for r in rows] == ["FIRST", "SECOND"]


# ──────────────────────────────────────────────────────────
# PnL snapshots
# ──────────────────────────────────────────────────────────

class TestPnLPersistence:
    def test_append_stores_components(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        db.append_pnl(run.id, {
            "strategy": "taleb_karpathy",
            "realized_pnl": 100.0,
            "unrealized_pnl": -50.0,
        })
        rows = db.get_pnl_history(run.id)
        assert len(rows) == 1
        assert rows[0]["report"]["realized_pnl"] == 100.0
        assert rows[0]["report"]["unrealized_pnl"] == -50.0

    def test_append_handles_missing_pnl_keys(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        # No realized/unrealized — pair-trading early-warmup case
        db.append_pnl(run.id, {"strategy": "pair_trading", "position": "FLAT"})
        rows = db.get_pnl_history(run.id)
        assert len(rows) == 1
        assert rows[0]["report"]["position"] == "FLAT"

    def test_append_none_report_still_logs(self, fresh_db):
        run = _make_run()
        db.insert_run(run)
        db.append_pnl(run.id, None)
        rows = db.get_pnl_history(run.id)
        assert len(rows) == 1
        assert rows[0]["report"] is None


# ──────────────────────────────────────────────────────────
# Restart hydration
# ──────────────────────────────────────────────────────────

class TestHydration:
    def test_orphan_runs_marked_stopped(self, fresh_db):
        # Two RUNNING + one STOPPING + one already STOPPED
        for run_id, status in [("r1", "RUNNING"), ("r2", "STOPPING"), ("r3", "RUNNING"),
                               ("r4", "STOPPED")]:
            r = _make_run()
            r.id = run_id
            r.status = status
            db.insert_run(r)
            db.update_run_status(run_id, status)

        n = db.mark_orphan_runs_stopped()
        assert n == 3  # r1, r2, r3

        for run_id in ("r1", "r2", "r3"):
            row = db.get_run(run_id)
            assert row["status"] == "STOPPED"
            assert "Backend restarted" in row["error"]
            assert row["stopped_at"] is not None

        # r4 already stopped — error should remain None
        r4 = db.get_run("r4")
        assert r4["status"] == "STOPPED"
        assert r4["error"] is None


# ──────────────────────────────────────────────────────────
# Foreign key cascade (defensive — confirms PRAGMA fk=ON works)
# ──────────────────────────────────────────────────────────

class TestForeignKeys:
    def test_proposal_insert_fails_for_unknown_run(self, fresh_db):
        with pytest.raises(Exception):
            db.append_proposal(
                "nonexistent-run", "ENTRY", "signal",
                _make_proposal(), {"status": "S"},
            )
