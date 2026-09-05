"""Tests for the /ma-momentum endpoint.

Surfaces the §6.3 frozen-MA paper holdout for the dashboard. These pin the
things this particular tab exists to prevent someone misreading:

  * the stop-overshoot correction is reported ALONGSIDE the raw booked P&L, not
    in place of it (the raw figure is the pre-registered number; silently
    restating it is how a holdout stops being a holdout),
  * a persistent HALT_MA_MOMENTUM_DAILY_LOSS is surfaced as PAUSED — the runner
    keeps writing EOD sidecars with entries suspended, so a P&L-only view shows
    "flat" when the truth is "dead",
  * positions come from the LIVE runner state, not the EOD sidecar (which
    force-closes at 15:25, so its open_pos is always 0),
  * the frozen SMA windows come from the strategy module, so a drifted state
    file cannot echo itself back as if it were correct, and
  * the no-session-yet case is a clean 200, not a 500.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import create_app
from backend import db, run_manager as rm
from backend.routers import ma_momentum as mm_router


def _book(*, realized_rupees=0.0, n_trades=0, win_rate=None, open_pos=0,
          session_realized_rupees=0.0, session_trades=None):
    """Mirror IntradayTrendStrategy.book_summary()'s shape."""
    return {
        "signal_kind": "ma", "n_trades": n_trades,
        "realized_points": realized_rupees, "realized_rupees": realized_rupees,
        "win_rate": win_rate, "open_pos": open_pos, "n_bars": 0,
        "session_n_trades": len(session_trades or []),
        "session_realized_rupees": session_realized_rupees,
        "session_trades": session_trades or [],
    }


def _write_eod(cache: Path, d: date, instruments, *, total=0.0, note="NO-GO",
               entries_halted=False):
    path = cache / f"ma_momentum_eod_{d.isoformat()}.json"
    path.write_text(json.dumps({
        "date": d.isoformat(), "system": "ma_momentum",
        "total_rupees": total, "n_trades": 0,
        "stop_overshoot_rupees": 0.0, "n_stop_fills": 0,
        "entries_halted": entries_halted, "halt_reasons": [],
        "instruments": instruments, "note": note,
    }))
    return path


def _inst(symbol, book, *, tradingsymbol="", overshoot=0.0, n_stop_fills=0):
    return {"symbol": symbol, "tradingsymbol": tradingsymbol, "ma": book,
            "stop_overshoot_points": overshoot / 75 if overshoot else 0.0,
            "stop_overshoot_rupees": overshoot, "n_stop_fills": n_stop_fills}


def _write_runner_state(cache: Path, rows, updated="2026-09-03T11:00:00"):
    """rows = list of (symbol, pos, tradingsymbol) → live runner state file."""
    cache.joinpath("ma_momentum_runner_state.json").write_text(json.dumps({
        "updated": updated,
        "instruments": [
            {"symbol": s, "book": {"pos": p}, "tradingsymbol": ts}
            for (s, p, ts) in rows
        ],
    }))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from tests._helpers import login_client
    rm._manager = None
    db.reset_for_tests(tmp_path / "test.db")
    cache = tmp_path / "data_cache"
    cache.mkdir()
    monkeypatch.setattr(mm_router, "DATA_CACHE", cache)
    app = create_app()
    with TestClient(app) as c:
        login_client(c)
        c._cache = cache
        yield c
    db.reset_for_tests(None)


END = "2026-09-03"


