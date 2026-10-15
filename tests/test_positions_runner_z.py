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
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.routers import positions as pos
from strategies.pair_trading import PairLeg
from tests.test_pair_trading import _make_strategy

HIST = [(-1) ** i * 5.0 for i in range(30)]


def _held():
    s = _make_strategy(mode="paper", spread_history=HIST)
    s.state.position = "LONG_SPREAD"
    s.state.entry_z = -1.96
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


def test_positions_api_shows_the_runner_z_and_bands_under_a_held_pair(tmp_path, monkeypatch):
    s = _held()
    s.check_and_rehedge()
    flat = _make_strategy(mode="paper", spread_history=HIST)
    flat.symbol_a, flat.symbol_b = "CCC", "DDD"
    state = {"updated_at": "2026-10-15T11:42:00", "mode": "live",
             "pairs": [json.loads(json.dumps(s.serialize_state(), default=str)),
                       json.loads(json.dumps(flat.serialize_state(), default=str))]}
    (tmp_path / "pair_paper_state_persistent.json").write_text(json.dumps(state))
    monkeypatch.setattr(pos, "DATA_CACHE", tmp_path)
    block = pos._build_pair_block("pair_persistent", "Pairs", "pair_paper_state_persistent.json",
                                  date(2026, 10, 15))
    legs = block.open_positions
    assert [leg.tradingsymbol for leg in legs] == ["AAA26OCTFUT", "BBB26OCTFUT"]
    z = legs[0].pair_z
    assert z is not None and z.current_z == s.state.last_z
    assert z.as_of.startswith("2026-10-15T11:42")
    assert z.entry_z == -1.96 and z.stop_z == 4.0
    assert z.exit_z == s.exit_z and z.exit_debounce_ticks == s.exit_debounce_ticks


def test_old_state_files_without_the_field_still_render():
    s = _held()
    blob = json.loads(json.dumps(s.serialize_state(), default=str))
    blob["state"].pop("last_z"); blob["state"].pop("last_z_at")
    fresh = _make_strategy(mode="paper", spread_history=HIST)
    fresh.restore_state(blob)
    assert fresh.state.last_z is None and fresh.state.last_z_at is None
