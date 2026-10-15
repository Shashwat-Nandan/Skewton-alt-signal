"""M-R1 / M-R2 — long-break warning and entry-price cross-check."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import logging
import pytest


# ──────────────────────────────────────────────────────────
# M-R1 — long-weekend warning at session end
# ──────────────────────────────────────────────────────────

class _FakeStrategy:
    """Just enough surface for end_of_session's branches."""
    def __init__(self, sa, sb, position="FLAT"):
        self.symbol_a, self.symbol_b = sa, sb
        self.state = SimpleNamespace(position=position, legs=[])

    def legs_expire_on(self, today):
        return False


def test_long_break_warning_fires_when_book_open(monkeypatch, caplog):
    from runners import run_paper_pairs as rpp
    caplog.set_level(logging.WARNING, logger="")
    s_open = _FakeStrategy("AAA", "BBB", position="LONG_SPREAD")
    s_flat = _FakeStrategy("CCC", "DDD", position="FLAT")
    args = SimpleNamespace(force_flatten_on_exit=False, system="baseline", mode="paper")
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    # Friday 2026-05-29 → next trading day Mon 2026-06-01 (3 calendar days)
    rpp.end_of_session([s_open, s_flat], date(2026, 5, 29), args,
                       logging.getLogger("test"), holidays=set())
    assert any("M-R1" in r.message and "3 calendar days" in r.message
               for r in caplog.records)


def test_no_warning_when_no_open_book(monkeypatch, caplog):
    from runners import run_paper_pairs as rpp
    caplog.set_level(logging.WARNING, logger="")
    args = SimpleNamespace(force_flatten_on_exit=False, system="baseline", mode="paper")
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    rpp.end_of_session([_FakeStrategy("CCC", "DDD")], date(2026, 5, 29), args,
                       logging.getLogger("test"), holidays=set())
    assert not any("M-R1" in r.message for r in caplog.records)


def test_no_warning_when_force_flatten_set(monkeypatch, caplog):
    from runners import run_paper_pairs as rpp
    caplog.set_level(logging.WARNING, logger="")
    args = SimpleNamespace(force_flatten_on_exit=True, system="baseline", mode="paper")
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "flatten_one", lambda *a, **k: None)
    s = _FakeStrategy("AAA", "BBB", position="LONG_SPREAD")
    rpp.end_of_session([s], date(2026, 5, 29), args,
                       logging.getLogger("test"), holidays=set())
    assert not any("M-R1" in r.message for r in caplog.records)


def test_calendar_days_helper_skips_weekend():
    from runners.run_paper_pairs import _calendar_days_until_next_trading_day
    # Friday 2026-05-29 → Mon 2026-06-01 = 3 calendar days
    assert _calendar_days_until_next_trading_day(date(2026, 5, 29), set()) == 3


def test_calendar_days_helper_skips_holiday():
    from runners.run_paper_pairs import _calendar_days_until_next_trading_day
    # Thursday with Friday as a holiday → Mon
    holidays = {date(2026, 5, 29)}  # Friday is a holiday
    assert _calendar_days_until_next_trading_day(
        date(2026, 5, 28), holidays
    ) == 4


# ──────────────────────────────────────────────────────────
# M-R2 — entry_price cross-check vs broker average_price
# ──────────────────────────────────────────────────────────

class _Leg:
    def __init__(self, tradingsymbol, quantity, lot_size, entry_price):
        self.tradingsymbol = tradingsymbol
        self.quantity = quantity
        self.lot_size = lot_size
        self.entry_price = entry_price


class _LiveStrategy:
    def __init__(self, sa, sb, legs):
        self.symbol_a, self.symbol_b = sa, sb
        self.mode = "live"
        self.state = SimpleNamespace(position="LONG_SPREAD", legs=legs)


def _kite_with_positions(positions):
    kite = MagicMock()
    kite.positions = MagicMock(return_value={"net": positions})
    return kite


def test_entry_price_drift_warns_does_not_block(caplog):
    from runners.run_paper_pairs import reconcile_with_broker
    caplog.set_level(logging.WARNING, logger="")
    s = _LiveStrategy("AAA", "BBB", [
        _Leg("AAA26APRFUT", quantity=1, lot_size=100, entry_price=1000.0),
    ])
    # Broker shares match but avg_price differs by >0.5%
    kite = _kite_with_positions([{
        "exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
        "quantity": 100, "average_price": 1015.0,
    }])
    # Must NOT raise — only warn
    reconcile_with_broker([s], kite, logging.getLogger("test"))
    assert any("M-R2" in r.message and "entry_price" in r.message
               for r in caplog.records)


