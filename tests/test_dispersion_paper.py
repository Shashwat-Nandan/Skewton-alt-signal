"""Paper book for the hedged hold-to-expiry dispersion.

The daily sign check was positive on this one variant. These tests pin the
accounting a paper book has to keep if that result is going to mean anything
forward. A failure here is a book that would mis-size, re-enter, skip the
hedge, or settle a number the replay did not use.
"""
from __future__ import annotations

import configparser
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
import strategies

from core.costs import estimate_transaction_cost
from research.backtest_dispersion import (
    PINNED_OPT_SLIPPAGE,
    exercise_stt,
    future_order_cost,
    futures_hedge_lots,
    option_order_cost,
)
from runners.run_paper_dispersion import (
    SHORT_VOL_STATE_FILE,
    STATE_FILE,
    _state_file,
    build_parser,
    build_session_view,
    front_from_rows,
    in_decision_window,
    previous_front,
)
from strategies.dispersion_paper import (
    INDEX,
    DispersionPaperStrategy,
    NameSurface,
    SessionView,
)


EXPIRY = date(2026, 11, 24)
ENTRY = date(2026, 10, 28)
PREV = date(2026, 10, 27)
NAMES = ("NIFTY", "AAA", "BBB")


class _Boom:
    """A client whose order method cannot succeed. The paper path must not call it."""

    def place_order(self, *args, **kwargs):
        raise AssertionError("paper path called place_order")


def _cfg(tmp_path) -> str:
    c = configparser.ConfigParser()
    c["mode"] = {"trading_mode": "paper"}
    c["logging"] = {"log_dir": str(tmp_path / "logs")}
    path = tmp_path / "cfg.ini"
    with path.open("w") as fh:
        c.write(fh)
    return str(path)


def _strategy(tmp_path, **kwargs) -> DispersionPaperStrategy:
    mode = kwargs.pop("mode", "paper")
    kwargs.setdefault("weights_path", None)
    strat = DispersionPaperStrategy(
        _Boom(), config_path=_cfg(tmp_path), mode=mode, **kwargs,
    )
    # Four names, two quoted: the 30–40% name gate needs 2 covered and keeps
    # at most 2, so AAA and BBB are the book and CCC/DDD stay in the count.
    strat.weights = {"AAA": 0.25, "BBB": 0.25, "CCC": 0.25, "DDD": 0.25}
    return strat


def _surface(symbol, spot, fut_px, strike=100.0, ce=5.0, pe=5.0) -> NameSurface:
    ce_sym = f"{symbol}26NOV{int(strike)}CE"
    pe_sym = f"{symbol}26NOV{int(strike)}PE"
    return NameSurface(
        symbol=symbol, spot=float(spot),
        quotes=((float(strike), float(ce), float(pe), 1.0, 1.0),),
        future_price=None if fut_px is None else float(fut_px),
        future_lot=1, future_symbol=f"{symbol}26NOVFUT",
        option_symbols={float(strike): {"CE": ce_sym, "PE": pe_sym}},
        option_lot=1,
    )


def _names(spot=100.0, fut=100.0, strike=100.0) -> dict:
    return {sym: _surface(sym, spot, fut, strike=strike) for sym in NAMES}


def _roll_view(spot=100.0, fut=100.0) -> SessionView:
    return SessionView(ENTRY, PREV, EXPIRY, _names(spot, fut))


def _view(session, names, previous=EXPIRY, front=EXPIRY) -> SessionView:
    return SessionView(session, previous, front, names)


def test_live_mode_is_refused(tmp_path):
    """Seven expiries on a daily tape are not a promoted book."""
    with pytest.raises(NotImplementedError, match="PAPER/SIGNALS ONLY"):
        DispersionPaperStrategy(_Boom(), config_path=_cfg(tmp_path), mode="live")


def test_strategy_stays_out_of_the_dashboard_registry():
    """The dashboard starts STRATEGIES. This book is paper-only."""
    assert "dispersion_paper" not in strategies.STRATEGIES


def test_non_roll_day_opens_nothing(tmp_path):
    """October is already the front. A mid-cycle start must not open it."""
    strat = _strategy(tmp_path)
    strat.on_close(SessionView(ENTRY, EXPIRY, EXPIRY, _names()))
    assert strat.book is None
    assert strat.closed == []


