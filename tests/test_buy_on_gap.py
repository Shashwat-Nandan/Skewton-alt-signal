"""Tests for the Buy-on-Gap intraday mean-reversion strategy + backtest.

These encode INTENT, not just behaviour (CLAUDE.md Rule 9): each asserts the
business rule would BREAK the test if removed —
  - the gap threshold must BIND (a small gap must NOT fire — catches a
    "buy everything" regression);
  - the trend filter must reject gap-downs below the long MA when on, accept
    them when off (this is the refinement the backtest found decisive);
  - the exit must realise (close-open)*qty NET of cost, and the catastrophic
    stop must take priority over the close;
  - no look-ahead: trailing σ/MA are shifted, so the warm-up window fires
    nothing;
  - serialize/restore round-trips the book.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from strategies.base import OrderValidationError, validate_order
from strategies.buy_on_gap import BuyOnGapStrategy, GapPosition
from backtest_buy_on_gap import BuyOnGapBacktester, ZERO_TRADE_PENALTY
from trade_proposer import TradeProposal


class _NullKite:
    pass


# Small, fast windows so a ~40-bar synthetic panel produces real signals.
TEST_PARAMS = {
    "std_window": 10, "ma_window": 10, "min_avg_turnover_cr": 1.0,
    "gap_std_mult": 1.0, "max_positions": 5, "stop_loss_pct": 5.0,
    "cost_pct": 0.10, "total_capital": 1_000_000.0,
}


def _strategy(params=None):
    s = BuyOnGapStrategy(kite=_NullKite(), config_path="/dev/null", mode="paper")
    s.params.update(TEST_PARAMS)
    if params:
        s.params.update(params)
    # Seed a tiny in-memory panel so any _ensure_features() call never falls
    # back to load_equity_panel()/load_universe(), which read gitignored
    # data_cache files absent in CI. Tests that need real bars call set_panel()
    # again (it overrides).
    s.set_panel(_build_symbol("AAA", n=12))
    return s


def _build_symbol(symbol, n=40, drift=0.02, noise=0.004, base=100.0,
                  gap_day=None, gap_ret=-0.03, day_reaction=0.0, stop_breach=False):
    """One symbol's daily OHLCV. A steep up-drift keeps the trailing MA well
    below price so a moderate gap-down's OPEN can still sit above the MA (the
    only regime where the trend filter passes — mirrors the live behaviour).

    On ``gap_day`` the open gaps by ``gap_ret`` off the prior close; the close
    moves ``day_reaction`` off that open (>0 = intraday reversion = a win);
    ``stop_breach`` drags the low below the catastrophic stop.
    """
    closes = []
    px = base
    for i in range(n):
        nz = noise * (1 if i % 2 == 0 else -1)
        px = px * (1 + drift + nz)
        closes.append(px)
    rows = []
    dates = pd.bdate_range("2024-01-01", periods=n)
    prev_close = None
    for i, (dt, c) in enumerate(zip(dates, closes)):
        if i == 0:
            o, hi, lo, cl = c, c * 1.005, c * 0.995, c
        elif gap_day is not None and i == gap_day:
            o = prev_close * (1 + gap_ret)
            cl = o * (1 + day_reaction)
            lo = o * (1 - 0.20) if stop_breach else min(o, cl) * 0.999
            hi = max(o, cl) * 1.001
        else:
            o = prev_close
            cl = c
            hi = max(o, cl) * 1.003
            lo = min(o, cl) * 0.997
        rows.append({"date": dt, "symbol": symbol, "open": o, "high": hi,
                     "low": lo, "close": cl, "volume": 5_000_000})
        prev_close = cl
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Signal gates
# ──────────────────────────────────────────────────────────────────────────────

class TestSignal:
    def test_unusual_gap_down_fires_buy(self):
        df = _build_symbol("AAA", gap_day=30, gap_ret=-0.03)
        s = _strategy()
        s.set_panel(df)
        s._ensure_features()
        s.set_current_date(df["date"].iloc[30])
        props = s.scan_and_propose()
        assert len(props) == 1
        p = props[0]
        assert p.tradingsymbol == "AAA" and p.transaction_type == "BUY"
        # entry is the gapped OPEN, not the prior close
        assert p.price == pytest.approx(df["open"].iloc[30], rel=1e-9)

    def test_small_gap_does_not_fire(self):
        # A −0.2% gap is well inside k·σ (σ≈0.4%): the threshold must BIND.
        df = _build_symbol("AAA", gap_day=30, gap_ret=-0.002)
        s = _strategy()
        s.set_panel(df)
        s._ensure_features()
        s.set_current_date(df["date"].iloc[30])
        assert s.scan_and_propose() == []

    def test_gap_up_never_fires(self):
        df = _build_symbol("AAA", gap_day=30, gap_ret=+0.03)
        s = _strategy()
        s.set_panel(df)
        s._ensure_features()
        s.set_current_date(df["date"].iloc[30])
        assert s.scan_and_propose() == []

    def test_blowup_gap_skipped(self):
        # A −30% gap exceeds max_gap_down_pct=20 → news/halt, not a revert.
        df = _build_symbol("AAA", gap_day=30, gap_ret=-0.30)
        s = _strategy({"max_gap_down_pct": 20.0})
        s.set_panel(df)
        s._ensure_features()
        s.set_current_date(df["date"].iloc[30])
        assert s.scan_and_propose() == []

    def test_trend_filter_rejects_below_ma_accepts_when_off(self):
        # Flat series → gap-day open sits BELOW the trailing MA.
        df = _build_symbol("AAA", drift=0.0, noise=0.004, gap_day=30, gap_ret=-0.03)
        dt = df["date"].iloc[30]
        on = _strategy({"use_trend_filter": 1})
        on.set_panel(df); on._ensure_features(); on.set_current_date(dt)
        assert on.scan_and_propose() == [], "trend filter must reject open<MA"
        off = _strategy({"use_trend_filter": 0})
        off.set_panel(df); off._ensure_features(); off.set_current_date(dt)
        assert len(off.scan_and_propose()) == 1, "trend OFF must accept the gap"

    def test_live_session_fires_when_today_not_in_panel(self):
        # The real live situation: the daily panel only reaches YESTERDAY at
        # 09:20, today's open arrives via quotes. Without prepare_live_session
        # the signal lookup misses today's index and NOTHING fires all session
        # (the bug this guards). With it, the gap-down fires.
        df = _build_symbol("AAA", n=40)  # no gap row; panel = "history"
        today = df["date"].iloc[-1] + pd.tseries.offsets.BDay(1)
        prev_close = df["close"].iloc[-1]
        quotes = {"AAA": {"open": prev_close * 0.97, "ltp": prev_close * 0.97,
                          "low": prev_close * 0.96}}

        # Without the live-session row: no entry (today absent from features).
        bug = _strategy({"use_trend_filter": 0})
        bug.set_panel(df); bug._ensure_features()
        bug.set_today_quotes(quotes); bug.set_current_date(today)
        assert bug.scan_and_propose() == []

        # With it: the gap-down fires, entry at the quoted open.
        fixed = _strategy({"use_trend_filter": 0})
        fixed.set_panel(df)
        fixed.prepare_live_session(today)
        fixed._ensure_features()
        fixed.set_today_quotes(quotes); fixed.set_current_date(today)
        props = fixed.scan_and_propose()
        assert len(props) == 1
        assert props[0].price == pytest.approx(prev_close * 0.97)

    def test_live_fill_at_ltp_not_open(self):
        # The scan runs 09:20-09:45: the open printed minutes ago and only the
        # LTP is attainable. The SIGNAL stays defined on the open (gap vs
        # prev_close) but the FILL, sizing and stop must anchor to the LTP —
        # entry at the open is a price nobody can trade (changed 2026-07-11).
        df = _build_symbol("AAA", n=40)
        today = df["date"].iloc[-1] + pd.tseries.offsets.BDay(1)
        prev_close = df["close"].iloc[-1]
        open_px, ltp = prev_close * 0.97, prev_close * 0.985  # gap half-reverted
        quotes = {"AAA": {"open": open_px, "ltp": ltp, "low": open_px * 0.999}}
        s = _strategy({"use_trend_filter": 0, "stop_loss_pct": 5.0})
        s.set_panel(df)
        s.prepare_live_session(today)
        s._ensure_features()
        s.set_today_quotes(quotes); s.set_current_date(today)
        props = s.scan_and_propose()
        assert len(props) == 1, "gap is still defined on the open"
        p = props[0]
        assert p.price == pytest.approx(ltp), "fill must be the attainable LTP"
        assert (p.greeks_snapshot or {})["stop_px"] == pytest.approx(ltp * 0.95), \
            "stop anchors to the fill, not the open"

    def test_scan_records_all_qualifying_candidates(self):
        # The intraday-capture hook must see every name that QUALIFIED, not
        # just the top-N entered — counterfactuals matter for a future
        # intraday-path backtest.
        df_a = _build_symbol("AAA", gap_day=30, gap_ret=-0.03)
        df_b = _build_symbol("BBB", gap_day=30, gap_ret=-0.04)
        panel = pd.concat([df_a, df_b], ignore_index=True)
        s = _strategy({"use_trend_filter": 0, "max_positions": 1})
        s.set_panel(panel)
        s._ensure_features()
        s.set_current_date(df_a["date"].iloc[30])
        props = s.scan_and_propose()
        assert len(props) == 1, "only one slot"
        assert set(s.last_scan_candidates) == {"AAA", "BBB"}

    def test_prepare_live_session_idempotent(self):
        # Post-close cache already contains today → no duplicate row.
        df = _build_symbol("AAA", n=40)
        today = df["date"].iloc[-1]  # already present
        s = _strategy()
        s.set_panel(df)
        n_before = len(s._panel)
        s.prepare_live_session(today)
        assert len(s._panel) == n_before

    def test_warmup_no_lookahead(self):
        # Before std_window/ma_window have warmed up, trailing σ/MA are NaN
        # (shifted) → nothing can fire even on an engineered gap.
        df = _build_symbol("AAA", gap_day=3, gap_ret=-0.05)
        s = _strategy()
        s.set_panel(df)
        s._ensure_features()
        s.set_current_date(df["date"].iloc[3])
        assert s.scan_and_propose() == []


# ──────────────────────────────────────────────────────────────────────────────
# Sizing
# ──────────────────────────────────────────────────────────────────────────────

class TestSizing:
    def test_equal_weight_notional(self):
        s = _strategy({"max_positions": 5, "max_gross_exposure_pct": 100.0,
                       "total_capital": 1_000_000.0})
        # per-name budget = 1,000,000 * 100% / 5 = 200,000
        assert s._size(100.0) == 2000
        assert s._size(250.0) == 800


# ──────────────────────────────────────────────────────────────────────────────
# Exits
# ──────────────────────────────────────────────────────────────────────────────

class TestExit:
    def test_close_exit_realizes_pnl_net_of_cost(self):
        pos = GapPosition(symbol="AAA", entry_dt=pd.Timestamp("2024-02-01"),
                          entry_px=100.0, qty=100, stop_px=95.0, gap_ret=-0.03,
                          gap_z=-1.5, rationale="x")
        s = _strategy({"cost_pct": 0.10})
        s.positions["AAA"] = pos
        s._force_close = True
        s.set_current_date(pd.Timestamp("2024-02-01"))
        # Inject today's bar: closes at 103, no stop breach.
        s.set_today_quotes({"AAA": {"open": 100.0, "ltp": 103.0, "low": 99.5}})
        exits = s.check_and_rehedge()
        assert len(exits) == 1 and exits[0].transaction_type == "SELL"
        s.execute_proposals(exits)
        # gross = (103-100)*100 = 300; cost = 0.10% of notional, half each leg:
        # entry 100*100*0.0005=5, exit 103*100*0.0005=5.15 → net ≈ 289.85
        closed = s.closed_positions[0]
        assert closed.exit_reason == "CLOSE"
        assert closed.pnl == pytest.approx(300 - 5 - 5.15, abs=0.01)

    def test_catastrophic_stop_takes_priority(self):
        pos = GapPosition(symbol="AAA", entry_dt=pd.Timestamp("2024-02-01"),
                          entry_px=100.0, qty=100, stop_px=95.0, gap_ret=-0.03,
                          gap_z=-1.5, rationale="x")
        s = _strategy()
        s.positions["AAA"] = pos
        s._force_close = True  # even on the close tick, the stop wins
        s.set_current_date(pd.Timestamp("2024-02-01"))
        # LIVE path: LTP 94 < stop 95 → must fill AT the stop level (SL-order
        # model), not the lower LTP, and must beat the force_close exit.
        s.set_today_quotes({"AAA": {"open": 100.0, "ltp": 94.0, "low": 94.0}})
        exits = s.check_and_rehedge()
        assert exits[0].price == pytest.approx(95.0)
        assert (exits[0].greeks_snapshot or {})["exit_reason"] == "CATASTROPHIC_STOP"

    def test_pre_entry_low_does_not_fire_stop_live(self):
        # The quote's day-low includes prints from BEFORE the 09:20-09:45 entry
        # scan. A pre-entry dip below the stop must NOT stop out a position the
        # price has since recovered above — only the live LTP may trigger it.
        # (This was the old day-low behaviour, changed 2026-07-11.)
        pos = GapPosition(symbol="AAA", entry_dt=pd.Timestamp("2024-02-01"),
                          entry_px=100.0, qty=100, stop_px=95.0, gap_ret=-0.03,
                          gap_z=-1.5, rationale="x")
        s = _strategy()
        s.positions["AAA"] = pos
        s._force_close = False
        s.set_current_date(pd.Timestamp("2024-02-01"))
        s.set_today_quotes({"AAA": {"open": 100.0, "ltp": 102.0, "low": 94.0}})
        assert s.check_and_rehedge() == [], "pre-entry low must not fire the stop"
        # At the close the same book flattens at the LTP, not the stop.
        s._force_close = True
        exits = s.check_and_rehedge()
        assert (exits[0].greeks_snapshot or {})["exit_reason"] == "CLOSE"
        assert exits[0].price == pytest.approx(102.0)

    def test_backtest_path_still_stops_on_day_low(self):
        # Daily bars have no scan-time LTP: the day-low remains the (conservative)
        # stop trigger in backtest — the harness documents this approximation.
        df = _build_symbol("AAA", gap_day=30, gap_ret=-0.03, stop_breach=True)
        s = _strategy({"use_trend_filter": 0})
        s.set_panel(df)
        s._ensure_features()
        s.set_current_date(df["date"].iloc[30])
        s._force_close = True
        s.execute_proposals(s.scan_and_propose())
        s.execute_proposals(s.check_and_rehedge())
        assert s.closed_positions[0].exit_reason == "CATASTROPHIC_STOP"

    def test_no_exit_intraday_without_stop_or_close(self):
        pos = GapPosition(symbol="AAA", entry_dt=pd.Timestamp("2024-02-01"),
                          entry_px=100.0, qty=100, stop_px=95.0, gap_ret=-0.03,
                          gap_z=-1.5, rationale="x")
        s = _strategy()
        s.positions["AAA"] = pos
        s._force_close = False  # mid-session, stop not breached → hold
        s.set_current_date(pd.Timestamp("2024-02-01"))
        s.set_today_quotes({"AAA": {"open": 100.0, "ltp": 101.0, "low": 98.0}})
        assert s.check_and_rehedge() == []


# ──────────────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────────────

class TestPersistence:
    def test_serialize_restore_roundtrip(self):
        s = _strategy()
        s.realized_pnl = 1234.5
        s.transaction_costs = 67.8
        s.positions["AAA"] = GapPosition(
            symbol="AAA", entry_dt=pd.Timestamp("2024-02-01"), entry_px=100.0,
            qty=100, stop_px=95.0, gap_ret=-0.03, gap_z=-1.5, rationale="x")
        blob = s.serialize_state()
        s2 = _strategy()
        s2.restore_state(blob)
        assert s2.realized_pnl == pytest.approx(1234.5)
        assert s2.transaction_costs == pytest.approx(67.8)
        assert "AAA" in s2.positions
        assert s2.positions["AAA"].stop_px == pytest.approx(95.0)
        assert s2.positions["AAA"].gap_ret == pytest.approx(-0.03)


# ──────────────────────────────────────────────────────────────────────────────
# Backtest integration
# ──────────────────────────────────────────────────────────────────────────────

class TestReentryGuard:
    def test_has_entered_today_open_position(self):
        s = _strategy()
        today = pd.Timestamp("2026-04-17")
        assert s.has_entered_today(today) is False
        s.positions["AAA"] = GapPosition(
            symbol="AAA", entry_dt=today, entry_px=100.0, qty=10, stop_px=95.0,
            gap_ret=-0.03, gap_z=-1.5, rationale="x")
        assert s.has_entered_today(today) is True

    def test_has_entered_today_after_stopout_closed(self):
        # The bug case: position opened today then closed (stop) → open-count is
        # 0 but we MUST still report entered so a restart doesn't re-deploy.
        s = _strategy()
        today = pd.Timestamp("2026-04-17")
        closed = GapPosition(symbol="AAA", entry_dt=today, entry_px=100.0,
                             qty=10, stop_px=95.0, gap_ret=-0.03, gap_z=-1.5,
                             rationale="x")
        closed.status = "CLOSED"
        closed.exit_dt = today
        s.closed_positions.append(closed)
        assert len(s.positions) == 0
        assert s.has_entered_today(today) is True

    def test_has_entered_today_ignores_prior_session(self):
        s = _strategy()
        yesterday = pd.Timestamp("2026-04-16")
        s.closed_positions.append(GapPosition(
            symbol="AAA", entry_dt=yesterday, entry_px=100.0, qty=10,
            stop_px=95.0, gap_ret=-0.03, gap_z=-1.5, rationale="x"))
        assert s.has_entered_today(pd.Timestamp("2026-04-17")) is False


class TestValidatorAcceptsTwoCharSymbols:
    def test_two_char_equity_symbol_passes(self):
        # NSE has real 2-char symbols (LT). The pre-submit validator must NOT
        # reject them, or the strategy silently drops valid equity orders.
        prop = TradeProposal(
            tradingsymbol="LT", instrument_token=0, strike=0.0, expiry="",
            option_type="EQ", lot_size=1, quantity=10, price=3500.0,
            transaction_type="BUY", iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=35000.0)
        validate_order(prop)  # must not raise

    def test_one_char_symbol_still_rejected(self):
        # The fat-finger floor still rejects junk (single char).
        prop = TradeProposal(
            tradingsymbol="X", instrument_token=0, strike=0.0, expiry="",
            option_type="EQ", lot_size=1, quantity=10, price=100.0,
            transaction_type="BUY", iv=0.0, bid_ask_spread_pct=0.0,
            margin_required=1000.0)
        with pytest.raises(OrderValidationError):
            validate_order(prop)


class TestBacktest:
    def test_fires_and_books_intraday_winner(self):
        # Engineer a gap-down that reverts intraday → a winning closed trade.
        df = _build_symbol("AAA", gap_day=30, gap_ret=-0.03, day_reaction=+0.02)
        bt = BuyOnGapBacktester(df, params_overrides={**TEST_PARAMS, "use_trend_filter": 0})
        summ = bt.run()
        assert summ["total_trades"] >= 1
        # book is flat each EOD (pure intraday)
        assert len(bt.strategy.positions) == 0
        winner = [t for t in bt.strategy.closed_positions if t.exit_reason == "CLOSE"]
        assert winner and winner[0].pnl > 0

    def test_zero_trades_sentinel(self):
        # No gap anywhere → no trades → the penalty sentinel (flat-fitness rule).
        df = _build_symbol("AAA", gap_day=None)
        bt = BuyOnGapBacktester(df, params_overrides={**TEST_PARAMS, "use_trend_filter": 0})
        summ = bt.run()
        assert summ["total_trades"] == 0
        assert summ["score"] == ZERO_TRADE_PENALTY


# ──────────────────────────────────────────────────────────────────────────────
# Code-review 2026-07-11 fixes
# ──────────────────────────────────────────────────────────────────────────────

class TestReviewFixes20260711:
    """Each test pins a fix from the 2026-07-11 code review (Rule 9)."""

    def test_blowup_cap_binds_the_fill_not_just_the_open(self):
        # Open gaps −3% (passes the 20% cap) but by the scan the LTP has
        # collapsed −25% vs prev_close: a live news crash. The cap must bind
        # the attainable FILL, not only the stale open, or the strategy buys
        # the falling knife the cap exists to exclude.
        df = _build_symbol("AAA", n=40)
        today = df["date"].iloc[-1] + pd.tseries.offsets.BDay(1)
        prev_close = df["close"].iloc[-1]
        quotes = {"AAA": {"open": prev_close * 0.97, "ltp": prev_close * 0.75,
                          "low": prev_close * 0.74}}
        s = _strategy({"use_trend_filter": 0, "max_gap_down_pct": 20.0})
        s.set_panel(df)
        s.prepare_live_session(today)
        s._ensure_features()
        s.set_today_quotes(quotes); s.set_current_date(today)
        assert s.scan_and_propose() == [], \
            "a fill below the blowup cap must not be bought"

    def test_new_post_entry_day_low_fires_stop_between_polls(self):
        # A dip through the stop BETWEEN 60s polls (LTP recovered by the next
        # poll) would have filled a resting SL order. The running day-low
        # making a NEW low ≤ stop since entry must fire it; booked at level.
        pos = GapPosition(symbol="AAA", entry_dt=pd.Timestamp("2024-02-01"),
                          entry_px=100.0, qty=100, stop_px=95.0, gap_ret=-0.03,
                          gap_z=-1.5, rationale="x", day_low_at_entry=98.0)
        s = _strategy()
        s.positions["AAA"] = pos
        s.set_current_date(pd.Timestamp("2024-02-01"))
        # LTP recovered to 99, but the day low printed 94 < 98 post-entry.
        s.set_today_quotes({"AAA": {"open": 100.0, "ltp": 99.0, "low": 94.0}})
        exits = s.check_and_rehedge()
        assert len(exits) == 1 and exits[0].price == pytest.approx(95.0)
        assert (exits[0].greeks_snapshot or {})["exit_reason"] == "CATASTROPHIC_STOP"

    def test_pre_entry_day_low_still_does_not_fire(self):
        # The same low that existed AT entry (pre-entry dip) must not fire —
        # only a NEW low counts as a post-entry print.
        pos = GapPosition(symbol="AAA", entry_dt=pd.Timestamp("2024-02-01"),
                          entry_px=100.0, qty=100, stop_px=95.0, gap_ret=-0.03,
                          gap_z=-1.5, rationale="x", day_low_at_entry=94.0)
        s = _strategy()
        s.positions["AAA"] = pos
        s.set_current_date(pd.Timestamp("2024-02-01"))
        s.set_today_quotes({"AAA": {"open": 100.0, "ltp": 102.0, "low": 94.0}})
        assert s.check_and_rehedge() == []

    def test_entry_records_day_low_and_it_roundtrips(self):
        df = _build_symbol("AAA", n=40)
        today = df["date"].iloc[-1] + pd.tseries.offsets.BDay(1)
        prev_close = df["close"].iloc[-1]
        lo = prev_close * 0.965
        quotes = {"AAA": {"open": prev_close * 0.97, "ltp": prev_close * 0.975,
                          "low": lo}}
        s = _strategy({"use_trend_filter": 0})
        s.set_panel(df)
        s.prepare_live_session(today)
        s._ensure_features()
        s.set_today_quotes(quotes); s.set_current_date(today)
        s.execute_proposals(s.scan_and_propose())
        pos = s.positions["AAA"]
        assert pos.day_low_at_entry == pytest.approx(lo)
        restored = GapPosition.from_dict(pos.to_dict())
        assert restored.day_low_at_entry == pytest.approx(lo, abs=0.01)
        # Pre-upgrade blob (no key) → -inf → LTP-only stop semantics.
        blob = pos.to_dict(); blob.pop("day_low_at_entry")
        assert GapPosition.from_dict(blob).day_low_at_entry == float("-inf")

    def test_scan_candidates_survive_same_day_restore_only(self):
        # Mid-session restart goes exit-only and never rescans: the candidate
        # list must survive a SAME-DAY restore (else the intraday capture
        # silently drops the counterfactual names), but yesterday's list must
        # not pollute a fresh session.
        s = _strategy()
        s.set_current_date(pd.Timestamp("2026-07-13"))
        s.last_scan_candidates = ["AAA", "BBB"]
        blob = s.serialize_state()

        same_day = _strategy()
        same_day.set_current_date(pd.Timestamp("2026-07-13"))
        same_day.restore_state(blob)
        assert same_day.last_scan_candidates == ["AAA", "BBB"]

        next_day = _strategy()
        next_day.set_current_date(pd.Timestamp("2026-07-14"))
        next_day.restore_state(blob)
        assert next_day.last_scan_candidates == []

    def test_restore_warns_on_ledger_drift(self, caplog):
        import logging as _logging
        s = _strategy()
        s.realized_pnl = 5000.0          # headline says +5k, ledger says 0
        blob = s.serialize_state()
        fresh = _strategy()
        with caplog.at_level(_logging.WARNING):
            fresh.restore_state(blob)
        assert any("LEDGER DRIFT" in r.message for r in caplog.records)
