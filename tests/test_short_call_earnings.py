"""
Tests for the short-call-into-earnings paper strategy.

Each test pins a property that, if it broke, would either lose money or make
the paper book lie about how much was lost. The strategy has NO measured edge
(docs/research/pre-earnings-iv-crush-2026-08-29.md §5.3), so its whole value is
that its accounting is honest — especially ``realised_R`` when an earnings gap
beats the stop.
"""
from __future__ import annotations

import configparser
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.greeks_engine import implied_volatility_bisect
from strategies import _atm_iv
from strategies.short_call_earnings import ShortCallEarningsStrategy, ShortCallPosition


def _cfg(tmp_path: Path, **overrides) -> str:
    c = configparser.ConfigParser()
    c["mode"] = {"trading_mode": "paper"}
    if overrides:
        c["short_call_earnings"] = {k: str(v) for k, v in overrides.items()}
    p = tmp_path / "cfg.ini"
    with open(p, "w") as fh:
        c.write(fh)
    return str(p)


def _strategy(tmp_path, **overrides) -> ShortCallEarningsStrategy:
    return ShortCallEarningsStrategy(kite=None, config_path=_cfg(tmp_path, **overrides),
                                     mode="paper")


def _panel(symbol="ACME", n=300, iv=0.30, start="2025-03-03") -> pd.DataFrame:
    # Starts early enough that >= _atm_iv.IVP_MIN_OBS sessions precede the
    # 2026-06-10 decision date used throughout these tests; a shorter history
    # correctly yields no trade (see test_no_ivp_history_means_no_trade).
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        "date": dates, "symbol": symbol, "spot": 1000.0, "strike": 1000.0,
        "dte": 20, "expiry": pd.Timestamp("2026-12-31"), "lot": 100,
        "ce_px": 30.0, "pe_px": 30.0, "iv_ce": iv, "iv_pe": iv, "atm_iv": iv,
        "ce_vol": 1000, "pe_vol": 1000,
    })


def _calendar(event_date, announced_at, symbol="ACME") -> pd.DataFrame:
    return pd.DataFrame({"symbol": [symbol],
                         "event_date": [pd.Timestamp(event_date)],
                         "announced_at": [pd.Timestamp(announced_at)]})


def _snap(call_px=20.0, **kw) -> dict:
    base = {"symbol": "ACME", "spot": 1000.0, "strike": 1000.0, "expiry": "2026-12-31",
            "dte": 20, "tradingsymbol": "ACME26DEC1000CE", "lot_size": 100,
            "call_px": call_px, "atm_iv": 0.60, "open": call_px,
            "high": call_px, "low": call_px, "date": pd.Timestamp("2026-06-10 15:05")}
    base.update(kw)
    return {"ACME": base}


# ── Safety ────────────────────────────────────────────────────────────────

def test_live_mode_is_refused(tmp_path):
    """Safety rule 3 + the research verdict: this must never reach a broker.
    A regression here would put an unbounded-loss naked short on real money."""
    with pytest.raises(NotImplementedError, match="PAPER/SIGNALS ONLY"):
        ShortCallEarningsStrategy(kite=None, config_path=_cfg(tmp_path), mode="live")


def test_sub_1R_config_is_refused(tmp_path):
    """The operator asked for a MINIMUM 1R. A config whose target is tighter
    than its stop is a losing risk/reward and must fail loudly at construction,
    not silently trade a 0.5R book."""
    with pytest.raises(ValueError, match="below min_rr"):
        _strategy(tmp_path, target_pct_of_credit=0.3, stop_pct_of_credit=0.6)


def test_default_config_is_exactly_1R(tmp_path):
    s = _strategy(tmp_path)
    assert s.params["target_pct_of_credit"] == s.params["stop_pct_of_credit"]


# ── Sizing: 1R means 1R ───────────────────────────────────────────────────

def test_size_lots_makes_the_stop_equal_one_R(tmp_path):
    """The whole point of the sizing rule: lots x lot_size x credit x stop_pct
    must not exceed the risk budget, or '1R' is a fiction."""
    s = _strategy(tmp_path, total_capital=1_000_000, risk_per_trade_pct=2.0,
                  stop_pct_of_credit=0.6)
    lots = s._size_lots(credit=20.0, lot_size=100)      # R/lot = 20*0.6*100 = 1200
    assert lots == 16                                    # floor(20000 / 1200)
    assert lots * 100 * 20.0 * 0.6 <= 20_000


