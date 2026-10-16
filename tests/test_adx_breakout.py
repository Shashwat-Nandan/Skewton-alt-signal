"""Pins for the ADX-filtered breakout replay.

The fills below are the ones a daily bar can invent if the code looks
ahead, takes a same-bar target, or lets a roll gap count as a profit.
A test that only checks "a trade happened" would still pass those bugs.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research.adx_breakout import (
    Params,
    _cost_points,
    add_indicators,
    build_continuous,
    fut_stt_rate,
    simulate,
    summarize,
)
from research.optimize_adx_breakout import (
    LADDERS,
    WARMUP_BARS,
    accept_experiment,
    apply_move,
    holdout_verdict,
    legal_moves,
    load_nifty_5min,
    load_nifty_front,
    net_at_cost_multiple,
    pick_move,
    ready_frame,
)

WIDE = Params(trail_amount=5000.0, bars_to_exit=20, atr_multiplier=5.0)


def _bars(rows):
    df = pd.DataFrame(rows)
    defaults = {
        "hhmm": 1530,
        "lot": 65,
        "offset": 0.0,
        "roll_flat": False,
        "adx": 10.0,
        "adx_ma": 12.0,
        "atr": 1000.0,
        "brk_long": np.nan,
        "brk_short": np.nan,
    }
    for key, value in defaults.items():
        if key not in df.columns:
            df[key] = value
    return df


def _day(n):
    return date(2026, 1, 5) + timedelta(days=n)


def test_breakout_level_includes_the_signal_bar():
    """EasyLanguage's "Buy next bar at Highest(High, N) stop" is evaluated at
    the signal bar's close and includes that bar. Leaving it out arms a
    buy-stop at a level the signal bar already traded through, so the next
    bar's "breakout" fills inside yesterday's range."""
    highs = [1.0, 1.0, 1.0, 1.0, 10.0, 1.0]
    df = pd.DataFrame({
        "date": [_day(i) for i in range(6)],
        "expiry": [date(2026, 1, 29)] * 6,
        "open": [1.0] * 6,
        "high": highs,
        "low": [1.0] * 6,
        "close": [1.0] * 6,
        "lot": [65] * 6,
    })
    out = add_indicators(build_continuous(df), Params(breakout_length=3))
    assert out.loc[3, "brk_long"] == pytest.approx(1.0)
    assert out.loc[4, "brk_long"] == pytest.approx(10.0)


def test_roll_gap_is_not_a_breakout_and_not_a_profit():
    """The next contract opens 8 points rich. That gap is not P&L and not a signal."""
    exp_a, exp_b = date(2026, 1, 6), date(2026, 2, 24)
    front = pd.DataFrame([
        dict(date=_day(0), expiry=exp_a, open=100, high=101, low=99, close=100, lot=65),
        dict(date=_day(1), expiry=exp_a, open=100, high=105, low=100, close=102, lot=65),
        dict(date=_day(2), expiry=exp_b, open=110, high=112, low=109, close=111, lot=65),
    ])
    cont = build_continuous(front)
    # No next-contract close: falls back to shifting day-1's close onto
    # day-2's open, and says so. The latest contract stays raw.
    assert cont.loc[1, "roll_basis"] == "open_gap"
    assert cont.loc[1, "close"] == pytest.approx(110.0)
    assert cont.loc[2, "open"] == pytest.approx(110.0)
    assert bool(cont.loc[1, "roll_flat"]) is True
    assert bool(cont.loc[2, "roll_flat"]) is False

    cont["hhmm"] = 1530
    cont["adx"] = 10.0
    cont["adx_ma"] = 12.0
    cont["atr"] = 1000.0
    cont["brk_long"] = np.nan
    cont["brk_short"] = np.nan
    # Above the signal close, and inside the next bar's adjusted high.
    cont.loc[0, "brk_long"] = 109.0
    result = simulate(cont, WIDE)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "ROLL"
    assert trade.exit_date == _day(1)
    # Raw fill 101, raw exit 102. The 110 open of the new contract is not in this.
    assert trade.points_gross == pytest.approx(1.0)
    assert trade.points_net < trade.points_gross


def test_roll_adjusts_by_the_calendar_basis_and_keeps_the_overnight_gap():
    """The new contract opens 10 above the old close, but 4 of that is the
    calendar basis (next contract's close that day). The other 6 is a real
    overnight move and must stay in the series ADX/ATR/levels read."""
    exp_a, exp_b = date(2026, 1, 6), date(2026, 2, 24)
    front = pd.DataFrame([
        dict(date=_day(0), expiry=exp_a, open=100, high=101, low=99, close=100, lot=65,
             next_expiry=exp_b, next_close=104.0),
        dict(date=_day(1), expiry=exp_a, open=100, high=105, low=100, close=102, lot=65,
             next_expiry=exp_b, next_close=106.0),
        dict(date=_day(2), expiry=exp_b, open=112, high=113, low=111, close=112, lot=65,
             next_expiry=None, next_close=np.nan),
    ])
    cont = build_continuous(front)
    assert cont.loc[1, "roll_basis"] == "calendar"
    assert cont.loc[1, "close"] == pytest.approx(106.0)
    assert cont.loc[2, "open"] - cont.loc[1, "close"] == pytest.approx(6.0)


def test_long_stop_fills_at_the_level_and_a_gap_fills_at_the_open():
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=100, high=101, low=100, close=100),
        dict(date=_day(2), open=104, high=104, low=103, close=103),
    ])
    # bars_to_exit 1: entry bar is index 1, the market exit is the open of index 3
    # only if a fourth bar exists. With three bars the time exit is not due yet
    # at the close of the entry bar (bars since entry is 0). Force the time
    # exit by using bars_to_exit 1 and a fourth bar.
    df = pd.concat([
        df,
        _bars([dict(date=_day(3), open=110, high=110, low=109, close=109)]),
    ], ignore_index=True)
    result = simulate(df, Params(trail_amount=5000.0, bars_to_exit=1, atr_multiplier=5.0))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_px == pytest.approx(100.0)
    assert trade.exit_i == 3
    assert trade.exit_reason == "TIME"
    assert trade.exit_px == pytest.approx(110.0)
    assert trade.points_gross == pytest.approx(10.0)

    gapped = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=105, high=106, low=104, close=104),
    ])
    gap_trade = simulate(gapped, WIDE).trades[0]
    assert gap_trade.entry_px == pytest.approx(105.0)
    assert gap_trade.exit_reason == "END"


def test_a_bar_that_can_fill_both_stops_is_skipped():
    df = _bars([
        dict(date=_day(0), open=95, high=96, low=94, close=95, brk_long=100, brk_short=90),
        dict(date=_day(1), open=95, high=101, low=89, close=95),
    ])
    result = simulate(df, WIDE)
    assert result.trades == []
    assert result.ambiguous_bars == 1


def test_an_open_beyond_one_level_fills_that_side_not_ambiguous():
    """A gap above the long level filled the buy-stop at the open, first.
    Dropping such bars as ambiguous removes the big-range breakout days the
    hypothesis is about."""
    df = _bars([
        dict(date=_day(0), open=95, high=96, low=94, close=95, brk_long=100, brk_short=90),
        dict(date=_day(1), open=102, high=103, low=89, close=95),
    ])
    result = simulate(df, WIDE)
    assert result.ambiguous_bars == 0
    trade = result.trades[0]
    assert trade.side == "LONG" and trade.entry_px == pytest.approx(102.0)


def test_close_already_through_the_level_does_not_place_a_buy_stop():
    df = _bars([
        dict(date=_day(0), open=104, high=106, low=104, close=105, brk_long=100),
        dict(date=_day(1), open=105, high=112, low=104, close=110),
    ])
    assert simulate(df, WIDE).trades == []


def test_high_adx_blocks_the_entry():
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100,
             adx=40.0, adx_ma=30.0),
        dict(date=_day(1), open=100, high=110, low=100, close=108),
    ])
    assert simulate(df, WIDE).trades == []


def test_outside_the_session_window_does_not_arm():
    df = _bars([
        dict(date=_day(0), hhmm=900, open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=100, high=110, low=100, close=108),
    ])
    assert simulate(df, WIDE).trades == []


def test_same_day_stop_out_does_not_rearm():
    """EntriesToday counts the fill. A second bar that same day must not enter."""
    same = _day(1)
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        # Fill at 100, stop 75 (5 x ATR 5), closes through it at 72.
        dict(date=same, open=99, high=110, low=70, close=72, brk_long=100),
        dict(date=same, open=100, high=101, low=99, close=100, brk_long=100),
        dict(date=_day(2), open=100, high=101, low=99, close=100),
    ])
    result = simulate(df, Params(trail_amount=15.0, bars_to_exit=20, atr_multiplier=5.0))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_date == same
    assert trade.exit_reason == "STOP"
    assert trade.exit_px == pytest.approx(75.0)


def test_one_entry_a_day_does_not_block_the_next_session():
    """On daily bars the order armed at today's close is for tomorrow. An
    entry today must not stop tomorrow's order being placed."""
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=_day(1), open=99, high=110, low=70, close=72, brk_long=100, atr=5.0),
        dict(date=_day(2), open=99, high=101, low=98, close=100, atr=5.0),
    ])
    result = simulate(df, Params(trail_amount=15.0, bars_to_exit=20, atr_multiplier=5.0))
    assert [t.entry_date for t in result.trades] == [_day(1), _day(2)]


