"""The options expression is priced by a model, not a tape. These pin the
pieces a wrong verdict would hide in: the pricer, the IV round trip, the
contract choice (no 0-DTE), the dated STT, and the stop when the model
does not match the bhavcopy."""
from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from research import options_breakout as ob
from research.adx_breakout import Trade


def test_black76_put_call_parity_and_expiry_is_intrinsic():
    f, k, t, s = 24700.0, 24650.0, 3 / 365, 0.13
    call = float(ob.black76(f, k, t, s, 1))
    put = float(ob.black76(f, k, t, s, -1))
    assert call - put == pytest.approx(np.exp(-ob.RATE * t) * (f - k), rel=1e-9)
    assert 100 < call < 200                    # a 3-day ATM weekly near 13% IV
    assert float(ob.black76(f, k, 0.0, s, 1)) == pytest.approx(50.0)
    assert float(ob.black76(f, k, 0.0, s, -1)) == 0.0


def test_implied_vol_round_trips_and_rejects_impossible_prices():
    t = 2 / 365
    for cp in (1, -1):
        price = float(ob.black76(24700, 24700, t, 0.17, cp))
        assert float(ob.implied_vol(price, 24700, 24700, t, cp)) == pytest.approx(0.17, abs=1e-6)
    # Below intrinsic: no volatility explains it.
    assert np.isnan(float(ob.implied_vol(10.0, 24800, 24700, t, 1)))


def test_time_runs_to_1530_on_expiry_day():
    exp = date(2025, 6, 12)
    assert ob.years_to_expiry(datetime(2025, 6, 12, 15, 30), exp) == 0.0
    assert ob.years_to_expiry(datetime(2025, 6, 12, 9, 30), exp) == pytest.approx(
        6 * 3600 / ob.YEAR_SECONDS)


def test_option_stt_is_dated_and_spread_replaces_core_slippage():
    """A 2025 exit paid 0.1% STT on premium, not core's 0.15%; core's 0.05%
    slippage is replaced by the half-spread in points."""
    sell25 = ob.option_cost(100.0, 75, True, date(2025, 6, 2), half_spread=0.0)
    sell26 = ob.option_cost(100.0, 75, True, date(2026, 6, 2), half_spread=0.0)
    assert sell26 - sell25 == pytest.approx(100 * 75 * 0.0005)
    buy0 = ob.option_cost(100.0, 75, False, date(2025, 6, 2), half_spread=0.0)
    buy1 = ob.option_cost(100.0, 75, False, date(2025, 6, 2), half_spread=1.0)
    assert buy1 - buy0 == pytest.approx(75.0)
    assert ob.opt_stt_rate(date(2024, 9, 30)) == 0.000625


def _chain(day, expiries, strikes, close=100.0):
    rows = [{"date": day, "expiry": e, "strike": k, "cp": cp, "close": close, "high": close,
             "low": close, "trades": 10.0, "lot": 75}
            for e in expiries for k in strikes for cp in (1, -1)]
    return pd.DataFrame(rows)


def _sample(day, entry_px, exit_px):
    return pd.DataFrame({"date": [day, day], "hhmm": [1000, 1100],
                         "close": [entry_px, exit_px]})


def test_contract_is_next_expiry_never_0dte_and_strike_nearest_forward():
    prior, entry = date(2025, 6, 11), date(2025, 6, 12)            # 12th is an expiry
    options = _chain(prior, [date(2025, 6, 12), date(2025, 6, 19)], [24600.0, 24650.0, 24700.0])
    closes = pd.Series({prior: 24640.0, entry: 24690.0})
    carry = pd.Series({prior: 0.0})
    trade = Trade("LONG", entry, entry, 0, 1, 24690.0, 24740.0, "TIME", 50.0, 40.0, 10.0, 75, 12.0)
    out = ob.express(trade, _sample(entry, 24690.0, 24740.0), options.groupby("date"),
                     closes, carry)
    assert out.expiry == date(2025, 6, 19)      # not the one expiring on entry day
    assert out.strike == 24700.0 and out.cp == 1
    assert out.exit_prem > out.entry_prem       # index up 50, call gains at fixed IV
    short = Trade("SHORT", entry, entry, 0, 1, 24690.0, 24740.0, "STOP", -50.0, -60.0, 10.0, 75, 12.0)
    put = ob.express(short, _sample(entry, 24690.0, 24740.0), options.groupby("date"),
                     closes, carry)
    assert put.cp == -1 and put.exit_prem < put.entry_prem


def test_no_prior_chain_means_unpriced_not_guessed():
    entry = date(2025, 6, 12)
    closes = pd.Series({date(2025, 6, 11): 24640.0, entry: 24690.0})
    trade = Trade("LONG", entry, entry, 0, 1, 24690.0, 24740.0, "TIME", 50.0, 40.0, 10.0, 75, 12.0)
    empty = _chain(date(2025, 6, 10), [date(2025, 6, 19)], [24700.0])
    assert ob.express(trade, _sample(entry, 24690.0, 24740.0), empty.groupby("date"),
                      closes, pd.Series({date(2025, 6, 11): 0.0})) is None


def test_verdict_needs_every_gate():
    base = {"trades": 150, "net_rupees": 1000.0, "sharpe": 0.5, "net_at_cost_stress": 10.0,
            "windows": [5.0, -1.0, 2.0]}
    assert ob.verdict(base) == "PASSES_TRAIN"
    for key, bad in (("trades", 99), ("net_rupees", -1.0), ("sharpe", 0.0),
                     ("net_at_cost_stress", -1.0), ("windows", [-1.0, -2.0, -3.0])):
        assert ob.verdict({**base, key: bad}) == "KILL"