def test_oversized_lot_is_skipped_not_truncated(tmp_path):
    """A single lot risking more than the budget must SKIP. Taking it anyway
    would silently run a multi-R position and break every downstream R metric."""
    s = _strategy(tmp_path, total_capital=1_000_000, risk_per_trade_pct=1.0)
    assert s._size_lots(credit=40.0, lot_size=3000) == 0     # R/lot = 72,000 >> 10,000


def test_oversized_lot_taken_only_when_explicitly_allowed(tmp_path):
    s = _strategy(tmp_path, total_capital=1_000_000, risk_per_trade_pct=1.0,
                  allow_min_one_lot=1)
    assert s._size_lots(credit=40.0, lot_size=3000) == 1


# ── Entry gates ───────────────────────────────────────────────────────────

def _armed(tmp_path, ivp_iv=0.60, **overrides):
    s = _strategy(tmp_path, total_capital=1_000_000, risk_per_trade_pct=2.0, **overrides)
    s.set_panel(_panel(iv=0.30))
    s.set_calendar(_calendar("2026-06-11", "2026-06-01"))
    s.set_current_date(pd.Timestamp("2026-06-10 15:05"),
                       next_session=pd.Timestamp("2026-06-11"))
    s.set_snapshots(_snap(atm_iv=ivp_iv))
    return s


def test_entry_fires_when_iv_is_at_a_yearly_high(tmp_path):
    s = _armed(tmp_path, ivp_iv=0.60)          # 0.60 ranks above every 0.30 in history
    props = s.scan_and_propose()
    assert len(props) == 1
    p = props[0]
    assert p.transaction_type == "SELL" and p.option_type == "CE"
    snap = p.greeks_snapshot
    assert snap["ivp"] == 100.0
    # target/stop straddle the credit symmetrically => exactly 1R
    assert snap["target_px"] == pytest.approx(20.0 * 0.4)
    assert snap["stop_px"] == pytest.approx(20.0 * 1.6)


def test_entry_blocked_when_iv_is_not_high(tmp_path):
    """The IVP>=90 gate is the only entry condition the research tested. If a
    refactor stops applying it, the runner silently trades a different, untested
    strategy."""
    s = _armed(tmp_path, ivp_iv=0.20)          # below every historical observation
    assert s.scan_and_propose() == []


def test_event_not_yet_public_is_never_traded(tmp_path):
    """Anti-look-ahead, enforced rather than assumed: the study found the
    contaminated version of this strategy produced a t=6.25 phantom edge (§7).
    Acting on a results date announced AFTER our decision time is exactly that
    bug, live."""
    s = _strategy(tmp_path, total_capital=1_000_000, risk_per_trade_pct=2.0)
    s.set_panel(_panel(iv=0.30))
    s.set_calendar(_calendar("2026-06-11", "2026-06-10 16:00"))   # announced AFTER
    s.set_current_date(pd.Timestamp("2026-06-10 15:05"),
                       next_session=pd.Timestamp("2026-06-11"))
    s.set_snapshots(_snap(atm_iv=0.60))
    assert s.scan_and_propose() == []


def test_no_ivp_history_means_no_trade(tmp_path):
    """Too little history must yield no trade, never a fabricated neutral 50
    (issue #75)."""
    s = _strategy(tmp_path)
    s.set_panel(_panel(n=10))
    s.set_calendar(_calendar("2026-06-11", "2026-06-01"))
    s.set_current_date(pd.Timestamp("2026-06-10 15:05"),
                       next_session=pd.Timestamp("2026-06-11"))
    s.set_snapshots(_snap(atm_iv=0.60))
    assert s.scan_and_propose() == []


def test_tiny_credit_is_skipped(tmp_path):
    """Below the credit floor the round trip is pure cost — §4.1 measured the
    break-even at ~0.7% of premium per leg-side."""
    s = _armed(tmp_path, min_credit_rupees=1_000_000)
    assert s.scan_and_propose() == []


