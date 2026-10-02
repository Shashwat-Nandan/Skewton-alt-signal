"""Cash rules for the research-only Nifty dispersion books.

The tests encode why a Bloch-style replay on NSE bhavcopy is not the
paper's half-spread study. A failure means one of these changed:

- the index straddle stays short even when M_rho is above 1
- whole lots below one are dropped and their weight stays in the denominator
- Book B's two cuts are the fixed ones, and the roll day does not move them
- exercise STT is the purchaser's, on long intrinsic only, and only on the
  hold-to-expiry exit
- a flatten at two days is a traded close, with the pinned 0.685% option
  half-spread instead of the 5 bp inside core.costs
- a Nifty weekly is not paired with monthly stock options
- daily bhavcopy announces that it is not a 5-minute go/no-go
"""
from __future__ import annotations

import logging
import math
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.backtest_timeframe import STANDARD_TIMEFRAME
from core.costs import estimate_transaction_cost
from core.greeks_engine import GreeksEngine
from research.backtest_dispersion import (
    EXERCISE_STT,
    FLATTEN_DTE,
    INDEX,
    M_RHO_MIN_HISTORY,
    M_RHO_QUANTILE,
    MAX_INDEX_LOTS,
    MIN_COVERED_WEIGHT,
    MIN_SHARED_NAMES,
    MONEYNESS_HI,
    MONEYNESS_LO,
    PINNED_OPT_SLIPPAGE,
    RISK_FREE,
    RV_WINDOW,
    Chain,
    Quote,
    _Leg,
    atm_strike,
    book_b_open,
    build_cycle_rows,
    choose_lots,
    dispersion_gamma_pnl,
    exercise_stt,
    future_order_cost,
    futures_hedge_lots,
    load_chain,
    main,
    option_order_cost,
    read_weights,
    realised_vol,
    roll_dates,
    run,
    simulate_cycle,
    _straddle_delta,
)

_ENG = GreeksEngine(risk_free_rate=RISK_FREE)


def _bs(spot, strike, dte, iv):
    t = dte / 365.0
    return (
        _ENG.bs_price(spot, strike, t, iv, "CE"),
        _ENG.bs_price(spot, strike, t, iv, "PE"),
    )


def _leg(symbol, side, strike=100.0, lots=1, lot_size=1, weight=0.0, iv=0.2):
    return _Leg(symbol, strike, lots, lot_size, side, weight, iv)


def _q(ce, pe, spot=100.0, lot=1):
    return Quote(ce, pe, spot, lot)


def test_registered_cuts_are_not_search_parameters():
    # These are the pre-registered replay rules. A grid search after a
    # losing sign check would show up as a change here.
    assert PINNED_OPT_SLIPPAGE == 0.00685
    assert EXERCISE_STT == 0.0015
    assert MIN_COVERED_WEIGHT == 0.30
    assert MAX_INDEX_LOTS == 200
    assert FLATTEN_DTE == 2
    assert MIN_SHARED_NAMES == 20
    assert RV_WINDOW == 20
    assert M_RHO_MIN_HISTORY == 20
    assert M_RHO_QUANTILE == 0.80
    assert MONEYNESS_LO == 0.85
    assert MONEYNESS_HI == 1.15


def test_option_slippage_is_the_measured_half_spread_not_five_bp():
    # price 100, one lot of 50. Turnover 5,000. The live helper embeds
    # 5 bp (2.50). This book replaces that slice with 0.685% (34.25).
    price, lots, lot = 100.0, 1, 50
    turnover = price * lots * lot
    for side, expected in (("SELL", 68.4829), ("BUY", 61.1329)):
        raw = estimate_transaction_cost(price, lots, lot, side, "OPT")
        got = option_order_cost(price, lots, lot, side)
        assert got == pytest.approx(
            raw - turnover * 0.0005 + turnover * PINNED_OPT_SLIPPAGE
        )
        assert got == pytest.approx(expected)
    # A straddle is two orders. One order at the combined premium pays one
    # brokerage; the two legs pay two.
    two_legs = option_order_cost(10, 1, 1, "SELL") + option_order_cost(10, 1, 1, "SELL")
    one_order = option_order_cost(20, 1, 1, "SELL")
    assert two_legs > one_order


