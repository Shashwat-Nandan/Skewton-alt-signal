"""Regression guards for the research/backtest_pairs.py strategy bootstrap (2026-06-02).

`make_strategy` builds PairTradingStrategy via __new__ to skip __init__'s
bhavcopy seed, then sets attributes by hand. That hand-list silently drifted
from __init__ as features landed (H5 cooldown, book-notional cap, place-order
backoff, exit debounce) — every tick then raised AttributeError, was swallowed
per-tick, and the whole backtest reported ₹0 with no trades. Separately, the
mock's synthetic futures tradingsymbol used an underscore that the pre-submit
validate_order regex rejects, so every entry order failed pre-submit.

These two tests fail loudly on a recurrence of either drift.
"""
from __future__ import annotations

import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research.backtest_pairs import make_strategy
from research.engine import MockBroker
from strategies.base import validate_order
from core.trade_proposer import TradeProposal


def _init_self_attrs() -> set:
    """Every `self.X = ...` assignment in PairTradingStrategy.__init__,
    annotation-aware (`self.x: T = ...`)."""
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "strategies", "pair_trading.py"),
        encoding="utf-8",
    ).read().splitlines()
    start = end = None
    for i, line in enumerate(src):
        if "def __init__" in line:
            start = i
        elif start is not None and re.match(r"^    def [a-z]", line) and i > start:
            end = i
            break
    attrs = set()
    for line in src[start:end]:
        for m in re.finditer(r"self\.([a-z_][a-z0-9_]*)\s*(?::[^=]+)?=(?!=)", line):
            attrs.add(m.group(1))
    return attrs


def _build():
    idx = pd.date_range("2025-01-01", periods=3, freq="D")
    panel = pd.DataFrame(
        {"AAA": [100.0, 101.0, 102.0], "BBB": [50.0, 50.5, 51.0]}, index=idx
    )
    kite = MockBroker(panel, {"AAA": 10, "BBB": 20})
    s = make_strategy(
        "AAA", "BBB", 1.0, kite,
        entry_z=2.0, exit_z=0.75, stop_z=4.0,
        lookback_days=60, max_holding_days=7, lots_per_leg=1,
    )
    return s, kite


def test_make_strategy_covers_all_init_attrs():
    """The __new__ bootstrap must set every attribute __init__ would, or the
    tick/entry/fill paths AttributeError mid-replay and the backtest silently
    zeroes out. This is the exact failure that produced the all-₹0 results."""
    s, _ = _build()
    missing = sorted(_init_self_attrs() - set(vars(s).keys()))
    assert not missing, (
        f"make_strategy() is missing {len(missing)} attribute(s) that "
        f"PairTradingStrategy.__init__ sets: {missing}. Add them to the "
        f"__new__ bootstrap in backtest_pairs.make_strategy (mirror the "
        f"__init__ defaults), or the backtest will silently produce no trades."
    )


def test_mock_futures_symbols_pass_pre_submit_validation():
    """MockBroker's synthetic FUT tradingsymbols must satisfy the pre-submit
    validate_order regex, or every backtest entry order is rejected and no
    position ever opens (the '_BTFUT' underscore bug)."""
    _, kite = _build()
    rows = kite.instruments("NFO")
    assert rows, "mock returned no NFO instruments"
    for row in rows:
        prop = TradeProposal(
            tradingsymbol=row["tradingsymbol"], instrument_token=1, strike=0,
            expiry=row["expiry"], option_type="FUT", lot_size=row["lot_size"],
            quantity=1, price=100.0, transaction_type="BUY", iv=0,
            bid_ask_spread_pct=0.0, margin_required=1000.0,
        )
        # Raises OrderValidationError on a bad symbol → fails the test loudly.
        validate_order(prop)


def test_expiry_flatten_survives_a_missing_expiry_day_bar():
    """The EXPIRY force-flatten must not need an exact expiry==bar-date hit.

    backtest_one replays `replay_panel[[a, b]].dropna()` over a panel built
    with min_coverage=0.50, so either leg missing on the expiry date removes
    that bar entirely. With an equality test the flatten never fires, and by
    the next bar the front month has already rolled — the position then
    carries across the roll on a continuous price series, free of charge,
    which is the exact free-carry the expiry calendar was added to remove.
    """
    from datetime import date, datetime
    from research.backtest_pairs import _legs_expired_by
    from strategies.pair_trading import PairLeg, PairState, PairTradingStrategy

    s = PairTradingStrategy.__new__(PairTradingStrategy)
    s.state = PairState()
    s.state.legs = [
        PairLeg(symbol="AAA", tradingsymbol="AAA-BTFUT", lot_size=10,
                quantity=1, entry_price=100.0, current_price=100.0,
                expiry="2025-01-30"),
        PairLeg(symbol="BBB", tradingsymbol="BBB-BTFUT", lot_size=20,
                quantity=-1, entry_price=50.0, current_price=50.0,
                expiry="2025-02-27"),
    ]
    assert _legs_expired_by(s, date(2025, 1, 29)) is False
    assert _legs_expired_by(s, date(2025, 1, 30)) is True, "expiry day itself"
    # The bar for 01-30 was dropped (a leg was NaN); the next bar is 01-31.
    assert _legs_expired_by(s, date(2025, 1, 31)) is True, (
        "a skipped expiry-day bar must not let the position carry across the roll"
    )
    assert datetime  # imported for symmetry with the module's other fixtures


def test_legs_without_an_expiry_field_do_not_trigger_a_flatten():
    """Legs restored from a pre-expiry-field state file carry expiry="" —
    they must be skipped, not crash or force a spurious flatten."""
    from datetime import date
    from research.backtest_pairs import _legs_expired_by
    from strategies.pair_trading import PairLeg, PairState, PairTradingStrategy

    s = PairTradingStrategy.__new__(PairTradingStrategy)
    s.state = PairState()
    s.state.legs = [
        PairLeg(symbol="AAA", tradingsymbol="AAA-BTFUT", lot_size=10,
                quantity=1, entry_price=100.0, current_price=100.0, expiry=""),
    ]
    assert _legs_expired_by(s, date(2099, 1, 1)) is False
