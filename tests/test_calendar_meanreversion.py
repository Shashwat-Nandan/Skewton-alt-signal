"""Tests for CalendarMeanReversionStrategy — Varsity-style mean reversion.

Covers spread/SD math, all four exit gates (CONVERGE / STOP / MAX_HOLD /
EXPIRY), the entry filters (min_history, liquidity, expiry-window,
direction toggles), and that fill handling is inherited from the parent
without regressions.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.arbitrage import ArbitrageState, CalendarLeg, CalendarTrade
from strategies.calendar_meanreversion import CalendarMeanReversionStrategy
from core.trade_proposer import TradeProposal


# ──────────────────────────────────────────────────────────
# Fixture
# ──────────────────────────────────────────────────────────

def _make_strategy(
    *, mode: str = "paper",
    universe=None,
    spread_history=None,
    volume_history=None,
    entry_n_sd: float = 1.0,
    exit_n_sd: float = 0.25,
    stop_loss_n_sd: float = 0.5,
    max_hold_days: int = 3,
    require_dte_near_le: int = 7,
    min_history: int = 60,
    min_avg_volume: int = 100_000,
    lookback_days: int = 200,
    max_open: int = 5,
    lots_per_leg: int = 1,
    max_leg_notional=None,
    allow_long: bool = True,
    allow_short: bool = True,
    clock_date: date = date(2026, 4, 17),
) -> CalendarMeanReversionStrategy:
    s = CalendarMeanReversionStrategy.__new__(CalendarMeanReversionStrategy)
    # Parent attributes (subset that the inherited methods touch).
    s.client = MagicMock()
    s.config = MagicMock()
    s.config_path = "config.ini"
    s.mode = mode
    s.universe = list(universe) if universe is not None else ["AAA", "BBB"]
    s.risk_free_rate = 0.07
    s.dividend_yield = 0.0
    s.dividend_yields = {}
    s.basis_entry_annual = 9.99
    s.basis_min_dte = 99
    s.calendar_entry_annual = 9.99
    s.calendar_exit_annual = 0.0
    s.calendar_max_holding_days = 999
    s.calendar_min_dte_near = 999
    s.calendar_max_leg_basis = 0.0
    s.calendar_stop_loss_mult = 0.0   # parent stop unused; mirror the builder
    s.disable_calendar = True
    s.lots_per_leg = 1
    s.max_open_calendars = 5
    s.max_leg_notional = None
    s.total_capital = 500_000
    s.state = ArbitrageState()
    s._instrument_cache = None
    s._ts_to_name = {}
    s._clock = lambda: datetime.combine(clock_date, datetime.min.time())

    # Mean-rev attributes.
    s.lookback_days = lookback_days
    s.entry_n_sd = entry_n_sd
    s.exit_n_sd = exit_n_sd
    s.stop_loss_n_sd = stop_loss_n_sd
    s.mr_max_hold_days = max_hold_days
    s.require_dte_near_le = require_dte_near_le
    s.min_history = min_history
    s.min_avg_volume = min_avg_volume
    s.mr_max_open = max_open
    s.mr_lots_per_leg = lots_per_leg
    s.mr_max_leg_notional = max_leg_notional
    s.allow_long = allow_long
    s.allow_short = allow_short
    s._spread_history = dict(spread_history or {})
    s._volume_history = dict(volume_history or {})
    s._last_history_date = {}
    s._entry_context = {}
    return s


def _snap(symbol="AAA", near_px=100.0, next_px=101.5, dte_near=5, dte_next=33,
          lot_size=100, basis_annual=0.0, basis_annual_next=0.0):
    return {
        "symbol": symbol, "spot": near_px - 0.1,
        "spot_is_fallback": False,
        "near": {"tradingsymbol": f"{symbol}26APRFUT", "lot_size": lot_size,
                 "expiry": "2026-04-28", "instrument_token": 1},
        "near_price": near_px, "dte_near": dte_near,
        "next": {"tradingsymbol": f"{symbol}26MAYFUT", "lot_size": lot_size,
                 "expiry": "2026-05-26", "instrument_token": 2},
        "next_price": next_px, "dte_next": dte_next,
        "basis_annual": basis_annual, "basis_annual_next": basis_annual_next,
        "carry_implied": None, "carry_diff": None,
    }


def _seed_history(n: int = 80, mean: float = 1.0, sd: float = 0.5,
                  end_date: date = date(2026, 4, 16)) -> list:
    """Build a deterministic spread series with target mean/SD.

    Uses a simple symmetric pattern so unit tests can reason about the
    rolling stats without touching numpy (alternation around the mean).
    """
    out = []
    for i in range(n):
        d = end_date - timedelta(days=(n - 1 - i))
        # Alternate ±sd around mean → exact mean=mean, exact sd=sd (population).
        delta = sd if i % 2 == 0 else -sd
        out.append((d, mean + delta))
    return out


def _seed_volume(n: int = 30, vol_curr: int = 200_000, vol_next: int = 200_000,
                 end_date: date = date(2026, 4, 16)) -> list:
    return [(end_date - timedelta(days=(n - 1 - i)), vol_curr, vol_next) for i in range(n)]


# ──────────────────────────────────────────────────────────
# Spread stats math
# ──────────────────────────────────────────────────────────

class TestRollingStats:
    def test_rolling_stats_basic(self):
        s = _make_strategy(min_history=10)
        s._spread_history = {"AAA": _seed_history(n=80, mean=2.0, sd=0.4)}
        stats = s._rolling_stats("AAA", exclude_date=date(2026, 4, 17))
        assert stats is not None
        assert abs(stats.mean - 2.0) < 1e-9
        assert abs(stats.sd - 0.4) < 1e-9
        assert stats.n == 80   # full history fits inside lookback_days=200

    def test_rolling_stats_excludes_today(self):
        s = _make_strategy(min_history=5)
        # Seed up to 2026-04-16, then add today's bar at a wildly different value
        # — it MUST be excluded from the stats so entry decisions can't see them.
        hist = _seed_history(n=10, mean=1.0, sd=0.2, end_date=date(2026, 4, 16))
        hist.append((date(2026, 4, 17), 9999.0))
        s._spread_history = {"AAA": hist}
        stats = s._rolling_stats("AAA", exclude_date=date(2026, 4, 17))
        assert stats is not None
        assert abs(stats.mean - 1.0) < 1e-9   # the 9999 must be invisible

    def test_below_min_history_returns_none(self):
        s = _make_strategy(min_history=60)
        s._spread_history = {"AAA": _seed_history(n=10, mean=1.0, sd=0.2)}
        assert s._rolling_stats("AAA", exclude_date=date(2026, 4, 17)) is None


# ──────────────────────────────────────────────────────────
# Entry — bands, direction toggles, gates
# ──────────────────────────────────────────────────────────

class TestEntry:
    def _seed(self, s):
        s._spread_history = {"AAA": _seed_history(n=80, mean=1.0, sd=0.4)}
        s._volume_history = {"AAA": _seed_volume(n=30)}

    def test_short_calendar_when_spread_above_upper(self):
        # Mean=1.0, SD=0.4, upper=1.4. spread_now=1.6 → SHORT_CALENDAR.
        s = _make_strategy()
        self._seed(s)
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        near = next(p for p in proposals if "APR" in p.tradingsymbol)
        nxt = next(p for p in proposals if "MAY" in p.tradingsymbol)
        assert near.transaction_type == "BUY"
        assert nxt.transaction_type == "SELL"
        # Entry context recorded for STOP gate later.
        assert "AAA" in s._entry_context
        assert s._entry_context["AAA"]["position"] == "SHORT_CALENDAR"

    def test_long_calendar_when_spread_below_lower(self):
        s = _make_strategy()
        self._seed(s)
        snap = _snap(near_px=100.0, next_px=100.4)   # spread=0.4 < lower=0.6
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2
        near = next(p for p in proposals if "APR" in p.tradingsymbol)
        nxt = next(p for p in proposals if "MAY" in p.tradingsymbol)
        assert near.transaction_type == "SELL"
        assert nxt.transaction_type == "BUY"

    def test_inside_band_no_entry(self):
        s = _make_strategy()
        self._seed(s)
        snap = _snap(near_px=100.0, next_px=101.0)   # spread=1.0 = mean
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_disable_short_blocks_short(self):
        s = _make_strategy(allow_short=False)
        self._seed(s)
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_disable_long_blocks_long(self):
        s = _make_strategy(allow_long=False)
        self._seed(s)
        snap = _snap(near_px=100.0, next_px=100.4)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_expiry_window_gate_blocks_far_dte(self):
        s = _make_strategy(require_dte_near_le=7)
        self._seed(s)
        # dte_near=20 → outside expiry window even though spread is wide.
        snap = _snap(near_px=100.0, next_px=101.6, dte_near=20, dte_next=48)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_expiry_imminent_gate_blocks_dte_le_1(self):
        # Strategy refuses to open when settlement is one bar away.
        s = _make_strategy(require_dte_near_le=7)
        self._seed(s)
        snap = _snap(near_px=100.0, next_px=101.6, dte_near=1, dte_next=29)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_min_history_gate_blocks_short_series(self):
        s = _make_strategy(min_history=60)
        s._spread_history = {"AAA": _seed_history(n=20, mean=1.0, sd=0.4)}
        s._volume_history = {"AAA": _seed_volume(n=30)}
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_liquidity_gate_blocks_thin_contracts(self):
        s = _make_strategy(min_avg_volume=500_000)
        s._spread_history = {"AAA": _seed_history(n=80, mean=1.0, sd=0.4)}
        s._volume_history = {"AAA": _seed_volume(n=30, vol_curr=50_000, vol_next=50_000)}
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_liquidity_gate_passes_when_volume_history_missing(self):
        # Open default — no volume data should default to passing rather than
        # silently zeroing the universe. Documented in _liquidity_ok.
        s = _make_strategy(min_avg_volume=500_000)
        s._spread_history = {"AAA": _seed_history(n=80, mean=1.0, sd=0.4)}
        s._volume_history = {}   # no rows for AAA
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        proposals = s.scan_and_propose()
        assert len(proposals) == 2

    def test_one_per_symbol(self):
        s = _make_strategy()
        self._seed(s)
        s.state.open_calendars["AAA"] = CalendarTrade(
            symbol="AAA", position="SHORT_CALENDAR",
            entry_time=datetime(2026, 4, 17), entry_carry_diff=0.0, legs=[],
        )
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_max_open_caps_new(self):
        s = _make_strategy(max_open=1)
        self._seed(s)
        s.state.open_calendars["XXX"] = CalendarTrade(
            symbol="XXX", position="SHORT_CALENDAR",
            entry_time=datetime(2026, 4, 17), entry_carry_diff=0.0, legs=[],
        )
        snap = _snap(symbol="AAA", near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        assert s.scan_and_propose() == []

    def test_records_history_on_scan(self):
        s = _make_strategy()
        s._spread_history = {"AAA": _seed_history(n=80, mean=1.0, sd=0.4,
                                                  end_date=date(2026, 4, 16))}
        s._volume_history = {"AAA": _seed_volume(n=30, end_date=date(2026, 4, 16))}
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        s.scan_and_propose()
        # Today's spread (1.6) appended.
        last_d, last_s = s._spread_history["AAA"][-1]
        assert last_d == date(2026, 4, 17)
        assert abs(last_s - 1.6) < 1e-9
        # Idempotent: a second scan on the same tick must not double-append.
        n_before = len(s._spread_history["AAA"])
        s.scan_and_propose()
        assert len(s._spread_history["AAA"]) == n_before


# ──────────────────────────────────────────────────────────
# Exit gates
# ──────────────────────────────────────────────────────────

class TestExits:
    def _seed_open_short(self, s, *, entry_spread=1.6, entry_mean=1.0, entry_sd=0.4,
                         entry_time=None):
        # Open SHORT_CALENDAR (entered when spread > upper).
        trade = CalendarTrade(
            symbol="AAA", position="SHORT_CALENDAR",
            entry_time=entry_time or datetime(2026, 4, 16, 10, 0),
            entry_carry_diff=(entry_spread - entry_mean) / entry_sd,
            legs=[
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26APRFUT",
                            expiry="2026-04-28", lot_size=100, quantity=1,
                            entry_price=100.0, current_price=100.0),
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26MAYFUT",
                            expiry="2026-05-26", lot_size=100, quantity=-1,
                            entry_price=101.6, current_price=101.6),
            ],
        )
        s.state.open_calendars["AAA"] = trade
        s._entry_context["AAA"] = {
            "entry_spread": entry_spread,
            "entry_mean": entry_mean,
            "entry_sd": entry_sd,
            "position": "SHORT_CALENDAR",
        }
        return trade

    def test_converge_exit(self):
        s = _make_strategy(exit_n_sd=0.25)
        self._seed_open_short(s)
        # spread now=1.05 → within ±0.1 of mean (=0.25 * 0.4)
        snap = _snap(near_px=100.0, next_px=101.05)
        s._observe_universe = lambda: [snap]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("CONVERGE" in p.rationale for p in exits)
        # Context cleaned up.
        assert "AAA" not in s._entry_context

    def test_stop_exit_short(self):
        s = _make_strategy(stop_loss_n_sd=0.5)
        # Entry at 1.6, sd=0.4, stop=1.6 + 0.5*0.4 = 1.8.
        self._seed_open_short(s, entry_spread=1.6, entry_mean=1.0, entry_sd=0.4)
        # spread_now=1.85 — past the stop on the wrong side.
        snap = _snap(near_px=100.0, next_px=101.85)
        s._observe_universe = lambda: [snap]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("STOP" in p.rationale for p in exits)

    def test_stop_exit_long(self):
        s = _make_strategy(stop_loss_n_sd=0.5)
        # Open LONG (entered below lower).
        trade = CalendarTrade(
            symbol="AAA", position="LONG_CALENDAR",
            entry_time=datetime(2026, 4, 16, 10, 0), entry_carry_diff=-1.5,
            legs=[
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26APRFUT",
                            expiry="2026-04-28", lot_size=100, quantity=-1,
                            entry_price=100.0, current_price=100.0),
                CalendarLeg(symbol="AAA", tradingsymbol="AAA26MAYFUT",
                            expiry="2026-05-26", lot_size=100, quantity=1,
                            entry_price=100.4, current_price=100.4),
            ],
        )
        s.state.open_calendars["AAA"] = trade
        s._entry_context["AAA"] = {
            "entry_spread": 0.4, "entry_mean": 1.0, "entry_sd": 0.4,
            "position": "LONG_CALENDAR",
        }
        # spread_now = 0.15 — past 0.4 - 0.5*0.4 = 0.2 stop.
        snap = _snap(near_px=100.0, next_px=100.15)
        s._observe_universe = lambda: [snap]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("STOP" in p.rationale for p in exits)

    def test_max_hold_exit(self):
        s = _make_strategy(max_hold_days=2,
                           clock_date=date(2026, 4, 19))
        self._seed_open_short(
            s, entry_time=datetime(2026, 4, 16, 10, 0)
        )
        # spread still wide — only the time-based exit can fire.
        snap = _snap(near_px=100.0, next_px=101.6)
        s._observe_universe = lambda: [snap]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("MAX_HOLD" in p.rationale for p in exits)

    def test_expiry_force_exit(self):
        s = _make_strategy()
        self._seed_open_short(s)
        snap = _snap(near_px=100.0, next_px=101.6, dte_near=1, dte_next=29)
        s._observe_universe = lambda: [snap]
        exits = s.check_and_rehedge()
        assert len(exits) == 2
        assert all("EXPIRY" in p.rationale for p in exits)


# ──────────────────────────────────────────────────────────
# Regression — fill handling inherited cleanly
# ──────────────────────────────────────────────────────────

class TestFillHandlingInheritance:
    """The mean-rev strategy must reuse the parent's fill state machine
    (which carries the post-review fixes for prefix-collision routing and
    per-trade realized_pnl). Verify by calling _apply_fill directly and
    asserting the same shape as the parent's TestFillHandling cases."""

    def _prop(self, ts, side, qty=1, price=100.0):
        return TradeProposal(
            tradingsymbol=ts, instrument_token=1, strike=0.0,
            expiry="2026-04-28" if "APR" in ts else "2026-05-26",
            option_type="FUT", lot_size=100, quantity=qty, price=price,
            transaction_type=side, iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=0.0, rationale="test",
        )

    def test_open_two_legs_creates_calendar(self):
        s = _make_strategy()
        s._ts_to_name = {"AAA26APRFUT": "AAA", "AAA26MAYFUT": "AAA"}
        s._apply_fill(self._prop("AAA26APRFUT", "BUY"))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL"))
        assert "AAA" in s.state.open_calendars
        assert s.state.open_calendars["AAA"].position == "SHORT_CALENDAR"

    def test_close_records_per_trade_pnl(self):
        s = _make_strategy()
        s._ts_to_name = {
            "AAA26APRFUT": "AAA", "AAA26MAYFUT": "AAA",
            "BBB26APRFUT": "BBB", "BBB26MAYFUT": "BBB",
        }
        # Round trip 1 — AAA, +200 gross.
        s._apply_fill(self._prop("AAA26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("AAA26APRFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("AAA26MAYFUT", "BUY", price=100.0))
        # Round trip 2 — BBB, +400 gross.
        s._apply_fill(self._prop("BBB26APRFUT", "BUY", price=100.0))
        s._apply_fill(self._prop("BBB26MAYFUT", "SELL", price=101.0))
        s._apply_fill(self._prop("BBB26APRFUT", "SELL", price=102.0))
        s._apply_fill(self._prop("BBB26MAYFUT", "BUY", price=99.0))

        assert len(s.state.closed_trades) == 2
        aaa, bbb = s.state.closed_trades
        # Same shape as TestPerTradePnL in test_arbitrage.py.
        assert abs(aaa["realized_pnl"] - (200.0 - aaa["transaction_costs"])) < 1e-6
        assert abs(bbb["realized_pnl"] - (400.0 - bbb["transaction_costs"])) < 1e-6


# ──────────────────────────────────────────────────────────
# Index-future (IDF) panel ingestion
# ──────────────────────────────────────────────────────────

class TestIndexPanelIngestion:
    """The same loader and history-builder that handle STF must also handle
    IDF (NIFTY/BANKNIFTY/etc.) once `instrument_types` is opened up. Pinning
    this so a future refactor of the loader can't silently drop indices."""

    def test_load_stf_panel_idf_only(self):
        # Skip if the cached archive isn't present (e.g. CI without bhavcopy).
        from pathlib import Path
        archive = Path("data_cache/bhavcopy_raw")
        if not list(archive.glob("bhavcopy_fo_*.csv")):
            pytest.skip("No bhavcopy archive in data_cache — skipping IDF panel test.")

        from research.backtest_arbitrage import load_stf_panel
        panel = load_stf_panel(instrument_types=("IDF",))
        assert not panel.empty
        present = set(panel["symbol"].unique())
        # At least the two flagship indices must be there.
        assert "NIFTY" in present
        assert "BANKNIFTY" in present
        # And NO STF tickers should leak through when we asked for IDF only.
        assert "RELIANCE" not in present
        assert "TCS" not in present

    def test_idf_panel_builds_spread_history(self):
        from pathlib import Path
        archive = Path("data_cache/bhavcopy_raw")
        if not list(archive.glob("bhavcopy_fo_*.csv")):
            pytest.skip("No bhavcopy archive in data_cache — skipping IDF panel test.")

        from research.backtest_arbitrage import load_stf_panel
        from research.backtest_calendar_meanreversion import build_spread_history
        panel = load_stf_panel(universe=["NIFTY", "BANKNIFTY"], instrument_types=("IDF",))
        spread_h, vol_h = build_spread_history(panel)
        # Both indices should yield a non-trivial spread series.
        assert "NIFTY" in spread_h and len(spread_h["NIFTY"]) > 50
        assert "BANKNIFTY" in spread_h and len(spread_h["BANKNIFTY"]) > 50
        # Volume series populated (these archives carry TtlTradgVol).
        assert "NIFTY" in vol_h
        # Spread is F_next − F_curr; for a healthy term structure it's positive.
        # Most NIFTY days we'd expect spread > 0 (cost of carry); allow some
        # negative days but the median should be positive.
        spreads = [s for _, s in spread_h["NIFTY"]]
        spreads.sort()
        median = spreads[len(spreads) // 2]
        assert median > 0, f"NIFTY median spread should be positive (cost of carry); got {median}"


class TestMaxOpenIsAnActualCap:
    """WHY (Rule 9, issue #235): `max_open` broke the scan on
    `len(open_calendars) >= mr_max_open`, but open_calendars does not change
    during a scan — positions are booked later in _apply_fill. Starting below
    the cap the loop never broke, so one tick could open the entire qualifying
    set. Same defect as ArbitrageStrategy's, found alongside it: the arbitrage
    book peaked at 11 concurrent against a cap of 5 on 2026-09-11.

    These tests put MANY qualifying symbols in ONE scan; a fixture that opens
    one calendar per tick passes the buggy code."""

    def _history(self, symbols, n=80):
        # Needs VARIANCE: a flat series gives sd == 0 and every symbol is
        # skipped, which made the first cut of these tests pass vacuously
        # (0 == 0 against a cap of 5). Small alternating noise, then today's
        # print lands far above the band → every symbol is a SHORT_CALENDAR
        # candidate on the same tick.
        base = date(2026, 4, 17)
        return {s: [(base - timedelta(days=n - i), 1.0 + (0.1 if i % 2 else -0.1))
                    for i in range(n)]
                for s in symbols}

    def _strategy(self, n_symbols, cap, already_open=0):
        syms = [f"S{i:02d}" for i in range(n_symbols)]
        s = _make_strategy(universe=syms, max_open=cap, min_history=10,
                           min_avg_volume=0, entry_n_sd=1.0,
                           spread_history=self._history(syms),
                           volume_history={x: [(date(2026, 4, 16), 10**6, 10**6)]
                                           for x in syms})
        for i in range(already_open):
            s.state.open_calendars[f"OPEN{i}"] = CalendarTrade(
                symbol=f"OPEN{i}", position="SHORT_CALENDAR",
                entry_time=datetime(2026, 4, 17, 9, 0), entry_carry_diff=0.0)
        # next_px well above the rolling mean → spread_now > upper for all.
        s._observe_universe = lambda: [
            _snap(symbol=x, near_px=100.0, next_px=140.0) for x in syms]
        return s

    def _n(self, proposals):
        return len([p for p in proposals if p.option_type == "FUT"]) // 2

    def test_one_scan_cannot_exceed_the_cap(self):
        assert self._n(self._strategy(20, cap=5).scan_and_propose()) == 5

    def test_it_counts_what_is_already_open(self):
        assert self._n(self._strategy(20, cap=5, already_open=4)
                       .scan_and_propose()) == 1

    def test_capacity_returns_after_positions_close(self):
        # `planned` must be per-scan, not per-session (#235 review).
        s = self._strategy(20, cap=5)
        assert self._n(s.scan_and_propose()) == 5
        s.state.open_calendars.clear()
        assert self._n(s.scan_and_propose()) == 5

    def test_the_strongest_z_takes_the_slot(self):
        # The cap now decides WHICH calendars open. A symbol just over
        # entry_n_sd must not take the slot from a far stronger one later in
        # the universe list (#235 review).
        syms = [f"S{i:02d}" for i in range(4)]
        s = _make_strategy(universe=syms, max_open=1, min_history=10,
                           min_avg_volume=0, entry_n_sd=1.0,
                           spread_history=self._history(syms),
                           volume_history={x: [(date(2026, 4, 16), 10**6, 10**6)]
                                           for x in syms})
        # S02 is the widest print; S00 comes first in the universe.
        widths = {"S00": 101.3, "S01": 101.6, "S02": 140.0, "S03": 101.4}
        s._observe_universe = lambda: [
            _snap(symbol=x, near_px=100.0, next_px=widths[x]) for x in syms]
        props = [p for p in s.scan_and_propose() if p.option_type == "FUT"]
        assert {p.tradingsymbol[:3] for p in props} == {"S02"}

    def test_a_full_book_proposes_nothing(self):
        # Paired with a positive control on the SAME fixture, or "zero
        # proposals" could mean the fixture simply never qualifies — which is
        # exactly how the first cut of these tests passed vacuously.
        assert self._n(self._strategy(20, cap=5, already_open=0)
                       .scan_and_propose()) > 0, "fixture must produce entries"
        assert self._n(self._strategy(20, cap=5, already_open=5)
                       .scan_and_propose()) == 0
