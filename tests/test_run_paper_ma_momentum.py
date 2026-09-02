"""Pure-core tests for the MA-momentum paper runner.

Kite-wired main() is host-only. What we can pin without a broker: EOD shape
the scoreboard consumes, restore refuses a drifted stop under an open
position, daily-loss uses session P&L, and 5-min aggregation still rolls.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from runners import run_paper_ma_momentum as r
from runners.run_paper_kalman_trend import BarAggregator
from strategies import ma_momentum as mm


def test_eod_report_is_what_the_scoreboard_reads():
    """An EOD sidecar missing total_rupees would silently drop the holdout
    from the decay ledger — the exact exemption CLAIMED_EOD_PATTERNS exists
    to prevent."""
    book = mm.build_book("NIFTY")
    book.pos, book.entry_price = 1, 100.0
    book.force_close(110.0)
    inst = r.InstrumentBook(symbol="NIFTY", book=book, tradingsymbol="NIFTY26SEPFUT")
    rep = r.eod_report([inst], date(2026, 8, 31))
    assert rep["system"] == "ma_momentum"
    assert "total_rupees" in rep and "date" in rep
    assert "NO-GO" in rep["note"]
    # +10 pts × 75 lot − 2 × 2.5 cost × 75
    assert rep["total_rupees"] == pytest.approx((10 - 5) * 75)
    assert rep["n_trades"] == 1
    assert rep["instruments"][0]["ma"]["n_trades"] == 1


def test_restore_raises_if_an_open_position_has_a_drifted_stop():
    book = mm.build_book("NIFTY")
    book.pos, book.entry_price, book.stop_price = 1, 24000.0, 23780.0
    book.stop_ticks = 1.0
    blob = r.InstrumentBook(symbol="NIFTY", book=book).serialize()
    with pytest.raises(RuntimeError, match="OPEN position"):
        r.InstrumentBook.restore(blob)


def test_restore_reasserts_frozen_windows_on_a_flat_book():
    book = mm.build_book("NIFTY")
    book.short, book.long, book.stop_ticks = 3, 8, 1.0
    blob = r.InstrumentBook(symbol="NIFTY", book=book).serialize()
    restored = r.InstrumentBook.restore(blob)
    assert restored.book.short == 34
    assert restored.book.long == 53
    assert restored.book.stop_ticks == pytest.approx(
        mm.FROZEN_PARAMS["NIFTY"]["stop_ticks"])
    assert restored.book.cost_per_unit == mm.COST_PER_UNIT_POINTS


def test_session_pnl_includes_open_mtm():
    """A daily-loss cap that only sees closed trades would miss a bleeding
    open short — the same class of 'guard that only looked present'."""
    book = mm.build_book("NIFTY")
    book.pos, book.entry_price = 1, 100.0
    book.on_session_start()
    inst = r.InstrumentBook(symbol="NIFTY", book=book)
    pnl = r.session_pnl_rupees([inst], {"NIFTY": 90.0})
    assert pnl == pytest.approx(1 * (90.0 - 100.0) * 75)


def test_bar_aggregator_still_rolls_on_300s():
    a = BarAggregator(width_seconds=300)
    assert a.add(0, 100.0) is None
    assert a.add(300, 103.0) == 100.0


def test_write_eod_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
    book = mm.build_book("BANKNIFTY")
    inst = r.InstrumentBook(symbol="BANKNIFTY", book=book)
    path = r.write_eod([inst], date(2026, 9, 1))
    blob = json.loads(path.read_text())
    assert blob["date"] == "2026-09-01"
    assert blob["total_rupees"] == 0.0
    assert path.name == "ma_momentum_eod_2026-09-01.json"


def _blob(sym, *, tradingsymbol, pos=0, entry=0.0, last_px=None):
    book = mm.build_book(sym)
    book.pos, book.entry_price = pos, entry
    if pos:
        book.stop_price = entry - pos * mm.FROZEN_PARAMS[sym]["stop_ticks"]
    inst = r.InstrumentBook(symbol=sym, book=book, tradingsymbol=tradingsymbol)
    inst.last_px = last_px
    return inst.serialize()


def test_write_state_preserves_a_symbol_that_could_not_be_loaded(tmp_path, monkeypatch):
    """Writing only the loaded books deletes the history of a symbol whose
    future did not resolve. eod_report sums the CUMULATIVE realized_rupees and
    the scoreboard reads it as a cumulative series, so losing a book prints as
    a monthly loss the size of its whole lifetime P&L — the decay machine would
    park the strategy on an artifact."""
    monkeypatch.setattr(r, "STATE_PATH", tmp_path / "state.json")
    carried = {"BANKNIFTY": _blob("BANKNIFTY", tradingsymbol="BANKNIFTY26SEPFUT")}
    book = mm.build_book("NIFTY")
    book.pos, book.entry_price = 1, 24000.0
    book.force_close(24100.0)
    loaded = r.InstrumentBook(symbol="NIFTY", book=book,
                              tradingsymbol="NIFTY26SEPFUT")

    r.write_state([loaded], carried)

    back = r.load_state()
    assert set(back) == {"NIFTY", "BANKNIFTY"}, "the skipped book was deleted"
    assert back["BANKNIFTY"]["tradingsymbol"] == "BANKNIFTY26SEPFUT"
    # and it is still restorable, with its history intact
    assert r.InstrumentBook.restore(back["BANKNIFTY"]).symbol == "BANKNIFTY"


def test_state_session_date_reads_the_write_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "STATE_PATH", tmp_path / "state.json")
    assert r.state_session_date() is None
    r.write_state([], {})
    assert r.state_session_date() == date.today()


def test_state_session_date_is_none_on_a_corrupt_file(tmp_path, monkeypatch):
    """A corrupt state file must not crash the runner at start; None means
    'unknown', and an unknown date is treated as not-carried."""
    p = tmp_path / "state.json"
    p.write_text("{not json")
    monkeypatch.setattr(r, "STATE_PATH", p)
    assert r.state_session_date() is None


def test_stop_overshoot_is_recorded_against_the_level_fill():
    """check_exit books at the stop LEVEL, but we poll every 30s. On BANKNIFTY
    the frozen stop is 14.38 pts — smaller than a routine 30s excursion — so
    the holdout's P&L is optimistic by the overshoot. Record it or the bias is
    invisible."""
    book = mm.build_book("BANKNIFTY")
    inst = r.InstrumentBook(symbol="BANKNIFTY", book=book,
                            tradingsymbol="BANKNIFTY26SEPFUT")
    book.pos, book.entry_price = 1, 57000.0
    book.stop_price = 56985.0
    inst.on_price(56945.0)          # gapped 40 pts through the stop

    assert book.pos == 0
    assert inst.n_stop_fills == 1
    assert inst.stop_overshoot_points == pytest.approx(40.0)
    rep = r.eod_report([inst], date(2026, 9, 1))
    # 40 pts × 15 lot of unrecorded slippage
    assert rep["stop_overshoot_rupees"] == pytest.approx(40.0 * 15)
    assert rep["n_stop_fills"] == 1
    # the pre-registered headline number is NOT silently restated
    assert rep["total_rupees"] == pytest.approx(book.realized_rupees())


def test_a_forced_exit_does_not_count_as_stop_overshoot():
    book = mm.build_book("NIFTY")
    inst = r.InstrumentBook(symbol="NIFTY", book=book)
    book.pos, book.entry_price, book.stop_price = 1, 24000.0, 23780.0
    inst.on_price(24010.0)          # nowhere near the stop
    book.force_close(24010.0)
    assert inst.n_stop_fills == 0
    assert inst.stop_overshoot_points == 0.0


def test_last_px_round_trips_so_a_crashed_position_can_be_marked():
    """A position that survives a crash must be squared at the last real mark
    of the contract it was opened on — not at the next session's first quote,
    and not at a stale stop_price (which books at the LEVEL and would swallow
    the whole overnight gap)."""
    blob = _blob("NIFTY", tradingsymbol="NIFTY26SEPFUT",
                 pos=1, entry=24000.0, last_px=24012.5)
    restored = r.InstrumentBook.restore(blob)
    assert restored.last_px == pytest.approx(24012.5)
    assert restored.book.pos == 1
    rec = restored.book.force_close(restored.last_px)
    assert rec.exit_price == pytest.approx(24012.5)