# ── Exits: the accounting that makes the paper run worth running ──────────

def _open_position(tmp_path, credit=20.0, lots=10, lot_size=100):
    s = _armed(tmp_path)
    s.execute_proposals(s.scan_and_propose())
    pos = s.positions["ACME"]
    return s, pos


def test_target_exit_books_a_full_R_win(tmp_path):
    s, pos = _open_position(tmp_path)
    s.set_snapshots(_snap(call_px=pos.target_px, low=pos.target_px,
                          date=pd.Timestamp("2026-06-11 10:00")))
    s.set_current_date(pd.Timestamp("2026-06-11 10:00"))
    s.execute_proposals(s.check_and_rehedge())
    closed = s.closed_positions[-1]
    assert closed.exit_reason == "TARGET"
    assert 0.7 < closed.realised_R < 1.0        # ~1R gross, less two legs of cost


def test_stop_exit_books_about_minus_one_R(tmp_path):
    s, pos = _open_position(tmp_path)
    s.set_snapshots(_snap(call_px=pos.stop_px, open=pos.credit, high=pos.stop_px,
                          date=pd.Timestamp("2026-06-11 10:00")))
    s.set_current_date(pd.Timestamp("2026-06-11 10:00"))
    s.execute_proposals(s.check_and_rehedge())
    closed = s.closed_positions[-1]
    assert closed.exit_reason == "STOP"
    assert -1.4 < closed.realised_R < -1.0      # -1R plus costs, never better than -1R


def test_gap_through_stop_is_labelled_and_costs_more_than_one_R(tmp_path):
    """THE test this paper run exists for. When the option OPENS above the stop
    the fill is the open, not the stop — the loss is worse than the nominal 1R
    and must be recorded as such. The study measured -1.55R mean / -3.13R worst
    on the ~5% of events that gap. A runner that booked these as -1.00R would
    understate the strategy's true risk and could talk someone into going live."""
    s, pos = _open_position(tmp_path)
    gap_px = pos.stop_px * 2.0
    s.set_snapshots(_snap(call_px=gap_px, open=gap_px, high=gap_px,
                          date=pd.Timestamp("2026-06-11 09:15")))
    s.set_current_date(pd.Timestamp("2026-06-11 09:15"))
    s.execute_proposals(s.check_and_rehedge())
    closed = s.closed_positions[-1]
    assert closed.exit_reason == "GAP_STOP"
    assert closed.exit_px == pytest.approx(gap_px)
    assert closed.realised_R < -2.0
    rep = s.generate_eod_report()
    assert rep["today"]["gap_through_stop_count"] == 1
    assert rep["today"]["gap_through_worst_R"] < -2.0
    assert rep["cumulative"]["gap_through_stop_count"] == 1


def test_gap_stop_does_not_fire_on_a_stale_intraday_bar(tmp_path):
    """The gap rule may only fire on a NEW session's open. Applying it to the
    entry session's own open would exit instantly at a price we never faced."""
    s, pos = _open_position(tmp_path)
    mid = (pos.credit + pos.stop_px) / 2
    s.set_snapshots(_snap(call_px=mid, open=pos.stop_px * 2, high=mid,
                          date=pd.Timestamp("2026-06-10 15:20")))   # same session
    s.set_current_date(pd.Timestamp("2026-06-10 15:20"))
    assert s.check_and_rehedge() == []


def test_time_stop_flattens_after_max_hold(tmp_path):
    """Never carry a naked short call past the event window it was sized for."""
    s, pos = _open_position(tmp_path)
    mid = (pos.credit + pos.stop_px) / 2
    for d in ("2026-06-11 15:20", "2026-06-12 15:20"):
        s.set_current_date(pd.Timestamp(d))
        s.set_snapshots(_snap(call_px=mid, open=mid, high=mid, low=mid,
                              date=pd.Timestamp(d)))
        s.execute_proposals(s.check_and_rehedge())
    assert not s.positions
    assert s.closed_positions[-1].exit_reason == "TIME"