def test_entry_price_within_tolerance_no_warn(caplog):
    from runners.run_paper_pairs import reconcile_with_broker
    caplog.set_level(logging.WARNING, logger="")
    s = _LiveStrategy("AAA", "BBB", [
        _Leg("AAA26APRFUT", quantity=1, lot_size=100, entry_price=1000.0),
    ])
    kite = _kite_with_positions([{
        "exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
        "quantity": 100, "average_price": 1003.0,  # 0.3% — under tol
    }])
    reconcile_with_broker([s], kite, logging.getLogger("test"))
    assert not any("M-R2" in r.message for r in caplog.records)


def test_share_mismatch_still_blocks(caplog):
    from runners.run_paper_pairs import reconcile_with_broker
    s = _LiveStrategy("AAA", "BBB", [
        _Leg("AAA26APRFUT", quantity=1, lot_size=100, entry_price=1000.0),
    ])
    # Broker disagrees on shares — must raise (M-R2 path doesn't reach)
    kite = _kite_with_positions([{
        "exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
        "quantity": 50, "average_price": 1000.0,
    }])
    with pytest.raises(RuntimeError, match="reconciliation FAILED"):
        reconcile_with_broker([s], kite, logging.getLogger("test"))


def test_offsetting_legs_across_pairs_reconcile_against_broker_net(caplog):
    """2026-07-09/10 incident: two pairs held equal-and-opposite legs in the
    same contract (+200/−200 M&M26JULFUT). Kite's "net" bucket reports ONE
    net row per contract (qty 0, and often no row at all), so per-leg
    comparison false-flagged BOTH legs, halting entries mid-session and
    refusing the next day's start while state and broker actually agreed.
    Expected shares must be summed across strategies per tradingsymbol."""
    from runners.run_paper_pairs import reconcile_with_broker
    caplog.set_level(logging.WARNING, logger="")
    s1 = _LiveStrategy("BHA", "MMM", [
        _Leg("BHA26JULFUT", quantity=-1, lot_size=475, entry_price=1900.0),
        _Leg("MMM26JULFUT", quantity=1, lot_size=200, entry_price=3200.0),
    ])
    s2 = _LiveStrategy("MMM", "HDF", [
        _Leg("MMM26JULFUT", quantity=-1, lot_size=200, entry_price=3150.0),
        _Leg("HDF26JULFUT", quantity=1, lot_size=1100, entry_price=640.0),
    ])
    # Broker: the two MMM legs net to zero → no MMM row at all (Kite may
    # also report a 0-qty row; absent is the harsher case).
    kite = _kite_with_positions([
        {"exchange": "NFO", "tradingsymbol": "BHA26JULFUT",
         "quantity": -475, "average_price": 1900.0},
        {"exchange": "NFO", "tradingsymbol": "HDF26JULFUT",
         "quantity": 1100, "average_price": 640.0},
    ])
    # Must NOT raise: +200 − 200 = 0 matches the absent broker row.
    reconcile_with_broker([s1, s2], kite, logging.getLogger("test"))
    # M-R2 must not fire for the shared contract (entry prices 3200/3150
    # differ, but no single broker average_price is attributable).
    assert not any("M-R2" in r.message for r in caplog.records)


def test_offsetting_legs_aggregate_mismatch_still_blocks():
    """Aggregation must not weaken the gate: if the summed expectation
    disagrees with the broker net, refuse to start and name every
    contributing pair."""
    from runners.run_paper_pairs import reconcile_with_broker
    s1 = _LiveStrategy("BHA", "MMM", [
        _Leg("MMM26JULFUT", quantity=1, lot_size=200, entry_price=3200.0),
    ])
    s2 = _LiveStrategy("MMM", "HDF", [
        _Leg("MMM26JULFUT", quantity=-2, lot_size=200, entry_price=3150.0),
    ])
    # Expected net = +200 − 400 = −200; broker says flat.
    kite = _kite_with_positions([])
    with pytest.raises(RuntimeError, match=r"MMM26JULFUT.*BHA/MMM \+200.*MMM/HDF -400"):
        reconcile_with_broker([s1, s2], kite, logging.getLogger("test"))