def test_exercise_stt_is_the_purchasers_only():
    # Long ITM: 0.15% of intrinsic. Short ITM, and anything with no
    # intrinsic, pays nothing. The short index leg is the one that is
    # often deep in the money at settlement.
    assert exercise_stt(110, 100, 1, 50, side=1) == pytest.approx(0.0015 * 10 * 50)
    assert exercise_stt(130, 100, 2, 65, side=-1) == 0.0
    assert exercise_stt(100, 100, 1, 50, side=1) == 0.0


def test_sub_lot_weight_stays_in_the_denominator():
    # Per index lot the heavy name is 40 shares of a 100-share lot, so
    # three index lots clear one stock lot. The light name is 5 shares
    # and still floors to zero. GONE has no quote. Covered weight is the
    # heavy name's 0.40, not 1 and not 0.40/0.45.
    sized = choose_lots(
        100.0, 100,
        {"HEAVY": 0.40, "LIGHT": 0.05, "GONE": 0.55},
        {"HEAVY": 100.0, "LIGHT": 100.0},
        {"HEAVY": 100, "LIGHT": 100},
    )
    assert sized is not None
    n, lots, covered = sized
    assert n == 3
    assert lots == {"HEAVY": 1}
    assert covered == pytest.approx(0.40)

    # 0.40 * 65 * 25000 / 1000 = 650 shares, two lots of 250, at one index lot.
    # The 2% name is 23 shares and is dropped. Its weight is not given away.
    sized = choose_lots(
        25000.0, 65,
        {"HEAVY": 0.40, "TINY": 0.02},
        {"HEAVY": 1000.0, "TINY": 1400.0},
        {"HEAVY": 250, "TINY": 250},
    )
    assert sized is not None
    n, lots, covered = sized
    assert n == 1
    assert lots == {"HEAVY": 2}
    assert covered == pytest.approx(0.40)

    # A name that is only 20% of the book never reaches the 30% floor,
    # however many index lots are used. Do not renormalise it up to 100%.
    assert choose_lots(
        100.0, 1, {"ONLY": 0.20, "REST": 0.80}, {"ONLY": 100.0}, {"ONLY": 1},
    ) is None


def test_book_b_uses_prior_history_only():
    # Sixteen calm points and four rich ones. The 80th percentile is 0.58.
    # 0.55 is below it. Appending the roll day pulls that percentile down
    # to 0.55 and would open the gate, so the roll day must not be in the
    # history the caller passes.
    prior = [0.5] * 16 + [0.9] * 4
    assert book_b_open(0.20, 0.20, 0.90, prior) is False  # IV must exceed RV
    assert book_b_open(0.30, None, 0.90, prior) is False
    assert book_b_open(0.30, 0.20, None, prior) is False
    assert book_b_open(0.30, 0.20, 0.90, prior[:19]) is False
    assert book_b_open(0.30, 0.20, 0.55, prior) is False
    assert book_b_open(0.30, 0.20, 0.55, prior + [0.55]) is True
    assert book_b_open(0.30, 0.20, 0.60, prior) is True


def test_atm_strike_rejects_untraded_and_off_band_quotes():
    spot = 100.0
    quotes = [
        (70.0, 1.0, 1.0, 10.0, 10.0),    # moneyness 0.70, outside 0.85–1.15
        (100.0, 50.0, 50.0, 0.0, 10.0),  # closer, but the call did not trade
        (101.0, 0.0, 1.0, 10.0, 10.0),   # theoretical close, last price style zero
        (105.0, 2.0, 2.0, 10.0, 10.0),
        (120.0, 1.0, 1.0, 10.0, 10.0),   # moneyness 1.20
    ]
    assert atm_strike(spot, quotes) == 105.0
    assert atm_strike(0.0, quotes) is None


def test_first_archive_session_is_not_an_entry():
    d0, d1, d2 = date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 30)
    front = {d0: date(2026, 1, 29), d1: date(2026, 1, 29), d2: date(2026, 2, 26)}
    assert roll_dates([d0, d1, d2], front) == [d2]
    assert roll_dates([d0], {d0: date(2026, 1, 29)}) == []


