"""Tests for the next-day-open fill flow in run_equity_swing.

The close scan no longer fills entries at signal-day close. Instead it
queues entries into ``equity_pending_entries`` and the next close scan
fills them at that day's official open. These tests pin that contract:

  - QUEUE: scan_and_propose's proposals land as PENDING rows with the
    correct absolute SL/target distances.
  - FILL: PENDING rows materialise into ``equity_positions`` at the
    next-day open, with SL/target re-anchored to the fill price.
  - SKIP_GAP: if next-day open gaps > 1.5×ATR from signal close, the
    pending is dropped (the setup is no longer valid at the new price).
  - SKIP_STALE: pending entries older than the max-age horizon are
    dropped; ditto if no panel bar exists for today.
  - DEDUPE: a fresh signal for an already-PENDING symbol is skipped, so
    a same-day re-run cannot double-queue.

These tests verify *intent* per CLAUDE.md Rule 9: each assertion is
keyed to a business decision (entry price = next-day open, risk in ATR
terms preserved across the gap), not just that bytes round-trip through
SQLite.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend import db
from run_equity_swing import (
    _PENDING_GAP_ATR_THRESHOLD,
    _PENDING_MAX_AGE_DAYS,
    _fill_pending_entries,
    _queue_pending_entries,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _StubProposal:
    tradingsymbol: str
    quantity: int
    price: float
    rationale: str = ""
    greeks_snapshot: Optional[dict] = None


class _StubStrategy:
    """Minimal stand-in for VarsityEquitySwingStrategy.

    ``_fill_pending_entries`` only reads ``_features``, ``positions``,
    and ``_db_id_by_symbol`` — we replicate that surface without pulling
    in the whole strategy stack (which requires the EQ panel cache).
    """

    def __init__(self, features: Dict[str, pd.DataFrame]):
        self._features = features
        self.positions: Dict[str, Any] = {}
        self._db_id_by_symbol: Dict[str, int] = {}


def _make_feature_frame(dates_to_open: Dict[date, float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates_to_open.keys()])
    return pd.DataFrame({"open": list(dates_to_open.values())}, index=idx)


@pytest.fixture
def fresh_db(tmp_path):
    db.reset_for_tests(tmp_path / "test.db")
    db.init_schema()
    yield
    db.reset_for_tests(None)


@pytest.fixture
def log():
    return logging.getLogger("test_equity_pending_entries")


# ─────────────────────────────────────────────────────────────────────────────
# QUEUE: scan proposals land as PENDING rows
# ─────────────────────────────────────────────────────────────────────────────

class TestQueue:
    def test_proposal_persists_distances_not_absolute_levels(self, fresh_db, log):
        """SL/target are stored as ATR-based distances so the next-day fill
        can re-anchor them — storing the absolute SL/target from signal day
        would silently distort risk after an overnight gap."""
        signal_day = date(2026, 5, 25)
        prop = _StubProposal(
            tradingsymbol="DIVISLAB",
            quantity=6,
            price=6756.50,
            rationale="trend up; ADX=36.2",
            greeks_snapshot={
                "atr": 153.79, "entry": 6756.50,
                "sl": 6372.02, "target": 7525.46,  # SL=−2.5×ATR, TGT=+5.0×ATR
            },
        )
        n = _queue_pending_entries([prop], signal_day, log)
        assert n == 1

        rows = db.list_equity_pending_entries(status="PENDING")
        assert len(rows) == 1
        r = rows[0]
        assert r["symbol"] == "DIVISLAB"
        assert r["qty"] == 6
        assert r["signal_close"] == pytest.approx(6756.50)
        assert r["sl_distance"] == pytest.approx(6756.50 - 6372.02)
        assert r["target_distance"] == pytest.approx(7525.46 - 6756.50)
        assert r["atr"] == pytest.approx(153.79)
        assert r["signal_dt"] == signal_day.isoformat()

    def test_dedupe_skips_existing_pending(self, fresh_db, log):
        """Same-day re-runs (or two signals on consecutive days before any
        fill) must not produce two PENDING rows for the same symbol."""
        signal_day = date(2026, 5, 25)
        prop = _StubProposal(
            tradingsymbol="MARICO", quantity=52, price=823.35,
            greeks_snapshot={"atr": 17.59, "entry": 823.35,
                             "sl": 779.37, "target": 911.31},
        )
        assert _queue_pending_entries([prop], signal_day, log) == 1
        # Second call with the same symbol — should dedupe to 0.
        assert _queue_pending_entries([prop], signal_day, log) == 0
        rows = db.list_equity_pending_entries(status="PENDING")
        assert len(rows) == 1

    def test_invalid_distances_dropped(self, fresh_db, log):
        """A proposal with SL above entry (or target below entry) is a
        broken signal — drop rather than persist a poison row."""
        broken = _StubProposal(
            tradingsymbol="BAD", quantity=10, price=100.0,
            greeks_snapshot={"atr": 5.0, "entry": 100.0,
                             "sl": 110.0,    # inverted
                             "target": 120.0},
        )
        assert _queue_pending_entries([broken], date(2026, 5, 25), log) == 0
        assert db.list_equity_pending_entries(status="PENDING") == []

    def test_nan_distances_rejected(self, fresh_db, log):
        """NaN sl_distance / target_distance must NOT pass the >0 guard
        (NaN <= 0 is False in Python — a poison row would otherwise reach
        the DB and produce a position whose SL/target are NaN, making
        every check_and_rehedge comparison False so the position can never
        exit. Use `math.isfinite(x) and x > 0` semantics in the guard."""
        for bad_key, bad_value in [("sl", float("nan")), ("target", float("nan")),
                                    ("atr", float("nan")), ("atr", float("inf"))]:
            snap = {"atr": 5.0, "entry": 100.0, "sl": 95.0, "target": 110.0}
            snap[bad_key] = bad_value
            prop = _StubProposal(
                tradingsymbol=f"NAN_{bad_key}_{bad_value}",
                quantity=10, price=100.0, greeks_snapshot=snap,
            )
            n = _queue_pending_entries([prop], date(2026, 5, 25), log)
            assert n == 0, f"{bad_key}={bad_value} should have been rejected"
        assert db.list_equity_pending_entries(status="PENDING") == []

    def test_missing_required_snapshot_key_raises(self, fresh_db, log):
        """If scan_and_propose ever drops a required key from greeks_snapshot
        (atr/entry/sl/target), it's a strategy-contract violation that must
        crash loudly — silently dropping every signal masks the upstream
        refactor bug (CLAUDE.md Rule 12)."""
        for missing in ("atr", "entry", "sl", "target"):
            snap = {"atr": 5.0, "entry": 100.0, "sl": 95.0, "target": 110.0}
            snap.pop(missing)
            prop = _StubProposal(
                tradingsymbol=f"MISSING_{missing}", quantity=10, price=100.0,
                greeks_snapshot=snap,
            )
            with pytest.raises(KeyError, match=missing):
                _queue_pending_entries([prop], date(2026, 5, 25), log)


# ─────────────────────────────────────────────────────────────────────────────
# FILL: next-day open materialises the position
# ─────────────────────────────────────────────────────────────────────────────

class TestFill:
    def test_fills_at_next_day_open_and_reanchors_sl_target(self, fresh_db, log):
        """The whole point of the change: entry_px = next-day open, NOT
        signal-day close, and SL/target distances stay constant in ATR
        terms (so risk-per-trade doesn't drift with the overnight gap)."""
        signal_day = date(2026, 5, 25)
        fill_day = date(2026, 5, 26)

        # Signal: close=6756.50, ATR=153.79 → sl_distance=384.48, tgt_distance=768.96
        db.insert_equity_pending_entry(
            signal_dt=signal_day.isoformat(),
            symbol="DIVISLAB", side="LONG",
            signal_close=6756.50,
            sl_distance=384.48, target_distance=768.96,
            atr=153.79, qty=6,
            rationale="trend up",
        )

        # Next-day open gaps up by ~50 pts (0.32×ATR) — within tolerance.
        next_open = 6806.50
        strategy = _StubStrategy(
            features={"DIVISLAB": _make_feature_frame({fill_day: next_open})}
        )

        filled, skip_gap, skip_stale, skip_open = _fill_pending_entries(strategy, fill_day, "close", log)
        assert (filled, skip_gap, skip_stale, skip_open) == (1, 0, 0, 0)
        # last_mtm_dt must be seeded to the bar date, not None — otherwise
        # _persist_proposals would later write wall-clock now() into a
        # column meant to hold the bar date (verified directly here so a
        # regression in the seed line is caught even before _persist_proposals
        # runs).
        assert strategy.positions["DIVISLAB"].last_mtm_dt == pd.Timestamp(fill_day)

        # In-memory position uses the actual fill price.
        pos = strategy.positions["DIVISLAB"]
        assert pos.entry_px == pytest.approx(next_open)
        assert pos.entry_dt == pd.Timestamp(fill_day)
        # Re-anchored SL/target: distances preserved, levels shifted by the gap.
        assert pos.initial_sl == pytest.approx(next_open - 384.48)
        assert pos.target == pytest.approx(next_open + 768.96)
        # ATR distance from entry stays 2.5×ATR — the contract.
        assert (pos.entry_px - pos.initial_sl) / pos.atr_at_entry == pytest.approx(2.5, abs=0.01)

        # DB side: pending resolved, position row written.
        pending_rows = db.list_equity_pending_entries(status="PENDING")
        assert pending_rows == []
        filled_rows = db.list_equity_pending_entries(status="FILLED")
        assert len(filled_rows) == 1
        open_positions = db.list_equity_positions(status="OPEN")
        assert len(open_positions) == 1
        assert open_positions[0]["entry_px"] == pytest.approx(next_open)
        assert open_positions[0]["symbol"] == "DIVISLAB"

    def test_skips_when_open_gaps_beyond_atr_threshold(self, fresh_db, log):
        """If the open prints > 1.5×ATR away from the signal close, the
        setup that motivated the signal is no longer valid at the new
        price level — drop the pending rather than chase a worse entry."""
        signal_day = date(2026, 5, 25)
        fill_day = date(2026, 5, 26)
        db.insert_equity_pending_entry(
            signal_dt=signal_day.isoformat(),
            symbol="SUNPHARMA", side="LONG",
            signal_close=1840.60,
            sl_distance=104.22, target_distance=208.44,
            atr=41.69, qty=23,
            rationale="trend up",
        )
        # Gap = 80 pts ≈ 1.92×ATR > 1.5×ATR threshold → SKIPPED_GAP.
        big_gap_open = 1920.60
        strategy = _StubStrategy(
            features={"SUNPHARMA": _make_feature_frame({fill_day: big_gap_open})}
        )

        filled, skip_gap, skip_stale, skip_open = _fill_pending_entries(strategy, fill_day, "close", log)
        assert (filled, skip_gap, skip_stale, skip_open) == (0, 1, 0, 0)
        assert "SUNPHARMA" not in strategy.positions
        assert db.list_equity_positions(status="OPEN") == []
        skipped = db.list_equity_pending_entries(status="SKIPPED_GAP")
        assert len(skipped) == 1
        # Note message always includes "exceeds" — pin to that, not to the
        # float-formatted gap which depends on arithmetic.
        assert "exceeds" in (skipped[0]["resolution_note"] or "")

    def test_boundary_gap_just_under_threshold_fills(self, fresh_db, log):
        """A gap *just under* 1.5×ATR must still fill — the threshold is
        the boundary, not the spec for normal volatility."""
        signal_day = date(2026, 5, 25)
        fill_day = date(2026, 5, 26)
        atr = 20.0
        gap = (_PENDING_GAP_ATR_THRESHOLD - 0.05) * atr  # 1.45×ATR
        db.insert_equity_pending_entry(
            signal_dt=signal_day.isoformat(),
            symbol="EDGE", side="LONG",
            signal_close=1000.0,
            sl_distance=50.0, target_distance=100.0,
            atr=atr, qty=10, rationale="",
        )
        strategy = _StubStrategy(
            features={"EDGE": _make_feature_frame({fill_day: 1000.0 + gap})}
        )
        filled = _fill_pending_entries(strategy, fill_day, "close", log)[0]
        assert filled == 1

    def test_skips_when_signal_too_old(self, fresh_db, log):
        """A signal older than the max-age horizon must be dropped — a
        system that's been down for a week shouldn't wake up and fire
        ancient entries on whatever price prints today."""
        fill_day = date(2026, 5, 26)
        stale_signal_day = fill_day - timedelta(days=_PENDING_MAX_AGE_DAYS + 1)
        db.insert_equity_pending_entry(
            signal_dt=stale_signal_day.isoformat(),
            symbol="ZOMBIE", side="LONG",
            signal_close=500.0,
            sl_distance=10.0, target_distance=20.0,
            atr=4.0, qty=100, rationale="",
        )
        # Even with a *perfect* next-day open (zero gap), staleness wins.
        strategy = _StubStrategy(
            features={"ZOMBIE": _make_feature_frame({fill_day: 500.0})}
        )
        filled, skip_gap, skip_stale, skip_open = _fill_pending_entries(strategy, fill_day, "close", log)
        assert (filled, skip_gap, skip_stale, skip_open) == (0, 0, 1, 0)
        assert "ZOMBIE" not in strategy.positions

    def test_skips_when_panel_missing_today(self, fresh_db, log):
        """No bar for today (symbol dropped from universe, bhavcopy gap)
        → don't guess; mark stale and surface it via the resolution note."""
        signal_day = date(2026, 5, 25)
        fill_day = date(2026, 5, 26)
        db.insert_equity_pending_entry(
            signal_dt=signal_day.isoformat(),
            symbol="DROPPED", side="LONG",
            signal_close=100.0,
            sl_distance=5.0, target_distance=10.0,
            atr=2.0, qty=50, rationale="",
        )
        # features dict has the symbol but no row for fill_day.
        strategy = _StubStrategy(
            features={"DROPPED": _make_feature_frame({signal_day: 100.0})}
        )
        filled, skip_gap, skip_stale, skip_open = _fill_pending_entries(strategy, fill_day, "close", log)
        assert (filled, skip_gap, skip_stale, skip_open) == (0, 0, 1, 0)

    def test_skips_when_symbol_already_open(self, fresh_db, log):
        """If the symbol was already opened (e.g. recovered from DB),
        don't double-fill — drop the pending with status SKIPPED_OPEN
        (a distinct bucket from real staleness, so analytics aren't
        confounded by routine reentry suppression)."""
        signal_day = date(2026, 5, 25)
        fill_day = date(2026, 5, 26)
        db.insert_equity_pending_entry(
            signal_dt=signal_day.isoformat(),
            symbol="DUP", side="LONG",
            signal_close=100.0,
            sl_distance=5.0, target_distance=10.0,
            atr=2.0, qty=50, rationale="",
        )
        strategy = _StubStrategy(
            features={"DUP": _make_feature_frame({fill_day: 100.0})}
        )
        # Pretend a position already exists in memory.
        strategy.positions["DUP"] = object()
        filled, skip_gap, skip_stale, skip_open = _fill_pending_entries(strategy, fill_day, "close", log)
        assert (filled, skip_gap, skip_stale, skip_open) == (0, 0, 0, 1)
        # Status string MUST be SKIPPED_OPEN, not SKIPPED_STALE — pinning
        # the analytics contract.
        assert db.list_equity_pending_entries(status="SKIPPED_OPEN")
        assert db.list_equity_pending_entries(status="SKIPPED_STALE") == []


# ─────────────────────────────────────────────────────────────────────────────
# Robustness: per-row isolation, panel pathologies, stored-row corruption
# ─────────────────────────────────────────────────────────────────────────────

class TestRobustness:
    def test_one_bad_row_doesnt_abort_the_batch(self, fresh_db, log):
        """A single failing pending row must not take down the rest. The
        function's contract is per-row resolution; a panel pathology on
        one symbol shouldn't drop the other 11 signals on the floor.
        Triggered by hand-INSERTing a row with an unparseable signal_dt —
        date.fromisoformat raises ValueError on row 1, but row 2 must
        still fill cleanly."""
        fill_day = date(2026, 5, 26)
        # Row 1: corrupt signal_dt that will explode at parse time.
        conn = db.get_conn()
        conn.execute(
            "INSERT INTO equity_pending_entries "
            "(signal_dt, symbol, side, signal_close, sl_distance, target_distance, "
            " atr, qty, rationale, status, created_at) "
            "VALUES (?, 'CORRUPT', 'LONG', 100, 5, 10, 2, 10, '', 'PENDING', ?)",
            ("not-an-iso-date", "2026-05-25T18:31:00"),
        )
        # Row 2: well-formed signal that should fill.
        db.insert_equity_pending_entry(
            signal_dt=date(2026, 5, 25).isoformat(),
            symbol="GOOD", side="LONG",
            signal_close=100.0, sl_distance=5.0, target_distance=10.0,
            atr=2.0, qty=10, rationale="",
        )
        strategy = _StubStrategy(
            features={"CORRUPT": _make_feature_frame({fill_day: 100.0}),
                      "GOOD": _make_feature_frame({fill_day: 101.0})},
        )
        filled, skip_gap, skip_stale, skip_open = _fill_pending_entries(strategy, fill_day, "close", log)
        assert filled == 1, "well-formed row 2 must still fill despite row 1 error"
        assert skip_stale == 1, "row 1 marked stale via the per-row except handler"
        assert "GOOD" in strategy.positions
        assert "CORRUPT" not in strategy.positions

    def test_duplicate_date_panel_rows_dont_fill(self, fresh_db, log):
        """If the panel has two rows for today (re-ingest race, ADR
        collision), `.loc[today_ts, "open"]` returns a Series. Without a
        defensive scalar coercion, `float(Series)` raises and the whole
        scan dies. The fix marks the row stale instead — load-bearing
        behaviour for fail-loud-not-fail-everywhere."""
        signal_day = date(2026, 5, 25)
        fill_day = date(2026, 5, 26)
        db.insert_equity_pending_entry(
            signal_dt=signal_day.isoformat(),
            symbol="DUPDATE", side="LONG",
            signal_close=100.0, sl_distance=5.0, target_distance=10.0,
            atr=2.0, qty=10, rationale="",
        )
        # Build a feature frame with TWO rows at the same midnight Timestamp.
        idx = pd.DatetimeIndex([pd.Timestamp(fill_day), pd.Timestamp(fill_day)])
        dup_df = pd.DataFrame({"open": [100.5, 100.7]}, index=idx)
        strategy = _StubStrategy(features={"DUPDATE": dup_df})

        filled, skip_gap, skip_stale, skip_open = _fill_pending_entries(strategy, fill_day, "close", log)
        # The duplicate is caught by the per-row except → SKIPPED_STALE.
        # Critical: the function returns rather than raising.
        assert (filled, skip_gap, skip_open) == (0, 0, 0)
        assert skip_stale == 1
        assert "DUPDATE" not in strategy.positions

    def test_sqlite_schema_rejects_nan_in_not_null_columns(self, fresh_db, log):
        """Belt-and-braces check: SQLite's NOT NULL constraint rejects NaN
        for atr / sl_distance / target_distance / signal_close. This means
        the queue-side validation is the only realistic defense; the fill-
        side defense-in-depth code is unreachable in practice. If a future
        schema change relaxes NOT NULL this test will fail, prompting a
        review of the now-reachable code path."""
        conn = db.get_conn()
        for col in ("atr", "sl_distance", "target_distance", "signal_close"):
            kw = {"signal_close": 100, "sl_distance": 5,
                  "target_distance": 10, "atr": 2}
            kw[col] = float("nan")
            with pytest.raises(Exception):  # sqlite3.IntegrityError
                conn.execute(
                    "INSERT INTO equity_pending_entries "
                    "(signal_dt, symbol, side, signal_close, sl_distance, "
                    " target_distance, atr, qty, rationale, status, created_at) "
                    "VALUES (?, 'X', 'LONG', ?, ?, ?, ?, 1, '', 'PENDING', ?)",
                    ("2026-05-25", kw["signal_close"], kw["sl_distance"],
                     kw["target_distance"], kw["atr"], "now"),
                )


# ─────────────────────────────────────────────────────────────────────────────
# Atomicity: position INSERT + pending FILLED commit together (EQ-FU-3)
# ─────────────────────────────────────────────────────────────────────────────

class TestAtomicFill:
    def test_fill_rolls_back_position_when_pending_update_fails(
            self, fresh_db, monkeypatch):
        """``fill_pending_entry`` wraps the equity_positions INSERT and the
        pending-row FILLED flip in one transaction. If the status update
        fails between them, the INSERT must roll back — otherwise a crash
        mid-fill leaves an OPEN position whose source row is still PENDING,
        which the next run would re-read and double-fill. Simulate the crash
        by making the status update raise, then assert neither write stuck."""
        db.insert_equity_pending_entry(
            signal_dt="2026-05-25", symbol="ATOM", side="LONG",
            signal_close=100.0, sl_distance=5.0, target_distance=10.0,
            atr=2.0, qty=10, rationale="",
        )
        pending_id = db.list_equity_pending_entries(status="PENDING")[0]["id"]

        def boom(*_a, **_k):
            raise RuntimeError("simulated crash between the two writes")
        monkeypatch.setattr(db, "update_equity_pending_entry_status", boom)

        pos_dict = {
            "symbol": "ATOM", "side": "LONG",
            "entry_dt": "2026-05-26T00:00:00",
            "entry_px": 101.0, "qty": 10,
            "initial_sl": 96.0, "target": 121.0,
        }
        with pytest.raises(RuntimeError):
            db.fill_pending_entry(pos_dict, opened_by_scan="close",
                                  pending_id=pending_id, fill_px=101.0)

        # INSERT rolled back: no orphan OPEN position.
        assert db.list_equity_positions(status="OPEN") == []
        # Source row untouched: still PENDING, never silently FILLED.
        assert len(db.list_equity_pending_entries(status="PENDING")) == 1
        assert db.list_equity_pending_entries(status="FILLED") == []

    def test_fill_commits_both_writes_on_success(self, fresh_db):
        """Happy path through the atomic helper directly: one OPEN position,
        source row FILLED, and the FILLED note carries the new position id."""
        db.insert_equity_pending_entry(
            signal_dt="2026-05-25", symbol="ATOM2", side="LONG",
            signal_close=100.0, sl_distance=5.0, target_distance=10.0,
            atr=2.0, qty=10, rationale="",
        )
        pending_id = db.list_equity_pending_entries(status="PENDING")[0]["id"]
        pos_dict = {
            "symbol": "ATOM2", "side": "LONG",
            "entry_dt": "2026-05-26T00:00:00",
            "entry_px": 101.0, "qty": 10,
            "initial_sl": 96.0, "target": 121.0,
        }
        pid = db.fill_pending_entry(pos_dict, opened_by_scan="close",
                                    pending_id=pending_id, fill_px=101.0)
        opens = db.list_equity_positions(status="OPEN")
        assert len(opens) == 1 and opens[0]["id"] == pid
        filled = db.list_equity_pending_entries(status="FILLED")
        assert len(filled) == 1
        assert str(pid) in (filled[0]["resolution_note"] or "")


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: queue today → fill tomorrow
# ─────────────────────────────────────────────────────────────────────────────

class TestRoundTrip:
    def test_queue_today_fills_tomorrow(self, fresh_db, log):
        """The full lifecycle that the cron pair will see day-to-day."""
        day_n = date(2026, 5, 25)
        day_n1 = date(2026, 5, 26)

        prop = _StubProposal(
            tradingsymbol="ABB", quantity=6, price=6755.00,
            rationale="trend up",
            greeks_snapshot={"atr": 222.10, "entry": 6755.00,
                             "sl": 6199.75, "target": 7865.50},
        )
        # Day N close scan: queue.
        assert _queue_pending_entries([prop], day_n, log) == 1
        assert len(db.list_equity_pending_entries(status="PENDING")) == 1
        assert db.list_equity_positions(status="OPEN") == []

        # Day N+1 close scan: fill at next-day open (modest gap).
        next_open = 6780.00
        strategy = _StubStrategy(
            features={"ABB": _make_feature_frame({day_n1: next_open})}
        )
        filled = _fill_pending_entries(strategy, day_n1, "close", log)[0]
        assert filled == 1
        opens = db.list_equity_positions(status="OPEN")
        assert len(opens) == 1
        assert opens[0]["entry_dt"].startswith(day_n1.isoformat())
        assert opens[0]["entry_px"] == pytest.approx(next_open)
        # Same SL distance (2.5×ATR) relative to the actual entry.
        sl_dist = opens[0]["entry_px"] - opens[0]["initial_sl"]
        assert sl_dist == pytest.approx(6755.00 - 6199.75)
