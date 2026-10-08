"""Tests for research/backtest_sector_reversal.py.

Each test pins a way this backtest could manufacture or hide a reversal
edge: a futures roll read as a price move, an expiring contract "held"
overnight, a sector mean that leaks the stock's own industry trend, a book
that is not dollar-neutral, or turnover that escapes its cost.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research import backtest_sector_reversal as bsr

D1, D2, D3 = date(2026, 3, 25), date(2026, 3, 26), date(2026, 3, 27)
MAR, APR = date(2026, 3, 26), date(2026, 4, 28)   # D2 is March expiry day


def _stf(rows):
    return pd.DataFrame(rows, columns=["date", "symbol", "expiry", "close",
                                       "lot_size", "volume"])


def test_roll_is_not_a_return():
    # April trades 1 % rich to March (carry). Price is FLAT in both contracts.
    # A stitched front-month series would show +1 % on the day after the
    # roll — a fake move a reversal signal would then fade.
    stf = _stf([
        (D1, "A", MAR, 100.0, 10, 5), (D1, "A", APR, 101.0, 10, 5),
        (D2, "A", MAR, 100.0, 10, 5), (D2, "A", APR, 101.0, 10, 5),
        (D3, "A", APR, 101.0, 10, 5),
    ])
    r = bsr.same_contract_returns(stf).set_index("date")["ret"]
    assert r.loc[D2] == pytest.approx(0.0)
    assert r.loc[D3] == pytest.approx(0.0)


def test_expiry_day_position_is_in_the_contract_that_survives():
    # At the D2 close (March expiry day) the book must hold APRIL; the D3
    # return is April's move (+2 %), not a NaN from the dead March contract.
    stf = _stf([
        (D1, "A", MAR, 100.0, 10, 5), (D1, "A", APR, 101.0, 10, 5),
        (D2, "A", MAR, 100.0, 10, 5), (D2, "A", APR, 100.0, 10, 5),
        (D3, "A", APR, 102.0, 10, 5),
    ])
    p = bsr.same_contract_returns(stf).set_index("date")
    assert p.loc[D2, "front"] == APR
    assert p.loc[D3, "ret"] == pytest.approx(0.02)


def _two_day(p1, lot1, p2, lot2):
    stf = _stf([(D1, "A", APR, p1, lot1, 5), (D2, "A", APR, p2, lot2, 5)])
    return bsr.same_contract_returns(stf).set_index("date").loc[D2, "ret"]


def test_split_is_rescaled_by_the_lot_ratio():
    # 1:10 split: price ÷10, lot ×10. Unadjusted this is a −90 % "loser" the
    # book would buy and a fake −90 % on anyone holding it.
    assert _two_day(9335.0, 125, 938.0, 1250) == pytest.approx(938.0 * 10 / 9335.0 - 1)


def test_routine_lot_revision_is_not_rescaled():
    # NSE revises lots with NO price change. Rescaling here would invent a
    # +20 % move out of a flat day.
    assert _two_day(500.0, 1000, 501.0, 1200) == pytest.approx(0.002)


def test_unexplained_demerger_sized_move_is_dropped_not_traded():
    # Demerger: −43 % with no lot change and no factor in the bhavcopy.
    assert np.isnan(_two_day(4928.90, 75, 2822.35, 75))


def test_genuine_crash_is_kept():
    # IndusInd 2025-03-11 was −27 % on real news: exactly the kind of move a
    # reversal book must see, so it must not be filtered as a corporate action.
    assert _two_day(901.25, 500, 657.10, 500) == pytest.approx(657.10 / 901.25 - 1)


def test_traded_value_counts_contracts_times_lot():
    # TtlTradgVol is in contracts; a lot-blind value would rank a 3,100-lot
    # name 3,100x too illiquid.
    stf = _stf([(D1, "A", APR, 400.0, 3100, 10)])
    v = bsr.same_contract_returns(stf)["value"].iloc[0]
    assert v == pytest.approx(10 * 3100 * 400.0)


def _panel(day_rets, sectors_of):
    """One liquid history for LIQ_WINDOW sessions, then the test day."""
    rows = []
    days = pd.bdate_range("2026-01-01", periods=bsr.LIQ_WINDOW + 1).date
    for i, d in enumerate(days):
        for s, r in day_rets.items():
            rows.append((d, s, r if i == len(days) - 1 else 0.0, 100.0, 10,
                         1e6 if s != "ILLIQ" else 1.0))
    return pd.DataFrame(rows, columns=["date", "symbol", "ret", "close",
                                       "lot_size", "value"]), days[-1]


def test_signal_fades_move_relative_to_own_sector_not_market():
    # Banks all +5 %, one bank only +3 %: relative to its sector it is a
    # LOSER (buy), even though it beat the market. Market-demeaning would
    # short it and quietly bet on the industry trend reversing.
    rets = {"B1": 0.05, "B2": 0.05, "B3": 0.03, "I1": -0.01, "I2": 0.0, "I3": 0.01}
    sectors = {"B1": "Bank", "B2": "Bank", "B3": "Bank", "I1": "IT", "I2": "IT", "I3": "IT"}
    panel, last = _panel(rets, sectors)
    sig = bsr.add_signal(panel, sectors)
    x = sig[sig["date"] == last].set_index("symbol")["x"]
    assert x["B3"] > 0
    assert x["B3"] == pytest.approx(-(0.03 - np.mean([0.05, 0.05, 0.03])))


def test_thin_sector_falls_back_to_market_and_is_flagged():
    rets = {"A1": 0.02, "A2": 0.0, "A3": -0.02, "LONE": 0.04}
    sectors = {"A1": "S", "A2": "S", "A3": "S", "LONE": "Solo"}
    panel, last = _panel(rets, sectors)
    sig = bsr.add_signal(panel, sectors).query("date == @last").set_index("symbol")
    assert bool(sig.loc["LONE", "fallback"]) and not bool(sig.loc["A1", "fallback"])
    assert sig.loc["LONE", "x"] == pytest.approx(-(0.04 - np.mean(list(rets.values()))))


def test_illiquid_and_unmapped_names_are_not_traded():
    rets = {f"S{i}": 0.001 * i for i in range(9)} | {"ILLIQ": 0.2, "NOSECTOR": -0.2}
    sectors = {s: "X" for s in rets if s != "NOSECTOR"}
    panel, last = _panel(rets, sectors)
    sig = bsr.add_signal(panel, sectors).query("date == @last").set_index("symbol")
    assert not sig.loc["ILLIQ", "eligible"]
    assert not sig.loc["NOSECTOR", "eligible"]


def test_book_is_dollar_neutral_and_long_the_losers():
    rets = {f"S{i}": 0.01 * (i - 5) for i in range(10)}
    sectors = {s: "X" for s in rets}
    panel, last = _panel(rets, sectors)
    w = bsr.daily_weights(bsr.add_signal(panel, sectors)).loc[last]
    assert w.sum() == pytest.approx(0.0)
    assert w[w > 0].sum() == pytest.approx(1.0)
    assert w["S0"] > 0 and w["S9"] < 0      # worst performer bought, best sold


def test_every_unit_of_turnover_pays_cost():
    dates = [D1, D2, D3]
    rets = pd.DataFrame({"A": [0.0, 0.01, 0.0], "B": [0.0, -0.01, 0.0]}, index=dates)
    cost = pd.DataFrame(0.001, index=dates, columns=["A", "B"])
    w = pd.DataFrame({"A": [1.0, -1.0], "B": [-1.0, 1.0]}, index=[D1, D2])
    pnl = bsr.simulate(w, rets, cost)
    # D1: open 2 units → cost 0.002; gross 0.01 − (−0.01) = 0.02
    assert pnl.loc[D2, "gross"] == pytest.approx(0.02)
    assert pnl.loc[D2, "cost"] == pytest.approx(0.002)
    # D2: flip both sides → |Δw| = 4 → cost 0.004
    assert pnl.loc[D3, "cost"] == pytest.approx(0.004)
    assert pnl.loc[D3, "net"] == pytest.approx(-0.004)


def test_futures_cost_includes_sell_side_stt():
    # Today's 0.05 % STT on the sell side means the average side cost cannot
    # be below 2.5 bp + 5 bp slippage; a cheaper number means STT went missing.
    panel = pd.DataFrame({"date": [date(2026, 6, 1)], "symbol": ["A"],
                          "close": [1000.0], "lot_size": [500]})
    c = bsr.side_cost_frac(panel).iloc[0, 0]
    assert c > 0.00025 + 0.0005


def test_history_is_charged_the_stt_in_force_then():
    # 0.0125 % → 0.02 % (2024-10-01) → 0.05 % (2026-04-01). Charging today's
    # rate on 2024-25 history overstated cost by up to 2.5x on this term.
    def cost(d):
        p = pd.DataFrame({"date": [d], "symbol": ["A"], "close": [1000.0], "lot_size": [500]})
        return bsr.side_cost_frac(p).iloc[0, 0]
    c24, c25, c26 = cost(date(2024, 9, 30)), cost(date(2025, 6, 2)), cost(date(2026, 6, 1))
    assert c25 - c24 == pytest.approx((0.0002 - 0.000125) / 2)
    assert c26 - c25 == pytest.approx((0.0005 - 0.0002) / 2)


def test_session_return_is_open_to_close_not_the_overnight_gap():
    # Close 100, next open 90, next close 91. The gap is the continuation
    # this trade does not hold; MIS earns the session, 91/90 − 1.
    panel = pd.DataFrame({"date": [D1, D2], "symbol": ["A", "A"], "front": [APR, APR]})
    prices = pd.DataFrame({
        "date": [D2], "symbol": ["A"], "expiry": [APR], "open": [90.0], "close": [91.0],
    })
    got = bsr.next_session_open_to_close(panel, prices)
    assert got.loc[got["date"] == D2, "ret"].iloc[0] == pytest.approx(91.0 / 90.0 - 1)


def test_missing_open_is_not_a_fill():
    # No open print means the order is not sent: no return and no cost,
    # and the day is counted so a silent drop cannot look like a flat fill.
    panel = pd.DataFrame({"date": [D1, D2], "symbol": ["A", "A"], "front": [APR, APR]})
    prices = pd.DataFrame({
        "date": [D2], "symbol": ["A"], "expiry": [APR], "open": [0.0], "close": [91.0],
    })
    ret = bsr.next_session_open_to_close(panel, prices)
    assert np.isnan(ret["ret"].iloc[0])
    w = pd.DataFrame({"A": [1.0], "B": [-1.0]}, index=[D1])
    rets = pd.DataFrame({"A": [np.nan, np.nan], "B": [0.0, 0.01]}, index=[D1, D2])
    pnl = bsr.simulate_mis(w, rets, bsr.MIS_SIDE_NOTIONAL, 0.0)
    assert pnl.loc[D2, "gross"] == pytest.approx(0.01 * -1.0)  # only B traded
    assert pnl.loc[D2, "missing"] == 1
    assert pnl.loc[D2, "cost"] == pytest.approx(bsr.mis_roundtrip_frac(bsr.MIS_SIDE_NOTIONAL, 0.0))


def test_mis_pays_the_round_trip_even_when_the_book_does_not_change():
    # The futures simulator charges |Δweight|, so a repeated book pays
    # nothing the next day. MIS is flat overnight: the next day is a new
    # entry and a new exit.
    w = pd.DataFrame({"A": [1.0, 1.0], "B": [-1.0, -1.0]}, index=[D1, D2])
    rets = pd.DataFrame(0.0, index=[D1, D2, D3], columns=["A", "B"])
    pnl = bsr.simulate_mis(w, rets, bsr.MIS_SIDE_NOTIONAL, 0.0)
    rt = bsr.mis_roundtrip_frac(bsr.MIS_SIDE_NOTIONAL, 0.0)
    assert pnl.loc[D2, "cost"] == pytest.approx(2 * rt)
    assert pnl.loc[D3, "cost"] == pytest.approx(2 * rt)


def test_diversified_mis_book_does_not_get_the_flat_brokerage_cap():
    # The ₹20 cap binds on a ₹10 lakh order and cuts that round trip to
    # about 4 bp. A quintile slice of a ₹10 lakh side is far smaller, and
    # charging the capped rate on it understates the book.
    w = pd.DataFrame({"A": [0.5], "B": [0.5], "C": [-0.5], "D": [-0.5]}, index=[D1])
    rets = pd.DataFrame(0.0, index=[D1, D2], columns=list("ABCD"))
    pnl = bsr.simulate_mis(w, rets, bsr.MIS_SIDE_NOTIONAL, 0.0)
    capped = 2 * bsr.mis_roundtrip_frac(bsr.MIS_SIDE_NOTIONAL, 0.0)
    per_name = bsr.mis_roundtrip_frac(0.5 * bsr.MIS_SIDE_NOTIONAL, 0.0)
    assert pnl.loc[D2, "cost"] == pytest.approx(4 * 0.5 * per_name)
    assert pnl.loc[D2, "cost"] > capped


def test_fixed_order_notional_is_the_size_the_brokerage_cap_sees():
    # The vehicle table's ~4 bp round trip is a ₹10 lakh order. A weight of
    # 0.5 on a ₹10 lakh side is a ₹5 lakh order and a dearer rate. Passing
    # the ₹10 lakh as the order size is the only way to price that table.
    w = pd.DataFrame({"A": [0.5], "B": [-0.5]}, index=[D1])
    rets = pd.DataFrame(0.0, index=[D1, D2], columns=["A", "B"])
    pnl = bsr.simulate_mis(
        w, rets, bsr.MIS_SIDE_NOTIONAL, 0.0, order_notional=bsr.MIS_NAME_NOTIONAL)
    rt = bsr.mis_roundtrip_frac(bsr.MIS_NAME_NOTIONAL, 0.0)
    assert pnl.loc[D2, "cost"] == pytest.approx(rt)


def test_slippage_is_five_bp_per_side_in_total_not_seven():
    # core.costs already includes 2 bp/side; adding 5 more double-counted it.
    from core.costs import estimate_transaction_cost
    p = pd.DataFrame({"date": [date(2026, 6, 1)], "symbol": ["A"], "close": [1000.0], "lot_size": [500]})
    notional = 1000.0 * 500
    model = (estimate_transaction_cost(1000.0, 1, 500, "BUY", "FUT")
             + estimate_transaction_cost(1000.0, 1, 500, "SELL", "FUT")) / 2 / notional
    assert bsr.side_cost_frac(p).iloc[0, 0] - model == pytest.approx(0.0003)