def test_gamma_split_is_attribution_not_a_second_cash_ledger():
    theta_b = -2.0
    theta_i = {"A": -1.0, "B": -1.0}
    weights = {"A": 0.5, "B": 0.5}
    sigmas = {"A": 0.2, "B": 0.2}
    # Realised the implied move and the single correlation: both terms vanish.
    for moves, rho in (
        ({"A": 1.0, "B": 1.0}, 1.0),
        ({"A": -1.0, "B": -1.0}, 1.0),
        ({"A": 1.0, "B": -1.0}, -1.0),
    ):
        diag, off = dispersion_gamma_pnl(
            theta_b, theta_i, weights, sigmas, 0.2, moves, rho,
        )
        assert diag == pytest.approx(0.0)
        assert off == pytest.approx(0.0)
    # No move, positive correlation, long-straddle theta negative: the
    # short index earns the correlation term. This is not rupees of premium.
    _, off = dispersion_gamma_pnl(
        theta_b, theta_i, weights, sigmas, 0.2, {"A": 0.0, "B": 0.0}, 0.5,
    )
    assert off > 0


def _two_day_path(entry_q, exit_q, dte_exit, spots_exit, futs=None):
    spots0 = {s: q.spot for s, q in entry_q.items()}
    return [
        (date(2026, 4, 1), 10, entry_q, (futs or ({}, {}))[0], spots0),
        (date(2026, 4, 28), dte_exit, exit_q, (futs or ({}, {}))[1], spots_exit),
    ]


def test_flatten_books_the_short_index_and_the_pinned_slippage():
    # Short index 10/10 -> 4/4. Long stock 3/3 -> 5/5. One lot, lot size 1.
    # Premium is (20-8) + (10-6) = 16. The long stock gaining is not a short.
    entry = {INDEX: _q(10, 10), "AAA": _q(3, 3)}
    exit_q = {INDEX: _q(4, 4), "AAA": _q(5, 5)}
    legs = [_leg(INDEX, -1), _leg("AAA", +1, weight=1.0)]
    sim = simulate_cycle(
        legs, _two_day_path(entry, exit_q, 2, {"NIFTY": 100.0, "AAA": 100.0}),
        exit_mode="flatten", hedge=False, rho=0.5, sigma_b=0.2,
    )
    assert sim is not None and sim[-1] == "ok"
    _, premium, fut, costs, stt, net, diag, off, _ = sim
    orders = [
        (10, "SELL"), (10, "SELL"), (3, "BUY"), (3, "BUY"),
        (4, "BUY"), (4, "BUY"), (5, "SELL"), (5, "SELL"),
    ]
    raw = sum(estimate_transaction_cost(px, 1, 1, side, "OPT") for px, side in orders)
    extra = sum(px * (PINNED_OPT_SLIPPAGE - 0.0005) for px, _ in orders)
    assert premium == pytest.approx(16.0)
    assert fut == 0.0
    assert stt == 0.0
    assert diag == 0.0 and off == 0.0
    assert costs == pytest.approx(raw + extra)
    assert net == pytest.approx(premium + fut - costs)


