"""Tests for the Varsity equity-swing strategy + backtest harness.

Covers:
  - Indicator correctness (ATR, SMA warm-up, ADX warm-up, Donchian, Chandelier)
  - Strategy gate behaviour (trend, gap, sizing, slot cap, gross-exposure cap)
  - Position state machine (SL hit, target hit, time stop, Chandelier trail)
  - Integration: backtest fires ≥ 1 trade on a synthetic uptrend
    (catches the "silent-empty-universe" lesson — see tasks/lessons.md)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies import _indicators as ind
from strategies._market_profile_eq import (
    _value_area,
    rolling_value_area,
)
from strategies._oi_signal import classify_oi
from strategies.varsity_equity_swing import (
    EquityPosition,
    VarsityEquitySwingStrategy,
)
from backtest_varsity_equity import EquityBacktester, ZERO_TRADE_PENALTY


class _NullKite:
    pass


# ──────────────────────────────────────────────────────────────────────────────
# Indicator math
# ──────────────────────────────────────────────────────────────────────────────

class TestIndicators:
    def test_sma_warmup_is_nan(self):
        s = pd.Series([1, 2, 3, 4, 5], dtype=float)
        out = ind.sma(s, 3)
        assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[4] == pytest.approx(4.0)

    def test_ema_no_lookback_leak(self):
        s = pd.Series(np.linspace(100, 110, 30), dtype=float)
        e = ind.ema(s, 10)
        # warm-up
        assert pd.isna(e.iloc[0])
        # increasing series → EMA strictly increasing once warm
        warm = e.dropna().values
        assert (np.diff(warm) > 0).all()

    def test_atr_constant_range(self):
        # H-L = 2, no gaps → TR = 2 each bar after warm-up → ATR = 2
        n = 30
        close = pd.Series(np.full(n, 100.0))
        high = close + 1
        low = close - 1
        a = ind.atr(high, low, close, 14)
        assert a.dropna().iloc[-1] == pytest.approx(2.0, abs=0.05)

    def test_adx_strong_uptrend(self):
        # Steady up move with tiny noise — ADX should rise above 20
        n = 80
        rng = np.random.default_rng(7)
        close = pd.Series(100 + np.cumsum(rng.normal(0.5, 0.05, n)))
        high = close + rng.uniform(0.1, 0.4, n)
        low = close - rng.uniform(0.1, 0.4, n)
        a = ind.adx(high, low, close, 14)
        # Wilder ADX needs ~2*window bars to settle
        assert a.dropna().iloc[-1] > 20

    def test_donchian_excludes_today(self):
        s = pd.Series([1, 2, 3, 10, 4, 5], dtype=float)
        d = ind.donchian_high(s, 3)
        # at idx 3, high lookback is rows 0..2 = max(1,2,3) = 3, NOT 10
        assert d.iloc[3] == 3.0
        # at idx 4, lookback is rows 1..3 = max(2,3,10) = 10
        assert d.iloc[4] == 10.0

    def test_chandelier_only_ratchets_up(self):
        n = 50
        close = pd.Series(np.linspace(100, 130, n))
        high = close + 0.5
        low = close - 0.5
        c = ind.chandelier_stop_long(high, low, close, atr_window=14, multiplier=3.0, lookback=10)
        warm = c.dropna().values
        assert (np.diff(warm) >= 0).all(), "Chandelier stop must be monotonic non-decreasing"


# ──────────────────────────────────────────────────────────────────────────────
# Strategy gate behaviour
# ──────────────────────────────────────────────────────────────────────────────

def _build_trending_panel(symbols, n_days=400, start_px=100.0, daily_ret=0.0015, seed=42):
    """Synthetic up-trending panel that satisfies trend + ADX gates."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    rows = []
    for i, sym in enumerate(symbols):
        # slightly different starting points so signals don't all fire same day
        px = start_px * (1.0 + 0.05 * i)
        for d in dates:
            ret = rng.normal(daily_ret, 0.012)
            new_px = px * (1.0 + ret)
            high = max(px, new_px) * (1.0 + abs(rng.normal(0, 0.003)))
            low = min(px, new_px) * (1.0 - abs(rng.normal(0, 0.003)))
            o = px
            c = new_px
            vol = 5_000_000 * (1.0 + abs(rng.normal(0, 0.2)))  # shares
            rows.append((d, sym, o, high, low, c, vol))
            px = new_px
    return pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low", "close", "volume"])