# ──────────────────────────────────────────────────────────
# 3.7 / M-6 — mid-session reconcile cadence (non-fatal drift handling)
# ──────────────────────────────────────────────────────────

def _live_leg_strategy():
    return _LiveStrategy("AAA", "BBB", [
        _Leg("AAA26APRFUT", quantity=1, lot_size=100, entry_price=1000.0),
    ])


def _flags(tmp_path, monkeypatch):
    """Shared operator flag, this runner's drift latch and its self-clearing
    unreadable-broker latch, all in tmp."""
    from runners import run_paper_pairs as rp
    shared = tmp_path / "HALT_NEW_ENTRIES"
    monkeypatch.setattr(rp, "HALT_NEW_ENTRIES_PATH", shared)
    return (rp, shared, tmp_path / "HALT_NEW_ENTRIES_pairs_persistent",
            tmp_path / "HALT_NEW_ENTRIES_pairs_persistent_broker_unreadable")


def _down(exc):
    kite = MagicMock()
    kite.positions = MagicMock(side_effect=exc)
    return kite


def test_mid_session_clean_reconcile_no_halt(tmp_path, monkeypatch):
    rp, shared, scoped, _ = _flags(tmp_path, monkeypatch)
    s = _live_leg_strategy()
    kite = _kite_with_positions([{
        "exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
        "quantity": 100, "average_price": 1000.0,
    }])
    assert rp.reconcile_mid_session([s], kite, logging.getLogger("test"), scoped) == "ok"
    assert not shared.exists() and not scoped.exists()


def test_mid_session_drift_halts_this_runner_without_raising(tmp_path, monkeypatch, caplog):
    # A share mismatch must NOT raise (it would crash the live loop). It halts
    # THIS runner's entries — its scoped flag — never the shared operator flag,
    # which froze every runner on 2026-10-13. The log states the facts, not
    # the start-up recovery advice (the runner is live and managing exits).
    rp, shared, scoped, _ = _flags(tmp_path, monkeypatch)
    caplog.set_level(logging.CRITICAL, logger="")
    s = _live_leg_strategy()
    kite = _kite_with_positions([{
        "exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
        "quantity": 50, "average_price": 1000.0,   # broker disagrees on shares
    }])
    assert rp.reconcile_mid_session([s], kite, logging.getLogger("test"), scoped) == "drift"
    assert scoped.exists() and not shared.exists()
    drift = [r.message for r in caplog.records if "MID-SESSION RECONCILE DRIFT" in r.message]
    assert drift and "AAA26APRFUT" in drift[0]
    assert "refusing to start" not in drift[0].lower()
    assert "move the state file aside" not in drift[0]


def test_mid_session_network_failure_is_unknown_not_drift(tmp_path, monkeypatch):
    # 2026-10-13: one Kotak positions() read timeout (a BrokerNetworkError) was
    # treated as drift. A transient read failure must not halt on its own.
    from core.broker.errors import BrokerNetworkError
    rp, shared, scoped, _ = _flags(tmp_path, monkeypatch)
    kite = _down(BrokerNetworkError("Kotak GET .../positions failed: Read timed out."))
    assert rp.reconcile_mid_session([_live_leg_strategy()], kite,
                                    logging.getLogger("test"), scoped) == "unknown"
    assert not shared.exists() and not scoped.exists()


def test_mid_session_permanent_failure_halts_at_once(tmp_path, monkeypatch):
    # A rejected session will not fix itself on retry: drift severity, scoped
    # halt immediately, not two minutes of "unknown" (PR #17 review).
    from core.broker.errors import BrokerOrderError
    rp, shared, scoped, _ = _flags(tmp_path, monkeypatch)
    kite = _down(BrokerOrderError("invalid session token"))
    assert rp.reconcile_mid_session([_live_leg_strategy()], kite,
                                    logging.getLogger("test"), scoped) == "drift"
    assert scoped.exists() and not shared.exists()


