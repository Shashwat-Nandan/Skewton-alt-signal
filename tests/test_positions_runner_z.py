"""The Positions tab must show the z the live pair runner actually exits on.

2026-10-15: the persistent-candidates page showed EICHERMOT/SHRIRAMFIN at
z=-0.41 (screener: refit beta 4.65, 130-session window) while the runner,
holding it on its entry beta 4.77 and 60-day window, had -1.11 — outside the
0.75 exit band, so it correctly held. The operator read the screener's z as
the exit signal. These pin that the runner records its own z for a held pair,
persists it, and that the Positions API returns it with the real bands.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.routers import positions as pos
from strategies.pair_trading import PairLeg
from tests.test_pair_trading import _make_strategy

HIST = [(-1) ** i * 5.0 for i in range(30)]


def _held():
    s = _make_strategy(mode="paper", spread_history=HIST)
    s.state.position = "LONG_SPREAD"
    s.state.entry_z = -1.96
    s.state.entry_time = datetime(2026, 10, 13, 14, 29)
    s.state.effective_stop_z = 4.0
    s.state.legs = [
        PairLeg(symbol="AAA", tradingsymbol="AAA26OCTFUT", lot_size=1, quantity=1,
                entry_price=100.0, current_price=100.0, expiry="2026-10-27"),
        PairLeg(symbol="BBB", tradingsymbol="BBB26OCTFUT", lot_size=1, quantity=-1,
                entry_price=50.0, current_price=50.0, expiry="2026-10-27"),
    ]
    s._observe_spread = lambda: (3.0, {"AAA": 101.0, "BBB": 50.0})
    s._clock = lambda: datetime(2026, 10, 15, 11, 42)
    return s


def test_runner_records_and_persists_its_own_z_for_a_held_pair():
    s = _held()
    s.check_and_rehedge()
    assert s.state.last_z == s._z_score(3.0)
    assert s.state.last_z_at == datetime(2026, 10, 15, 11, 42)
    blob = json.loads(json.dumps(s.serialize_state(), default=str))
    assert blob["state"]["last_z"] == s.state.last_z
    assert blob["exit_z"] == s.exit_z and blob["max_holding_days"] == s.max_holding_days
    fresh = _make_strategy(mode="paper", spread_history=HIST)
    fresh.restore_state(blob)
    assert fresh.state.last_z == s.state.last_z
    assert fresh.state.last_z_at == datetime(2026, 10, 15, 11, 42)


IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 15, 11, 43, tzinfo=IST)


def _block(tmp_path, monkeypatch, blobs, now=NOW):
    state = {"updated_at": "2026-10-15T11:42:00", "mode": "live", "pairs": blobs}
    (tmp_path / "pair_paper_state_persistent.json").write_text(json.dumps(state))
    monkeypatch.setattr(pos, "DATA_CACHE", tmp_path)
    return pos._build_pair_block("pair_persistent", "Pairs", "pair_paper_state_persistent.json",
                                 now.date(), now=now)


def _blob(s):
    return json.loads(json.dumps(s.serialize_state(), default=str))


def test_positions_api_shows_the_runner_z_and_bands_once_under_a_held_pair(tmp_path, monkeypatch):
    s = _held()
    s.check_and_rehedge()
    flat = _make_strategy(mode="paper", spread_history=HIST)
    flat.symbol_a, flat.symbol_b = "CCC", "DDD"
    legs = _block(tmp_path, monkeypatch, [_blob(s), _blob(flat)]).open_positions
    assert [leg.tradingsymbol for leg in legs] == ["AAA26OCTFUT", "BBB26OCTFUT"]
    # Pair-level: carried once, on the last leg, where the tab draws it.
    assert legs[0].pair_z is None
    z = legs[1].pair_z
    assert z is not None and z.current_z == s.state.last_z and not z.stale
    assert z.as_of == "2026-10-15T11:42:00+05:30"
    assert z.entry_z == -1.96 and z.stop_z == 4.0
    assert z.exit_z == s.exit_z and z.exit_debounce_ticks == s.exit_debounce_ticks
    assert z.exit_streak == s.state.mean_revert_streak
    assert z.held_trading_days == 2 and z.max_holding_days == s.max_holding_days


def test_the_stop_shown_is_the_one_the_runner_applies(tmp_path, monkeypatch):
    """check_and_rehedge stops at effective_stop_z or, when that is 0, the
    global stop_z. A 0 must not make the stop vanish from the page."""
    s = _held()
    s.state.effective_stop_z = 0.0
    s.check_and_rehedge()
    z = _block(tmp_path, monkeypatch, [_blob(s)]).open_positions[-1].pair_z
    assert z.stop_z == s.stop_z


def test_a_frozen_z_is_flagged_stale(tmp_path, monkeypatch):
    """A rate-limited / failed-quote tick computes no z and must leave the old
    one alone; the page must then say it is stale, not show it as current —
    and yesterday's value before today's first tick is stale too."""
    s = _held()
    s.check_and_rehedge()
    before = s.state.last_z
    s._observe_spread = lambda: (None, {})
    s._clock = lambda: datetime(2026, 10, 15, 11, 50)
    s.check_and_rehedge()
    assert s.state.last_z == before and s.state.last_z_at == datetime(2026, 10, 15, 11, 42)
    z = _block(tmp_path, monkeypatch, [_blob(s)], now=datetime(2026, 10, 15, 11, 50, tzinfo=IST))
    assert z.open_positions[-1].pair_z.stale
    z = _block(tmp_path, monkeypatch, [_blob(s)], now=datetime(2026, 10, 16, 9, 12, tzinfo=IST))
    assert z.open_positions[-1].pair_z.stale


def test_no_row_when_the_state_records_no_runner_z(tmp_path, monkeypatch):
    """Old-code runner and Kalman blobs have no last_z: show no z row (a
    'Runner z —' would claim the runner has no z), and still render the legs."""
    blob = _blob(_held())
    blob["state"].pop("last_z")
    blob["state"].pop("last_z_at")
    legs = _block(tmp_path, monkeypatch, [blob]).open_positions
    assert len(legs) == 2 and all(leg.pair_z is None for leg in legs)


def test_bad_last_z_never_breaks_restore_or_the_api(tmp_path, monkeypatch):
    """A display-only value must not abandon a live position on restore (a
    raise there drops the saved position) nor 500 the whole /positions call."""
    blob = _blob(_held())
    blob["state"]["last_z"] = "garbage"
    blob["state"]["last_z_at"] = 12
    fresh = _make_strategy(mode="paper", spread_history=HIST)
    fresh.restore_state(blob)
    assert fresh.state.position == "LONG_SPREAD" and len(fresh.state.legs) == 2
    assert fresh.state.last_z is None and fresh.state.last_z_at is None
    blob["state"]["last_z"] = float("nan")
    block = _block(tmp_path, monkeypatch, [blob])
    z = block.open_positions[-1].pair_z
    assert z.current_z is None and z.stale
    json.dumps(block.model_dump(), allow_nan=False)   # what the response encoder requires
