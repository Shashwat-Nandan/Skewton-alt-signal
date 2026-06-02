"""Tests for the arbitrage-paper dashboard router.

The one bug class that matters here: realized_pnl in each EOD sidecar is
CUMULATIVE across sessions (the strategy restores and accumulates it every
morning). So the daily P&L column must come from the per-session delta fields,
and the cumulative curve must be taken straight from the report — never built
by summing the daily values, or every day past the first double-counts the
whole book.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import backend.routers.arbitrage_paper as ap


def _write_sidecar(dc, d: date, *, realized, unrealized, sess_r, sess_u,
                   closed=0, opens=None):
    """Write a sidecar shaped exactly like run_paper_arbitrage.write_eod_sidecar
    (payload.report == ArbitrageStrategy.generate_eod_report())."""
    report = {
        "strategy": "arbitrage",
        "open_calendars": opens or [],
        "realized_pnl": realized,        # cumulative since inception
        "unrealized_pnl": unrealized,    # point-in-time open MTM
        "transaction_costs": 0.0,
        "n_closed_trades": closed,
        "last_basis_snapshot": [],
        "universe_size": 50,
        "session_realized_delta": sess_r,
        "session_unrealized_delta": sess_u,
    }
    payload = {"date": d.isoformat(), "generated_at": "x",
               "system": "baseline", "report": report}
    (dc / f"arbitrage_paper_eod_{d.isoformat()}.json").write_text(json.dumps(payload))


def test_cumulative_is_not_double_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "DATA_CACHE", tmp_path)
    # Two consecutive trading days. Book grows 1000 -> 1500 cumulative.
    # Day 1 made +1000 this session; day 2 made +500 this session.
    d1, d2 = date(2026, 4, 16), date(2026, 4, 17)  # Thu, Fri
    _write_sidecar(tmp_path, d1, realized=1000.0, unrealized=0.0,
                   sess_r=1000.0, sess_u=0.0, closed=1)
    _write_sidecar(tmp_path, d2, realized=1500.0, unrealized=0.0,
                   sess_r=500.0, sess_u=0.0, closed=2)

    resp = ap.arbitrage_paper(days=2, end=d2.isoformat(), system="baseline")

    rows = {r.date: r for r in resp.daily if r.has_data}
    # Per-day P&L is the SESSION delta, not the cumulative figure.
    assert rows[d1.isoformat()].day_pnl == 1000.0
    assert rows[d2.isoformat()].day_pnl == 500.0
    # Cumulative comes from the report directly — day 2 shows 1500, NOT
    # 1000 + 1500 = 2500 (the double-count bug this test guards).
    assert rows[d1.isoformat()].cumulative_net_pnl == 1000.0
    assert rows[d2.isoformat()].cumulative_net_pnl == 1500.0
    # Summary headline = latest cumulative book P&L.
    assert resp.summary.net_pnl == 1500.0
    assert resp.summary.n_closed_trades == 2  # latest snapshot, not a sum


def test_summary_includes_open_mtm_in_net(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "DATA_CACHE", tmp_path)
    d = date(2026, 4, 17)
    _write_sidecar(tmp_path, d, realized=200.0, unrealized=-50.0,
                   sess_r=200.0, sess_u=-50.0,
                   opens=[{"symbol": "AAA", "position": "LONG_CALENDAR",
                           "entry_carry_diff": 0.03,
                           "legs": [{"tradingsymbol": "AAA26APRFUT", "qty": -1,
                                     "entry": 100.0, "current": 99.0}]}])
    resp = ap.arbitrage_paper(days=1, end=d.isoformat(), system="baseline")
    assert resp.summary.realized_pnl == 200.0
    assert resp.summary.unrealized_pnl == -50.0
    assert resp.summary.net_pnl == 150.0
    assert resp.summary.n_open_calendars == 1
    assert resp.open_calendars[0].symbol == "AAA"


def test_missing_sidecar_marks_no_data(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "DATA_CACHE", tmp_path)
    d = date(2026, 4, 17)
    resp = ap.arbitrage_paper(days=3, end=d.isoformat(), system="baseline")
    assert all(not r.has_data for r in resp.daily)
    assert resp.summary.n_days_with_data == 0
    assert resp.summary.net_pnl == 0.0