def test_the_trail_is_not_live_on_the_entry_bar():
    """The first pass booked a 15-point trail inside a 200-point daily bar,
    so every trade exited on its entry bar at exactly -trail: the KILL
    measured the simulator, not the strategy. A "next bar" trail is armed
    at the entry bar's close."""
    params = Params(trail_amount=15.0, bars_to_exit=20, atr_multiplier=5.0)
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=95, high=120, low=94, close=118),
        dict(date=_day(2), open=118, high=119, low=104, close=106),
    ])
    trade = simulate(df, params).trades[0]
    assert trade.entry_px == pytest.approx(100.0)
    assert trade.exit_i == 2                     # not the entry bar
    assert trade.exit_reason == "TRAIL"
    assert trade.exit_px == pytest.approx(105.0)   # 120 - 15
    assert trade.points_gross == pytest.approx(5.0)


def test_entry_bar_initial_stop_only_when_the_tape_proves_it():
    """A level fill: the low may have printed before the breakout. Closing
    through the stop proves it; crossing and closing back is held and counted."""
    params = Params(trail_amount=5000.0, bars_to_exit=20, atr_multiplier=2.0)
    proven = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=_day(1), open=95, high=101, low=85, close=86),
    ])
    trade = simulate(proven, params).trades[0]
    assert (trade.exit_i, trade.exit_reason) == (1, "STOP")
    assert trade.exit_px == pytest.approx(90.0)

    unresolved = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=_day(1), open=95, high=101, low=85, close=100),
    ])
    result = simulate(unresolved, params)
    assert result.trades[0].exit_reason == "END"
    assert result.entry_bar_unresolved == 1


