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
    import run_paper_pairs as rpp
    caplog.set_level(logging.WARNING, logger="")
    s_open = _FakeStrategy("AAA", "BBB", position="LONG_SPREAD")
    s_flat = _FakeStrategy("CCC", "DDD", position="FLAT")
    args = SimpleNamespace(force_flatten_on_exit=False, system="baseline")
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    # Friday 2026-05-29 → next trading day Mon 2026-06-01 (3 calendar days)
    rpp.end_of_session([s_open, s_flat], date(2026, 5, 29), args,
                       logging.getLogger("test"), holidays=set())
    assert any("M-R1" in r.message and "3 calendar days" in r.message
               for r in caplog.records)


def test_no_warning_when_no_open_book(monkeypatch, caplog):
    import run_paper_pairs as rpp
    caplog.set_level(logging.WARNING, logger="")
    args = SimpleNamespace(force_flatten_on_exit=False, system="baseline")
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    rpp.end_of_session([_FakeStrategy("CCC", "DDD")], date(2026, 5, 29), args,
                       logging.getLogger("test"), holidays=set())
    assert not any("M-R1" in r.message for r in caplog.records)


def test_no_warning_when_force_flatten_set(monkeypatch, caplog):
    import run_paper_pairs as rpp
    caplog.set_level(logging.WARNING, logger="")
    args = SimpleNamespace(force_flatten_on_exit=True, system="baseline")
    monkeypatch.setattr(rpp, "write_state_file", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "write_eod_sidecar", lambda *a, **k: None)
    monkeypatch.setattr(rpp, "flatten_one", lambda *a, **k: None)
    s = _FakeStrategy("AAA", "BBB", position="LONG_SPREAD")
    rpp.end_of_session([s], date(2026, 5, 29), args,
                       logging.getLogger("test"), holidays=set())
    assert not any("M-R1" in r.message for r in caplog.records)


def test_calendar_days_helper_skips_weekend():
    from run_paper_pairs import _calendar_days_until_next_trading_day
    # Friday 2026-05-29 → Mon 2026-06-01 = 3 calendar days
    assert _calendar_days_until_next_trading_day(date(2026, 5, 29), set()) == 3


def test_calendar_days_helper_skips_holiday():
    from run_paper_pairs import _calendar_days_until_next_trading_day
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
    from run_paper_pairs import reconcile_with_broker
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
    from run_paper_pairs import reconcile_with_broker
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
    from run_paper_pairs import reconcile_with_broker
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