def test_roll_sells_the_index_and_buys_only_the_sized_names(tmp_path):
    """Four equal names, two quoted, at the same spot: 2 of 4 must be
    covered, which takes 4 index lots. AAA and BBB then carry the index
    notional at 2 lots each, and that package is the whole book. Costs are the pinned
    option half-spread plus the entry hedge, not the 5 bp inside core.costs."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    book = strat.book
    assert book is not None
    assert book.index_lots == 4
    assert book.covered_weight == pytest.approx(0.5)
    assert book.weighting == "equal"
    assert book.notional_ratio == pytest.approx(1.0)
    by_sym = {leg.symbol: leg for leg in book.legs}
    assert set(by_sym) == {"NIFTY", "AAA", "BBB"}
    assert by_sym["NIFTY"].side == -1 and by_sym["NIFTY"].lots == 4
    assert by_sym["AAA"].side == 1 and by_sym["AAA"].lots == 2
    assert by_sym["BBB"].lots == 2
    opt = 0.0
    for leg in book.legs:
        side = "SELL" if leg.side < 0 else "BUY"
        opt += option_order_cost(leg.ce, leg.lots, leg.lot_size, side)
        opt += option_order_cost(leg.pe, leg.lots, leg.lot_size, side)
    fut = 0.0
    for pos in book.futures.values():
        if pos.lots:
            side = "BUY" if pos.lots > 0 else "SELL"
            fut += future_order_cost(pos.last_px, abs(pos.lots), pos.lot_size, side)
    assert book.costs == pytest.approx(opt + fut)
    pinned = option_order_cost(5.0, 2, 1, "SELL")
    embedded = estimate_transaction_cost(5.0, 2, 1, "SELL", "OPT")
    turnover = 5.0 * 2 * 1
    assert pinned == pytest.approx(
        embedded - turnover * 0.0005 + turnover * PINNED_OPT_SLIPPAGE
    )
    assert pinned != pytest.approx(embedded)
    # The entry close is hedged once. A second call the same session is a no-op.
    costs = book.costs
    lots = {sym: pos.lots for sym, pos in book.futures.items()}
    strat.on_close(_roll_view())
    assert strat.book.costs == pytest.approx(costs)
    assert {sym: pos.lots for sym, pos in strat.book.futures.items()} == lots
    assert len(strat.traded_expiries) == 1


def test_coverage_under_thirty_percent_does_not_open(tmp_path):
    """One index lot floors both names to zero. The book waits rather than
    opening a package below the 30% floor."""
    strat = _strategy(tmp_path, max_index_lots=1)
    strat.on_close(_roll_view())
    assert strat.book is None


def test_dropped_name_stays_in_the_weight_denominator(tmp_path):
    """CCC has weight and no quote. It stays in the coverage gate's
    denominator, so the index lot count is the one choose_lots returns
    with CCC still counted."""
    strat = _strategy(tmp_path)
    strat.weights = {"AAA": 0.4, "BBB": 0.4, "CCC": 0.2}
    strat.on_close(_roll_view())
    book = strat.book
    assert book is not None
    assert book.index_lots == 3
    assert book.covered_weight == pytest.approx(0.8)
    assert "CCC" not in {leg.symbol for leg in book.legs}
    # CCC gates the entry but is not left uncovered in the size: AAA and BBB
    # are rescaled to the 3-lot index notional (1.5 lots each, rounded half
    # up to 2). The whole-lot miss is recorded, not hidden.
    by_sym = {leg.symbol: leg for leg in book.legs}
    assert by_sym["AAA"].lots == 2 and by_sym["BBB"].lots == 2
    assert book.notional_ratio == pytest.approx(4 * 100.0 / (3 * 100.0))


def test_default_weights_are_the_nse_free_float_snapshot(tmp_path):
    """Bloch §7.6.5.1 weights by market cap. The default book reads the NSE
    free-float file and says so. Equal weight is an explicit opt-out."""
    from research.backtest_dispersion import NIFTY50_2026_10_01

    strat = DispersionPaperStrategy(_Boom(), config_path=_cfg(tmp_path), mode="paper")
    assert strat.weighting == "free_float"
    assert set(strat.weights) == set(NIFTY50_2026_10_01)
    assert sum(strat.weights.values()) == pytest.approx(1.0)
    assert max(strat.weights.values()) > 2 * min(strat.weights.values())
    equal = DispersionPaperStrategy(
        _Boom(), config_path=_cfg(tmp_path), mode="paper", weights_path=None,
    )
    assert equal.weighting == "equal"


def test_a_weights_file_missing_a_constituent_is_refused(tmp_path):
    """A partial file would size every other leg off the wrong denominator."""
    from research.backtest_dispersion import NIFTY50_2026_10_01

    path = tmp_path / "w.csv"
    names = list(NIFTY50_2026_10_01)[1:]
    pd.DataFrame({"symbol": names, "weight": [1.0] * len(names)}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="missing"):
        DispersionPaperStrategy(
            _Boom(), config_path=_cfg(tmp_path), mode="paper", weights_path=path,
        )


def test_hedge_lots_match_the_replay_and_the_next_move_is_futures_pnl(tmp_path):
    """The hedge uses the entry IV. A second close the same session does not
    trade again. The following session's futures pnl is the lots already held
    times the future's price change."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    day2 = date(2026, 10, 29)
    names = {sym: _surface(sym, 110.0, 110.0, strike=100.0) for sym in NAMES}
    view2 = _view(day2, names)
    strat.on_close(view2)
    dte = (EXPIRY - day2).days
    for leg in strat.book.legs:
        expect = futures_hedge_lots(
            110.0, leg.strike, dte, leg.iv, leg.lots, leg.lot_size, leg.side, 1,
        )
        assert strat.book.futures[leg.symbol].lots == expect
        assert expect != 0
    costs = strat.book.costs
    lots = {sym: pos.lots for sym, pos in strat.book.futures.items()}
    strat.on_close(view2)
    assert strat.book.costs == pytest.approx(costs)
    assert {sym: pos.lots for sym, pos in strat.book.futures.items()} == lots

    day3 = date(2026, 10, 30)
    names3 = {
        "NIFTY": _surface("NIFTY", 110.0, 120.0, strike=100.0),
        "AAA": _surface("AAA", 110.0, 110.0, strike=100.0),
        "BBB": _surface("BBB", 110.0, 110.0, strike=100.0),
    }
    before = strat.book.futures_pnl
    strat.on_close(_view(day3, names3))
    assert strat.book.futures_pnl == pytest.approx(before + lots["NIFTY"] * 1 * 10)


