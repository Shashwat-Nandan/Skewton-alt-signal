"""2026-10-07 live naked-leg incident: the pair book must fail safe.

What happened: EICHERMOT/BAJFINANCE (09:21) and BHARTIARTL/M&M (11:55) each
filled leg A, Kotak refused leg B, and Kotak refused the reversal of leg A.
The pair stayed position=FLAT while state.legs still held leg A, so:
reconciliation skipped it ("0 expected positions match"), the untracked broker
position was only a WARNING, scan_and_propose kept trying new entries, and the
15:25 session end would have carried both naked futures overnight. The owner
squared them off by hand.

Each test pins one of those holes. A failure here means a refused order can
again leave real, unhedged exposure that the runner neither sees nor closes.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import runners.run_paper_pairs as rpp
from core.broker.kotak import BrokerOrderError, KotakNeoClient
from core.trade_proposer import TradeProposal
from tests.test_pair_trading import _make_strategy


def _prop(ts, lot, qty, price, side):
    return TradeProposal(
        tradingsymbol=ts, instrument_token=0, strike=0.0,
        expiry="2026-04-28 00:00:00", option_type="FUT", lot_size=lot,
        quantity=qty, price=price, transaction_type=side, iv=0.0,
        bid_ask_spread_pct=0.0, margin_required=price * lot * qty * 0.2,
        rationale="test",
    )


def _ok(lots, px):
    return {"order_id": "1", "status": "COMPLETE", "filled_lots": lots,
            "average_price": px, "mode": "live"}


REFUSED = {"order_id": None, "status": "FAILED", "filled_lots": 0,
           "average_price": 0.0, "mode": "live",
           "error": "place_order rejected: error from core"}


def _live_strategy(script):
    """Live-mode pair whose broker answers from `script` in order."""
    history = [(-1) ** i * 5.0 for i in range(30)]
    s = _make_strategy(mode="live", spread_history=history)
    s._signal_publisher = None
    s.signal_system_tag = "persistent"
    s._pending_entry_z = None
    s._margin_precheck_ok = lambda proposals: True
    calls = []

    def fake_live(prop, bypass_backoff=False):
        calls.append((prop.tradingsymbol, prop.transaction_type, bypass_backoff))
        return script.pop(0)

    s._live_execute = fake_live
    return s, calls


def _todays_partial_entry():
    """Leg A fills, leg B refused, reversal of A refused — 09:21 today."""
    s, calls = _live_strategy([_ok(1, 7024.5), REFUSED, REFUSED])
    s.execute_proposals([
        _prop("AAA26APRFUT", 100, 1, 7024.5, "BUY"),
        _prop("BBB26APRFUT", 750, 1, 900.0, "SELL"),
    ])
    return s, calls


def test_failed_reversal_latches_and_keeps_the_naked_leg_in_state():
    s, calls = _todays_partial_entry()
    assert [c[:2] for c in calls] == [
        ("AAA26APRFUT", "BUY"), ("BBB26APRFUT", "SELL"), ("AAA26APRFUT", "SELL"),
    ]
    assert s.state.position == "FLAT"          # one leg is no spread
    assert s.state.unwind_pending is True      # ...but it is exposure
    assert [(leg.tradingsymbol, leg.quantity) for leg in s.state.legs] == [("AAA26APRFUT", 1)]


def test_a_pair_with_a_naked_leg_opens_nothing_new():
    """Today the runner kept proposing BHARTIARTL/M&M and EICHERMOT/BAJFINANCE
    entries on top of the stray legs."""
    s, _ = _todays_partial_entry()
    s._observe_spread = lambda: pytest.fail("must not even look for an entry")
    assert s.scan_and_propose() == []


def test_unwind_retries_through_the_backoff_and_clears_when_closed():
    s, calls = _todays_partial_entry()
    s._place_order_skip_ticks_left = 40      # M-B5 armed, as it was today
    script = [REFUSED, _ok(1, 7000.0)]
    s._live_execute = lambda prop, bypass_backoff=False: (
        calls.append((prop.tradingsymbol, prop.transaction_type, bypass_backoff))
        or script.pop(0)
    )
    assert s.retry_unwind() is False         # first retry refused
    assert s.state.unwind_pending is True and s.state.legs
    assert s.retry_unwind() is True          # next tick it goes through
    assert s.state.unwind_pending is False and s.state.legs == []
    unwinds = calls[3:]
    assert [c[:2] for c in unwinds] == [("AAA26APRFUT", "SELL")] * 2
    assert all(c[2] is True for c in unwinds), "unwind must bypass the backoff"


def test_latch_and_stray_leg_survive_a_restart():
    s, _ = _todays_partial_entry()
    blob = json.loads(json.dumps(s.serialize_state(), default=str))
    assert blob["state"]["unwind_pending"] is True
    fresh = _make_strategy(mode="live", spread_history=[(-1) ** i * 5.0 for i in range(30)])
    fresh.restore_state(blob)
    assert fresh.state.unwind_pending is True
    assert [leg.tradingsymbol for leg in fresh.state.legs] == ["AAA26APRFUT"]
    # A file written before the latch existed: FLAT with legs is still a stray.
    del blob["state"]["unwind_pending"]
    old = _make_strategy(mode="live", spread_history=[(-1) ** i * 5.0 for i in range(30)])
    old.restore_state(blob)
    assert old.state.unwind_pending is True


@pytest.fixture
def halt_path(tmp_path, monkeypatch):
    path = tmp_path / "HALT_NEW_ENTRIES"
    monkeypatch.setattr(rpp, "HALT_NEW_ENTRIES_PATH", path)
    return path


class _Log:
    def __init__(self):
        self.records = []

    def _add(self, level, msg, *a):
        self.records.append((level, msg % a if a else msg))

    def info(self, m, *a): self._add("INFO", m, *a)
    def warning(self, m, *a): self._add("WARNING", m, *a)
    def error(self, m, *a): self._add("ERROR", m, *a)
    def critical(self, m, *a): self._add("CRITICAL", m, *a)
    def exception(self, m, *a): self._add("ERROR", m, *a)


def _broker(net):
    return SimpleNamespace(positions=lambda: {"net": net})


def test_reconcile_counts_a_flat_pairs_stray_leg(halt_path):
    """Today: state held the leg, broker held the leg, yet the check said
    '0 expected positions match' and called the broker's copy untracked."""
    s, _ = _todays_partial_entry()
    log = _Log()
    rpp.reconcile_with_broker(
        [s], _broker([{"exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
                       "quantity": 100, "average_price": 7024.5}]), log)
    assert any("1 expected NFO position" in m for _, m in log.records)
    assert not halt_path.exists()


