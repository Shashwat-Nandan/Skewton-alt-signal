"""Tests for run_paper_buy_on_gap pure helpers (no auth / no market).

Two behaviours that would silently corrupt a session if they regressed:
  - the intraday book is NEVER carried overnight: a restored position dated
    before today must be dropped (not resurrected and then flattened at a
    stale mark);
  - fetch_today_quotes maps kite.quote's NSE:<sym> / ohlc shape into the
    {open, ltp, low} the strategy expects, and degrades to {} (not a crash)
    on a total quote failure so the heartbeat can catch a dead token.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import run_paper_buy_on_gap as r
from strategies.buy_on_gap import BuyOnGapStrategy, GapPosition

LOG = logging.getLogger("test")


class _NullKite:
    pass


def _strategy():
    s = BuyOnGapStrategy(kite=_NullKite(), config_path="/dev/null", mode="paper")
    return s


def test_restore_drops_overnight_positions():
    today = date(2026, 4, 17)
    s = _strategy()
    # Prior blob: one position from today (intraday restart), one from a prior
    # session (must be dropped — never held overnight).
    s.positions["TODAY"] = GapPosition(
        symbol="TODAY", entry_dt=pd.Timestamp("2026-04-17"), entry_px=100.0,
        qty=10, stop_px=95.0, gap_ret=-0.03, gap_z=-1.5, rationale="x")
    s.positions["STALE"] = GapPosition(
        symbol="STALE", entry_dt=pd.Timestamp("2026-04-16"), entry_px=100.0,
        qty=10, stop_px=95.0, gap_ret=-0.03, gap_z=-1.5, rationale="x")
    blob = s.serialize_state()

    fresh = _strategy()
    r.restore_strategy(fresh, blob, today, LOG)
    assert "TODAY" in fresh.positions
    assert "STALE" not in fresh.positions


def test_fetch_today_quotes_maps_kite_shape():
    class FakeKite:
        def quote(self, keys):
            return {
                "NSE:INFY": {"last_price": 1492.0,
                             "ohlc": {"open": 1500.0, "high": 1505.0,
                                      "low": 1480.0, "close": 1510.0}},
                # missing ohlc/last_price for one symbol → dropped, not crashed
                "NSE:TCS": {},
            }
    out = r.fetch_today_quotes(FakeKite(), ["INFY", "TCS", "WIPRO"], LOG)
    assert out["INFY"] == {"open": 1500.0, "ltp": 1492.0, "low": 1480.0}
    # TCS had no fields → present but Nones; WIPRO absent entirely.
    assert "WIPRO" not in out


def test_fetch_today_quotes_total_failure_returns_empty():
    class DeadKite:
        def quote(self, keys):
            raise RuntimeError("token expired")
    assert r.fetch_today_quotes(DeadKite(), ["INFY"], LOG) == {}


# ── Experiment kill rule (efficiency review 2026-07-05 §2.4) ──────────────
# WHY these tests matter: the strategy was deployed as a known-overfit
# experiment. The kill rule is the pre-agreed answer to "when has the forward
# record confirmed the overfit?" — if these thresholds silently stop binding,
# the experiment bleeds indefinitely (the failure mode the review found across
# the paper book). It takes raw per-trade pnls (not objects) so main() can
# evaluate it on the persisted blob BEFORE the panel-load/auth startup cost.
def test_kill_reason_fires_on_cumulative_net_loss_floor():
    reason = r.experiment_kill_reason(-50_000.0, [])
    assert reason is not None and "floor" in reason


def test_kill_reason_fires_on_losing_win_rate_with_enough_trades():
    closed = [-1000.0] * 10 + [500.0] * 5   # 15 trades, 33% win rate
    reason = r.experiment_kill_reason(-10_000.0, closed)
    assert reason is not None and "win rate" in reason


def test_kill_reason_holds_fire_below_both_thresholds():
    # The book's real state at rule-introduction time (−₹39k over 5 losers)
    # must NOT fire: the rule is a pre-agreed floor, not a retro-kill.
    assert r.experiment_kill_reason(-39_268.0, [-7853.0] * 5) is None
    # Win-rate leg needs the trade count: 14 losers is not yet an answer.
    assert r.experiment_kill_reason(-10_000.0, [-100.0] * 14) is None


def test_kill_reason_tolerates_none_pnls_from_raw_state():
    # Raw state blobs can carry pnl=None on a malformed row; None must count
    # as a non-win, not crash the gate that decides whether to trade.
    closed = [None] * 10 + [500.0] * 5
    assert r.experiment_kill_reason(-10_000.0, closed) is not None


def test_kill_reason_legs_can_be_disabled():
    assert r.experiment_kill_reason(-9e9, [], net_loss_floor_inr=0,
                                    min_trades=0) is None
    assert r.experiment_kill_reason(-1.0, [-1.0] * 100,
                                    net_loss_floor_inr=0, min_trades=0) is None


def test_killed_sentinel_written_and_self_describing(tmp_path, monkeypatch):
    # Once the rule fires the runner stops writing EOD sidecars, so the
    # sentinel is the only artifact distinguishing "killed" from "broken"
    # for the dashboard and the scoreboard. It must exist and carry the reason.
    monkeypatch.setattr(r, "KILLED_SENTINEL_PATH", tmp_path / "HALT_BOG_KILLED")
    r._drop_killed_sentinel("cumulative net realized -60,000 breached", LOG)
    text = (tmp_path / "HALT_BOG_KILLED").read_text()
    assert "breached" in text and "does NOT re-enable" in text


def test_kill_rule_pins_entry_halt_for_the_whole_session(tmp_path, monkeypatch):
    # With open positions the runner must go EXIT-ONLY: halt_new stays True
    # across refresh() even when no operator HALT_* file exists.
    monkeypatch.setattr(r, "HALT_ALL_PATH", tmp_path / "HALT_ALL")
    monkeypatch.setattr(r, "HALT_NEW_ENTRIES_PATH", tmp_path / "HALT_NEW")
    monkeypatch.setattr(r, "HALT_GAP_DAILY_LOSS_PATH", tmp_path / "HALT_LOSS")
    hs = r.GapHaltState(kill_rule=True)
    hs.refresh(LOG)
    assert hs.halt_new and not hs.halt_all
    plain = r.GapHaltState()
    plain.refresh(LOG)
    assert not plain.halt_new