def test_a_gap_entry_bar_is_wholly_after_the_fill():
    """Filled at the open, the whole range is after the fill, so a low
    through the initial stop is a real stop-out."""
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=_day(1), open=105, high=106, low=90, close=100),
    ])
    trade = simulate(df, Params(trail_amount=5000.0, bars_to_exit=20, atr_multiplier=2.0)).trades[0]
    assert trade.entry_px == pytest.approx(105.0)
    assert (trade.exit_i, trade.exit_reason) == (1, "STOP")
    assert trade.exit_px == pytest.approx(95.0)


def test_initial_stop_distance_is_fixed_at_entry():
    """ATR rising after entry must not loosen the protective stop."""
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=_day(1), open=100, high=101, low=99, close=100, atr=50.0),
        dict(date=_day(2), open=100, high=100, low=85, close=86, atr=50.0),
    ])
    trade = simulate(df, Params(trail_amount=5000.0, bars_to_exit=20, atr_multiplier=2.0)).trades[0]
    assert trade.exit_reason == "STOP"
    assert trade.exit_px == pytest.approx(90.0)    # 100 - 2 x 5, not 100 - 2 x 50


def test_trail_uses_the_prior_extreme_not_this_bars_high():
    """Low sits between the old trail and the trail this bar's high would set.

    Firing the tighter trail on the same bar would book a stop the tape
    cannot justify. The next bar, with that extreme now known, does stop.
    """
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=99, high=110, low=90, close=100),
        dict(date=_day(2), open=100, high=100, low=94, close=96),
    ])
    result = simulate(df, Params(trail_amount=15.0, bars_to_exit=20, atr_multiplier=5.0))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_i == 2
    assert trade.exit_reason == "TRAIL"
    assert trade.exit_px == pytest.approx(95.0)