def test_hold_to_expiry_uses_intrinsic_and_charges_only_the_long_leg():
    # A zero close sits on the expiry row on purpose. Settlement must use
    # the spot. Index finishes 30 in the money and pays no exercise STT.
    # The long stock finishes 10 in the money and pays 0.15% of that.
    entry = {INDEX: _q(10, 10, spot=100), "AAA": _q(3, 3, spot=100)}
    decoy = {INDEX: _q(0, 0, spot=130), "AAA": _q(0, 0, spot=110)}
    spots = {INDEX: 130.0, "AAA": 110.0}
    path = [
        (date(2026, 4, 1), 10, entry, {}, {INDEX: 100.0, "AAA": 100.0}),
        (date(2026, 4, 29), 1, decoy, {}, spots),
        (date(2026, 4, 30), 0, decoy, {}, spots),
    ]
    legs = [_leg(INDEX, -1), _leg("AAA", +1)]
    # The expiry session has to be on the path. Stopping at 1 DTE is not a hold.
    assert simulate_cycle(
        legs, path[:2], exit_mode="expiry", hedge=False, rho=0.5, sigma_b=0.2,
    ) is None
    sim = simulate_cycle(
        legs, path, exit_mode="expiry", hedge=False, rho=0.5, sigma_b=0.2,
    )
    assert sim is not None and sim[-1] == "ok"
    _, premium, fut, costs, stt, net, _, _, _ = sim
    # Short: intrinsic 30 against entry premium 20 → -10. Long: 10 against 6 → +4.
    assert premium == pytest.approx(-6.0)
    assert fut == 0.0
    assert stt == pytest.approx(0.0015 * 10)
    entry_costs = (
        option_order_cost(10, 1, 1, "SELL") * 2
        + option_order_cost(3, 1, 1, "BUY") * 2
    )
    assert costs == pytest.approx(entry_costs + stt)
    assert net == pytest.approx(premium + fut - costs)
    # A missing settlement spot is an unfilled cycle, not a zero-price expiry.
    broken = dict(spots)
    del broken["AAA"]
    path[-1] = (date(2026, 4, 30), 0, decoy, {}, broken)
    missed = simulate_cycle(
        legs, path, exit_mode="expiry", hedge=False, rho=0.5, sigma_b=0.2,
    )
    assert missed is not None
    assert len(missed) == 9
    assert missed[-1] == "no_settlement_spot"
    assert missed[1:8] == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_flatten_does_not_skip_an_untraded_exit_to_a_later_close():
    entry = {INDEX: _q(10, 10), "AAA": _q(3, 3)}
    incomplete = {INDEX: _q(4, 4)}  # the stock strike did not trade
    later = {INDEX: _q(1, 1), "AAA": _q(1, 1)}
    path = [
        (date(2026, 4, 1), 5, entry, {}, {INDEX: 100.0, "AAA": 100.0}),
        (date(2026, 4, 28), 2, incomplete, {}, {INDEX: 100.0, "AAA": 100.0}),
        (date(2026, 4, 29), 1, later, {}, {INDEX: 100.0, "AAA": 100.0}),
    ]
    legs = [_leg(INDEX, -1), _leg("AAA", +1)]
    sim = simulate_cycle(
        legs, path, exit_mode="flatten", hedge=False, rho=0.5, sigma_b=0.2,
    )
    assert sim is not None
    assert sim[0] == date(2026, 4, 28)
    assert sim[-1] == "no_traded_exit"
    assert sim[1:8] == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_futures_hedge_lots_is_the_replay_rounding():
    """The paper book calls this helper. It has to be the same rounding
    the hold-to-expiry replay uses, including a missing IV meaning no hedge."""
    assert futures_hedge_lots(200.0, 100.0, 10, 0.30, 1, 50, 1, 50) == -1
    shares = _straddle_delta(200.0, 100.0, 10, 0.30, 1, 50, 1)
    assert futures_hedge_lots(200.0, 100.0, 10, 0.30, 1, 50, 1, 50) == int(round(-shares / 50))
    assert futures_hedge_lots(200.0, 100.0, 10, 0.0, 1, 50, 1, 50) == 0


def test_future_hedge_is_marked_and_flattened_at_the_exit():
    # Deep in-the-money straddle, delta about +1. One option lot is 50
    # shares and the future's lot is 50, so the hedge is short one future
    # and buys it back on the exit day.
    leg = _leg("AAA", +1, strike=100.0, lots=1, lot_size=50, weight=1.0, iv=0.30)
    entry_q = {"AAA": Quote(10.0, 1.0, 200.0, 50)}
    exit_q = {"AAA": Quote(10.0, 1.0, 200.0, 50)}
    path = [
        (date(2026, 4, 1), 10, entry_q, {"AAA": (150.0, 50)}, {"AAA": 200.0}),
        (date(2026, 4, 28), 2, exit_q, {"AAA": (155.0, 50)}, {"AAA": 200.0}),
    ]
    sim = simulate_cycle(
        [leg], path, exit_mode="flatten", hedge=True, rho=0.5, sigma_b=0.30,
    )
    assert sim is not None and sim[-1] == "ok"
    _, premium, fut, costs, stt, net, _, _, _ = sim
    assert fut == pytest.approx(-1 * 50 * (155.0 - 150.0))
    assert stt == 0.0
    fut_costs = (
        future_order_cost(150.0, 1, 50, "SELL")
        + future_order_cost(155.0, 1, 50, "BUY")
    )
    opt_costs = (
        option_order_cost(10.0, 1, 50, "BUY") + option_order_cost(1.0, 1, 50, "BUY")
        + option_order_cost(10.0, 1, 50, "SELL") + option_order_cost(1.0, 1, 50, "SELL")
    )
    assert costs == pytest.approx(opt_costs + fut_costs)
    assert net == pytest.approx(premium + fut - costs)
    # Same path with the hedge off does not trade the future.
    bare = simulate_cycle(
        [leg], path, exit_mode="flatten", hedge=False, rho=0.5, sigma_b=0.30,
    )
    assert bare is not None
    assert bare[2] == 0.0
    assert bare[3] == pytest.approx(opt_costs)