def test_flat_expiry_settles_intrinsic_and_charges_long_exercise_stt_only(tmp_path):
    """An ATM book is already flat in the future, so the expiry orders have
    quantity 0. Settlement still runs, the ledger uses intrinsic 0 rather than
    the 0.01 validation floor, and exercise STT is the long legs only.
    The same expiry does not open again."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    assert all(pos.lots == 0 for pos in strat.book.futures.values())
    names = {
        "NIFTY": _surface("NIFTY", 100.0, 100.0, strike=100.0),
        "AAA": _surface("AAA", 110.0, 110.0, strike=100.0),
        "BBB": _surface("BBB", 110.0, 110.0, strike=100.0),
    }
    strat.set_view(_view(EXPIRY, names))
    strat.book.last_hedge_session = EXPIRY
    props = strat.check_and_rehedge()
    futures = [p for p in props if (p.greeks_snapshot or {}).get("kind") == "future"]
    assert futures
    assert all(p.quantity == 0 and p.greeks_snapshot["target_lots"] == 0 for p in futures)
    before = strat.book.costs
    strat.execute_proposals(props)
    assert strat.book is None
    row = strat.closed[-1]
    assert row["status"] == "ok"
    # Short index, 4 lots: received 40, intrinsic 0. Each long, 2 lots: paid
    # 20, intrinsic 20. Using the 0.01 floor on the short would book 39.96.
    assert row["premium_pnl"] == pytest.approx(40.0)
    stt = exercise_stt(110.0, 100.0, 2, 1, 1) + exercise_stt(110.0, 100.0, 2, 1, 1)
    assert stt > 0
    assert exercise_stt(100.0, 100.0, 4, 1, -1) == 0.0
    assert row["exercise_stt"] == pytest.approx(stt)
    assert row["costs"] == pytest.approx(before + stt)
    assert row["net"] == pytest.approx(row["premium_pnl"] + row["futures_pnl"] - row["costs"])
    assert EXPIRY.isoformat() in strat.traded_expiries

    again = SessionView(date(2026, 11, 25), PREV, EXPIRY, _names())
    strat.on_close(again)
    assert strat.book is None
    assert len(strat.closed) == 1


def test_live_hedge_is_flattened_at_expiry(tmp_path):
    """Expiry sets the future target to 0 and marks the lots already held."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    day2 = date(2026, 10, 29)
    names = {sym: _surface(sym, 110.0, 110.0, strike=100.0) for sym in NAMES}
    strat.on_close(_view(day2, names))
    held = strat.book.futures["NIFTY"].lots
    assert held != 0
    expiry_names = {
        "NIFTY": _surface("NIFTY", 110.0, 120.0, strike=100.0),
        "AAA": _surface("AAA", 110.0, 110.0, strike=100.0),
        "BBB": _surface("BBB", 110.0, 110.0, strike=100.0),
    }
    strat.set_view(_view(EXPIRY, expiry_names))
    props = strat.check_and_rehedge()
    futures = [p for p in props if (p.greeks_snapshot or {}).get("kind") == "future"]
    assert all(p.greeks_snapshot["target_lots"] == 0 for p in futures)
    strat.execute_proposals(props)
    row = strat.closed[-1]
    assert row["futures_pnl"] == pytest.approx(held * 1 * (120.0 - 110.0))
    assert strat.book is None
    assert row["status"] == "ok"


