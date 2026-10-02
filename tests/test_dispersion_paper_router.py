"""Dispersion paper books on the dashboard.

The page is how the owner compares the two books, so it must show what the
runner actually stored: state files written by the real strategy (not a
hand-made shape that can drift), a cumulative built from closed cycles in
expiry order, the hedge-only nature of an open book's running figure, and a
corrupt state file as an error rather than an empty, healthy-looking book.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from backend import db, run_manager as rm
from backend.main import create_app
from backend.routers import dispersion_paper as dr
from tests.test_dispersion_paper import (
    EXPIRY,
    NAMES,
    _roll_view,
    _strategy,
    _surface,
    _view,
)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setattr(dr, "DATA_CACHE", d)
    return d


def _settled_and_reopened(tmp_path, sizing="matched"):
    """One cycle settled by the real strategy, then the next one opened."""
    strat = _strategy(tmp_path, sizing=sizing)
    strat.on_close(_roll_view())
    day2 = date(2026, 10, 29)
    strat.on_close(_view(day2, {s: _surface(s, 110.0, 110.0) for s in NAMES}))
    strat.on_close(_view(EXPIRY, {s: _surface(s, 110.0, 110.0) for s in NAMES}))
    assert strat.book is None and len(strat.closed) == 1
    return strat


def test_paper_book_is_read_from_the_real_state_shape(tmp_path, cache):
    strat = _settled_and_reopened(tmp_path)
    # An earlier cycle appended out of order, and a second book's open
    # position, stitched into one state file.
    strat.closed.append({**strat.closed[0], "expiry": "2026-09-29", "net": -50.0})
    reopened = _strategy(tmp_path)
    reopened.on_close(_roll_view())
    state = strat.to_dict()
    state["book"] = reopened.to_dict()["book"]
    (cache / "dispersion_paper_state.json").write_text(json.dumps(state))

    resp = dr.dispersion_paper()
    book = next(b for b in resp.books if b.name == "dispersion_paper")
    assert book.has_state and book.state_error is None
    # Cumulative runs in expiry order, whatever order the file lists them in.
    assert [c.expiry for c in book.paper] == ["2026-09-29", EXPIRY.isoformat()]
    second = strat.closed[0]["net"]
    assert book.paper[-1].cumulative == pytest.approx(-50.0 + second)
    assert book.paper_summary.total_net == pytest.approx(-50.0 + second)
    assert book.paper_summary.cycles == 2
    # The open book: legs and hedge lots straight from state; the running
    # figure is hedge P&L and costs, with no invented option mark.
    ob = book.open
    assert ob is not None
    assert ob.index_lots == reopened.book.index_lots
    assert ob.n_names == len(reopened.book.legs) - 1
    assert ob.notional_ratio == pytest.approx(reopened.book.notional_ratio)
    assert ob.costs == pytest.approx(reopened.book.costs)
    assert {leg.symbol for leg in ob.legs} == {leg.symbol for leg in reopened.book.legs}
    # The settlement is a 15:00 LTP proxy, and every row says so.
    assert {c.settle_basis for c in book.paper} == {"window_ltp_proxy"}
    # Weightings come from what was recorded, not from the label.
    assert book.weightings == ["equal"]
    assert book.label == "Dispersion (matched)"


def test_mixed_weightings_in_one_state_file_are_reported(tmp_path, cache):
    """--equal-weight switches the matched book on the same state file. A
    history with both must say so, not read as one free-float record."""
    strat = _settled_and_reopened(tmp_path)
    strat.closed.append({**strat.closed[0], "expiry": "2026-12-29", "weighting": "free_float"})
    (cache / "dispersion_paper_state.json").write_text(json.dumps(strat.to_dict()))
    book = next(b for b in dr.dispersion_paper().books if b.name == "dispersion_paper")
    assert book.weightings == ["equal", "free_float"]


def test_missing_state_is_empty_and_corrupt_state_is_an_error(cache):
    (cache / "dispersion_short_vol_paper_state.json").write_text("{not json")
    resp = dr.dispersion_paper()
    by = {b.name: b for b in resp.books}
    assert by["dispersion_paper"].has_state is False
    assert by["dispersion_paper"].paper == []
    assert by["dispersion_short_vol_paper"].has_state is True
    assert by["dispersion_short_vol_paper"].state_error


def test_replay_shows_only_the_running_config(cache):
    """Book A, held to expiry, futures hedge, filled. The unhedged and
    Book B rows are other configs and must not leak into this curve."""
    rows = []
    for expiry, net in (("2025-01-30", 100.0), ("2025-02-27", -40.0)):
        for book, exit_mode, hedge, status, n in (
            ("A", "expiry", "future", "ok", net),
            ("A", "expiry", "none", "ok", 9_999.0),
            ("B", "expiry", "future", "ok", 9_999.0),
            ("A", "flatten", "future", "ok", 9_999.0),
        ):
            rows.append({"book": book, "expiry": expiry, "entry": "2025-01-01",
                         "exit_mode": exit_mode, "hedge": hedge, "status": status,
                         "index_lots": 20, "covered_weight": 0.6, "premium_pnl": 0.0,
                         "futures_pnl": 0.0, "costs": 1.0, "net": n})
    rows.append({**rows[0], "expiry": "2025-03-27", "status": "no_entry_mark", "net": 0.0})
    pd.DataFrame(rows).to_csv(cache / "dispersion_cycles_matched.csv", index=False)
    book = next(b for b in dr.dispersion_paper().books if b.name == "dispersion_paper")
    assert [c.net for c in book.replay] == [100.0, -40.0]
    assert book.replay[-1].cumulative == pytest.approx(60.0)
    assert book.replay_summary.wins == 1
    assert book.replay_source == "dispersion_cycles_matched.csv"


def test_route_is_behind_the_dashboard_session(tmp_path, cache):
    rm._manager = None
    db.reset_for_tests(tmp_path / "t.db")
    try:
        with TestClient(create_app()) as c:
            assert c.get("/api/dispersion-paper").status_code == 401
            from tests._helpers import login_client
            login_client(c)
            r = c.get("/api/dispersion-paper")
            assert r.status_code == 200
            assert {b["name"] for b in r.json()["books"]} == {
                "dispersion_paper", "dispersion_short_vol_paper"}
    finally:
        db.reset_for_tests(None)


def test_silent_fail_marker_is_reported(cache):
    """A dead runner must not look like a quiet, healthy book."""
    assert dr.dispersion_paper().runner_silent_fail is False
    (cache / "SILENT_FAIL_dispersion_paper").touch()
    assert dr.dispersion_paper().runner_silent_fail is True