def test_profit_target_is_not_taken_on_the_entry_bar():
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=100, high=110, low=100, close=108),
    ])
    params = Params(
        trail_amount=5000.0, bars_to_exit=20, atr_multiplier=5.0,
        use_profit_target=True, profit_target_points=5.0,
    )
    trade = simulate(df, params).trades[0]
    assert trade.exit_reason == "END"
    assert trade.exit_px == pytest.approx(108.0)


def test_later_bar_stop_wins_when_stop_and_target_both_trade():
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=_day(1), open=100, high=101, low=96, close=100, atr=5.0),
        dict(date=_day(2), open=100, high=120, low=80, close=100, atr=5.0),
    ])
    params = Params(
        trail_amount=5000.0, bars_to_exit=20, atr_multiplier=2.0,
        use_profit_target=True, profit_target_points=5.0,
    )
    trade = simulate(df, params).trades[0]
    assert trade.exit_reason == "STOP"
    assert trade.exit_px == pytest.approx(90.0)


def test_short_gap_fills_at_the_open():
    df = _bars([
        dict(date=_day(0), open=95, high=96, low=94, close=95, brk_short=90),
        dict(date=_day(1), open=80, high=81, low=70, close=70),
    ])
    trade = simulate(df, WIDE).trades[0]
    assert trade.side == "SHORT"
    assert trade.entry_px == pytest.approx(80.0)
    assert trade.points_gross == pytest.approx(10.0)


def test_inconsistent_bar_is_refused():
    df = _bars([
        dict(date=_day(0), open=90, high=91, low=95, close=90, brk_long=100),
    ])
    with pytest.raises(ValueError, match="inconsistent"):
        simulate(df, WIDE)


def test_published_defaults_sit_on_the_search_ladders():
    params = Params()
    for name, ladder in LADDERS.items():
        assert getattr(params, name) in ladder


def test_a_dead_target_distance_is_not_offered_while_the_target_is_off():
    off = legal_moves(Params())
    assert not any(key.startswith("profit_target_points=") for key in off)
    on = legal_moves(apply_move(Params(), "use_profit_target=True"))
    assert any(key.startswith("profit_target_points=") for key in on)


def test_a_rejected_rung_does_not_make_the_rungs_beyond_it_unreachable():
    """First pass: rejecting trail 15 -> 40 banned the only path to 80+,
    the only trails wider than a daily range. Any rung is one move away."""
    legal = legal_moves(Params())
    assert {"trail_amount=40.0", "trail_amount=80.0", "trail_amount=400.0"} <= set(legal)
    banned = {"trail_amount=40.0"}
    choice = {"probabilities": {"trail_amount=40.0": 0.6, "trail_amount=150.0": 0.3, "stop": 0.1}}
    assert pick_move(choice, legal, banned) == "trail_amount=150.0"
    assert apply_move(Params(), "trail_amount=150.0").trail_amount == 150.0


def test_apply_move_sets_one_knob_and_stays_on_the_ladder():
    stepped = apply_move(Params(), "trail_amount=40.0")
    assert stepped.trail_amount == 40.0
    assert stepped.adx_threshold == Params().adx_threshold
    for bad in ("trail_amount=7.0", "trail_amount=15.0", "trail_amount_up", "nope=1"):
        with pytest.raises(ValueError):
            apply_move(Params(), bad)


def test_mechanism_veto_and_window_gate_block_a_better_train_number():
    baseline = {"trades": 150, "net_points": -100.0, "max_dd_points": 50.0,
                "windows": [-40.0, -30.0, -30.0]}
    better = {"trades": 150, "net_points": 10.0, "max_dd_points": 20.0,
              "windows": [4.0, 3.0, 3.0]}
    kept, _ = accept_experiment(better, baseline, fit_score=1.5)
    assert kept is True
    vetoed, _ = accept_experiment(better, baseline, fit_score=0.4)
    assert vetoed is False
    still_all_red = {"trades": 150, "net_points": -10.0, "max_dd_points": 20.0,
                     "windows": [-2.0, -3.0, -5.0]}
    rejected, reason = accept_experiment(still_all_red, baseline, fit_score=2.0)
    assert rejected is False
    assert "windows" in reason
    thin = dict(better, trades=99)        # MIN_TRADES is 100
    assert accept_experiment(thin, baseline, fit_score=2.0)[0] is False