def test_two_days_before_expiry_does_not_flatten(tmp_path):
    """The flatten-at-two-days path is a different book. This one holds."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    session = EXPIRY - timedelta(days=2)
    strat.on_close(_view(session, _names()))
    assert strat.book is not None
    assert strat.closed == []


def test_missed_expiry_is_labelled_and_does_not_open_the_next_one(tmp_path):
    """A process that wakes up after expiry settles what it can and stays
    flat for the new front. Entering on the same close would be a late book."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    new_expiry = date(2026, 12, 29)
    late = date(2026, 11, 25)
    out = strat.on_close(_view(late, _names(), previous=EXPIRY, front=new_expiry))
    assert strat.book is None
    assert strat.closed[-1]["status"] == "missed_settlement"
    assert new_expiry.isoformat() not in strat.traded_expiries
    assert not any(row.get("status") == "PAPER_OPEN" for row in out)
    # The runner ticks every minute through the window and may restart. The
    # skip has to outlive this call, or the next tick opens the late book.
    again = strat.on_close(_view(late, _names(), previous=EXPIRY, front=new_expiry))
    assert strat.book is None
    assert not any(row.get("status") == "PAPER_OPEN" for row in again)
    restored = _strategy(tmp_path)
    restored.load_dict(strat.to_dict())
    restored.on_close(_view(late, _names(), previous=EXPIRY, front=new_expiry))
    assert restored.book is None


def test_missed_settlement_marks_the_expired_future_at_spot(tmp_path):
    """After expiry the nearest future is next month's. Its price minus the
    expired contract's last mark is a calendar spread, not hedge pnl. The
    expired contract settled at the underlying, the level the options use."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    day2 = date(2026, 10, 29)
    strat.on_close(_view(day2, {sym: _surface(sym, 110.0, 110.0) for sym in NAMES}))
    held = {sym: pos.lots for sym, pos in strat.book.futures.items()}
    assert held["NIFTY"] != 0
    late = date(2026, 11, 25)
    names = {}
    for sym in NAMES:
        surf = _surface(sym, 112.0, 130.0)
        surf.future_symbol = f"{sym}26DECFUT"
        names[sym] = surf
    strat.on_close(_view(late, names, previous=EXPIRY, front=date(2026, 12, 29)))
    row = strat.closed[-1]
    assert row["status"] == "missed_settlement"
    expect = sum(lots * 1 * (112.0 - 110.0) for lots in held.values())
    assert row["futures_pnl"] == pytest.approx(expect)


def test_half_point_strike_symbols_pass_validation():
    """ITC and POWERGRID list strikes like 317.5. Rejecting the dot rejects
    the whole open batch on a roll where one of them is ATM."""
    from core.trade_proposer import TradeProposal
    from strategies.base import OrderValidationError, validate_order

    def prop(sym):
        return TradeProposal(
            tradingsymbol=sym, instrument_token=0, strike=317.5, expiry="2026-10-27",
            option_type="CE", lot_size=1600, quantity=1, price=5.0,
            transaction_type="BUY", iv=0.2, bid_ask_spread_pct=0.0, margin_required=0.0,
        )

    validate_order(prop("ITC26OCT317.5CE"))
    validate_order(prop("POWERGRID26OCT232.5PE"))
    for bad in ("ITC.CE", "ITC26OCT317.5", "ITC26OCT317..5CE", "A.5CE.5PE"):
        with pytest.raises(OrderValidationError):
            validate_order(prop(bad))


def test_missing_settlement_spot_leaves_the_book_open(tmp_path):
    """No spot, no invented intrinsic. The book stays open and unlabelled."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    names = _names()
    del names["AAA"]
    strat.on_close(_view(EXPIRY, names))
    assert strat.book is not None
    assert strat.closed == []


