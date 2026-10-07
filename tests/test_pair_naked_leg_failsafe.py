"""2026-10-07 live naked-leg incident: the pair book must fail safe.

What happened: EICHERMOT/BAJFINANCE (09:21) and BHARTIARTL/M&M (11:55) each
filled leg A, Kotak refused leg B, and Kotak refused the reversal of leg A.
The pair stayed position=FLAT while state.legs still held leg A, so:
reconciliation skipped it ("0 expected positions match"), the untracked broker
position was only a WARNING, scan_and_propose kept trying new entries, and the
15:25 session end would have carried both naked futures overnight. The owner
squared them off by hand.

The PR #11 review then showed that a naive fix is worse: a blind retry flips
the leg when a close fills unseen, session end ignored HALT_ALL, signals mode
reached the broker, the latch froze every runner, and rejection logging could
carry login secrets. Each test pins one of those. A failure here means a
refused order can again leave — or now create — real unhedged exposure.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import runners.run_paper_pairs as rpp
from core.broker.kotak import BrokerOrderError, KotakNeoClient
from core.trade_proposer import TradeProposal
from tests.test_pair_trading import _make_strategy

HIST = [(-1) ** i * 5.0 for i in range(30)]


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


class _Broker:
    """Net positions the way Kotak reports them, moved by our own fills."""

    def __init__(self):
        self.net = {}

    def positions(self):
        return {"net": [{"exchange": "NFO", "tradingsymbol": ts, "quantity": q,
                         "average_price": 0.0} for ts, q in self.net.items()]}

    def book(self, prop, result):
        if result.get("status") == "COMPLETE":
            shares = result["filled_lots"] * prop.lot_size
            sign = 1 if prop.transaction_type == "BUY" else -1
            self.net[prop.tradingsymbol] = self.net.get(prop.tradingsymbol, 0) + sign * shares


def _live_strategy(script, broker=None):
    """Live pair whose orders are answered from `script`, in order."""
    s = _make_strategy(mode="live", spread_history=HIST)
    s._signal_publisher = None
    s.signal_system_tag = "persistent"
    s._pending_entry_z = None
    s._margin_precheck_ok = lambda proposals: True
    s._observe_spread = lambda: (None, {})
    broker = broker or _Broker()
    s.client = broker
    calls = []

    def fake_live(prop, bypass_backoff=False):
        calls.append((prop.tradingsymbol, prop.transaction_type, bypass_backoff))
        result = script.pop(0)
        broker.book(prop, result)
        return result

    s._live_execute = fake_live
    return s, calls, broker


def _todays_partial_entry(extra=()):
    """Leg A fills, leg B refused, reversal of A refused — 09:21 today."""
    s, calls, broker = _live_strategy([_ok(1, 7024.5), REFUSED, REFUSED, *extra])
    s.execute_proposals([
        _prop("AAA26APRFUT", 100, 1, 7024.5, "BUY"),
        _prop("BBB26APRFUT", 200, 1, 900.0, "SELL"),
    ])
    return s, calls, broker


def test_failed_reversal_latches_and_keeps_the_naked_leg_in_state():
    s, calls, broker = _todays_partial_entry()
    assert [c[:2] for c in calls] == [
        ("AAA26APRFUT", "BUY"), ("BBB26APRFUT", "SELL"), ("AAA26APRFUT", "SELL"),
    ]
    assert s.state.position == "FLAT"          # one leg is no spread
    assert s.state.unwind_pending is True      # ...but it is exposure
    assert [(leg.tradingsymbol, leg.quantity) for leg in s.state.legs] == [("AAA26APRFUT", 1)]
    assert broker.net == {"AAA26APRFUT": 100}


def test_a_pair_with_a_naked_leg_opens_nothing_new():
    s, _, _ = _todays_partial_entry()
    s._observe_spread = lambda: pytest.fail("must not even look for an entry")
    assert s.scan_and_propose() == []


def test_unwind_retries_through_the_backoff_and_records_one_trade():
    s, calls, broker = _todays_partial_entry(extra=[REFUSED, _ok(1, 7000.0)])
    s._place_order_skip_ticks_left = 40      # M-B5 armed, as it was today
    assert s.retry_unwind() is False         # first retry refused
    assert s.retry_unwind() is True          # next tick it goes through
    assert s.state.legs == [] and s.state.unwind_pending is False
    assert broker.net == {"AAA26APRFUT": 0}
    unwinds = calls[3:]
    assert [c[:2] for c in unwinds] == [("AAA26APRFUT", "SELL")] * 2
    assert all(c[2] is True for c in unwinds), "unwind must bypass the backoff"
    # Failed entry + unwind is one trade row carrying exactly its loss.
    assert len(s.state.closed_trades) == 1
    assert s.state.closed_trades[0]["realized_pnl"] < 0


def test_a_close_that_filled_unseen_is_never_sent_again():
    """Review finding 1: FAILED reported, but the broker filled the close. A
    blind retry would SELL again and flip the account short every tick."""
    s, calls, broker = _todays_partial_entry()

    def fills_but_reports_failed(prop, bypass_backoff=False):
        calls.append((prop.tradingsymbol, prop.transaction_type, bypass_backoff))
        broker.book(prop, _ok(1, 7000.0))
        return REFUSED

    s._live_execute = fills_but_reports_failed
    assert s.retry_unwind() is False
    assert broker.net == {"AAA26APRFUT": 0}
    for _ in range(3):
        s.retry_unwind()
    assert broker.net == {"AAA26APRFUT": 0}, "must not flip the leg"
    assert len(calls) == 4                    # 3 entry/reversal + exactly 1 close
    assert s.state.legs == [] and s.state.unwind_pending is False


def test_broker_disagreeing_on_side_is_not_traded_and_attempts_are_capped():
    s, _, broker = _todays_partial_entry()
    broker.net = {"AAA26APRFUT": -100}       # broker says short; state says long
    s._live_execute = lambda prop, bypass_backoff=False: pytest.fail("no blind order")
    assert s.retry_unwind() is False
    broker.net = {"AAA26APRFUT": 100}
    sent = []
    s._live_execute = lambda prop, bypass_backoff=False: sent.append(1) or REFUSED
    for _ in range(10):
        s.retry_unwind()
    assert len(sent) == s.MAX_UNWIND_ATTEMPTS


def test_unknown_broker_state_sends_nothing():
    s, _, broker = _todays_partial_entry()

    def down():
        raise RuntimeError("429")

    broker.positions = down
    s._live_execute = lambda prop, bypass_backoff=False: pytest.fail("no order blind")
    assert s.retry_unwind() is False and s.state.legs


def test_signals_mode_never_reaches_the_broker():
    """Review finding 3."""
    s, _, _ = _todays_partial_entry()
    s.mode = "signals"
    s._live_execute = lambda *a, **k: pytest.fail("signals mode sent an order")
    s._paper_execute = lambda *a, **k: pytest.fail("signals mode filled on paper")
    assert s.retry_unwind() is False
    assert s.state.unwind_pending is True


def test_latch_and_stray_leg_survive_a_restart():
    s, _, _ = _todays_partial_entry()
    blob = json.loads(json.dumps(s.serialize_state(), default=str))
    assert blob["state"]["unwind_pending"] is True
    fresh = _make_strategy(mode="live", spread_history=HIST)
    fresh.restore_state(blob)
    assert fresh.state.unwind_pending is True
    assert [leg.tradingsymbol for leg in fresh.state.legs] == ["AAA26APRFUT"]
    del blob["state"]["unwind_pending"]      # file from before the latch
    old = _make_strategy(mode="live", spread_history=HIST)
    old.restore_state(blob)
    assert old.state.unwind_pending is True


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


@pytest.fixture
def flags(tmp_path, monkeypatch):
    """Point every flag at tmp; the scoped latch is what the runner passes."""
    shared = tmp_path / "HALT_NEW_ENTRIES"
    monkeypatch.setattr(rpp, "HALT_NEW_ENTRIES_PATH", shared)
    monkeypatch.setattr(rpp, "HALT_ALL_PATH", tmp_path / "HALT_ALL")
    return SimpleNamespace(shared=shared, scoped=tmp_path / "HALT_NEW_ENTRIES_pairs_persistent",
                           halt_all=tmp_path / "HALT_ALL")


def _net(**qty):
    return SimpleNamespace(positions=lambda: {"net": [
        {"exchange": "NFO", "tradingsymbol": ts, "quantity": q, "average_price": 0.0}
        for ts, q in qty.items()]})


def test_reconcile_counts_a_flat_pairs_stray_leg(flags):
    s, _, _ = _todays_partial_entry()
    log = _Log()
    rpp.reconcile_with_broker([s], _net(AAA26APRFUT=100), log, flags.scoped)
    assert any("1 expected NFO position" in m for _, m in log.records)
    assert not flags.scoped.exists() and not flags.shared.exists()


def test_untracked_position_in_our_contract_halts_only_this_runner(flags):
    s = _make_strategy(mode="live", spread_history=HIST)
    log = _Log()
    rpp.reconcile_with_broker([s], _net(BBB26OCTFUT=-475), log, flags.scoped)
    assert flags.scoped.exists()
    assert not flags.shared.exists(), "never the operator-owned shared flag"
    assert any(lvl == "CRITICAL" and "BBB26OCTFUT" in m for lvl, m in log.records)


def test_untracked_position_in_another_contract_is_loud_but_does_not_halt(flags):
    """Review finding 5: a manual hedge or another book on the account."""
    s = _make_strategy(mode="live", spread_history=HIST)
    log = _Log()
    rpp.reconcile_with_broker([s], _net(ZZZ26OCTFUT=50), log, flags.scoped)
    assert not flags.scoped.exists()
    assert any(lvl == "CRITICAL" and "ZZZ26OCTFUT" in m for lvl, m in log.records)


def test_tick_unwinds_first_and_latches_the_scoped_flag(flags):
    s, _, _ = _todays_partial_entry(extra=[_ok(1, 7000.0)])
    s.scan_and_propose = lambda: pytest.fail("no entry scan while a leg is naked")
    out = rpp.tick_one(s, _Log(), halt_path=flags.scoped)
    assert out.attempted_execution and not out.errored
    assert flags.scoped.exists() and not flags.shared.exists()
    assert s.state.legs == []


def test_paper_naked_leg_does_not_latch_any_flag(flags):
    """Review finding 4: a paper runner must never freeze the live book."""
    s, _, _ = _todays_partial_entry()
    s.mode = "paper"
    s._paper_execute = lambda prop: _ok(prop.quantity, 7000.0)
    rpp.tick_one(s, _Log(), halt_path=flags.scoped)
    assert not flags.scoped.exists() and not flags.shared.exists()


def _eod(strategies, monkeypatch):
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    args = SimpleNamespace(force_flatten_on_exit=False, system="persistent", mode="live")
    rpp.end_of_session(strategies, date(2026, 10, 7), args, _Log())


def test_session_end_closes_a_naked_leg(flags, monkeypatch):
    s, calls, _ = _todays_partial_entry(extra=[_ok(1, 7000.0)])
    _eod([s], monkeypatch)
    assert calls[-1][:2] == ("AAA26APRFUT", "SELL")
    assert s.state.legs == []


def test_session_end_respects_halt_all_and_still_escalates(flags, monkeypatch):
    """Review findings 2 and 10: HALT_ALL means no orders, and a naked leg
    left open must end the run non-zero so notify-failure@ fires."""
    flags.halt_all.touch()
    s, _, _ = _todays_partial_entry()
    s._live_execute = lambda *a, **k: pytest.fail("order sent under HALT_ALL")
    with pytest.raises(RuntimeError, match="naked leg"):
        _eod([s], monkeypatch)
    assert s.state.legs


def test_kotak_order_rejection_keeps_the_reason_but_not_secrets():
    """41 orders said only 'error from core'; the reason was dropped."""
    body = {"stat": "Not_Ok", "emsg": "error from core", "stCode": 1009,
            "errMsg": "RMS: some real reason", "token": "SECRET", "sid": "SID1"}
    resp = SimpleNamespace(status_code=200, json=lambda: body, text=json.dumps(body))
    client = KotakNeoClient.__new__(KotakNeoClient)
    client.session = SimpleNamespace(request=lambda *a, **k: resp)
    with pytest.raises(BrokerOrderError) as e:
        client._request_json("POST", "https://x/quick/order/rule/ms/place",
                             headers={}, detail=True)
    msg = str(e.value)
    assert "[HTTP 200]" in msg and '"stCode": 1009' in msg
    assert "RMS: some real reason" in msg
    assert "SECRET" not in msg and "SID1" not in msg
    # Login/IP/quote calls never opt in, so no body at all (review finding 6).
    with pytest.raises(BrokerOrderError) as e2:
        client._request_json("POST", "https://x/login/1.0/tradeApiValidate", headers={})
    assert "body=" not in str(e2.value)
