"""EQ-FU-2 — backtest applies the gap-skip + max-age filters that live's
_fill_pending_entries enforces.

These tests drive ``EquityBacktester._fill_queued`` directly (the
production fill loop, extracted from ``run()``). Originally the tests
re-implemented the loop in their own helper — a Rule 9 violation:
they tested my mental model, not the actual code. Caught during
self-review of the EQ-FU batch.

Live's filter constants live in strategies.varsity_equity_swing:
  - PENDING_GAP_ATR_THRESHOLD (1.5×ATR)
  - PENDING_MAX_AGE_DAYS (5 calendar days)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

import pandas as pd
import pytest

from research.backtest_varsity_equity import EquityBacktester
from strategies.varsity_equity_swing import (
    PENDING_GAP_ATR_THRESHOLD,
    PENDING_MAX_AGE_DAYS,
)


@dataclass
class _FakeProposal:
    tradingsymbol: str
    quantity: int
    price: float
    transaction_type: str = "BUY"
    rationale: str = "test"
    greeks_snapshot: Dict[str, float] = field(default_factory=dict)


def _build_panel(symbol: str, rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """Build a daily-bar panel with the columns load_equity_panel returns."""
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["symbol"] = symbol
    return df


def _make_backtester(panel: pd.DataFrame) -> EquityBacktester:
    bt = EquityBacktester(panel=panel, slippage_bps=0.0)
    bt.strategy.set_panel(panel, sorted(panel["symbol"].unique().tolist()))
    bt.strategy._ensure_features()
    return bt


def test_constants_match_live_runner():
    """Rule 7: the two filter constants must be defined in exactly one
    place and read by both run_equity_swing and backtest. The strategy
    module is the single source of truth."""
    from runners.run_equity_swing import (
        _PENDING_GAP_ATR_THRESHOLD,
        _PENDING_MAX_AGE_DAYS,
    )
    assert _PENDING_GAP_ATR_THRESHOLD == PENDING_GAP_ATR_THRESHOLD
    assert _PENDING_MAX_AGE_DAYS == PENDING_MAX_AGE_DAYS


def test_gap_within_threshold_fills():
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-05", "open": 1015.0, "high": 1030.0,
         "low":  1010.0, "close": 1020.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    # gap = 15 / 20 = 0.75×ATR (under 1.5×) → fills
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-05")
    bt._fill_queued(fill_dt, [(prop, signal_dt)], cash=1_000_000.0)
    assert "INFY" in bt.strategy.positions
    assert bt.n_skipped_gap == 0
    assert bt.n_skipped_stale == 0


def test_gap_above_threshold_skipped():
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-05", "open": 1040.0, "high": 1050.0,
         "low":  1035.0, "close": 1045.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    # gap = 40 / 20 = 2.0×ATR (above 1.5×) → SKIPPED_GAP
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-05")
    bt._fill_queued(fill_dt, [(prop, signal_dt)], cash=1_000_000.0)
    assert "INFY" not in bt.strategy.positions
    assert bt.n_skipped_gap == 1


def test_gap_at_exact_threshold_fills():
    """Boundary case: gap == 1.5×ATR must fill (strict `>` comparison).
    Off-by-one regression guard."""
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-05", "open": 1030.0, "high": 1040.0,
         "low":  1020.0, "close": 1035.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    # gap = 30 / 20 = 1.5×ATR (exactly at threshold) → fills
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-05")
    bt._fill_queued(fill_dt, [(prop, signal_dt)], cash=1_000_000.0)
    assert "INFY" in bt.strategy.positions
    assert bt.n_skipped_gap == 0


def test_signal_aged_past_max_skipped():
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-12", "open": 1005.0, "high": 1020.0,
         "low":  1000.0, "close": 1015.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    # age = 8 days > 5 (PENDING_MAX_AGE_DAYS) → SKIPPED_STALE
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-12")
    bt._fill_queued(fill_dt, [(prop, signal_dt)], cash=1_000_000.0)
    assert "INFY" not in bt.strategy.positions
    assert bt.n_skipped_stale == 1


def test_panel_missing_open_counted_as_stale():
    """live treats missing/non-positive bars as SKIPPED_STALE; backtest
    must match (counter parity + Rule 7)."""
    # Panel has a row for 2026-05-04 but NOT for 2026-05-05
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-05")  # no panel row
    bt._fill_queued(fill_dt, [(prop, signal_dt)], cash=1_000_000.0)
    assert "INFY" not in bt.strategy.positions
    assert bt.n_skipped_stale == 1


def test_fill_decrements_cash_and_books_position():
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-05", "open": 1010.0, "high": 1020.0,
         "low":  1005.0, "close": 1015.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-05")
    new_cash = bt._fill_queued(fill_dt, [(prop, signal_dt)],
                                cash=1_000_000.0)
    # 10 shares × 1010 = 10_100 notional + the statutory delivery entry cost
    # (slippage_bps=0 in the fixture, but STT/stamp/exchange are never zero).
    from core.costs import estimate_equity_cost
    entry_cost = estimate_equity_cost(1010.0, 10, "BUY", "delivery", slippage_bps=0.0)
    assert new_cash == pytest.approx(1_000_000.0 - 10_100.0 - entry_cost)
    pos = bt.strategy.positions["INFY"]
    assert pos.entry_px == 1010.0
    assert pos.qty == 10


def test_summary_surfaces_skip_counters():
    """summary() must include n_skipped_gap and n_skipped_stale in both
    branches (zero-trade and with-trades). Autoresearch sweeps read
    these to detect when backtest trade count overshoots live."""
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-05", "open": 1005.0, "high": 1020.0,
         "low":  1000.0, "close": 1015.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    bt.n_skipped_gap = 3
    bt.n_skipped_stale = 1
    bt.equity_curve = [
        (pd.Timestamp("2026-05-04"), 1_000_000.0),
        (pd.Timestamp("2026-05-05"), 1_000_000.0),
    ]
    s = bt.summary()
    # Zero-trade branch (trade_log is empty) — counters still surface
    assert s["total_trades"] == 0
    assert s["n_skipped_gap"] == 3
    assert s["n_skipped_stale"] == 1
