"""Tests for the buy-on-gap-paper dashboard router.

Same bug class as the arbitrage router: realized_pnl in each EOD sidecar is
CUMULATIVE across sessions, so the per-day column must come from the session
delta fields and the cumulative curve must be read straight from the report —
never built by summing daily values.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import backend.routers.buy_on_gap_paper as bg


def _write_sidecar(dc, d: date, *, realized, unrealized, sess_r, sess_u,
                   closed=0, win_rate=0.0, opens=None):
    """Shaped exactly like run_paper_buy_on_gap.write_eod_sidecar
    (payload.report == BuyOnGapStrategy.generate_eod_report())."""
    report = {
        "strategy": "buy_on_gap",
        "realized_pnl": realized,        # cumulative since inception
        "unrealized_pnl": unrealized,    # point-in-time open MTM
        "transaction_costs": 0.0,
        "n_closed_trades": closed,
        "session_realized_delta": sess_r,
        "session_unrealized_delta": sess_u,
        "win_rate": win_rate,
        "universe_size": 209,
        "open_positions": opens or [],
        "closed_today": [],
    }
    payload = {"date": d.isoformat(), "generated_at": "x",
               "system": "baseline", "report": report}
    (dc / f"buy_on_gap_paper_eod_{d.isoformat()}.json").write_text(json.dumps(payload))


def test_cumulative_is_not_double_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "DATA_CACHE", tmp_path)
    d1, d2 = date(2026, 4, 16), date(2026, 4, 17)  # Thu, Fri
    _write_sidecar(tmp_path, d1, realized=1000.0, unrealized=0.0,
                   sess_r=1000.0, sess_u=0.0, closed=3)
    _write_sidecar(tmp_path, d2, realized=1500.0, unrealized=0.0,
                   sess_r=500.0, sess_u=0.0, closed=7)

    resp = bg.buy_on_gap_paper(days=2, end=d2.isoformat(), system="baseline")
    rows = {r.date: r for r in resp.daily if r.has_data}
    # Per-day P&L is the SESSION delta.
    assert rows[d1.isoformat()].day_pnl == 1000.0
    assert rows[d2.isoformat()].day_pnl == 500.0
    # Cumulative read straight from each report — day 2 is 1500, NOT 2500.
    assert rows[d1.isoformat()].cumulative_net_pnl == 1000.0
    assert rows[d2.isoformat()].cumulative_net_pnl == 1500.0
    assert resp.summary.net_pnl == 1500.0
    assert resp.summary.n_closed_trades == 7  # latest snapshot, not a sum


def test_open_positions_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "DATA_CACHE", tmp_path)
    d = date(2026, 4, 17)
    _write_sidecar(tmp_path, d, realized=200.0, unrealized=-50.0,
                   sess_r=200.0, sess_u=-50.0, win_rate=0.6,
                   opens=[{"symbol": "INFY", "entry_px": 1500.0, "qty": 130,
                           "stop_px": 1425.0, "gap_z": -1.8, "last_mtm_px": 1492.0,
                           "pnl": 0.0}])
    resp = bg.buy_on_gap_paper(days=1, end=d.isoformat(), system="baseline")
    assert resp.summary.net_pnl == 150.0
    assert resp.summary.win_rate == 0.6
    assert resp.summary.n_open_positions == 1
    assert resp.open_positions[0].symbol == "INFY"
    assert resp.open_positions[0].gap_z == -1.8


def test_missing_sidecar_marks_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(bg, "DATA_CACHE", tmp_path)
    d = date(2026, 4, 17)
    resp = bg.buy_on_gap_paper(days=3, end=d.isoformat(), system="baseline")
    assert all(not r.has_data for r in resp.daily)
    assert resp.summary.n_days_with_data == 0
    assert resp.summary.net_pnl == 0.0