def test_stop_takes_priority_over_target_in_the_same_bar(tmp_path):
    """A bar that touched both must book the LOSS. Booking the win would
    flatter every backtest and every paper report."""
    s, pos = _open_position(tmp_path)
    s.set_snapshots(_snap(call_px=pos.credit, open=pos.credit,
                          high=pos.stop_px, low=pos.target_px,
                          date=pd.Timestamp("2026-06-11 10:00")))
    s.set_current_date(pd.Timestamp("2026-06-11 10:00"))
    s.execute_proposals(s.check_and_rehedge())
    assert s.closed_positions[-1].exit_reason == "STOP"


# ── Persistence ───────────────────────────────────────────────────────────

def test_state_round_trip_preserves_the_risk_levels(tmp_path):
    """A restart must not lose the stop, the target or R — a restored position
    with a wrong stop is an unmanaged naked short."""
    s, pos = _open_position(tmp_path)
    blob = s.serialize_state()
    s2 = _strategy(tmp_path)
    s2.restore_state(blob)
    r = s2.positions["ACME"]
    assert (r.stop_px, r.target_px, r.r_rupees, r.lots) == \
           (pos.stop_px, pos.target_px, pos.r_rupees, pos.lots)
    assert math.isinf(r.day_high_at_entry) == math.isinf(pos.day_high_at_entry)


# ── The IV panel the entry gate depends on ────────────────────────────────

def test_vectorised_iv_matches_the_repo_solver():
    """The panel uses a fast vectorised bisection; the runner's live path uses
    core.greeks_engine. If they disagree, IVP is ranking today's IV against a
    history computed on a different scale (Rule 7: one convention)."""
    rng = np.random.default_rng(0)
    S = rng.uniform(200, 3000, 40)
    K = S * rng.uniform(0.97, 1.03, 40)
    T = rng.uniform(7, 40, 40) / 365.0
    px = S * rng.uniform(0.02, 0.08, 40)
    fast = _atm_iv.implied_vol_vec(px, S, K, T, True)
    for i in range(len(S)):
        ref = implied_volatility_bisect(px[i], S[i], K[i], T[i], _atm_iv.RISK_FREE, "CE")
        assert abs(ref - fast[i]) < 1e-5


def test_iv_percentile_excludes_today_and_needs_enough_history():
    p = _panel(n=200, iv=0.30)
    asof = p.date.iloc[-1]
    assert _atm_iv.iv_percentile(p, "ACME", 0.60, asof) == 100.0
    assert _atm_iv.iv_percentile(p, "ACME", 0.10, asof) == 0.0
    assert _atm_iv.iv_percentile(p, "ACME", 0.60, p.date.iloc[5]) is None
    assert _atm_iv.iv_percentile(p, "MISSING", 0.60, asof) is None


def test_position_dict_round_trip_handles_infinite_day_high():
    pos = ShortCallPosition(
        symbol="X", tradingsymbol="XCE", strike=100.0, expiry="2026-12-31",
        event_date="2026-06-11", entry_dt=pd.Timestamp("2026-06-10"),
        credit=10.0, lots=1, lot_size=100, target_px=4.0, stop_px=16.0,
        r_rupees=600.0, ivp_at_entry=95.0, spot_at_entry=100.0,
    )
    back = ShortCallPosition.from_dict(pos.to_dict())
    assert math.isinf(back.day_high_at_entry)
    assert back.stop_px == pos.stop_px


# ── Uncapped paper mode (2026-08-29) ──────────────────────────────────────

def test_max_positions_zero_means_unlimited(tmp_path):
    """Paper default is uncapped: a cap silently truncates the signal set (at 3,
    208 of 417 tested events were dropped for want of a slot, so the book
    measured 35% of the strategy). Regressing to a cap would make the forward
    run quietly unrepresentative rather than visibly wrong."""
    s = _strategy(tmp_path, max_positions=0)
    assert s.params["max_positions"] == 0
    cal = pd.concat([_calendar("2026-06-11", "2026-06-01", sym) for sym in
                     ("AAA", "BBB", "CCC", "DDD", "EEE")], ignore_index=True)
    panel = pd.concat([_panel(symbol=sym) for sym in
                       ("AAA", "BBB", "CCC", "DDD", "EEE")], ignore_index=True)
    s.set_panel(panel)
    s.set_calendar(cal)
    s.set_current_date(pd.Timestamp("2026-06-10 15:05"),
                       next_session=pd.Timestamp("2026-06-11"))
    snaps = {}
    for sym in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        one = _snap()["ACME"].copy()
        one.update(symbol=sym, tradingsymbol=f"{sym}26DEC1000CE", atm_iv=0.60)
        snaps[sym] = one
    s.set_snapshots(snaps)
    assert len(s.scan_and_propose()) == 5