def test_correlation_term_is_reported_only_on_the_hedged_path():
    # Two long names, spots unchanged, so the standardised move is zero.
    # The off-diagonal of a short index is then the correlation term, and
    # it is not part of the unhedged cash result.
    entry = {
        INDEX: _q(5, 5),
        "AAA": _q(2, 2),
        "BBB": _q(2, 2),
    }
    path = _two_day_path(entry, entry, 2, {INDEX: 100.0, "AAA": 100.0, "BBB": 100.0})
    legs = [
        _leg(INDEX, -1, iv=0.2),
        _leg("AAA", +1, weight=0.5, iv=0.2),
        _leg("BBB", +1, weight=0.5, iv=0.2),
    ]
    hedged = simulate_cycle(
        legs, path, exit_mode="flatten", hedge=True, rho=0.5, sigma_b=0.2,
    )
    bare = simulate_cycle(
        legs, path, exit_mode="flatten", hedge=False, rho=0.5, sigma_b=0.2,
    )
    assert hedged is not None and bare is not None
    assert hedged[7] > 0
    assert bare[6] == 0.0 and bare[7] == 0.0


def _put(books, quotes, spots, day, sym, strike, ce, pe, spot, lot=1):
    books[(day, sym)] = [(float(strike), ce, pe, 1.0, 1.0)]
    quotes[(day, sym, float(strike))] = Quote(ce, pe, spot, lot)
    spots[(day, sym)] = spot


def _atm(books, quotes, spots, day, sym, spot, iv, expiry, lot=1):
    ce, pe = _bs(spot, spot, (expiry - day).days, iv)
    _put(books, quotes, spots, day, sym, spot, ce, pe, spot, lot)


def test_book_a_stays_short_the_index_when_m_rho_is_above_one():
    # Index IV 0.40 against stock IV 0.20. M_rho is about 4. The package
    # still sells the index. The index premium then collapses and the
    # stock premiums do not, so premium P&L is positive only because the
    # index leg is short.
    d0, d1, d2 = date(2026, 3, 31), date(2026, 4, 1), date(2026, 4, 28)
    exp0, exp = date(2026, 3, 31), date(2026, 4, 30)
    books, quotes, spots = {}, {}, {}
    for sym, iv in ((INDEX, 0.40), ("AAA", 0.20), ("BBB", 0.20)):
        _atm(books, quotes, spots, d1, sym, 100.0, iv, exp)
    entry_i = quotes[(d1, INDEX, 100.0)]
    for sym in (INDEX, "AAA", "BBB"):
        q = quotes[(d1, sym, 100.0)]
        ce, pe = (1.0, 1.0) if sym == INDEX else (q.ce, q.pe)
        _put(books, quotes, spots, d2, sym, 100.0, ce, pe, 100.0)
    chain = Chain(
        sessions=[d0, d1, d2],
        front={d0: exp0, d1: exp, d2: exp},
        quotes=quotes, books=books, futures={}, spot=spots,
    )
    rows = build_cycle_rows(
        chain, {"AAA": 0.5, "BBB": 0.5}, weighting="equal",
        index_closes=[], close_dates=[],
    )
    frame = pd.DataFrame([r.__dict__ for r in rows])
    filled = frame[(frame.book == "A") & (frame.exit_mode == "flatten") & (frame.status == "ok")]
    assert len(filled) == 2
    assert (filled.m_rho > 1).all()
    assert (filled.index_lots == 2).all()
    assert (filled.n_names == 2).all()
    assert filled.covered_weight.iloc[0] == pytest.approx(1.0)
    # Two index lots, exit straddle priced at 2. Stock marks unchanged.
    entry_px = (entry_i.ce + entry_i.pe) * 2
    assert filled.premium_pnl.iloc[0] == pytest.approx(entry_px - 4.0)
    assert (filled.premium_pnl > 0).all()
    assert (filled.exercise_stt == 0).all()
    assert np.allclose(filled.net, filled.premium_pnl + filled.futures_pnl - filled.costs)
    bare = filled[filled.hedge == "none"].iloc[0]
    assert bare.diagonal == 0.0 and bare.off_diagonal == 0.0
    closed = frame[frame.book == "B"]
    assert set(closed.status) == {"gate_closed"}
    # The first session is not a cycle, and there is no hold without the expiry.
    assert set(frame.entry) == {d1}
    held = frame[(frame.book == "A") & (frame.exit_mode == "expiry")]
    assert set(held.status) == {"no_entry_mark"}