def test_a_thin_baseline_does_not_let_any_loser_through():
    """First pass scored a <20-trade baseline as -inf, so any 20-trade
    candidate "improved" however much it lost. Quality is compared as is."""
    thin_base = {"trades": 12, "net_points": -100.0, "max_dd_points": 100.0,
                 "windows": [0.0, -50.0, -50.0]}
    big_loser = {"trades": 150, "net_points": -5000.0, "max_dd_points": 2500.0,
                 "windows": [10.0, -2500.0, -2510.0]}
    kept, reason = accept_experiment(big_loser, thin_base, fit_score=2.0)
    assert kept is False and "did not beat" in reason


def test_holdout_verdict_is_kill_unless_the_sample_clears_every_bar():
    """Owner-approved gates: >= 40 trades, net > 0, Sharpe > 0, and net
    still > 0 with costs x 1.5 — an edge that only survives exact costs is
    not one to paper-trade."""
    ok = {"trades": 40, "gross_points": 300.0, "net_points": 200.0, "sharpe": 0.2}
    assert holdout_verdict(ok) == "SURVIVES_SAMPLE"      # stressed net 150
    assert holdout_verdict(dict(ok, trades=39)) == "KILL"
    assert holdout_verdict(dict(ok, net_points=0.0)) == "KILL"
    assert holdout_verdict(dict(ok, sharpe=0.0)) == "KILL"
    thin_edge = {"trades": 40, "gross_points": 300.0, "net_points": 90.0, "sharpe": 0.5}
    assert net_at_cost_multiple(thin_edge, 1.5) == pytest.approx(-15.0)
    assert holdout_verdict(thin_edge) == "KILL"


def test_pick_move_skips_a_rejected_rung():
    legal = {"trail_amount=40.0": "widen", "stop": "stop"}
    choice = {"probabilities": {"trail_amount=40.0": 0.7, "stop": 0.3}}
    assert pick_move(choice, legal, set()) == "trail_amount=40.0"
    assert pick_move(choice, legal, {"trail_amount=40.0"}) == "stop"
    assert pick_move({"probabilities": {}}, legal, set()) == "stop"


def test_loader_keeps_the_near_nifty_and_drops_other_indexes(tmp_path):
    rows = [
        ("2026-01-05", "IDF", "NIFTY", "2026-02-24", 101, 102, 100, 101, 65),
        ("2026-01-05", "IDF", "NIFTY", "2026-01-29", 100, 103, 99, 102, 65),
        ("2026-01-05", "IDF", "BANKNIFTY", "2026-01-29", 500, 510, 490, 505, 15),
        ("2026-01-05", "IDF", "NIFTY", "2026-01-04", 90, 91, 89, 90, 65),
    ]
    df = pd.DataFrame(rows, columns=[
        "TradDt", "FinInstrmTp", "TckrSymb", "XpryDt",
        "OpnPric", "HghPric", "LwPric", "ClsPric", "NewBrdLotQty",
    ])
    df.to_parquet(tmp_path / "bhavcopy_fo_20260105.parquet")
    front = load_nifty_front(tmp_path)
    assert len(front) == 1
    assert front.iloc[0]["expiry"] == date(2026, 1, 29)
    assert front.iloc[0]["close"] == pytest.approx(102.0)
    assert int(front.iloc[0]["lot"]) == 65
    # The next NIFTY contract's same-day close, for the roll's calendar basis.
    assert front.iloc[0]["next_expiry"] == date(2026, 2, 24)
    assert front.iloc[0]["next_close"] == pytest.approx(101.0)


def test_summarize_drawdown_is_the_peak_to_trough_of_net_points():
    df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=100, high=101, low=100, close=100),
        dict(date=_day(2), open=90, high=91, low=90, close=90),
    ])
    # Time exit is not due. Last bar ends the trade at 90, a loss, so the
    # drawdown is that loss. Costs make it a bit worse than 10 points.
    result = simulate(df, WIDE)
    stats = summarize(result, df, WIDE)
    assert stats["trades"] == 1
    assert stats["gross_points"] == pytest.approx(-10.0)
    assert stats["max_dd_points"] == pytest.approx(abs(stats["net_points"]))
    assert stats["net_points"] < stats["gross_points"]