def test_positive_max_positions_still_caps_and_prefers_highest_ivp(tmp_path):
    """The cap must still work when an operator sets one — and when it binds,
    the survivors must be the highest-IVP names. A flat-IV fixture cannot test
    this (every symbol ranks 100), so the histories here are spread so the three
    candidates get genuinely different percentiles."""
    s = _strategy(tmp_path, max_positions=2, entry_ivp_min=5.0)
    syms = ("AAA", "BBB", "CCC")
    panels = []
    for sym in syms:
        pn = _panel(symbol=sym)
        pn["atm_iv"] = np.linspace(0.10, 1.00, len(pn))   # spread => distinct ranks
        panels.append(pn)
    s.set_panel(pd.concat(panels, ignore_index=True))
    s.set_calendar(pd.concat([_calendar("2026-06-11", "2026-06-01", x) for x in syms],
                             ignore_index=True))
    s.set_current_date(pd.Timestamp("2026-06-10 15:05"),
                       next_session=pd.Timestamp("2026-06-11"))
    snaps = {}
    for sym, iv in zip(syms, (0.35, 0.95, 0.60)):    # BBB richest, then CCC, AAA last
        one = _snap()["ACME"].copy()
        one.update(symbol=sym, tradingsymbol=f"{sym}26DEC1000CE", atm_iv=iv)
        snaps[sym] = one
    s.set_snapshots(snaps)
    ivps = {sym: _atm_iv.iv_percentile(s._panel, sym, snaps[sym]["atm_iv"],
                                       s._current_date) for sym in syms}
    assert ivps["BBB"] > ivps["CCC"] > ivps["AAA"] >= 5.0, ivps
    props = s.scan_and_propose()
    assert len(props) == 2
    assert {p.greeks_snapshot["underlying"] for p in props} == {"BBB", "CCC"}


# ── Regressions from the 2026-08-29 code review ───────────────────────────

def test_snapshot_for_a_different_contract_is_refused(tmp_path):
    """Finding 1. The runner re-derives an ATM strike each tick for entry
    candidates. If that ever leaks into a HELD position, the stop is measured
    against an option we do not own — and on the results gap, the day this
    strategy exists to measure, the freshly-struck call has barely moved, so the
    stop never fires and gap_through_stop_count reads zero while the real
    position bleeds. Marking against the wrong contract must be refused."""
    s, pos = _open_position(tmp_path)
    wrong = _snap(call_px=pos.stop_px * 3, open=pos.stop_px * 3, high=pos.stop_px * 3,
                  date=pd.Timestamp("2026-06-11 09:15"))
    wrong["ACME"]["tradingsymbol"] = "ACME26DEC1200CE"      # re-struck, not ours
    s.set_current_date(pd.Timestamp("2026-06-11 09:15"))
    s.set_snapshots(wrong)
    assert s.check_and_rehedge() == []
    assert "ACME" in s.positions          # still open, not phantom-exited


def test_day_high_guard_expires_after_the_entry_session(tmp_path):
    """Finding 5. day_high_at_entry exists to stop a PRE-entry spike firing the
    stop on the entry session. Carried into later sessions it suppresses real
    stops for the life of the trade: entry at 20 after the call printed 70
    earlier that day, then a genuine rally to 68 on results day would be
    ignored because 68 < 70."""
    s = _armed(tmp_path)
    s.set_snapshots(_snap(high=70.0))                       # spike BEFORE we sold
    s.execute_proposals(s.scan_and_propose())
    pos = s.positions["ACME"]
    assert pos.day_high_at_entry == 70.0 and pos.stop_px == 32.0
    # same session: a high below the pre-entry spike must NOT fire
    s.set_snapshots(_snap(call_px=25.0, high=68.0, low=25.0,
                          date=pd.Timestamp("2026-06-10 15:20")))
    s.set_current_date(pd.Timestamp("2026-06-10 15:20"))
    assert s.check_and_rehedge() == []
    # next session: the same high IS a real post-entry rally and must fire
    s.set_snapshots(_snap(call_px=25.0, open=25.0, high=68.0, low=25.0,
                          date=pd.Timestamp("2026-06-11 11:00")))
    s.set_current_date(pd.Timestamp("2026-06-11 11:00"))
    s.execute_proposals(s.check_and_rehedge())
    assert s.closed_positions[-1].exit_reason == "STOP"