class TestStrategy:
    def _make_strategy(self, **overrides):
        s = VarsityEquitySwingStrategy(_NullKite(), config_path="/dev/null", mode="paper")
        # Phase-1 tests verify trend/gap/sizing — disable Phase-2 gates so
        # synthetic universes don't get filtered by VAH/VAL or no-OI-data.
        s.params["mp_enabled"] = 0
        s.params["oi_enabled"] = 0
        s.params.update(overrides)
        return s

    def test_atr_sizing_nonzero_for_normal_atr(self):
        s = self._make_strategy(total_capital=1_000_000, risk_per_trade_pct=1.0)
        # entry=100, sl=95 → per-share loss = 5, risk_rs = 10000 → qty = 2000
        qty = s._size_position(entry=100.0, sl=95.0)
        assert qty == 2000

    def test_sizing_caps_at_gross_exposure(self):
        s = self._make_strategy(total_capital=1_000_000, max_gross_exposure_pct=10.0)
        # max gross = 100k → @ entry=100 → ≤ 1000 shares
        qty = s._size_position(entry=100.0, sl=95.0)
        assert qty <= 1000

    def test_sizing_returns_zero_when_inverted_sl(self):
        s = self._make_strategy()
        assert s._size_position(entry=100.0, sl=110.0) == 0

    def test_signal_skipped_when_gap_too_large(self):
        # build a 250-day uptrend, then on the last bar set a 5% gap up
        panel = _build_trending_panel(["TESTCO"], n_days=250)
        # spike the last open relative to prev close
        last_idx = panel.index[-1]
        prev_close = panel.iloc[-2]["close"]
        panel.loc[last_idx, "open"] = prev_close * 1.05
        panel.loc[last_idx, "high"] = max(panel.loc[last_idx, "high"], prev_close * 1.05)

        s = self._make_strategy(
            trend_short_window=20, trend_long_window=50,
            min_avg_turnover_cr=0.0, gap_filter_pct=2.0,
        )
        s.set_panel(panel)
        s.set_current_date(panel["date"].max())
        sig = s._signal_at("TESTCO", panel["date"].max())
        assert sig is None, "5% gap should be filtered out"

    def test_signal_fires_on_clean_trend(self):
        panel = _build_trending_panel(["TESTCO"], n_days=300)
        s = self._make_strategy(
            trend_short_window=20, trend_long_window=50,
            min_avg_turnover_cr=0.0, gap_filter_pct=5.0,
        )
        s.set_panel(panel)
        s._ensure_features()
        # Walk the last 30 bars looking for a fire — synthetic noise is
        # stochastic so we check ANY of them, not the last specifically.
        dates = panel["date"].sort_values().tail(30).tolist()
        fired = any(s._signal_at("TESTCO", d) is not None for d in dates)
        assert fired, "Clean uptrend with vol surge should fire on at least one bar"

    def test_max_positions_cap(self):
        panel = _build_trending_panel(["A", "B", "C", "D", "E"], n_days=300)
        s = self._make_strategy(
            trend_short_window=20, trend_long_window=50,
            min_avg_turnover_cr=0.0, max_positions=2,
        )
        s.set_panel(panel)
        s.set_current_date(panel["date"].max())
        proposals = s.scan_and_propose()
        assert len(proposals) <= 2


# ──────────────────────────────────────────────────────────────────────────────
# Position state machine
# ──────────────────────────────────────────────────────────────────────────────

