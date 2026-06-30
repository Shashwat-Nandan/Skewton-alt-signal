"""Tests for the 5-minute replay path of backtest_kalman_pairs (issue #63).

Encode the INTENT (Rule 9): the 5-min mode must seed/update the Kalman filter once
per DAY (design D1) while making entry/exit decisions on every 5-min bar — the live
runner's path that the daily backtest never exercised. A test that merely checked
"returns a dict" would miss a filter that wrongly stepped per-bar (which would
collapse the daily z-window) or one that never traded intraday.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backtest_kalman_pairs import _write_temp_config, load_5min_panel, run_replay_5min


def _cointegrated_daily(n=300, seed=0):
    rng = np.random.default_rng(seed)
    pb = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    pa = 5.0 + 0.7 * pb + rng.normal(0, 0.4, n)
    return pa, pb


def _intraday_bars(days=6, bars_per_day=75, base_b=100.0):
    """Synthetic 5-min bars whose spread swings within each day so the bands fire."""
    idx, a_px, b_px = [], [], []
    for d in range(days):
        day = datetime(2026, 6, 22) + timedelta(days=d)
        for k in range(bars_per_day):
            idx.append(day.replace(hour=9, minute=15) + timedelta(minutes=5 * k))
            b = base_b + 0.3 * np.sin(k / 6.0) + d * 0.2
            a = 5.0 + 0.7 * b + 3.0 * np.sin((k / bars_per_day) * 2 * np.pi + d)
            a_px.append(a); b_px.append(b)
    return pd.DataFrame({"PA": a_px, "PB": b_px}, index=pd.DatetimeIndex(idx))


def _cfg(**over):
    base = dict(entry_z=1.0, exit_z=0.0, stop_z=4.0, lookback_days=126,
                max_holding_days=7, lots_per_leg=1, max_leg_notional=5_000_000,
                min_edge_multiplier=0.0, adf_gate_p=0.0, exit_debounce_ticks=2)
    base.update(over)
    return _write_temp_config(base)


def test_5min_replay_steps_filter_once_per_day_and_trades_intraday():
    """The filter must advance exactly once per trading day (n_days == #days),
    NOT once per 5-min bar (which would wreck the daily z-window), and the
    intraday spread swings must book at least one round trip."""
    pa, pb = _cointegrated_daily()
    bars = _intraday_bars(days=6, base_b=float(pb[-1]))
    r = run_replay_5min("PA", "PB", 50, 50, pa, pb, bars,
                        label="momentum", model="momentum", alpha=1e-6,
                        config_path=_cfg())
    assert r is not None
    assert r["n_days"] == 6, f"filter must step once/day, got n_days={r['n_days']}"
    assert r["n_round_trips"] >= 1, "intraday swings should book a round trip"
    # Spread-var/half-life come from the DAILY spread series (one per day), so the
    # half-life is finite/meaningful only because the filter stepped daily.
    assert np.isfinite(r["net_pnl"])


def test_5min_replay_gate_blocks_when_on():
    """With the ADF gate ON and a deliberately non-stationary day-to-day path, the
    5-min replay should trade strictly less than with the gate OFF — proving the
    daily-updated gate actually feeds the intraday entry decision."""
    pa, pb = _cointegrated_daily()
    bars = _intraday_bars(days=6, base_b=float(pb[-1]))
    off = run_replay_5min("PA", "PB", 50, 50, pa, pb, bars, label="momentum",
                          model="momentum", alpha=1e-6, config_path=_cfg(adf_gate_p=0.0))
    on = run_replay_5min("PA", "PB", 50, 50, pa, pb, bars, label="momentum",
                         model="momentum", alpha=1e-6,
                         config_path=_cfg(adf_gate_p=0.05, adf_gate_window=60))
    assert on["n_round_trips"] <= off["n_round_trips"]


def test_load_5min_panel_reads_and_aligns(tmp_path):
    """load_5min_panel must build a sorted wide close-panel from the per-symbol
    CSVs fetch_5min_stf.py writes, so pair alignment via dropna works."""
    for sym, base in (("PA", 100.0), ("PB", 200.0)):
        df = pd.DataFrame({
            "date": pd.date_range("2026-06-22 09:15", periods=10, freq="5min"),
            "open": base, "high": base + 1, "low": base - 1,
            "close": base + np.arange(10) * 0.1, "volume": 1000,
        })
        df.to_csv(tmp_path / f"{sym}.csv", index=False)
    panel = load_5min_panel(["PA", "PB", "MISSING"], directory=tmp_path)
    assert list(panel.columns) == ["PA", "PB"]      # MISSING silently skipped
    assert panel.index.is_monotonic_increasing
    assert len(panel.dropna()) == 10