def test_missing_future_on_expiry_leaves_a_hedged_book_open(tmp_path):
    """A flatten that cannot see the future it holds is not a close."""
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    day2 = date(2026, 10, 29)
    strat.on_close(_view(day2, {sym: _surface(sym, 110.0, 110.0) for sym in NAMES}))
    assert strat.book.futures["AAA"].lots != 0
    names = _names()
    names["AAA"] = _surface("AAA", 100.0, None, strike=100.0)
    strat.on_close(_view(EXPIRY, names))
    assert strat.book is not None
    assert strat.closed == []


def test_missing_future_on_a_never_hedged_leg_still_settles(tmp_path):
    """A leg that holds no future has nothing to flatten. Waiting for its
    quote would hold the book past expiry and block every later roll."""
    strat = _strategy(tmp_path)
    no_fut = _names()
    no_fut["AAA"] = _surface("AAA", 100.0, None, strike=100.0)
    strat.on_close(SessionView(ENTRY, PREV, EXPIRY, no_fut))
    assert strat.book is not None
    assert strat.book.futures.get("AAA") is None
    strat.on_close(_view(EXPIRY, dict(no_fut)))
    assert strat.book is None
    assert strat.closed[-1]["status"] == "ok"


def test_incomplete_straddle_opens_nothing(tmp_path):
    """A batch with the calls and not the puts fills nothing."""
    strat = _strategy(tmp_path)
    view = _roll_view()
    strat.set_view(view)
    props = [p for p in strat.scan_and_propose() if p.option_type == "CE"]
    out = strat.execute_proposals(props)
    assert strat.book is None
    assert out and out[0]["status"] == "REJECTED"


def test_signals_mode_logs_and_does_not_fill(tmp_path):
    strat = _strategy(tmp_path, mode="signals")
    out = strat.on_close(_roll_view())
    assert strat.book is None
    assert strat.traded_expiries == set()
    assert out and all(row["status"] == "SIGNAL_LOGGED" for row in out)


def test_state_round_trip_keeps_the_hedge_and_blocks_reentry(tmp_path):
    strat = _strategy(tmp_path)
    strat.on_close(_roll_view())
    day2 = date(2026, 10, 29)
    strat.on_close(_view(day2, {sym: _surface(sym, 110.0, 110.0) for sym in NAMES}))
    raw = strat.to_dict()
    restored = _strategy(tmp_path)
    restored.load_dict(raw)
    assert restored.book is not None
    assert restored.book.index_lots == strat.book.index_lots
    assert restored.book.costs == pytest.approx(strat.book.costs)
    assert restored.book.futures["NIFTY"].lots == strat.book.futures["NIFTY"].lots
    assert restored.book.legs[0].strike == strat.book.legs[0].strike
    assert len(restored.book.legs) == len(strat.book.legs)
    restored.load_dict(raw)
    assert len(restored.book.legs) == len(strat.book.legs)

    omitted = dict(raw)
    omitted["traded_expiries"] = []
    fresh = _strategy(tmp_path)
    fresh.load_dict(omitted)
    assert strat.book.expiry.isoformat() in fresh.traded_expiries
    flat = dict(raw)
    flat["book"] = None
    closed = _strategy(tmp_path)
    closed.load_dict(flat)
    closed.on_close(_roll_view())
    assert closed.book is None


def test_parser_has_no_live_switch():
    dests = {action.dest for action in build_parser()._actions}
    assert "mode" not in dests


def test_decision_window_is_the_close():
    morning = datetime(2026, 10, 5, 9, 30)
    assert not in_decision_window(morning, False)
    assert in_decision_window(datetime(2026, 10, 5, 15, 0), False)
    assert in_decision_window(datetime(2026, 10, 5, 15, 5), False)
    assert in_decision_window(datetime(2026, 10, 5, 15, 20), False)
    assert not in_decision_window(datetime(2026, 10, 5, 15, 21), False)
    assert in_decision_window(morning, True)