def test_model_check_recovers_a_consistent_chain():
    """When the next session's bhavcopy is what the model itself would print
    (index flat, IV unchanged), the measured close error is near zero, so a
    large error in the real run means the model, not the plumbing."""
    p, d, exp = date(2025, 6, 9), date(2025, 6, 10), date(2025, 6, 12)
    spot, k, iv = 24700.0, 24700.0, 0.14
    t_p = ob.years_to_expiry(datetime(2025, 6, 9, 15, 30), exp)
    t_d = ob.years_to_expiry(datetime(2025, 6, 10, 15, 30), exp)
    rows = []
    for day, t in ((p, t_p), (d, t_d)):
        for cp in (1, -1):
            px = float(ob.black76(spot, k, t, iv, cp))
            rows.append({"date": day, "expiry": exp, "strike": k, "cp": cp, "close": px,
                         "high": px * 1.5, "low": px, "trades": 10.0, "lot": 75})
    options = pd.DataFrame(rows)
    bars = pd.DataFrame({"date": [p, d, d], "hhmm": [1530, 1000, 1530],
                         "high": spot, "low": spot, "close": spot})
    check = ob.model_check(options, bars, pd.Series({p: 0.0, d: 0.0}), {d})
    assert len(check) == 2
    assert check["close_err"].abs().max() < 1e-6


def test_a_check_with_nothing_to_compare_stops_the_run():
    """The stop must fail closed: NaN > limit is False, so a NaN median
    used to wave an unvalidated model through to a verdict."""
    assert ob.model_fails(ob.check_summary(pd.DataFrame()))
    nan_rows = pd.DataFrame({"close_err": [np.nan], "high_err": [np.nan], "dte": [2],
                             "close_err_pts": [np.nan], "high_err_pts": [np.nan]})
    assert ob.model_fails(ob.check_summary(nan_rows))
    good = pd.DataFrame({"close_err": [0.05, -0.04], "high_err": [0.0, 0.0], "dte": [2, 3],
                         "close_err_pts": [5.0, -4.0], "high_err_pts": [0.0, 0.0]})
    assert not ob.model_fails(ob.check_summary(good))
    assert ob.model_fails(ob.check_summary(good.assign(close_err=[0.2, -0.2])))


def test_one_sided_chain_is_unpriced_not_a_crash():
    prior, entry = date(2025, 6, 10), date(2025, 6, 11)
    options = _chain(prior, [date(2025, 6, 12)], [24700.0])
    options = options[options["cp"] == -1]                      # only puts traded
    closes = pd.Series({prior: 24690.0, entry: 24690.0})
    trade = Trade("LONG", entry, entry, 0, 1, 24690.0, 24740.0, "TIME", 50.0, 40.0, 10.0, 75, 12.0)
    assert ob.express(trade, _sample(entry, 24690.0, 24740.0), options.groupby("date"),
                      closes, pd.Series({prior: 0.0})) is None


def test_exit_after_the_option_settled_is_unpriced():
    prior, entry, exp, nxt = date(2025, 6, 10), date(2025, 6, 11), date(2025, 6, 12), date(2025, 6, 13)
    options = _chain(prior, [exp], [24700.0])
    closes = pd.Series({prior: 24690.0, entry: 24690.0, exp: 24700.0, nxt: 24740.0})
    sample = pd.DataFrame({"date": [entry, nxt], "hhmm": [1500, 1000],
                           "close": [24690.0, 24740.0]})
    trade = Trade("LONG", entry, nxt, 0, 1, 24690.0, 24740.0, "TIME", 50.0, 40.0, 10.0, 75, 12.0)
    assert ob.express(trade, sample, options.groupby("date"), closes,
                      pd.Series({prior: 0.0})) is None


def test_carry_keeps_the_eve_of_expiry_and_carries_over_expiry_day():
    """Only expiry day (t = 0) has no carry of its own. Dropping the eve too
    left trades on expiry day and the day after unpriced, every month."""
    eve, exp, after = date(2025, 6, 25), date(2025, 6, 26), date(2025, 6, 27)
    front = pd.DataFrame({"date": [eve, exp, after],
                          "expiry": [exp, exp, date(2025, 7, 31)],
                          "close": [24710.0, 24700.0, 24800.0]})
    index_close = pd.Series({eve: 24700.0, exp: 24700.0, after: 24700.0})
    carry = ob.carry_by_date(front, index_close)
    assert list(carry.index) == [eve, exp, after]
    assert carry[exp] == carry[eve] > 0


def test_model_check_skips_the_contract_expiring_that_day():
    """The strategy never holds a contract on its expiry day, so the check
    must not score one either."""
    p, d = date(2025, 6, 11), date(2025, 6, 12)
    rows = []
    for exp, px in ((d, 40.0), (date(2025, 6, 19), 150.0)):
        for day in (p, d):
            for cp in (1, -1):
                rows.append({"date": day, "expiry": exp, "strike": 24700.0, "cp": cp,
                             "close": px, "high": px, "low": px, "trades": 10.0, "lot": 75})
    bars = pd.DataFrame({"date": [p, d, d], "hhmm": [1530, 1000, 1530],
                         "high": 24700.0, "low": 24700.0, "close": 24700.0})
    check = ob.model_check(pd.DataFrame(rows), bars, pd.Series({p: 0.0, d: 0.0}), {d})
    assert set(check["expiry"]) == {date(2025, 6, 19)}


def test_a_fill_is_priced_at_its_bar_start_not_its_close():
    """``hhmm`` is the bar close; a fill at the bar's open has 5 more
    minutes to expiry than the close stamp implies."""
    assert ob._bar_start(date(2025, 6, 12), 1005) == datetime(2025, 6, 12, 10, 0)