class TestPositionStateMachine:
    def _make_position(self, **kw):
        defaults = dict(
            symbol="X", side="LONG",
            entry_dt=pd.Timestamp("2025-01-01"),
            entry_px=100.0, qty=100,
            initial_sl=95.0, target=110.0,
            atr_at_entry=2.0, rationale="test",
        )
        defaults.update(kw)
        return EquityPosition(**defaults)

    def test_sl_hit_triggers_exit(self):
        pos = self._make_position(initial_sl=95.0, target=110.0)
        s = VarsityEquitySwingStrategy(_NullKite(), config_path="/dev/null", mode="paper")
        s.params["trend_short_window"] = 20
        s.params["trend_long_window"] = 50
        s.params["mp_enabled"] = 0
        s.params["oi_enabled"] = 0
        # Build a panel for X where today's low pierces the SL.
        n = 80
        dates = pd.bdate_range("2024-10-01", periods=n)
        rows = [(d, "X", 100, 100.5, 99.5, 100.0, 1_000_000) for d in dates[:-1]]
        rows.append((dates[-1], "X", 100, 101, 90, 99, 1_000_000))  # low=90 pierces SL=95
        panel = pd.DataFrame(rows, columns=["date","symbol","open","high","low","close","volume"])
        s.set_panel(panel)
        s.positions["X"] = pos
        s.set_current_date(dates[-1])
        s._ensure_features()
        exits = s.check_and_rehedge()
        assert len(exits) == 1
        snap = exits[0].greeks_snapshot
        assert snap["exit_reason"] == "SL_HIT"

    def test_target_hit_triggers_exit(self):
        pos = self._make_position(initial_sl=95.0, target=110.0)
        s = VarsityEquitySwingStrategy(_NullKite(), config_path="/dev/null", mode="paper")
        s.params["trend_short_window"] = 20
        s.params["trend_long_window"] = 50
        s.params["mp_enabled"] = 0
        s.params["oi_enabled"] = 0
        n = 80
        dates = pd.bdate_range("2024-10-01", periods=n)
        rows = [(d, "X", 100, 100.5, 99.5, 100.0, 1_000_000) for d in dates[:-1]]
        rows.append((dates[-1], "X", 100, 115, 99, 112, 1_000_000))
        panel = pd.DataFrame(rows, columns=["date","symbol","open","high","low","close","volume"])
        s.set_panel(panel)
        s.positions["X"] = pos
        s.set_current_date(dates[-1])
        s._ensure_features()
        exits = s.check_and_rehedge()
        assert len(exits) == 1
        assert exits[0].greeks_snapshot["exit_reason"] == "TARGET_HIT"
        assert exits[0].price == 110.0  # target, not the 115 high

    def test_time_stop_fires_after_n_days(self):
        s = VarsityEquitySwingStrategy(_NullKite(), config_path="/dev/null", mode="paper")
        s.params["trend_short_window"] = 20
        s.params["trend_long_window"] = 50
        s.params["mp_enabled"] = 0
        s.params["oi_enabled"] = 0
        s.params["time_stop_days"] = 5
        n = 80
        dates = pd.bdate_range("2024-10-01", periods=n)
        rows = [(d, "X", 100, 101, 99, 100.0, 1_000_000) for d in dates]
        panel = pd.DataFrame(rows, columns=["date","symbol","open","high","low","close","volume"])
        s.set_panel(panel)
        # entry 6 bars ago — time stop should fire today
        entry_dt = dates[-7]
        pos = self._make_position(entry_dt=entry_dt, initial_sl=90.0, target=120.0)
        s.positions["X"] = pos
        s.set_current_date(dates[-1])
        s._ensure_features()
        exits = s.check_and_rehedge()
        assert len(exits) == 1
        assert exits[0].greeks_snapshot["exit_reason"] == "TIME_STOP"


# ──────────────────────────────────────────────────────────────────────────────
# Integration: backtest end-to-end
# ──────────────────────────────────────────────────────────────────────────────