def test_a_one_name_expiry_is_not_the_front():
    """A nearer expiry with one stock name is a weekly. The front is the
    later expiry that has two names and Nifty options."""
    near = date(2026, 11, 3)
    far = date(2026, 11, 24)
    session = date(2026, 10, 28)

    def opt(name, expiry, kind, symbol):
        return {
            "name": name, "instrument_type": kind, "expiry": expiry,
            "strike": 100.0, "tradingsymbol": symbol, "lot_size": 1,
        }

    rows = [
        opt("AAA", near, "CE", "AAANEARCE"), opt("AAA", near, "PE", "AAANEARPE"),
        opt("NIFTY", near, "CE", "NIFTYNEARCE"), opt("NIFTY", near, "PE", "NIFTYNEARPE"),
        opt("AAA", far, "CE", "AAAFARCE"), opt("AAA", far, "PE", "AAAFARPE"),
        opt("BBB", far, "CE", "BBBFARCE"), opt("BBB", far, "PE", "BBBFARPE"),
        opt("NIFTY", far, "CE", "NIFTYFARCE"), opt("NIFTY", far, "PE", "NIFTYFARPE"),
    ]
    assert front_from_rows(rows, session, ["AAA", "BBB"], min_names=2) == far


def test_previous_front_skips_a_fallback_and_a_file_without_volume(tmp_path):
    """The newest usable bhavcopy decides the front. A fallback that would
    call a one-week expiry the front, and a file with no traded-volume
    column, are both skipped."""
    weekly = date(2026, 10, 6)
    monthly = date(2026, 10, 27)

    def row(session, sym, kind, expiry, vol=10):
        return {
            "TradDt": session, "TckrSymb": sym, "FinInstrmTp": kind,
            "XpryDt": expiry, "TtlTradgVol": vol,
        }

    good = [
        row(date(2026, 10, 1), "AAA", "STO", weekly),
        row(date(2026, 10, 1), "NIFTY", "IDO", weekly),
        row(date(2026, 10, 1), "AAA", "STO", monthly),
        row(date(2026, 10, 1), "BBB", "STO", monthly),
        row(date(2026, 10, 1), "NIFTY", "IDO", monthly),
    ]
    fallback = [
        row(date(2026, 10, 3), "AAA", "STO", weekly),
        row(date(2026, 10, 3), "BBB", "STO", weekly),
        row(date(2026, 10, 3), "NIFTY", "IDO", weekly),
    ]
    pd.DataFrame(good).to_parquet(tmp_path / "bhavcopy_fo_20261001.parquet")
    thin = tmp_path / "bhavcopy_fo_20261002.parquet"
    pd.DataFrame({"TradDt": [date(2026, 10, 2)]}).to_parquet(thin)
    bad = tmp_path / "bhavcopy_fo_20261003.parquet"
    pd.DataFrame(fallback).to_parquet(bad)
    bad.with_suffix(".broker-fallback").write_text("fallback\n")

    found = previous_front(tmp_path, date(2026, 10, 5), ["AAA", "BBB"], min_names=2)
    assert found == monthly