def test_roll_day_m_rho_does_not_open_book_b():
    # Twenty prior M_rho values: sixteen at 0.50 and four at 0.90. The roll
    # day is 0.55, below the 80th percentile. IV is above a flat market's
    # realised vol, so the only thing keeping Book B shut is that percentile.
    # Folding the roll day into the history would open it.
    start = date(2026, 1, 5)
    days = [start + timedelta(days=i) for i in range(22)]
    roll = days[-1]
    exp_a, exp_b = date(2026, 6, 25), date(2026, 3, 28)
    flat = date(2026, 3, 26)
    assert (exp_b - flat).days == FLATTEN_DTE
    books, quotes, spots = {}, {}, {}
    targets = {}
    for i, day in enumerate(days[1:-1], start=1):
        targets[day] = 0.5 if i <= 16 else 0.9
    targets[roll] = 0.55
    for day, rho in targets.items():
        expiry = exp_b if day == roll else exp_a
        _atm(books, quotes, spots, day, INDEX, 100.0, 0.20 * math.sqrt(rho), expiry)
        _atm(books, quotes, spots, day, "AAA", 100.0, 0.20, expiry)
        _atm(books, quotes, spots, day, "BBB", 100.0, 0.20, expiry)
    for sym in (INDEX, "AAA", "BBB"):
        _put(books, quotes, spots, flat, sym, 100.0, 1.0, 1.0, 100.0)
    front = {day: exp_a for day in days[:-1]}
    front[roll] = exp_b
    front[flat] = exp_b
    chain = Chain(
        sessions=days + [flat], front=front, quotes=quotes, books=books,
        futures={}, spot=spots,
    )
    closes = [100.0] * 22
    close_dates = [roll - timedelta(days=21 - i) for i in range(21)] + [roll]
    assert realised_vol(closes[:21]) == pytest.approx(0.0)
    rows = build_cycle_rows(
        chain, {"AAA": 0.5, "BBB": 0.5}, weighting="equal",
        index_closes=closes, close_dates=close_dates,
    )
    frame = pd.DataFrame([r.__dict__ for r in rows])
    book_b = frame[frame.book == "B"]
    assert set(book_b.status) == {"gate_closed"}
    assert book_b.m_rho.iloc[0] == pytest.approx(0.55, abs=0.01)
    assert book_b.realised.iloc[0] == pytest.approx(0.0)
    assert book_b.index_iv.iloc[0] > book_b.realised.iloc[0]
    filled = frame[(frame.book == "A") & (frame.status == "ok") & (frame.exit_mode == "flatten")]
    assert len(filled) == 2


def test_daily_replay_names_issue_63(caplog):
    chain = Chain(sessions=[], front={}, quotes={}, books={}, futures={}, spot={})
    with caplog.at_level(logging.WARNING):
        out = run(chain, {"AAA": 1.0}, weighting="file", index_closes=[], close_dates=[])
    assert out.empty
    hits = [r.getMessage() for r in caplog.records if "issue #63" in r.getMessage()]
    assert len(hits) == 1
    assert STANDARD_TIMEFRAME in hits[0]
    assert "daily" in hits[0]


