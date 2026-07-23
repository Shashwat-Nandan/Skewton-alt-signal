"""Tests for the delivery-accumulation strategy + the swing delivery overlay.

Standalone: each entry gate must individually block (Rule 9 — a test that
only checks "some signal fires" would pass with the gates deleted).
Overlay: deliv_enabled=0 must leave varsity_equity_swing bit-identical to
its pre-overlay behaviour — that file is money-affecting.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies import _delivery
from strategies.delivery_accumulation import DeliveryAccumulationStrategy
from strategies.varsity_equity_swing import VarsityEquitySwingStrategy


class _NullKite:
    pass


N_BARS = 300


def _ohlcv_panel(sym="AAA", n=N_BARS, downtrend=True):
    """A liquid stock drifting DOWN into the bottom of its range (the
    accumulation setup), with enough bars to warm the 252d windows."""
    dates = pd.bdate_range("2025-01-01", periods=n)
    close = np.linspace(150, 100, n) if downtrend else np.linspace(100, 150, n)
    return pd.DataFrame({
        "date": dates, "symbol": sym,
        "open": close, "high": close + 2.0, "low": close - 2.0,
        "close": close, "volume": 10_000_000,
    })


def _deliv_panel(sym="AAA", n=N_BARS, spike_days=None, base=40.0, spike=90.0):
    """Delivery rows aligned to the OHLCV dates; `spike_days` are offsets
    from the END (0 = last bar) that get the extreme value."""
    dates = pd.bdate_range("2025-01-01", periods=n)
    vals = np.full(n, base)
    for off in (spike_days or []):
        vals[n - 1 - off] = spike
    return pd.DataFrame({
        "date": dates, "symbol": sym,
        "traded_qty": 10_000_000,
        "deliv_qty": (10_000_000 * vals / 100).astype(int),
        "deliv_per": vals,
    })


def _strategy(panel, deliv, **param_overrides):
    s = DeliveryAccumulationStrategy(_NullKite(), config_path="/dev/null", mode="paper")
    s.params.update(param_overrides)
    s.set_panel(panel)
    s.set_delivery_panel(deliv)
    s._ensure_features()
    return s


LAST = pd.bdate_range("2025-01-01", periods=N_BARS)[-1]


class TestEntryGates:

    def test_full_setup_fires(self):
        """Extreme clustered delivery + price near lows → signal. The
        positive control every blocking test below mutates one gate of."""
        s = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[1, 2, 3]))
        sig = s._signal_at("AAA", LAST)
        assert sig is not None
        assert sig["sl"] < sig["entry"] < sig["target"]

    def test_price_near_highs_blocks(self):
        """Same delivery extreme but price at the TOP of its range must not
        fire — without the range gate this is momentum chasing, not the
        accumulation-while-down setup the article describes."""
        s = _strategy(_ohlcv_panel(downtrend=False), _deliv_panel(spike_days=[1, 2, 3]))
        assert s._signal_at("AAA", LAST) is None

    def test_single_spike_blocks(self):
        """One extreme day (hits=1 < min 2) = block-deal/BTST noise."""
        s = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[1]))
        assert s._signal_at("AAA", LAST) is None

    def test_no_delivery_history_blocks(self):
        """Missing delivery data can't CREATE a position."""
        s = _strategy(_ohlcv_panel(), _deliv_panel()[0:0])
        assert s._signal_at("AAA", LAST) is None

    def test_unextreme_delivery_blocks(self):
        """Flat 40% delivery every day = pctile never extreme (ties rank
        mid-distribution), no matter how cheap the stock got."""
        s = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[]))
        assert s._signal_at("AAA", LAST) is None

    def test_illiquid_blocks(self):
        s = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[1, 2, 3]),
                      min_avg_turnover_cr=1e9)
        assert s._signal_at("AAA", LAST) is None

    def test_lag_one_day(self):
        """With deliv_lag_days=1, spikes on the last 3 bars are only fully
        visible one bar later: at LAST the lagged hits window sees just 2 of
        them — still fires with min_hits=2, but must NOT fire with
        min_hits=3 (an unlagged merge would see all 3 and fire)."""
        s = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[0, 1, 2]),
                      deliv_min_hits=3)
        assert s._signal_at("AAA", LAST) is None
        s2 = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[0, 1, 2]),
                       deliv_min_hits=3, deliv_lag_days=0)
        assert s2._signal_at("AAA", LAST) is not None