class TestMaMomentum:
    def test_reports_overshoot_correction_beside_the_raw_number(self, client):
        """The booked P&L is optimistic: stops fill at their LEVEL on a 30s poll.
        Both figures must be present — the raw one is what was pre-registered."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [
            _inst("NIFTY", _book(realized_rupees=5_000.0, n_trades=3),
                  overshoot=600.0, n_stop_fills=2),
            _inst("BANKNIFTY", _book(realized_rupees=-2_000.0, n_trades=9),
                  overshoot=900.0, n_stop_fills=6),
        ], total=3_000.0)

        r = client.get(f"/api/ma-momentum?end={END}")
        assert r.status_code == 200
        body = r.json()

        assert body["total_rupees"] == 3_000.0            # recomputed: 5000 + (−2000)
        assert body["stop_overshoot_rupees"] == 1_500.0
        assert body["n_stop_fills"] == 8
        # the correction is subtracted, and the raw figure still stands beside it
        assert body["total_rupees_ex_overshoot"] == 1_500.0
        assert body["total_rupees"] != body["total_rupees_ex_overshoot"]

    def test_persistent_daily_loss_flag_shows_paused_not_flat(self, client):
        """Nothing clears HALT_MA_MOMENTUM_DAILY_LOSS. Once written, entries stay
        suspended for every later session while sidecars keep being produced, so
        a P&L-only view reads 'flat'. The tab must say PAUSED and how to resume."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())])
        (cache / "HALT_MA_MOMENTUM_DAILY_LOSS").write_text("breached\n")

        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["halt"]["entries_halted"] is True
        assert body["halt"]["daily_loss_flag"] is True
        joined = " ".join(body["halt"]["reasons"])
        assert "PERSISTS" in joined
        assert "rm data_cache/HALT_MA_MOMENTUM_DAILY_LOSS" in joined

    def test_healthy_when_no_flag_is_present(self, client):
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())])
        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["halt"]["entries_halted"] is False
        assert body["halt"]["reasons"] == []

    def test_scoped_and_shared_halt_flags_are_named_separately(self, client):
        """The scoped flag must never be confused with the shared one — the
        2026-07-15 incident was a scoped monitor tripping the shared flag and
        freezing the LIVE pair runner for 6.5 sessions."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())])
        (cache / "HALT_NEW_ENTRIES_ma_momentum").touch()

        body = client.get(f"/api/ma-momentum?end={END}").json()
        joined = " ".join(body["halt"]["reasons"])
        assert "HALT_NEW_ENTRIES_ma_momentum" in joined
        assert body["halt"]["daily_loss_flag"] is False

    def test_positions_come_from_live_runner_state_not_eod(self, client):
        """The EOD sidecar force-closes at 15:25 (open_pos always 0); the tab
        must show real intraday exposure from ma_momentum_runner_state.json."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [
            _inst("NIFTY", _book(open_pos=0), tradingsymbol="NIFTY26SEPFUT"),
        ])
        _write_runner_state(cache, [("NIFTY", -1, "NIFTY26OCTFUT")])

        body = client.get(f"/api/ma-momentum?end={END}").json()
        nifty = next(i for i in body["instruments"] if i["symbol"] == "NIFTY")
        assert nifty["open_pos"] == -1
        # and the LIVE contract wins after a roll, not the one the sidecar traded
        assert nifty["tradingsymbol"] == "NIFTY26OCTFUT"

    def test_frozen_windows_come_from_the_strategy_not_the_sidecar(self, client):
        """§6.3 forbids retuning. The tab asserts what the runner is REQUIRED to
        trade, so a drifted state file shows as a mismatch instead of being
        echoed back as correct."""
        from strategies.ma_momentum import FROZEN_PARAMS
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())])

        body = client.get(f"/api/ma-momentum?end={END}").json()
        nifty = next(i for i in body["instruments"] if i["symbol"] == "NIFTY")
        assert nifty["short"] == FROZEN_PARAMS["NIFTY"]["short"] == 34
        assert nifty["long"] == FROZEN_PARAMS["NIFTY"]["long"] == 53

    def test_latest_session_wins_and_progress_counts_every_sidecar(self, client):
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 1), [_inst("NIFTY", _book(realized_rupees=1.0))])
        _write_eod(cache, date(2026, 9, 2), [_inst("NIFTY", _book(realized_rupees=2.0))])
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book(realized_rupees=7.0))])

        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["latest_date"] == "2026-09-03"
        assert body["total_rupees"] == 7.0
        assert body["n_sessions_recorded"] == 3
        assert body["holdout_sessions"] == 60

    def test_end_date_excludes_later_sidecars(self, client):
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 2), [_inst("NIFTY", _book(realized_rupees=2.0))])
        _write_eod(cache, date(2026, 9, 9), [_inst("NIFTY", _book(realized_rupees=99.0))])

        body = client.get("/api/ma-momentum?end=2026-09-02").json()
        assert body["latest_date"] == "2026-09-02"
        assert body["total_rupees"] == 2.0
        assert body["n_sessions_recorded"] == 1

    def test_session_trades_are_read_through(self, client):
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [
            _inst("NIFTY", _book(
                realized_rupees=375.0, n_trades=1, session_realized_rupees=375.0,
                session_trades=[{"side": 1, "entry_price": 24000.0,
                                 "exit_price": 24010.0, "pnl_points": 5.0,
                                 "pnl_rupees": 375.0, "reason": "stop"}])),
        ])
        body = client.get(f"/api/ma-momentum?end={END}").json()
        nifty = next(i for i in body["instruments"] if i["symbol"] == "NIFTY")
        assert nifty["session_realized_rupees"] == 375.0
        assert len(nifty["session_trades"]) == 1
        assert nifty["session_trades"][0]["reason"] == "stop"

    def test_no_session_yet_is_a_clean_200(self, client):
        """Before the timer's first run there is no sidecar at all — that is a
        healthy empty snapshot, not an error."""
        r = client.get(f"/api/ma-momentum?end={END}")
        assert r.status_code == 200
        body = r.json()
        assert body["latest_date"] is None
        assert body["instruments"] == []
        assert body["total_rupees"] == 0.0
        assert body["total_rupees_ex_overshoot"] == 0.0
        assert body["n_sessions_recorded"] == 0
        assert body["halt"]["entries_halted"] is False

    def test_malformed_instrument_is_skipped_not_500(self, client):
        """One bad record must not empty the whole table, and the headline must
        still sum to the rows that survived."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [
            "not-a-dict",
            _inst("NIFTY", _book(realized_rupees=500.0, n_trades=1)),
        ], total=99_999.0)

        r = client.get(f"/api/ma-momentum?end={END}")
        assert r.status_code == 200
        body = r.json()
        assert [i["symbol"] for i in body["instruments"]] == ["NIFTY"]
        assert body["total_rupees"] == 500.0    # recomputed, not the sidecar's 99,999

    def test_bad_end_date_is_a_400(self, client):
        r = client.get("/api/ma-momentum?end=not-a-date")
        assert r.status_code == 400

    def test_progress_excludes_sessions_with_entries_halted(self, client):
        """A halted session still writes a sidecar. Counting files as progress
        would report a paused holdout as advancing toward its decision date —
        and the daily-loss flag persists across sessions."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 1), [_inst("NIFTY", _book())])
        _write_eod(cache, date(2026, 9, 2), [_inst("NIFTY", _book())],
                   entries_halted=True)
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())],
                   entries_halted=True)

        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["n_sessions_recorded"] == 3
        assert body["n_sessions_measured"] == 1

    def test_sidecars_predating_the_halt_field_count_as_measured(self, client):
        """Sessions recorded before `entries_halted` shipped did take entries,
        by construction — they must not be silently dropped from progress."""
        cache: Path = client._cache
        path = cache / "ma_momentum_eod_2026-09-01.json"
        path.write_text(json.dumps({
            "date": "2026-09-01", "system": "ma_momentum",
            "total_rupees": 0.0, "n_trades": 0, "instruments": [],
        }))
        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["n_sessions_measured"] == 1

    def test_a_dead_runner_is_not_reported_as_healthy(self, client):
        """SILENT_FAIL means the heartbeat tripped and the process EXITED — so
        'entries are live; exits and the 15:25 flatten always run' is false on
        both clauses. The card's contract is that absence means checked."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())])
        (cache / "SILENT_FAIL_ma_momentum").touch()

        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["halt"]["runner_silent_fail"] is True
        joined = " ".join(body["halt"]["reasons"])
        assert "SILENT_FAIL_ma_momentum" in joined
        # it is NOT an entry halt — a different failure, held separately
        assert body["halt"]["entries_halted"] is False

    def test_stale_state_file_is_not_reported_as_live_positions(self, client):
        """A runner that died leaves a frozen state file. Rendering its last
        `pos` as a live position is a lie the operator would act on."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())])
        _write_runner_state(cache, [("NIFTY", -1, "NIFTY26SEPFUT")],
                            updated="2026-08-20T11:00:00")

        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["positions_stale"] is True
        assert body["state_updated"].startswith("2026-08-20")
        # the position is still reported (the operator needs to know it exists)
        assert next(i for i in body["instruments"]
                    if i["symbol"] == "NIFTY")["open_pos"] == -1

    def test_unparseable_state_timestamp_is_treated_as_stale(self, client):
        """Never assert 'live' from an absence."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book())])
        _write_runner_state(cache, [("NIFTY", 1, "N")], updated="garbage")
        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["positions_stale"] is True

    def test_carried_rows_are_flagged_as_a_partial_session(self, client):
        cache: Path = client._cache
        row = _inst("BANKNIFTY", _book(realized_rupees=500.0, n_trades=2))
        row["carried"] = True
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book()), row])

        body = client.get(f"/api/ma-momentum?end={END}").json()
        assert body["carried_symbols"] == ["BANKNIFTY"]
        bn = next(i for i in body["instruments"] if i["symbol"] == "BANKNIFTY")
        assert bn["carried"] is True
        # and its cumulative P&L still counts toward the headline
        assert body["total_rupees"] == 500.0

    def test_corrupt_state_file_degrades_instead_of_500ing(self, client):
        """A non-dict decode raised AttributeError straight out of the endpoint;
        non-UTF-8 bytes raised UnicodeDecodeError. Both must degrade to
        'no live positions' so the sidecar data still renders."""
        cache: Path = client._cache
        _write_eod(cache, date(2026, 9, 3), [_inst("NIFTY", _book(realized_rupees=42.0))])

        cache.joinpath("ma_momentum_runner_state.json").write_text("[1, 2, 3]")
        r1 = client.get(f"/api/ma-momentum?end={END}")
        assert r1.status_code == 200
        assert r1.json()["total_rupees"] == 42.0

        cache.joinpath("ma_momentum_runner_state.json").write_bytes(b"\xff\xfe not utf8")
        r2 = client.get(f"/api/ma-momentum?end={END}")
        assert r2.status_code == 200
        assert r2.json()["total_rupees"] == 42.0

    def test_sidecar_decoding_to_a_list_does_not_500(self, client):
        cache: Path = client._cache
        (cache / "ma_momentum_eod_2026-09-03.json").write_text("[]")
        r = client.get(f"/api/ma-momentum?end={END}")
        assert r.status_code == 200
        assert r.json()["instruments"] == []