def test_mid_session_read_gives_up_quickly(tmp_path, monkeypatch):
    # A hung positions() must not stall the tick loop's exits for ~30 s.
    import time as _t
    rp, shared, scoped, _ = _flags(tmp_path, monkeypatch)
    monkeypatch.setattr(rp, "RECONCILE_READ_TIMEOUT_S", 0.2)
    kite = MagicMock()
    kite.positions = MagicMock(side_effect=lambda: _t.sleep(2) or {"net": []})
    t0 = _t.monotonic()
    status = rp.reconcile_mid_session([_live_leg_strategy()], kite,
                                      logging.getLogger("test"), scoped)
    assert status == "unknown" and _t.monotonic() - t0 < 1.5


def test_start_up_still_refuses_on_an_unreadable_broker():
    from core.broker.errors import BrokerNetworkError
    from runners import run_paper_pairs as rp
    with pytest.raises(RuntimeError, match="could not run"):
        rp.reconcile_with_broker([_live_leg_strategy()], _down(BrokerNetworkError("timeout")),
                                 logging.getLogger("test"))


def test_mid_session_noop_for_paper(tmp_path, monkeypatch):
    rp, shared, scoped, _ = _flags(tmp_path, monkeypatch)
    s = _live_leg_strategy()
    s.mode = "paper"
    kite = MagicMock()
    assert rp.reconcile_mid_session([s], kite, logging.getLogger("test"), scoped) == "ok"
    assert not shared.exists() and not scoped.exists()
    kite.positions.assert_not_called()


def test_the_runner_reads_the_flags_it_sets():
    # Wiring (PR #17 review): the paths the runner latches are the ones its
    # _HaltState reads, under the names the operator docs and dashboard show.
    from runners import run_paper_pairs as rp
    drift, unreadable = rp.naked_leg_halt_path("persistent"), rp.broker_unreadable_halt_path("persistent")
    assert drift.name == "HALT_NEW_ENTRIES_pairs_persistent"
    assert unreadable.name == "HALT_NEW_ENTRIES_pairs_persistent_broker_unreadable"
    assert drift != unreadable


def test_reconcile_schedule_retries_fast_halts_on_a_streak_and_self_clears(tmp_path, monkeypatch):
    # One unreadable check must not halt; it re-checks after 60 s. Three in a
    # row (~3 minutes blind) latch the unreadable flag; the next readable
    # answer clears it again, so a blip cannot freeze entries until someone
    # notices. A drift latch is never cleared by recovery.
    rp, _, drift_flag, unreadable = _flags(tmp_path, monkeypatch)
    log = logging.getLogger("test")
    sched = rp.ReconcileSchedule(now=0.0, unreadable_path=unreadable)
    assert not sched.due(3599.0) and sched.due(3600.0)
    t = 3600.0
    sched.record("unknown", t, log)
    assert not unreadable.exists() and sched.due(t + rp.RECONCILE_RETRY_S)
    for _ in range(rp.RECONCILE_UNKNOWN_HALT_AFTER - 1):
        t += rp.RECONCILE_RETRY_S
        sched.record("unknown", t, log)
    assert unreadable.exists()
    drift_flag.touch()
    sched.record("ok", t + 60, log)
    assert not unreadable.exists(), "recovery must clear the unreadable halt"
    assert drift_flag.exists(), "recovery must never clear a drift halt"
    assert sched.unknown_streak == 0 and not sched.due(t + 60 + 3599)


def test_mid_session_check_uses_the_refreshed_client_and_halts_this_tick(tmp_path, monkeypatch):
    # PR #17 review: (1) after a token refresh the check must read through
    # the CURRENT client, not the dead start-up one; (2) a halt it latches
    # must stop entries in the same tick, so halt flags are re-read.
    rp, shared, scoped, unreadable = _flags(tmp_path, monkeypatch)
    log = logging.getLogger("test")
    from core.broker.errors import BrokerOrderError
    dead = _down(BrokerOrderError("invalid session token"))
    fresh = _kite_with_positions([{"exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
                                   "quantity": 100, "average_price": 1000.0}])
    client_ref = {"kite": dead}
    client_ref["kite"] = fresh                       # what _refresh_broker does
    halt_state = rp._HaltState(tmp_path / "HALT_DAILY_LOSS", scoped_path=[scoped, unreadable])
    sched = rp.ReconcileSchedule(now=0.0, unreadable_path=unreadable)
    s = _live_leg_strategy()
    assert rp.run_mid_session_reconcile([s], client_ref, sched, halt_state, log,
                                        scoped, now=3600.0) == "ok"
    dead.positions.assert_not_called()
    assert halt_state.halt_new is False

    client_ref["kite"] = _kite_with_positions([{"exchange": "NFO", "tradingsymbol": "AAA26APRFUT",
                                                "quantity": 50, "average_price": 1000.0}])
    assert rp.run_mid_session_reconcile([s], client_ref, sched, halt_state, log,
                                        scoped, now=7200.0) == "drift"
    assert halt_state.halt_new is True, "the halt must apply in the same tick"
    assert not shared.exists()