def test_chain_quote_uses_a_positive_last_and_the_future_when_cash_is_missing(caplog):
    """Kotak quotes have no volume. A positive last is stored as traded.
    A missing cash spot is marked with the future, and a strike outside
    the moneyness band is not requested."""
    session = date(2026, 10, 28)
    near = date(2026, 11, 3)
    far = date(2026, 11, 24)
    rows = []

    def add(name, kind, expiry, strike, symbol, lot=1):
        rows.append({
            "name": name, "instrument_type": kind, "expiry": expiry,
            "strike": strike, "tradingsymbol": symbol, "lot_size": lot,
        })

    for name, expiry, strike, ce, pe in (
        ("NIFTY", near, 100, "NIFTYNEARCE", "NIFTYNEARPE"),
        ("AAA", near, 100, "AAANEARCE", "AAANEARPE"),
        ("NIFTY", far, 100, "NIFTY26NOV100CE", "NIFTY26NOV100PE"),
        ("NIFTY", far, 200, "NIFTY26NOV200CE", "NIFTY26NOV200PE"),
        ("AAA", far, 100, "AAA26NOV100CE", "AAA26NOV100PE"),
        ("AAA", far, 200, "AAA26NOV200CE", "AAA26NOV200PE"),
        ("BBB", far, 100, "BBB26NOV100CE", "BBB26NOV100PE"),
    ):
        add(name, "CE", expiry, strike, ce)
        add(name, "PE", expiry, strike, pe)
    add("NIFTY", "FUT", date(2026, 10, 29), 0, "NIFTY26OCTFUT")
    add("AAA", "FUT", date(2026, 10, 29), 0, "AAA26OCTFUT")
    add("BBB", "FUT", date(2026, 10, 29), 0, "BBB26OCTFUT")
    book = {
        "NSE:NIFTY 50": 100.0,
        "NFO:NIFTY26OCTFUT": 101.0,
        "NFO:NIFTY26NOV100CE": 5.0,
        "NFO:NIFTY26NOV100PE": 5.0,
        "NFO:AAA26OCTFUT": 107.0,
        "NFO:AAA26NOV100CE": 5.0,
        "NFO:AAA26NOV100PE": 5.0,
        "NFO:BBB26OCTFUT": 100.0,
        "NSE:BBB-EQ": 100.0,
        "NFO:BBB26NOV100CE": 5.0,
        "NFO:BBB26NOV100PE": 5.0,
    }
    asked = []

    class _Client:
        def instruments(self, exchange=None):
            return rows

        def quote(self, keys):
            asked.extend(keys)
            out = {}
            for key in keys:
                if key not in book:
                    raise RuntimeError(key)
                out[key] = {"last_price": book[key]}
            return out

        def place_order(self, *args, **kwargs):
            raise AssertionError("quote path called place_order")

    with caplog.at_level("WARNING"):
        view = build_session_view(
            _Client(), session, PREV, ["AAA", "BBB"], books=[None], min_names=2,
        )
    assert view.front_expiry == far
    assert view.is_roll
    nifty = view.names["NIFTY"]
    assert nifty.spot == pytest.approx(100.0)
    assert nifty.quotes
    assert nifty.quotes[0][3] == 1.0 and nifty.quotes[0][4] == 1.0
    assert view.names["AAA"].spot == pytest.approx(107.0)
    assert "AAA26NOV200CE" not in asked
    assert "NIFTY26NOV200CE" not in asked
    assert any("future price" in rec.message for rec in caplog.records)
    assert INDEX == "NIFTY"


def test_short_vol_book_is_the_pr9_sizing_with_its_own_name_and_state(tmp_path):
    """The raw book sizes each covered name at its raw weight once 30% of
    the weight is covered. With four equal names (two quoted) that is one
    lot each against four index lots: half the index notional, so the book
    is net short index vol by construction — which is why it is labelled
    and stored apart from the matched book, and gets no match warning."""
    raw = _strategy(tmp_path, sizing="raw")
    matched = _strategy(tmp_path)
    assert raw.name == "dispersion_short_vol_paper"
    assert matched.name == "dispersion_paper"
    raw.on_close(_roll_view())
    matched.on_close(_roll_view())
    r = {leg.symbol: leg.lots for leg in raw.book.legs}
    m = {leg.symbol: leg.lots for leg in matched.book.legs}
    assert r == {"NIFTY": 4, "AAA": 1, "BBB": 1}
    assert m == {"NIFTY": 4, "AAA": 2, "BBB": 2}
    assert raw.book.notional_ratio == pytest.approx(0.5)
    assert matched.book.notional_ratio == pytest.approx(1.0)
    assert raw.generate_eod_report()["sizing"] == "raw"
    assert _state_file(raw) == SHORT_VOL_STATE_FILE
    assert _state_file(matched) == STATE_FILE
    assert SHORT_VOL_STATE_FILE != STATE_FILE
    with pytest.raises(ValueError, match="sizing"):
        _strategy(tmp_path, sizing="grid")