def test_target_cannot_fire_off_a_pre_entry_low(tmp_path):
    """Finding 6. The stop was guarded against pre-entry prints and the target
    was not — so the entry session's own low could book a phantom +1R win. The
    asymmetry biased the book in the strategy's favour, which is the one thing
    this run cannot afford."""
    s = _armed(tmp_path)
    s.set_snapshots(_snap(low=1.0))                          # dip BEFORE we sold
    s.execute_proposals(s.scan_and_propose())
    pos = s.positions["ACME"]
    assert pos.day_low_at_entry == 1.0 and pos.target_px == 8.0
    s.set_snapshots(_snap(call_px=20.0, high=20.0, low=1.0,
                          date=pd.Timestamp("2026-06-10 15:20")))
    s.set_current_date(pd.Timestamp("2026-06-10 15:20"))
    assert s.check_and_rehedge() == []                       # no phantom target
    assert "ACME" in s.positions


def test_eod_report_separates_today_from_cumulative(tmp_path):
    """Finding 7. Positions carry across sessions and closed_positions is
    restored on restart, so a single aggregate under a per-day filename would
    make an operator diffing sidecars double-count."""
    s, pos = _open_position(tmp_path)
    s.set_current_date(pd.Timestamp("2026-06-11 10:00"))
    s.set_snapshots(_snap(call_px=pos.target_px, low=pos.target_px,
                          date=pd.Timestamp("2026-06-11 10:00")))
    s.execute_proposals(s.check_and_rehedge())
    same_day = s.generate_eod_report()
    assert same_day["today"]["closed_trades"] == 1
    assert same_day["cumulative"]["closed_trades"] == 1
    # next session: yesterday's close must leave TODAY empty but cumulative intact
    s.set_current_date(pd.Timestamp("2026-06-12 15:20"))
    later = s.generate_eod_report()
    assert later["today"]["closed_trades"] == 0
    assert later["today"]["realized_pnl"] == 0
    assert later["cumulative"]["closed_trades"] == 1
    assert later["cumulative"]["realized_pnl"] == same_day["cumulative"]["realized_pnl"]


def test_position_round_trip_preserves_both_entry_extremes(tmp_path):
    s = _armed(tmp_path)
    s.set_snapshots(_snap(high=70.0, low=1.0))
    s.execute_proposals(s.scan_and_propose())
    before = s.positions["ACME"]
    s2 = _strategy(tmp_path)
    s2.restore_state(s.serialize_state())
    after = s2.positions["ACME"]
    assert (after.day_high_at_entry, after.day_low_at_entry) == \
           (before.day_high_at_entry, before.day_low_at_entry)


def test_unknown_announcement_time_fails_closed(tmp_path):
    """The publicity check must fail CLOSED. Requiring `pd.notna(ann)` before
    comparing made it fail OPEN: an intimation whose timestamp NSE reformatted
    would skip the comparison entirely and be traded, reintroducing exactly the
    look-ahead the research doc records as a t=6.25 phantom edge (§7)."""
    s = _strategy(tmp_path, total_capital=1_000_000, risk_per_trade_pct=2.0)
    s.set_panel(_panel(iv=0.30))
    cal = _calendar("2026-06-11", "2026-06-01")
    cal.loc[0, "announced_at"] = pd.NaT
    s.set_calendar(cal)
    s.set_current_date(pd.Timestamp("2026-06-10 15:05"),
                       next_session=pd.Timestamp("2026-06-11"))
    s.set_snapshots(_snap(atm_iv=0.60))
    assert s.scan_and_propose() == []


# ── Holding-period backstops ──────────────────────────────────────────────