class TestScanAndSizing:

    def test_scan_proposes_and_sizes(self):
        s = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[1, 2, 3]))
        s.set_current_date(LAST)
        proposals = s.scan_and_propose()
        assert len(proposals) == 1
        p = proposals[0]
        # risk_per_trade sizing: qty*stop_distance ≈ 1% of capital
        snap = p.greeks_snapshot
        risk = (snap["entry"] - snap["sl"]) * p.quantity
        assert risk <= s.params["total_capital"] * s.params["risk_per_trade_pct"] / 100.0
        assert p.quantity > 0

    def test_same_scan_proposals_respect_gross_cap(self):
        """Six simultaneous signals from an empty book must TOGETHER stay
        under max_gross_exposure_pct — pre-fix each was sized against the
        full remaining cap, so a selloff day (the target regime) could gear
        the book ~6x over the cap and push backtest cash negative
        (code-review 2026-07-22)."""
        syms = [f"SYM{i}" for i in range(8)]
        panel = pd.concat([_ohlcv_panel(sym=s) for s in syms], ignore_index=True)
        deliv = pd.concat([_deliv_panel(sym=s, spike_days=[1, 2, 3]) for s in syms],
                          ignore_index=True)
        # default 1% risk sizes each entry ~₹80k, so 6 slots (~₹480k) overrun
        # the ₹300k cap only in aggregate — the exact pre-fix blind spot
        s = _strategy(panel, deliv)
        s.set_current_date(LAST)
        proposals = s.scan_and_propose()
        assert len(proposals) > 1  # the test is vacuous with a single fill
        gross = sum(p.price * p.quantity for p in proposals)
        max_gross = s.params["total_capital"] * s.params["max_gross_exposure_pct"] / 100.0
        assert gross <= max_gross

    def test_paper_roundtrip_books_costs(self):
        """Open then force an exit via target=entry (corrupt-level flatten
        path is NOT used here — we exit through _paper_execute) and verify
        pnl is net of BOTH legs' delivery costs (the §4.1 zero-cost class)."""
        s = _strategy(_ohlcv_panel(), _deliv_panel(spike_days=[1, 2, 3]))
        s.set_current_date(LAST)
        proposals = s.scan_and_propose()
        s.execute_proposals(proposals)
        assert "AAA" in s.positions
        exit_p = proposals[0]
        exit_p.transaction_type = "SELL"
        exit_p.greeks_snapshot = {"exit_reason": "MANUAL"}
        s.execute_proposals([exit_p])
        assert not s.positions
        closed = s.closed_positions[0]
        assert closed.costs > 0
        # flat exit price → pnl == -costs exactly
        assert closed.pnl == pytest.approx(-closed.costs)

    def test_live_mode_raises(self):
        with pytest.raises(NotImplementedError):
            DeliveryAccumulationStrategy(_NullKite(), config_path="/dev/null", mode="live")

    def test_same_bar_not_adjudicated_twice(self):
        """The runner's open scan anchors to the SAME bar the previous close
        scan already judged, with the trail stop ratcheted at that bar's
        close — re-adjudicating would fire a spurious TRAIL_STOP at prices
        the backtest never trades (code-review 2026-07-22). A bar recorded
        in last_mtm_dt must be skipped; an unseen bar must still adjudicate."""
        from strategies.varsity_equity_swing import EquityPosition
        s = _strategy(_ohlcv_panel(), _deliv_panel())
        s.set_current_date(LAST)
        dates = pd.bdate_range("2025-01-01", periods=N_BARS)
        low_last = s._features["AAA"].loc[LAST, "low"]

        def _pos():
            p = EquityPosition(
                symbol="AAA", side="LONG", entry_dt=dates[-20],
                entry_px=110.0, qty=10, initial_sl=90.0, target=200.0,
                atr_at_entry=4.0, rationale="test")
            p.current_sl = float(low_last) + 0.5  # ratcheted above the bar's low
            return p

        # Bar already judged (last_mtm_dt == current bar) → no exit
        p1 = _pos()
        p1.last_mtm_dt = LAST
        s.positions = {"AAA": p1}
        assert s.check_and_rehedge() == []
        # Unseen bar (last_mtm_dt None) → the trail stop legitimately fires
        p2 = _pos()
        s.positions = {"AAA": p2}
        exits = s.check_and_rehedge()
        assert len(exits) == 1
        assert exits[0].greeks_snapshot["exit_reason"] == "TRAIL_STOP"

    def test_trading_days_use_union_calendar(self):
        """The time-stop calendar must come from the panel's UNION of dates,
        not whichever symbol's frame iterates first — a truncated
        alphabetically-first symbol used to undercount days_held for every
        position and could kill the time stop (code-review 2026-07-22)."""
        full = _ohlcv_panel(sym="ZZZ")                    # 300 bars
        truncated = _ohlcv_panel(sym="AAA").head(50)      # iterates first
        panel = pd.concat([truncated, full], ignore_index=True)
        s = _strategy(panel, _deliv_panel(sym="ZZZ"))
        dates = pd.bdate_range("2025-01-01", periods=N_BARS)
        # (a, b] spanning bars 100..150 — entirely outside AAA's 50 bars
        assert s._trading_days_between(dates[100], dates[150]) == 50

    def test_backtester_mtm_uses_last_known_close(self):
        """A position whose symbol stops printing bars must be marked at its
        LAST KNOWN close, not entry value — the entry-value fallback erased
        open losses from the equity curve (code-review 2026-07-22)."""
        from research.backtest_delivery_accum import DeliveryBacktester
        bt = DeliveryBacktester(_ohlcv_panel(), deliv_panel=_deliv_panel())
        from strategies.varsity_equity_swing import EquityPosition
        pos = EquityPosition(symbol="AAA", side="LONG",
                             entry_dt=LAST, entry_px=100.0, qty=10,
                             initial_sl=90.0, target=120.0, atr_at_entry=4.0,
                             rationale="test")
        pos.last_mtm_px = 80.0  # marked down 20% before the data gap
        off_calendar = LAST + pd.Timedelta(days=365)
        assert bt._mtm_value(pos, off_calendar) == pytest.approx(80.0 * 10)