def test_weight_file_rejects_anything_other_than_symbol_and_weight(tmp_path):
    path = tmp_path / "w.csv"
    path.write_text("symbol,shares\nAAA,1\n")
    with pytest.raises(ValueError, match="symbol,weight"):
        read_weights(path)
    path.write_text("symbol,weight\nAAA,-0.1\n")
    with pytest.raises(ValueError, match="negative"):
        read_weights(path)
    path.write_text("symbol,weight\nAAA,0.5\nAAA,0.5\n")
    with pytest.raises(ValueError, match="duplicate"):
        read_weights(path)


def test_equal_weight_is_labelled_and_the_cli_takes_one_source(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="research.backtest_dispersion"):
        with pytest.raises(FileNotFoundError):
            main(["--equal-weight", "--raw-dir", str(tmp_path)])
    text = caplog.text
    assert "not the free-float" in text
    assert "look-ahead" in text
    with pytest.raises(SystemExit):
        main(["--raw-dir", str(tmp_path)])
    weights = tmp_path / "w.csv"
    weights.write_text("symbol,weight\nAAA,1\n")
    with pytest.raises(SystemExit):
        main(["--equal-weight", "--weights", str(weights), "--raw-dir", str(tmp_path)])


def _opt_row(day, sym, kind, expiry, strike, opt, px, und, vol, lot):
    return {
        "TradDt": str(day), "TckrSymb": sym, "FinInstrmTp": kind, "XpryDt": str(expiry),
        "StrkPric": strike, "OptnTp": opt, "ClsPric": px, "UndrlygPric": und,
        "TtlTradgVol": vol, "NewBrdLotQty": lot,
    }


def test_weekly_index_expiry_is_not_paired_with_monthly_stocks(tmp_path, caplog):
    # 2026-03-26 has a busy Nifty chain and one stock. That is a weekly-style
    # expiry. 2026-04-30 has two stocks. With the production floor of 20
    # names neither is shared. With a floor of 2, only April is, and the
    # untraded 100 strike loses to the traded 105.
    day = date(2026, 3, 2)
    weekly, monthly = date(2026, 3, 26), date(2026, 4, 30)
    rows = []
    for expiry, names in ((weekly, ("AAA",)), (monthly, ("AAA", "BBB"))):
        for sym in names:
            for opt in ("CE", "PE"):
                rows.append(_opt_row(day, sym, "STO", expiry, 100, opt, 5, 100, 10, 50))
    for strike, vol, px in ((100, 0, 50), (70, 100, 1), (105, 10, 5)):
        for opt in ("CE", "PE"):
            rows.append(_opt_row(day, "NIFTY", "IDO", monthly, strike, opt, px, 100, vol, 65))
    for opt in ("CE", "PE"):
        rows.append(_opt_row(day, "NIFTY", "IDO", weekly, 100, opt, 5, 100, 1_000_000, 65))
    rows.append(_opt_row(day, "NIFTY", "IDF", date(2026, 5, 28), 0, "", 101, 100, 10, 65))
    rows.append(_opt_row(day, "NIFTY", "IDF", monthly, 0, "", 100, 100, 10, 65))
    good = pd.DataFrame(rows)
    good.to_parquet(tmp_path / "bhavcopy_fo_20260302.parquet", index=False)
    bad = good.drop(columns=["TtlTradgVol"])
    bad.to_parquet(tmp_path / "bhavcopy_fo_20260301.parquet", index=False)

    with caplog.at_level(logging.WARNING):
        blocked, _, _ = load_chain(tmp_path, ["AAA", "BBB"])
    assert blocked.front[day] is None
    assert any("TtlTradgVol" in r.getMessage() for r in caplog.records)

    chain, close_dates, closes = load_chain(tmp_path, ["AAA", "BBB"], min_shared_names=2)
    assert chain.front[day] == monthly
    assert weekly not in chain.front.values()
    assert atm_strike(100.0, chain.books[(day, INDEX)]) == 105.0
    assert (day, INDEX, 100.0) not in chain.quotes
    assert chain.futures[(day, INDEX)] == (100.0, 65)
    assert close_dates == [day]
    assert closes == [100.0]