def _armed_with_expiry(tmp_path, expiry, **kw):
    s = _armed(tmp_path, **kw)
    snaps = _snap()
    snaps["ACME"]["expiry"] = expiry
    s.set_snapshots(snaps)
    s.execute_proposals(s.scan_and_propose())
    return s


def test_normal_hold_is_two_sessions(tmp_path):
    """The designed holding period: enter at the T-1 close, flat by T+1."""
    s = _armed_with_expiry(tmp_path, "2026-07-30")
    for d in ("2026-06-11 15:20", "2026-06-12 15:20"):
        s.set_current_date(pd.Timestamp(d))
        snaps = _snap(call_px=20.0, date=pd.Timestamp(d))
        snaps["ACME"]["expiry"] = "2026-07-30"
        s.set_snapshots(snaps)
        s.execute_proposals(s.check_and_rehedge())
    assert not s.positions
    assert s.closed_positions[-1].exit_reason == "TIME"


def test_never_carried_into_physical_settlement(tmp_path):
    """A short single-stock call held to expiry is a DELIVERY obligation —
    Indian stock options are physically settled and NSE ramps margin through
    expiry week. Before this backstop the exit logic referenced the expiry date
    nowhere at all: a position could sit open on expiry day whenever the session
    counter had not yet reached max_hold_sessions. Same defect the kalman-pairs
    runner shipped with (PR #69)."""
    s = _armed_with_expiry(tmp_path, "2026-06-12")     # expiry 2 days out
    s.set_current_date(pd.Timestamp("2026-06-11 15:20"))
    snaps = _snap(call_px=20.0, date=pd.Timestamp("2026-06-11 15:20"))
    snaps["ACME"]["expiry"] = "2026-06-12"
    s.set_snapshots(snaps)
    s.execute_proposals(s.check_and_rehedge())
    assert not s.positions
    closed = s.closed_positions[-1]
    assert closed.exit_reason == "EXPIRY_FLATTEN"      # not TIME — sessions_held is only 1
    assert closed.sessions_held < 2


def test_expiry_flatten_does_not_pre_empt_a_price_trigger(tmp_path):
    """A stop that actually filled must still book at the stop, not at the mark:
    price triggers model a resting order and are the honest fill."""
    s = _armed_with_expiry(tmp_path, "2026-06-12")
    pos = s.positions["ACME"]
    s.set_current_date(pd.Timestamp("2026-06-11 15:20"))
    snaps = _snap(call_px=pos.stop_px, open=pos.credit, high=pos.stop_px,
                  date=pd.Timestamp("2026-06-11 15:20"))
    snaps["ACME"]["expiry"] = "2026-06-12"
    s.set_snapshots(snaps)
    s.execute_proposals(s.check_and_rehedge())
    assert s.closed_positions[-1].exit_reason == "STOP"


def test_runner_downtime_cannot_extend_the_hold_indefinitely(tmp_path):
    """max_hold_sessions counts sessions the runner OBSERVED. An outage skips
    the increment, so a nominally 2-session position survived 6 calendar days in
    a missed-tick trace. The wall-clock backstop bounds that."""
    s = _armed_with_expiry(tmp_path, "2026-07-30", max_hold_calendar_days=5)
    # runner is down for a week, then returns for a single tick
    d = "2026-06-17 15:20"                              # 7 calendar days after entry
    s.set_current_date(pd.Timestamp(d))
    snaps = _snap(call_px=20.0, date=pd.Timestamp(d))
    snaps["ACME"]["expiry"] = "2026-07-30"
    s.set_snapshots(snaps)
    s.execute_proposals(s.check_and_rehedge())
    assert not s.positions
    closed = s.closed_positions[-1]
    assert closed.exit_reason == "TIME_CALENDAR"        # distinct from TIME on purpose
    assert closed.sessions_held < 2


# ── Regressions from the second code review (2026-08-30) ──────────────────