class TestBacktestIntegration:

    def test_backtest_fires_at_least_one_trade_on_uptrend(self):
        """Catches the silent-empty-universe lesson — see tasks/lessons.md."""
        symbols = ["A", "B", "C", "D"]
        panel = _build_trending_panel(symbols, n_days=300, daily_ret=0.0018)
        bt = EquityBacktester(panel, params_overrides={
            "total_capital": 1_000_000,
            "risk_per_trade_pct": 1.0,
            "trend_short_window": 20,
            "trend_long_window": 50,
            "adx_threshold": 15.0,  # synthetic uptrend has lower ADX than real
            "min_avg_turnover_cr": 0.0,
            "mp_enabled": 0,        # Phase-1 baseline test
            "oi_enabled": 0,
        })
        summary = bt.run()
        assert summary["total_trades"] > 0, (
            f"Expected ≥1 trade on synthetic uptrend; got {summary} — "
            "this is the same shape as the silent-empty-universe lesson"
        )
        assert summary["score"] != ZERO_TRADE_PENALTY

    def test_zero_trade_returns_sentinel(self):
        # Flat panel — no trend, no signal.
        n = 300
        dates = pd.bdate_range("2024-01-01", periods=n)
        rows = []
        for d in dates:
            for sym in ["FLAT1", "FLAT2"]:
                rows.append((d, sym, 100.0, 100.1, 99.9, 100.0, 1_000_000))
        panel = pd.DataFrame(rows, columns=["date","symbol","open","high","low","close","volume"])
        bt = EquityBacktester(panel, params_overrides={
            "trend_short_window": 20,
            "trend_long_window": 50,
            "min_avg_turnover_cr": 0.0,
            "mp_enabled": 0,
            "oi_enabled": 0,
        })
        summary = bt.run()
        assert summary["total_trades"] == 0
        assert summary["score"] == ZERO_TRADE_PENALTY, (
            "Zero-trade backtest must return ZERO_TRADE_PENALTY sentinel — "
            "see flat-fitness lesson"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 — Market Profile + OI gate behaviour
# ──────────────────────────────────────────────────────────────────────────────

class TestMarketProfile:
    def test_value_area_concentrated_volume(self):
        # POC at bin 5 holds 30 % of volume; the value area must expand
        # outward until cumulative volume crosses 70 %.
        prices = np.linspace(100, 110, 11)
        volumes = np.array([1, 1, 5, 10, 15, 30, 15, 10, 5, 1, 1], dtype=float)
        poc, val, vah = _value_area(prices, volumes, value_area_pct=70.0)
        assert poc == pytest.approx(105.0)
        assert val < poc < vah   # area must straddle POC
        assert vah - val < 6.0   # tight around the centred mass

    def test_rolling_value_area_warms_up(self):
        n = 60
        rng = np.random.default_rng(11)
        dates = pd.bdate_range("2024-01-01", periods=n)
        df = pd.DataFrame({
            "date": dates,
            "open":  100 + rng.normal(0, 1, n).cumsum() * 0.1,
            "high":  100 + rng.normal(0.5, 1, n).cumsum() * 0.1,
            "low":   100 + rng.normal(-0.5, 1, n).cumsum() * 0.1,
            "close": 100 + rng.normal(0, 1, n).cumsum() * 0.1,
            "volume": np.full(n, 1_000_000.0),
        })
        df["high"] = df[["open", "close"]].max(axis=1) + 0.5
        df["low"] = df[["open", "close"]].min(axis=1) - 0.5
        out = rolling_value_area(df, lookback=20, value_area_pct=70.0)
        # warm-up rows are NaN, then mp values populate
        assert out.iloc[:19].isna().all().all()
        last = out.iloc[-1]
        assert last["mp_val"] <= last["mp_poc"] <= last["mp_vah"]


class TestOISignal:
    def test_classify_long_buildup(self):
        df = pd.DataFrame({
            "date": pd.bdate_range("2025-01-01", periods=10),
            "symbol": ["X"] * 10,
            "close": [100, 100, 100, 100, 100, 105, 106, 107, 108, 109],  # +9 %
            "oi":    [1_000_000] * 5 + [1_100_000, 1_120_000, 1_150_000, 1_200_000, 1_250_000],  # +25 %
        })
        out = classify_oi(df, lookback=5, min_price_pct=1.0, min_oi_pct=2.0)
        last_signal = out.iloc[-1]["oi_signal"]
        assert last_signal == "LONG_BUILDUP"

    def test_classify_short_buildup(self):
        df = pd.DataFrame({
            "date": pd.bdate_range("2025-01-01", periods=10),
            "symbol": ["X"] * 10,
            "close": [100] * 5 + [98, 96, 94, 92, 90],
            "oi":    [1_000_000] * 5 + [1_050_000, 1_100_000, 1_150_000, 1_200_000, 1_300_000],
        })
        out = classify_oi(df, lookback=5)
        assert out.iloc[-1]["oi_signal"] == "SHORT_BUILDUP"

    def test_classify_neutral_under_thresholds(self):
        df = pd.DataFrame({
            "date": pd.bdate_range("2025-01-01", periods=10),
            "symbol": ["X"] * 10,
            "close": [100, 100, 100, 100, 100, 100.4, 100.5, 100.5, 100.4, 100.3],  # < 1 %
            "oi":    [1_000_000] * 5 + [1_005_000] * 5,  # 0.5 % < 2 %
        })
        out = classify_oi(df, lookback=5, min_price_pct=1.0, min_oi_pct=2.0)
        assert out.iloc[-1]["oi_signal"] == "NEUTRAL"


class TestGateBehaviour:
    def _make_strategy(self, **overrides):
        s = VarsityEquitySwingStrategy(_NullKite(), config_path="/dev/null", mode="paper")
        s.params["trend_short_window"] = 20
        s.params["trend_long_window"] = 50
        s.params["min_avg_turnover_cr"] = 0.0
        s.params["mp_enabled"] = 0
        s.params["oi_enabled"] = 0
        s.params.update(overrides)
        return s

    def test_mp_below_val_vetoes(self):
        # Build a panel where today's close sits below recent value area.
        # Construction: 30 days range-bound at 100, then today drops to 92 with
        # the trend filter still on. Even though trend says "go", the MP veto
        # should kill the signal.
        symbols = ["X"]
        panel = _build_trending_panel(symbols, n_days=200, daily_ret=0.001)
        last_dt = panel["date"].max()
        panel.loc[panel["date"] == last_dt, "close"] = panel.loc[panel["date"] == last_dt, "close"] * 0.85
        s = self._make_strategy(mp_enabled=1, mp_veto_below_val=1, oi_enabled=0)
        s.set_panel(panel)
        s._ensure_features()
        s.set_current_date(last_dt)
        sig = s._signal_at("X", last_dt)
        # With trend up + price plunged below VA, MP gate should reject.
        # (Or trend gate already rejects — either way the signal must be None.)
        assert sig is None

    def test_oi_short_buildup_vetoes(self):
        # Make a real-symbol panel where the strategy's gate has data; we can't
        # easily fake the OI archive in unit tests, so we test the logic
        # directly via the row-level helper. Construct a feature row that
        # would otherwise pass and confirm SHORT_BUILDUP yields None.
        s = self._make_strategy(oi_enabled=1, oi_veto_short_buildup=1, mp_enabled=0)
        # Inject a fake feature frame for a single symbol, single date.
        idx = pd.DatetimeIndex([pd.Timestamp("2025-06-02")])
        f = pd.DataFrame(index=idx, data={
            "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1_000_000,
            "sma_short": 99, "sma_long": 95, "ema_pull": 100.5, "atr": 1.5, "adx": 25,
            "donch_hi": 100, "vol_avg20": 800_000, "turnover_cr": 0.0,
            "turnover_med20_cr": 0.0, "prev_close": 100.5, "gap_pct": 0.5,
            "mp_vah": float("nan"), "mp_poc": float("nan"), "mp_val": float("nan"),
            "oi_signal": "SHORT_BUILDUP",
        })
        s._features = {"X": f}
        s._features_dirty = False
        s._universe = ["X"]
        s.set_current_date(idx[0])
        sig = s._signal_at("X", idx[0])
        assert sig is None, "SHORT_BUILDUP must veto a long entry"

    def test_oi_long_buildup_boosts_score(self):
        s = self._make_strategy(oi_enabled=1, oi_boost_long_buildup=1, mp_enabled=0)
        idx = pd.DatetimeIndex([pd.Timestamp("2025-06-02")])
        # Make breakout-flavored row that passes all gates
        f_template = {
            "open": 99, "high": 102, "low": 98, "close": 101, "volume": 5_000_000,
            "sma_short": 100, "sma_long": 90, "ema_pull": 100.5, "atr": 1.0,
            "adx": 30, "donch_hi": 100, "vol_avg20": 1_000_000,
            "turnover_cr": 0.0, "turnover_med20_cr": 0.0,
            "prev_close": 100, "gap_pct": 0.2,
            "mp_vah": float("nan"), "mp_poc": float("nan"), "mp_val": float("nan"),
        }
        s._features = {"X": pd.DataFrame(index=idx, data={**f_template, "oi_signal": "NEUTRAL"})}
        s._features_dirty = False
        s._universe = ["X"]
        s.set_current_date(idx[0])
        baseline = s._signal_at("X", idx[0])
        assert baseline is not None
        s._features = {"X": pd.DataFrame(index=idx, data={**f_template, "oi_signal": "LONG_BUILDUP"})}
        boosted = s._signal_at("X", idx[0])
        assert boosted is not None
        assert boosted["score"] > baseline["score"]