def test_one_view_quotes_the_chain_when_either_book_is_flat():
    """Both books read one view. If one is open and the other flat on a
    roll, the view must carry the entry chain for the flat one as well as
    the open book's names, or the flat book silently skips its roll."""
    from types import SimpleNamespace

    session = date(2026, 10, 28)
    far = date(2026, 11, 24)
    rows = []
    for name in ("NIFTY", "AAA", "BBB"):
        for kind in ("CE", "PE"):
            rows.append({"name": name, "instrument_type": kind, "expiry": far,
                         "strike": 100.0, "tradingsymbol": f"{name}26NOV100{kind}", "lot_size": 1})
        rows.append({"name": name, "instrument_type": "FUT", "expiry": far, "strike": 0,
                     "tradingsymbol": f"{name}26NOVFUT", "lot_size": 1})

    class _Client:
        def instruments(self, exchange=None):
            return rows

        def quote(self, keys):
            return {k: {"last_price": 5.0 if k.endswith(("CE", "PE")) else 100.0} for k in keys}

    open_book = SimpleNamespace(legs=[SimpleNamespace(symbol="AAA")])
    view = build_session_view(
        _Client(), session, PREV, ["AAA", "BBB"], books=[open_book, None], min_names=2,
    )
    assert view.is_roll
    assert set(view.names) == {"NIFTY", "AAA", "BBB"}
    assert view.names["BBB"].quotes
    held_only = build_session_view(
        _Client(), session, PREV, ["AAA", "BBB"], books=[open_book, open_book], min_names=2,
    )
    assert set(held_only.names) == {"AAA"}


def test_a_failed_restore_overwrites_neither_state_file(tmp_path, monkeypatch):
    """The runner refuses to start when a state file will not load. Its
    `finally` must not then save the empty, never-loaded books over the
    broken file — or over the other book's good one — or the next run
    starts clean and forgets a book it never settled."""
    import runners.run_paper_dispersion as r

    good = tmp_path / "sv.json"
    broken = tmp_path / "m.json"
    strat = _strategy(tmp_path, sizing="raw")
    strat.on_close(_roll_view())
    good.write_text(json.dumps(strat.to_dict()))
    broken.write_text('{"book": {"expiry": "2026-11-24"')          # truncated
    good_before, broken_before = good.read_bytes(), broken.read_bytes()

    monkeypatch.setattr(r, "STATE_FILE", broken)
    monkeypatch.setattr(r, "SHORT_VOL_STATE_FILE", good)
    monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(r, "LOCK_FILE", tmp_path / ".lock")
    monkeypatch.setattr(r, "SILENT_FAIL_FLAG", tmp_path / "SF")
    monkeypatch.setattr(r, "assert_timezone_ist", lambda log: None)
    monkeypatch.setattr(r, "previous_front", lambda *a, **k: PREV)
    monkeypatch.setattr(r, "market_client", lambda cfg: _Boom())
    with pytest.raises(Exception):
        r.main(["--force", "--once", "--ignore-entry-window", "--config", _cfg(tmp_path)])
    assert broken.read_bytes() == broken_before
    assert good.read_bytes() == good_before


def test_silent_fail_exits_with_the_code_the_unit_will_not_restart(tmp_path, monkeypatch):
    """Every pass failing must end the day as a FAILED unit (so
    notify-failure@ alerts), not a restart into a fresh heartbeat count that
    reaches 15:20 and exits 0. The runner's code and the unit's
    RestartPreventExitStatus must agree, and a crash (1) must still restart."""
    import re
    from pathlib import Path

    import runners.run_paper_dispersion as r

    unit = (Path(r.HERE) / "deploy" / "dispersion-paper.service").read_text()
    prevent = re.search(r"^RestartPreventExitStatus=(.+)$", unit, re.M)
    assert prevent and prevent.group(1).split() == [str(r.SILENT_FAIL_EXIT)]
    assert r.SILENT_FAIL_EXIT != 1
    assert re.search(r"^Restart=on-failure$", unit, re.M)

    class _Down(_Boom):
        def instruments(self, exchange=None):
            raise RuntimeError("quote outage")

    monkeypatch.setattr(r, "STATE_FILE", tmp_path / "m.json")
    monkeypatch.setattr(r, "SHORT_VOL_STATE_FILE", tmp_path / "sv.json")
    monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(r, "LOCK_FILE", tmp_path / ".lock")
    monkeypatch.setattr(r, "SILENT_FAIL_FLAG", tmp_path / "SF")
    monkeypatch.setattr(r, "SILENT_FAIL_THRESHOLD", 1)
    monkeypatch.setattr(r, "assert_timezone_ist", lambda log: None)
    monkeypatch.setattr(r, "previous_front", lambda *a, **k: PREV)
    monkeypatch.setattr(r, "market_client", lambda cfg: _Down())
    code = r.main(["--force", "--once", "--ignore-entry-window", "--config", _cfg(tmp_path)])
    assert code == r.SILENT_FAIL_EXIT
    assert (tmp_path / "SF").exists()
