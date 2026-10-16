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
    add_indicators,
    build_continuous,
    simulate,
    summarize,
)
from research.optimize_adx_breakout import (
    LADDERS,
    accept_experiment,
    apply_move,
    holdout_verdict,
    legal_moves,
    load_nifty_front,
    pick_move,
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


def test_breakout_level_excludes_the_signal_bar():
    """Highest(High, N)[1] is the prior N highs. Today's spike is tomorrow's level."""
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
    assert out.loc[4, "brk_long"] == pytest.approx(1.0)
    assert out.loc[5, "brk_long"] == pytest.approx(10.0)


def test_roll_gap_is_not_a_breakout_and_not_a_profit():
    """The next contract opens 8 points rich. That gap is not P&L and not a signal."""
    exp_a, exp_b = date(2026, 1, 6), date(2026, 2, 24)
    front = pd.DataFrame([
        dict(date=_day(0), expiry=exp_a, open=100, high=101, low=99, close=100, lot=65),
        dict(date=_day(1), expiry=exp_a, open=100, high=105, low=100, close=102, lot=65),
        dict(date=_day(2), expiry=exp_b, open=110, high=112, low=109, close=111, lot=65),
    ])
    cont = build_continuous(front)
    # Day-1 close is shifted onto day-2's open. The latest contract stays raw.
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
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=same, open=99, high=110, low=80, close=90, brk_long=100),
        dict(date=same, open=100, high=101, low=99, close=100, brk_long=100),
        dict(date=_day(2), open=100, high=101, low=99, close=100),
    ])
    result = simulate(df, Params(trail_amount=15.0, bars_to_exit=20, atr_multiplier=5.0))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_date == same
    assert trade.exit_reason == "TRAIL"
    assert trade.exit_px == pytest.approx(85.0)


def test_entry_bar_stop_does_not_fill_at_the_pre_entry_open():
    """The open prints before a level fill. Booking it as the stop turns a
    15-point trail into a loss of the whole open-to-level distance, which
    is what a daily bar does on almost every breakout.
    """
    params = Params(trail_amount=15.0, bars_to_exit=20, atr_multiplier=5.0)
    long_df = _bars([
        dict(date=_day(0), open=99, high=99, low=98, close=99, brk_long=100),
        dict(date=_day(1), open=80, high=110, low=79, close=90, brk_long=100),
    ])
    long_trade = simulate(long_df, params).trades[0]
    assert long_trade.entry_px == pytest.approx(100.0)
    assert long_trade.exit_i == 1
    assert long_trade.exit_reason == "TRAIL"
    assert long_trade.exit_px == pytest.approx(85.0)

    short_df = _bars([
        dict(date=_day(0), open=101, high=102, low=100, close=101, brk_short=100),
        dict(date=_day(1), open=120, high=121, low=90, close=110, brk_short=100),
    ])
    short_trade = simulate(short_df, params).trades[0]
    assert short_trade.side == "SHORT"
    assert short_trade.entry_px == pytest.approx(100.0)
    assert short_trade.exit_reason == "TRAIL"
    assert short_trade.exit_px == pytest.approx(115.0)


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
    assert not any(key.startswith("profit_target_points_") for key in off)
    on = legal_moves(apply_move(Params(), "use_profit_target_up"))
    assert any(key.startswith("profit_target_points_") for key in on)


def test_apply_move_steps_one_rung_and_will_not_walk_off_the_ladder():
    stepped = apply_move(Params(), "trail_amount_up")
    assert stepped.trail_amount == 40.0
    assert stepped.adx_threshold == Params().adx_threshold
    with pytest.raises(ValueError):
        apply_move(Params(), "trail_amount_down")


def test_mechanism_veto_and_window_gate_block_a_better_train_number():
    baseline = {"trades": 30, "net_points": -100.0, "max_dd_points": 50.0,
                "windows": [-40.0, -30.0, -30.0]}
    better = {"trades": 30, "net_points": 10.0, "max_dd_points": 20.0,
              "windows": [4.0, 3.0, 3.0]}
    kept, _ = accept_experiment(better, baseline, fit_score=1.5)
    assert kept is True
    vetoed, _ = accept_experiment(better, baseline, fit_score=0.4)
    assert vetoed is False
    still_all_red = {"trades": 30, "net_points": -10.0, "max_dd_points": 20.0,
                     "windows": [-2.0, -3.0, -5.0]}
    rejected, reason = accept_experiment(still_all_red, baseline, fit_score=2.0)
    assert rejected is False
    assert "windows" in reason
    thin = dict(better, trades=5)
    assert accept_experiment(thin, baseline, fit_score=2.0)[0] is False


def test_holdout_verdict_is_kill_unless_the_sample_clears_every_bar():
    assert holdout_verdict({"trades": 9, "net_points": 10.0, "sharpe": 1.0}) == "KILL"
    assert holdout_verdict({"trades": 10, "net_points": 0.0, "sharpe": 1.0}) == "KILL"
    assert holdout_verdict({"trades": 12, "net_points": 5.0, "sharpe": 0.0}) == "KILL"
    assert holdout_verdict({"trades": 12, "net_points": 5.0, "sharpe": 0.2}) == "SURVIVES_SAMPLE"


def test_pick_move_skips_a_rejected_rung():
    legal = {"trail_amount_up": "widen", "stop": "stop"}
    choice = {"probabilities": {"trail_amount_up": 0.7, "stop": 0.3}}
    assert pick_move(choice, legal, set()) == "trail_amount_up"
    assert pick_move(choice, legal, {"trail_amount_up"}) == "stop"
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
