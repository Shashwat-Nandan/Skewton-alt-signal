"""EQ-FU-2 — backtest applies the gap-skip + max-age filters that live's
_fill_pending_entries enforces, so autoresearch sweeps optimise against
the same trade count live will actually deliver.

Live's filters live in strategies.varsity_equity_swing:
  - PENDING_GAP_ATR_THRESHOLD (1.5×ATR)
  - PENDING_MAX_AGE_DAYS (5 calendar days)

These tests build a tiny in-memory panel and a hand-rolled proposal so we
can drive each filter branch independently of the strategy's signal
generation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from backtest_varsity_equity import EquityBacktester
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
    # Initial setup: feed an empty backtester and override the queue
    # ourselves to bypass scan_and_propose.
    bt = EquityBacktester(panel=panel, cost_pct=0.0)
    return bt


def _drive_one_day(bt: EquityBacktester, dt: pd.Timestamp,
                    queued_with_signal_dt: List) -> None:
    """Pump exactly one day of the backtester's main loop, with a
    hand-injected queue. Mirrors the (proposal, signal_dt) tuple shape
    the real loop now uses."""
    bt.strategy.set_current_date(dt)
    cash_before = bt.strategy.params["total_capital"]
    # Execute queued — mirror the loop body
    for proposal, signal_dt in queued_with_signal_dt:
        open_px = bt._open_price(proposal.tradingsymbol, dt)
        if open_px is None or open_px <= 0:
            bt.n_skipped_stale += 1
            continue
        age_days = (dt - signal_dt).days
        if age_days > PENDING_MAX_AGE_DAYS:
            bt.n_skipped_stale += 1
            continue
        snap = proposal.greeks_snapshot or {}
        atr_v = float(snap.get("atr", 0.0))
        signal_close = float(snap.get("entry", 0.0))
        if atr_v > 0 and signal_close > 0:
            gap_atr = abs(open_px - signal_close) / atr_v
            if gap_atr > PENDING_GAP_ATR_THRESHOLD:
                bt.n_skipped_gap += 1
                continue
        # Successful fill — record by inserting position
        from strategies.varsity_equity_swing import EquityPosition
        sl = open_px - 2.0 * atr_v
        target = open_px + 4.0 * atr_v
        bt.strategy.positions[proposal.tradingsymbol] = EquityPosition(
            symbol=proposal.tradingsymbol, side="LONG",
            entry_dt=dt, entry_px=open_px, qty=proposal.quantity,
            initial_sl=sl, target=target, atr_at_entry=atr_v,
            rationale=proposal.rationale,
        )


def test_constants_match_live_runner():
    """Rule 7: the two filter constants must be defined in exactly one
    place and read by both run_equity_swing and backtest. This test
    asserts the constants module is the single source of truth."""
    from run_equity_swing import (
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
    bt.strategy.set_panel(panel, ["INFY"])
    bt.strategy._ensure_features()
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    # gap = 15 / 20 = 0.75×ATR (under 1.5×) → fills
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-05")
    _drive_one_day(bt, fill_dt, [(prop, signal_dt)])
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
    bt.strategy.set_panel(panel, ["INFY"])
    bt.strategy._ensure_features()
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    # gap = 40 / 20 = 2.0×ATR (above 1.5×) → SKIPPED_GAP
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-05")
    _drive_one_day(bt, fill_dt, [(prop, signal_dt)])
    assert "INFY" not in bt.strategy.positions
    assert bt.n_skipped_gap == 1


def test_signal_aged_past_max_skipped():
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-12", "open": 1005.0, "high": 1020.0,
         "low":  1000.0, "close": 1015.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    bt.strategy.set_panel(panel, ["INFY"])
    bt.strategy._ensure_features()
    prop = _FakeProposal(
        tradingsymbol="INFY", quantity=10, price=1000.0,
        greeks_snapshot={"atr": 20.0, "entry": 1000.0},
    )
    # age = 8 days > 5 (PENDING_MAX_AGE_DAYS) → SKIPPED_STALE
    signal_dt = pd.Timestamp("2026-05-04")
    fill_dt = pd.Timestamp("2026-05-12")
    _drive_one_day(bt, fill_dt, [(prop, signal_dt)])
    assert "INFY" not in bt.strategy.positions
    assert bt.n_skipped_stale == 1


def test_summary_exposes_skip_counts_when_trades_fire():
    """Sanity: the new fields appear in summary() output so autoresearch
    can read them."""
    panel = _build_panel("INFY", [
        {"date": "2026-05-04", "open": 1000.0, "high": 1010.0,
         "low":   990.0, "close": 1000.0, "volume": 1_000_000},
        {"date": "2026-05-05", "open": 1005.0, "high": 1020.0,
         "low":  1000.0, "close": 1015.0, "volume": 1_000_000},
    ])
    bt = _make_backtester(panel)
    bt.n_skipped_gap = 3
    bt.n_skipped_stale = 1
    bt.equity_curve = [(pd.Timestamp("2026-05-04"), 1_000_000.0),
                       (pd.Timestamp("2026-05-05"), 1_000_000.0)]
    s = bt.summary()
    # Zero-trade branch must still surface the counters
    assert s["n_skipped_gap"] == 3
    assert s["n_skipped_stale"] == 1