def test_costs_use_the_futures_stt_in_force_on_the_fill_date():
    """core.costs carries today's 0.05% futures STT; a 2025 sale paid 0.02%.
    Charging today's rate on 2024-25 trades biases the net toward KILL."""
    assert fut_stt_rate(date(2024, 9, 30)) == pytest.approx(0.000125)
    assert fut_stt_rate(date(2025, 6, 2)) == pytest.approx(0.0002)
    assert fut_stt_rate(date(2026, 5, 4)) == pytest.approx(0.0005)
    old = _cost_points(24000.0, 65, True, date(2025, 6, 2))
    new = _cost_points(24000.0, 65, True, date(2026, 5, 4))
    assert new - old == pytest.approx(24000.0 * (0.0005 - 0.0002))
    # Buy side has no STT, so the date does not move it.
    assert _cost_points(24000.0, 65, False, date(2025, 6, 2)) == pytest.approx(
        _cost_points(24000.0, 65, False, date(2026, 5, 4)))


def test_ready_frame_waits_out_the_wilder_seed():
    """ADX is an EWM seeded from its first value. "Finite" is not "warmed
    up": the first ready bars must sit WARMUP_BARS into the history."""
    n = WARMUP_BARS + 30
    full = pd.DataFrame({
        "date": [date(2025, 1, 1) + timedelta(days=i) for i in range(n)],
        "adx": 10.0, "adx_ma": 12.0, "atr": 50.0, "brk_long": 1.0, "brk_short": 1.0,
    })
    ready = ready_frame(full)
    assert len(ready) == 30
    assert ready.iloc[0]["date"] == full.iloc[WARMUP_BARS]["date"]


def test_5min_frame_uses_bar_close_time_and_the_lot_in_force():
    """EasyLanguage's Time is the bar close: a 09:15 candle is 0920, so the
    09:30 start first arms on the 09:25 candle. The lot is the futures lot
    on that session (nearest earlier when the archive skips a day)."""
    ts = pd.to_datetime([
        "2025-12-30 09:15", "2025-12-30 09:25", "2025-12-30 15:25", "2026-01-02 09:15",
    ]).tz_localize("Asia/Kolkata")
    five = pd.DataFrame({"ts": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
    front = pd.DataFrame({"date": [date(2025, 12, 29), date(2026, 1, 1)], "lot": [75, 65]})
    out = load_nifty_5min(five, front)
    assert list(out["hhmm"]) == [920, 930, 1530, 920]
    assert list(out["lot"]) == [75, 75, 75, 65]
    assert list(out["date"]) == [date(2025, 12, 30)] * 3 + [date(2026, 1, 2)]
    assert (out["offset"] == 0).all() and not out["roll_flat"].any()


def test_intraday_one_entry_a_day_and_the_session_window():
    """On 5-minute bars a same-day stop-out must not re-enter that session,
    an order can only be armed by a bar closing inside 09:30-15:30, and
    the next session trades again."""
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    rows = [
        dict(date=d1, hhmm=925, open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=d1, hhmm=930, open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=d1, hhmm=935, open=99, high=101, low=80, close=80, brk_long=100, atr=5.0),
        dict(date=d1, hhmm=940, open=80, high=99, low=80, close=99, brk_long=100, atr=5.0),
        dict(date=d1, hhmm=945, open=99, high=102, low=98, close=101, brk_long=100, atr=5.0),
        dict(date=d1, hhmm=1535, open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=d2, hhmm=920, open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=d2, hhmm=930, open=99, high=99, low=98, close=99, brk_long=100, atr=5.0),
        dict(date=d2, hhmm=935, open=99, high=101, low=99, close=100, atr=5.0),
    ]
    result = simulate(_bars(rows), Params(trail_amount=15.0, bars_to_exit=20, atr_multiplier=2.0))
    # d1: armed by the 09:30 close (09:25 is outside), filled 09:35 and
    # stopped there (closed through 90); the 09:45 break is not re-entered.
    # The 15:35 bar cannot arm for d2, nor can d2's 09:20 bar; 09:30 does.
    assert [(t.entry_date, t.entry_i, t.exit_reason) for t in result.trades] == [
        (d1, 2, "STOP"), (d2, 8, "END"),
    ]