class TestSwingOverlayRegression:

    def _swing(self, monkeypatch, deliv_on, deliv_panel=None):
        s = VarsityEquitySwingStrategy(_NullKite(), config_path="/dev/null", mode="paper")
        # isolate from real data_cache overlays
        s.params.update({"mp_enabled": 0, "oi_enabled": 0, "fii_enabled": 0,
                         "deliv_enabled": 1 if deliv_on else 0})
        if deliv_panel is not None:
            monkeypatch.setattr(_delivery, "load_delivery_panel",
                                lambda *a, **k: deliv_panel)
        panel = _ohlcv_panel(downtrend=False)  # swing wants an uptrend
        s.set_panel(panel)
        s._ensure_features()
        return s

    def test_disabled_overlay_adds_nothing(self, monkeypatch):
        """deliv_enabled=0 (the shipped default) must not touch the feature
        frames — the money-affecting regression guard: identical features →
        identical signals → bit-identical behaviour pre/post this change."""
        s = self._swing(monkeypatch, deliv_on=False)
        assert "deliv_pctile" not in s._features["AAA"].columns

    def test_enabled_overlay_boosts_score_only(self, monkeypatch):
        """With the overlay ON and an extreme lagged percentile, the SAME
        setup scores exactly +1 vs overlay-off — and is never vetoed."""
        deliv = _deliv_panel(spike_days=[0, 1, 2, 3, 4])
        s_off = self._swing(monkeypatch, deliv_on=False)
        s_on = self._swing(monkeypatch, deliv_on=True, deliv_panel=deliv)
        sig_off = s_off._signal_at("AAA", LAST)
        sig_on = s_on._signal_at("AAA", LAST)
        # Positive control is an ASSERT, not a skip (code-review 2026-07-22):
        # a skip here would let fixture drift silently disarm the only guard
        # on a money-affecting overlay.
        assert sig_off is not None, ("fixture no longer triggers the swing "
                                     "setup — fix the fixture, this guard must not skip")
        assert sig_on is not None
        assert sig_on["score"] == pytest.approx(sig_off["score"] + 1.0)

    def test_enabled_overlay_empty_cache_neutral(self, monkeypatch):
        """Overlay ON but no delivery data: default-neutral, not a veto —
        scores must equal overlay-off exactly."""
        empty = _deliv_panel()[0:0]
        s_off = self._swing(monkeypatch, deliv_on=False)
        s_on = self._swing(monkeypatch, deliv_on=True, deliv_panel=empty)
        sig_off = s_off._signal_at("AAA", LAST)
        sig_on = s_on._signal_at("AAA", LAST)
        assert sig_off is not None, ("fixture no longer triggers the swing "
                                     "setup — fix the fixture, this guard must not skip")
        assert sig_on is not None
        assert sig_on["score"] == pytest.approx(sig_off["score"])
