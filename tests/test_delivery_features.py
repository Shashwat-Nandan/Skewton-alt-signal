"""Tests for strategies/_delivery.py — own-history percentile features.

The percentile IS the signal, so these tests pin its math and its
anti-lookahead contract rather than just shape-checking output.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies import _delivery
from strategies._delivery import build_delivery_features, load_delivery_panel


def _panel(sym: str, deliv_pers, start="2026-01-01"):
    dates = pd.bdate_range(start, periods=len(deliv_pers))
    return pd.DataFrame({
        "date": dates,
        "symbol": sym,
        "traded_qty": 1_000_000,
        "deliv_qty": [int(1_000_000 * p / 100) for p in deliv_pers],
        "deliv_per": deliv_pers,
    })


class TestPercentile:

    def test_hand_computed_rank(self):
        """10-row series, window=10, min_periods=5: the last value (highest)
        must rank 1.0, a mid value mid-rank. If someone swaps rolling.rank
        for a full-series rank, these exact values change — that's the
        lookahead regression this guards."""
        vals = [30, 32, 31, 33, 34, 35, 36, 38, 37, 60]
        feats = build_delivery_features(
            _panel("AAA", vals), pctile_window=10, pctile_min_periods=5,
        )
        assert feats["deliv_pctile"].iloc[-1] == pytest.approx(1.0)
        # row 4 (value 34) ranks 5th of the first 5 values → 5/5 = 1.0
        assert feats["deliv_pctile"].iloc[4] == pytest.approx(1.0)
        # row 2 (value 31): below min_periods → NaN
        assert np.isnan(feats["deliv_pctile"].iloc[2])

    def test_min_periods_warmup_is_nan(self):
        """A fabricated rank on thin history is the pinned-50 bug in new
        clothes; below min_periods the feature must be NaN, not neutral."""
        feats = build_delivery_features(
            _panel("AAA", [40] * 10), pctile_window=252, pctile_min_periods=126,
        )
        assert feats["deliv_pctile"].isna().all()

    def test_appending_future_rows_does_not_change_past(self):
        """THE anti-lookahead invariant: features at date D depend only on
        data ≤ D. Appending future rows must leave every previously
        computed value bit-identical."""
        vals = list(np.linspace(30, 70, 40))
        base = build_delivery_features(
            _panel("AAA", vals), pctile_window=20, pctile_min_periods=5,
        )
        extended = build_delivery_features(
            _panel("AAA", vals + [95, 5, 50]), pctile_window=20, pctile_min_periods=5,
        )
        pd.testing.assert_frame_equal(extended.iloc[: len(base)], base)

    def test_per_symbol_isolation(self):
        """Symbol A's distribution must never leak into symbol B's rank —
        cross-symbol pollution would recreate the 'absolute threshold'
        mistake the Varsity article warns about."""
        a = _panel("AAA", [10] * 9 + [20])   # 20 is AAA's all-time high
        b = _panel("BBB", [90] * 9 + [20])   # 20 is BBB's all-time low
        feats = build_delivery_features(
            pd.concat([a, b], ignore_index=True), pctile_window=10, pctile_min_periods=5,
        )
        last = feats.groupby("symbol")["deliv_pctile"].last()
        assert last["AAA"] == pytest.approx(1.0)
        assert last["BBB"] == pytest.approx(0.1)


class TestHitsAndValueRank:

    def test_hits_counts_clustered_extremes(self):
        """One spike day → hits=1; a cluster → hits grows. The strategy
        requires ≥2 so a lone block-deal day can't trigger an entry."""
        vals = [30] * 30 + [80] + [30] * 4 + [80, 80]
        feats = build_delivery_features(
            _panel("AAA", vals), pctile_window=20, pctile_min_periods=5,
            hits_threshold=0.9, hits_lookback=5,
        )
        # after the lone spike (index 30): exactly 1 hit in window
        assert feats["deliv_hits_5d"].iloc[30] == pytest.approx(1.0)
        # the lone spike has aged out of the 5-day window by the final pair
        assert feats["deliv_hits_5d"].iloc[-1] == pytest.approx(2.0)

    def test_value_rank_needs_price_panel(self):
        """No price panel → deliv_val_pctile NaN (degrade, don't fabricate)."""
        feats = build_delivery_features(
            _panel("AAA", [40] * 200), pctile_window=20, pctile_min_periods=5,
        )
        assert feats["deliv_val_pctile"].isna().all()
        assert feats["deliv_pctile"].notna().any()  # %-rank still works

    def test_value_rank_uses_close(self):
        """Flat deliv_per with rising price: %-rank stays flat but value-rank
        must trend up — the two features are genuinely different signals."""
        n = 30
        panel = _panel("AAA", [40] * n)
        price = pd.DataFrame({
            "date": panel["date"], "symbol": "AAA",
            "close": np.linspace(100, 200, n),
        })
        feats = build_delivery_features(
            panel, price, pctile_window=10, pctile_min_periods=5,
        )
        assert feats["deliv_val_pctile"].iloc[-1] == pytest.approx(1.0)
        assert feats["deliv_pctile"].iloc[-1] < 1.0  # ties don't rank 1.0


class TestLoader:

    def test_missing_cache_dir_returns_empty(self, tmp_path):
        out = load_delivery_panel(cache_dir=tmp_path / "nope")
        assert out.empty
        assert list(out.columns) == _delivery.PANEL_COLS

    def test_universe_subset_and_missing_symbols(self, tmp_path, monkeypatch):
        """Symbols without a cache table are silently absent from the panel
        (→ NaN features downstream), not an error: partial coverage is the
        steady state right after a backfill."""
        p = _panel("AAA", [40, 41, 42])
        p[["date", "traded_qty", "deliv_qty", "deliv_per"]].to_parquet(
            tmp_path / "AAA.parquet", index=False)
        out = load_delivery_panel(["AAA", "MISSING"], cache_dir=tmp_path)
        assert set(out["symbol"]) == {"AAA"}
        assert len(out) == 3

    def test_empty_panel_builds_empty_features(self):
        feats = build_delivery_features(pd.DataFrame(columns=_delivery.PANEL_COLS))
        assert feats.empty
        assert list(feats.columns) == _delivery.FEATURE_COLS