# ──────────────────────────────────────────────────────────
# Per-runner daily-loss breaker isolation — a paper runner's loss breach
# must NOT freeze a co-running live pair runner. Both pair runners share
# data_cache, so the daily-loss flag is namespaced per --system while the
# persistent (live) runner keeps the canonical flag its alert watches.
# ──────────────────────────────────────────────────────────

class _BreachStrategy:
    """Minimal surface for check_daily_loss_limit: a fixed session ΔP&L of
    (realized + unrealized) − (session-start realized + unrealized)."""
    def __init__(self, session_delta):
        self.state = SimpleNamespace(realized_pnl=session_delta,
                                     unrealized_pnl=0.0)
        self._session_start_realized = 0.0
        self._session_start_unrealized = 0.0


def test_halt_daily_loss_path_persistent_is_canonical():
    from runners import run_paper_pairs as rpp
    # The LIVE (persistent) runner keeps the canonical flag so its Telegram
    # alert (deploy/pair-halt-alert.path) and `rm` runbook stay valid unchanged.
    assert rpp.halt_daily_loss_path("persistent") == rpp.HALT_DAILY_LOSS_PATH
    assert rpp.halt_daily_loss_path("persistent").name == "HALT_DAILY_LOSS"


def test_halt_daily_loss_path_baseline_is_namespaced():
    from runners import run_paper_pairs as rpp
    p = rpp.halt_daily_loss_path("baseline")
    # WHY: distinct from the canonical flag, so a baseline (paper) breach can
    # never touch the live persistent runner's breaker.
    assert p.name == "HALT_DAILY_LOSS_baseline"
    assert p != rpp.HALT_DAILY_LOSS_PATH


def test_baseline_breach_does_not_touch_live_flag(tmp_path):
    # A baseline paper loss breach must touch ONLY its own flag; the canonical
    # HALT_DAILY_LOSS the live persistent runner reads must stay absent —
    # otherwise a paper loss would freeze the real-money book's entries.
    from runners import run_paper_pairs as rpp
    live_flag = tmp_path / "HALT_DAILY_LOSS"
    baseline_flag = tmp_path / "HALT_DAILY_LOSS_baseline"
    breaching = _BreachStrategy(session_delta=-200_000.0)   # ₹2L loss > ₹1L cap
    rpp.check_daily_loss_limit([breaching], 100_000.0,
                               logging.getLogger("test"), baseline_flag)
    assert baseline_flag.exists()      # own flag tripped
    assert not live_flag.exists()      # live runner's breaker untouched


def test_halt_state_reads_only_its_own_daily_loss_flag(tmp_path, monkeypatch):
    # _HaltState bound to the baseline flag must ignore the canonical/live flag:
    # a live-runner breach must not suspend the baseline runner and vice-versa.
    from runners import run_paper_pairs as rpp
    monkeypatch.setattr(rpp, "HALT_ALL_PATH", tmp_path / "HALT_ALL")
    monkeypatch.setattr(rpp, "HALT_NEW_ENTRIES_PATH", tmp_path / "HALT_NEW_ENTRIES")
    baseline_flag = tmp_path / "HALT_DAILY_LOSS_baseline"
    (tmp_path / "HALT_DAILY_LOSS").touch()          # canonical/live flag present
    hs = rpp._HaltState(baseline_flag)
    hs.refresh(logging.getLogger("test"))
    assert hs.halt_new is False                      # live flag ignored
    baseline_flag.touch()                            # own flag now present
    hs.refresh(logging.getLogger("test"))
    assert hs.halt_new is True