def test_an_untracked_broker_position_halts_new_entries(halt_path):
    s = _make_strategy(mode="live", spread_history=[(-1) ** i * 5.0 for i in range(30)])
    log = _Log()
    rpp.reconcile_with_broker(
        [s], _broker([{"exchange": "NFO", "tradingsymbol": "ZZZ26OCTFUT",
                       "quantity": -475, "average_price": 1847.2}]), log)
    assert halt_path.exists()
    assert any(lvl == "CRITICAL" and "ZZZ26OCTFUT" in m for lvl, m in log.records)


def test_tick_unwinds_first_and_halts_entries(halt_path):
    s, _ = _todays_partial_entry()
    s._live_execute = lambda prop, bypass_backoff=False: _ok(1, 7000.0)
    s.scan_and_propose = lambda: pytest.fail("no entry scan while a leg is naked")
    out = rpp.tick_one(s, _Log())
    assert out.attempted_execution and not out.errored
    assert halt_path.exists()
    assert s.state.legs == [] and s.state.unwind_pending is False


def test_session_end_never_carries_a_naked_leg(halt_path, monkeypatch):
    s, _ = _todays_partial_entry()
    closed = []
    s._live_execute = lambda prop, bypass_backoff=False: (
        closed.append(prop.tradingsymbol) or _ok(1, 7000.0))
    args = SimpleNamespace(force_flatten_on_exit=False, system="persistent", mode="live")
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    rpp.end_of_session([s], __import__("datetime").date(2026, 10, 7), args, _Log())
    assert closed == ["AAA26APRFUT"]
    assert s.state.legs == []


def test_kotak_rejection_keeps_the_whole_response():
    """41 orders today said only 'error from core'; the reason was dropped."""
    body = {"stat": "Not_Ok", "emsg": "error from core", "stCode": 1009,
            "errMsg": "RMS: some real reason"}
    resp = SimpleNamespace(status_code=200, json=lambda: body, text=json.dumps(body))
    client = KotakNeoClient.__new__(KotakNeoClient)
    client.session = SimpleNamespace(request=lambda *a, **k: resp)
    with pytest.raises(BrokerOrderError) as e:
        client._request_json("POST", "https://x/quick/order/rule/ms/place", headers={})
    msg = str(e.value)
    assert "[HTTP 200]" in msg
    assert '"stCode": 1009' in msg and "RMS: some real reason" in msg