def test_intraday_jump_books_at_the_market_not_the_nominal_stop(tmp_path):
    """Finding 1, and the worst defect found so far. Indian results are
    routinely announced DURING market hours, so the price is often already far
    through the stop when we poll — and the fresh-session `open` branch never
    sees it. Booking the nominal stop_px there invents a fill nobody could get,
    reports realised_R as a tidy -1.05, and makes gap_through_stop_count read
    ZERO on exactly the events it exists to count."""
    s, pos = _open_position(tmp_path)
    jump = pos.stop_px * 3
    s.set_current_date(pd.Timestamp("2026-06-11 11:00"))
    s.set_snapshots(_snap(call_px=jump, open=pos.credit, high=jump, low=pos.credit,
                          date=pd.Timestamp("2026-06-11 11:00")))
    s.execute_proposals(s.check_and_rehedge())
    closed = s.closed_positions[-1]
    assert closed.exit_reason == "GAP_STOP"
    assert closed.exit_px == pytest.approx(jump)       # not pos.stop_px
    assert closed.realised_R < -2.0
    assert s.generate_eod_report()["today"]["gap_through_stop_count"] == 1


def test_resting_stop_still_fills_at_the_level(tmp_path):
    """The counterpart: when the LTP is back below the stop but the session
    traded through it, a resting SL would plausibly have filled AT the level.
    That case must keep booking stop_px, or every ordinary stop is mislabelled."""
    s, pos = _open_position(tmp_path)
    s.set_current_date(pd.Timestamp("2026-06-11 11:00"))
    s.set_snapshots(_snap(call_px=pos.credit, open=pos.credit, high=pos.stop_px,
                          low=pos.credit, date=pd.Timestamp("2026-06-11 11:00")))
    s.execute_proposals(s.check_and_rehedge())
    closed = s.closed_positions[-1]
    assert closed.exit_reason == "STOP"
    assert closed.exit_px == pytest.approx(pos.stop_px)


def test_an_event_is_traded_at_most_once(tmp_path):
    """Finding 4. scan_and_propose only skipped symbols with an OPEN position,
    so a stop-out at 15:07 made the name eligible again at 15:08 — re-selling
    the same event on every remaining tick of the entry window, booking full
    costs each time and destroying the per-event basis of every R statistic."""
    s = _armed(tmp_path)
    s.execute_proposals(s.scan_and_propose())
    pos = s.positions["ACME"]
    s.set_current_date(pd.Timestamp("2026-06-10 15:07"))
    s.set_snapshots(_snap(call_px=pos.stop_px * 2, open=pos.credit,
                          high=pos.stop_px * 2, date=pd.Timestamp("2026-06-10 15:07")))
    s.execute_proposals(s.check_and_rehedge())
    assert not s.positions                                  # stopped out
    s.set_current_date(pd.Timestamp("2026-06-10 15:08"),
                       next_session=pd.Timestamp("2026-06-11"))
    s.set_snapshots(_snap(atm_iv=0.60))
    assert s.scan_and_propose() == []                       # must NOT re-sell


def test_traded_event_lock_survives_a_restart(tmp_path):
    """A restart inside the entry window must not re-sell an event already
    traded — the state file is the only thing that remembers."""
    s = _armed(tmp_path)
    s.execute_proposals(s.scan_and_propose())
    blob = s.serialize_state()
    s2 = _strategy(tmp_path)
    s2.restore_state(blob)
    s2.positions.clear()                                    # as if it had closed
    s2.set_panel(_panel(iv=0.30))
    s2.set_calendar(_calendar("2026-06-11", "2026-06-01"))
    s2.set_current_date(pd.Timestamp("2026-06-10 15:08"),
                        next_session=pd.Timestamp("2026-06-11"))
    s2.set_snapshots(_snap(atm_iv=0.60))
    assert s2.scan_and_propose() == []


def test_cost_model_honours_the_documented_monkeypatch_target(tmp_path):
    """Finding 7. core/costs.py's header pins the convention (Rule 7): the suite
    fakes costs by patching strategies.taleb_karpathy.estimate_transaction_cost.
    A module-level binding would escape that patch and show up as wrong numbers
    rather than a failing test."""
    import strategies.taleb_karpathy as tk
    s = _strategy(tmp_path)
    real = tk.estimate_transaction_cost
    try:
        tk.estimate_transaction_cost = lambda *a, **k: 999.0
        # 999 from the fake + slippage (1% of premium x lots x lot_size)
        assert s._cost(20.0, 1, 100, "SELL") == pytest.approx(999.0 + 20.0)
    finally:
        tk.estimate_transaction_cost = real
