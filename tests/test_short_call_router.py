"""Tests for the short-call dashboard router.

The router is read-only, so the risks here are all about *misreporting*: the
strategy has no measured edge, and a page that quietly shows a friendlier number
than the book actually holds is worse than no page at all.

Note the sidecar shape differs from buy_on_gap's: this strategy's EOD report
splits `today` and `cumulative` blocks precisely so a per-day file cannot be
mistaken for cumulative figures.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import backend.routers.short_call as sc


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(sc, "STATE_FILE", tmp_path / "short_call_paper_state.json")
    return tmp_path


def _state(cache, positions=None, closed=None, realized=0.0):
    (cache / "short_call_paper_state.json").write_text(json.dumps({
        "positions": positions or {},
        "closed_positions": closed or [],
        "realized_pnl": realized,
        "transaction_costs": 0.0,
    }))


def _closed(symbol="ACME", reason="STOP", pnl=-12000.0, r=-1.05):
    return {"symbol": symbol, "tradingsymbol": f"{symbol}CE", "event_date": "2026-06-11",
            "entry_dt": "2026-06-10T15:05:00", "exit_dt": "2026-06-11T10:00:00",
            "credit": 20.0, "exit_px": 32.0, "lots": 16, "lot_size": 100,
            "exit_reason": reason, "pnl": pnl, "realised_R": r, "ivp_at_entry": 97.0,
            "strike": 1000.0, "expiry": "2026-06-25", "target_px": 8.0,
            "stop_px": 32.0, "r_rupees": 19200.0, "last_mtm_px": 32.0}


def test_gap_through_stops_are_surfaced_not_averaged_away(cache):
    """The headline metric of the whole paper run. A GAP_STOP fills worse than
    the nominal -1R, and the summary must report the count and the worst R
    rather than letting them vanish into a mean."""
    _state(cache, closed=[
        _closed(reason="TARGET", pnl=11000.0, r=0.97),
        _closed(reason="GAP_STOP", pnl=-52000.0, r=-2.71),
    ], realized=-41000.0)
    resp = sc.short_call(days=3, end=None)
    assert resp.summary.gap_through_stop_count == 1
    assert resp.summary.gap_through_worst_R == pytest.approx(-2.71)
    assert resp.summary.worst_realised_R == pytest.approx(-2.71)


def test_daily_rows_use_the_today_block_not_cumulative(cache):
    """This strategy carries positions across sessions and restores
    closed_positions on restart, so its EOD report splits today/cumulative. If
    the router read the cumulative block into the per-day column, every daily
    figure would double-count history."""
    d = date(2026, 6, 11)
    (cache / f"short_call_paper_eod_{d.isoformat()}.json").write_text(json.dumps({
        "date": d.isoformat(), "open_positions": 1,
        "today": {"closed_trades": 1, "realized_pnl": -12000.0,
                  "gap_through_stop_count": 1, "exit_reasons": {"GAP_STOP": 1}},
        "cumulative": {"closed_trades": 9, "realized_pnl": -99000.0,
                       "gap_through_stop_count": 3, "exit_reasons": {"GAP_STOP": 3}},
    }))
    _state(cache)
    resp = sc.short_call(days=5, end=d.isoformat())
    row = next(r for r in resp.daily if r.date == d.isoformat())
    assert row.day_pnl == pytest.approx(-12000.0)      # today, not -99000
    assert row.day_closed == 1
    assert row.gap_through_stop == 1


def test_missing_state_and_sidecars_degrade_quietly(cache):
    """A fresh deploy has neither file. The page must render empty rather than
    500 — this runner is expected to sit idle for weeks between earnings
    seasons."""
    resp = sc.short_call(days=3, end=None)
    assert resp.summary.n_closed_trades == 0
    assert resp.open_positions == [] and resp.closed_trades == []
    assert "NO MEASURED EDGE" in resp.summary.health_note


def test_unrealized_R_is_relative_to_the_positions_own_R(cache):
    _state(cache, positions={"ACME": {
        **_closed(), "exit_dt": None, "exit_px": None, "exit_reason": None,
        "last_mtm_px": 10.0, "sessions_held": 1,
    }})
    resp = sc.short_call(days=3, end=None)
    p = resp.open_positions[0]
    # short 16x100 at 20, marked 10 -> +10 * 1600 = +16,000 on a 19,200 R
    assert p.unrealized == pytest.approx(16000.0)
    assert p.unrealized_R == pytest.approx(16000.0 / 19200.0, rel=1e-3)


def test_health_note_always_states_the_verdict(cache):
    """A populated table must never read as a validated signal."""
    _state(cache, closed=[_closed(reason="TARGET", pnl=11000.0, r=0.97)] * 5,
           realized=55000.0)
    resp = sc.short_call(days=3, end=None)
    assert resp.summary.win_rate == 100.0          # a flattering book ...
    assert "NO MEASURED EDGE" in resp.summary.health_note   # ... still says so
    assert "t=0.33" in resp.summary.health_note


def test_upcoming_blocks_events_with_no_intimation_timestamp(monkeypatch):
    """The runner fails CLOSED on an unknown announcement time; the dashboard
    must predict the same thing, or it advertises a trade that will not happen."""
    cal = pd.DataFrame({"symbol": ["ACME"],
                        "event_date": [pd.Timestamp.now().normalize() + pd.Timedelta(days=2)],
                        "announced_at": [pd.NaT]})
    panel = pd.DataFrame({"date": pd.bdate_range("2025-03-03", periods=200),
                          "symbol": "ACME", "spot": 1000.0, "strike": 1000.0, "dte": 20,
                          "expiry": pd.Timestamp("2026-12-31"), "lot": 100,
                          "ce_px": 30.0, "pe_px": 30.0, "iv_ce": 0.3, "iv_pe": 0.3,
                          "atm_iv": 0.3, "ce_vol": 1, "pe_vol": 1})
    monkeypatch.setattr(sc, "_panel", lambda: panel)
    import market_data.fetch_board_meetings as fbm
    monkeypatch.setattr(fbm, "load_results_calendar", lambda *a, **k: cal)
    resp = sc.upcoming(days=30)
    ev = next(e for e in resp.events if e.symbol == "ACME")
    assert ev.qualifies is False
    assert "intimation" in (ev.blocked_by or "")


def _gate_passing_panel(symbols, last_date="2026-08-28"):
    """252 sessions of mixed IV + a last-EOD row at 0.40.

    Ranking 0.40 through yesterday is 227/252 = 90.08 (clears the 90 gate);
    ranking it *including* that last row is 227/253 = 89.72 (blocked). That is
    the look-ahead the runner's `asof=today` contract exists to prevent.
    """
    dates = pd.bdate_range(end=last_date, periods=253)
    iv = [0.30] * 227 + [0.50] * 25 + [0.40]
    frames = []
    for sym in symbols:
        frames.append(pd.DataFrame({
            "date": dates, "symbol": sym, "spot": 1000.0, "strike": 1000.0, "dte": 20,
            "expiry": pd.Timestamp("2026-12-31"), "lot": 100,
            "ce_px": 30.0, "pe_px": 30.0, "iv_ce": iv, "iv_pe": iv, "atm_iv": iv,
            "ce_vol": 1, "pe_vol": 1,
        }))
    return pd.concat(frames, ignore_index=True)


def _freeze_upcoming_clock(monkeypatch, today):
    """Dashboard `upcoming()` reads the wall clock; pin it so T-1 / IVP asof
    assertions do not depend on when CI runs."""
    monkeypatch.setattr(sc, "_today", lambda: today)
    monkeypatch.setattr(sc, "load_holidays", lambda *a, **k: set())


def test_upcoming_ivp_does_not_rank_today_against_itself(monkeypatch):
    """The last EOD ATM IV is the *value* being ranked, not a member of the
    history. `asof=today+1d` would include today's panel row and can flip a
    name at the 90 gate (227/252 = 90.1 qualifies; 227/253 = 89.7 is blocked)."""
    from strategies import _atm_iv

    today = date(2026, 8, 28)   # Friday; next session is Monday
    panel = _gate_passing_panel(["ACME"], last_date=today.isoformat())
    cal = pd.DataFrame({
        "symbol": ["ACME"],
        "event_date": [pd.Timestamp("2026-08-31")],   # next trading session
        "announced_at": [pd.Timestamp("2026-07-01")],
    })
    monkeypatch.setattr(sc, "_panel", lambda: panel)
    import market_data.fetch_board_meetings as fbm
    monkeypatch.setattr(fbm, "load_results_calendar", lambda *a, **k: cal)
    _freeze_upcoming_clock(monkeypatch, today)

    resp = sc.upcoming(days=30)
    ev = next(e for e in resp.events if e.symbol == "ACME")
    through_yesterday = _atm_iv.iv_percentile(
        panel, "ACME", 0.40, pd.Timestamp(today))
    including_today = _atm_iv.iv_percentile(
        panel, "ACME", 0.40, pd.Timestamp(today) + pd.Timedelta(days=1))
    assert through_yesterday is not None and including_today is not None
    assert through_yesterday >= 90.0
    assert including_today < 90.0
    assert ev.ivp == pytest.approx(round(through_yesterday, 1))
    assert ev.ivp != pytest.approx(round(including_today, 1))
    assert ev.qualifies is True


def test_upcoming_would_enter_only_on_t_minus_1(monkeypatch):
    """The 14:55 runner only sells when results are the *next trading session*.
    A 5–20d name that clears IVP/size/DTE must not get the would-enter badge,
    and Friday→Monday is 1 session, not 3 calendar days."""
    today = date(2026, 8, 28)   # Friday
    panel = _gate_passing_panel(["T1", "FAR", "TODAY", "LATE"], last_date=today.isoformat())
    cal = pd.DataFrame({
        "symbol": ["T1", "FAR", "TODAY", "LATE"],
        "event_date": [
            pd.Timestamp("2026-08-31"),                 # next session (Monday)
            pd.Timestamp("2026-08-28") + pd.offsets.BDay(10),
            pd.Timestamp("2026-08-28"),                 # event day
            pd.Timestamp("2026-08-31"),
        ],
        "announced_at": [
            pd.Timestamp("2026-07-01"),
            pd.Timestamp("2026-07-01"),
            pd.Timestamp("2026-07-01"),
            pd.Timestamp.now() + pd.Timedelta(days=1),  # after decision time
        ],
    })
    monkeypatch.setattr(sc, "_panel", lambda: panel)
    import market_data.fetch_board_meetings as fbm
    monkeypatch.setattr(fbm, "load_results_calendar", lambda *a, **k: cal)
    _freeze_upcoming_clock(monkeypatch, today)

    resp = sc.upcoming(days=30)
    by_sym = {e.symbol: e for e in resp.events}
    assert by_sym["T1"].qualifies is True
    assert by_sym["T1"].sessions_until == 1          # Mon is 1 session from Fri
    assert by_sym["FAR"].qualifies is False
    assert "T-1" in (by_sym["FAR"].blocked_by or "")
    assert by_sym["FAR"].ivp is not None             # still show the structure
    assert by_sym["TODAY"].qualifies is False
    assert "event day" in (by_sym["TODAY"].blocked_by or "").lower()
    assert by_sym["LATE"].qualifies is False
    assert "announced" in (by_sym["LATE"].blocked_by or "").lower()


def test_upcoming_empty_calendar_explains_itself(monkeypatch):
    import market_data.fetch_board_meetings as fbm
    monkeypatch.setattr(fbm, "load_results_calendar",
                        lambda *a, **k: pd.DataFrame({
                            "symbol": pd.Series(dtype="object"),
                            "event_date": pd.Series(dtype="datetime64[ns]"),
                            "announced_at": pd.Series(dtype="datetime64[ns]")}))
    resp = sc.upcoming(days=30)
    assert resp.events == [] and resp.top_ivp == []
    assert "EMPTY" in resp.note and "fetch_board_meetings" in resp.note
