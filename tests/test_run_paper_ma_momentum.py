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


def test_eod_carries_a_symbol_the_runner_could_not_load(tmp_path, monkeypatch):
    """write_state's carry kept the STATE whole, but the EOD sidecar is what the
    scoreboard reads as a cumulative series. Omitting the symbol there drops its
    whole lifetime P&L for that session — the same phantom-loss artifact, just
    through the other writer."""
    monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(r, "STATE_PATH", tmp_path / "state.json")

    # BANKNIFTY has history but fails to load today.
    bn = mm.build_book("BANKNIFTY")
    bn.pos, bn.entry_price = 1, 57000.0
    bn.force_close(57100.0)                       # +100 pts × 15 − costs
    carried_rupees = bn.realized_rupees()
    carry = {"BANKNIFTY": r.InstrumentBook(
        symbol="BANKNIFTY", book=bn, tradingsymbol="BANKNIFTY26SEPFUT").serialize()}

    nifty = mm.build_book("NIFTY")
    nifty.pos, nifty.entry_price = 1, 24000.0
    nifty.force_close(24100.0)
    loaded = r.InstrumentBook(symbol="NIFTY", book=nifty,
                              tradingsymbol="NIFTY26SEPFUT")

    rep = r.eod_report([loaded], date(2026, 9, 4), carry)

    assert rep["carried_symbols"] == ["BANKNIFTY"]
    assert {i["symbol"] for i in rep["instruments"]} == {"NIFTY", "BANKNIFTY"}
    # the headline still contains BANKNIFTY's lifetime P&L
    assert rep["total_rupees"] == pytest.approx(
        nifty.realized_rupees() + carried_rupees)
    assert rep["n_trades"] == 2
    # and the carried row is flagged, not passed off as a session result
    bn_row = next(i for i in rep["instruments"] if i["symbol"] == "BANKNIFTY")
    assert bn_row["carried"] is True
    assert bn_row["ma"]["session_n_trades"] == 0
    assert bn_row["ma"]["session_realized_rupees"] == 0.0
    assert next(i for i in rep["instruments"]
                if i["symbol"] == "NIFTY")["carried"] is False


def test_eod_does_not_double_count_a_symbol_that_did_load(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
    book = mm.build_book("NIFTY")
    book.pos, book.entry_price = 1, 24000.0
    book.force_close(24100.0)
    inst = r.InstrumentBook(symbol="NIFTY", book=book)
    carry = {"NIFTY": inst.serialize()}           # same symbol in BOTH

    rep = r.eod_report([inst], date(2026, 9, 4), carry)
    assert rep["carried_symbols"] == []
    assert len(rep["instruments"]) == 1
    assert rep["total_rupees"] == pytest.approx(book.realized_rupees())


def test_eod_records_whether_entries_were_halted(tmp_path, monkeypatch):
    """A halted session still writes a sidecar. Without this flag a reader
    counting files as holdout progress counts sessions that could never
    trade — the daily-loss flag persists across sessions."""
    monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(r, "HALT_DAILY_LOSS_PATH",
                        tmp_path / "HALT_MA_MOMENTUM_DAILY_LOSS")
    monkeypatch.setattr(r, "HALT_ALL_PATH", tmp_path / "HALT_ALL")
    monkeypatch.setattr(r, "HALT_NEW_ENTRIES_PATH", tmp_path / "HALT_NEW_ENTRIES")
    monkeypatch.setattr(r, "HALT_ENTRIES_PATH", tmp_path / "HALT_NEW_ENTRIES_ma_momentum")
    inst = r.InstrumentBook(symbol="NIFTY", book=mm.build_book("NIFTY"))

    path = r.write_eod([inst], date(2026, 9, 4))
    assert json.loads(path.read_text())["entries_halted"] is False

    (tmp_path / "HALT_MA_MOMENTUM_DAILY_LOSS").write_text("breached\n")
    path = r.write_eod([inst], date(2026, 9, 5))
    blob = json.loads(path.read_text())
    assert blob["entries_halted"] is True
    assert blob["halt_reasons"] == ["HALT_MA_MOMENTUM_DAILY_LOSS"]


def test_carried_entry_skips_a_blob_it_cannot_parse():
    """A corrupt stored blob must not take the EOD write down — it is the
    fallback path for a symbol whose restore ALREADY failed."""
    assert r._carried_entry({"symbol": "NIFTY"}) is None
    assert r._carried_entry({"symbol": "NIFTY", "book": "not-a-dict"}) is None
    assert r._carried_entry({"symbol": "NIFTY",
                             "book": {"realized_points": "nope"}}) is None


def test_carried_entry_survives_corrupt_overshoot_fields():
    """These conversions sat OUTSIDE the guard, so an unparseable value
    propagated out of main() and NO sidecar was written for the session — the
    inverse of the contract, on a path that only runs for blobs whose restore
    already failed."""
    book = mm.build_book("NIFTY")
    blob = r.InstrumentBook(symbol="NIFTY", book=book).serialize()
    blob["stop_overshoot_points"] = "oops"
    assert r._carried_entry(blob) is None          # not an exception

    blob2 = r.InstrumentBook(symbol="NIFTY", book=mm.build_book("NIFTY")).serialize()
    blob2["n_stop_fills"] = "nope"
    assert r._carried_entry(blob2) is None


def test_missing_lot_size_is_unreadable_not_zero_pnl():
    """`or 0.0` turned a missing lot_size into realized_rupees=0.0 on a row
    that still looked well-formed — the phantom-loss artifact masked rather
    than visible, which is worse than the bug this fixes."""
    book = mm.build_book("BANKNIFTY")
    book.pos, book.entry_price = 1, 57000.0
    book.force_close(57100.0)
    assert book.realized_rupees() != 0.0
    blob = r.InstrumentBook(symbol="BANKNIFTY", book=book).serialize()
    del blob["book"]["lot_size"]
    assert r._carried_entry(blob) is None

    blob["book"]["lot_size"] = 0
    assert r._carried_entry(blob) is None


def test_an_unreadable_carried_blob_still_trips_the_partial_alarm(tmp_path, monkeypatch):
    """Skipping it silently removed the symbol from carried_symbols too — the
    field gating the PARTIAL alarm — so the sidecar reported itself COMPLETE
    while its totals were short."""
    monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
    inst = r.InstrumentBook(symbol="NIFTY", book=mm.build_book("NIFTY"))
    carry = {"BANKNIFTY": {"symbol": "BANKNIFTY", "book": None}}

    rep = r.eod_report([inst], date(2026, 9, 4), carry)
    assert rep["dropped_symbols"] == ["BANKNIFTY"]
    assert rep["carried_symbols"] == []
    assert {i["symbol"] for i in rep["instruments"]} == {"NIFTY"}


class TestEntryGateLatch:
    """`entries_halted` must mean 'entries were NEVER possible this session',
    which is how the dashboard excludes a session from holdout progress. A
    15:25 snapshot answered a different question."""

    def _write(self, tmp_path, gate):
        inst = r.InstrumentBook(symbol="NIFTY", book=mm.build_book("NIFTY"))
        return json.loads(r.write_eod([inst], date(2026, 9, 4), None, gate).read_text())

    def test_a_session_that_traded_then_breached_still_counts_as_measured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
        gate = {"evaluated": 40, "allowed": True, "halted": True,
                "reasons": {"HALT_MA_MOMENTUM_DAILY_LOSS"}}
        blob = self._write(tmp_path, gate)
        # it took entries — excluding it would discount the session that
        # produced the biggest loss
        assert blob["entries_halted"] is False
        assert blob["entries_halted_intraday"] is True
        assert blob["halt_reasons"] == ["HALT_MA_MOMENTUM_DAILY_LOSS"]

    def test_a_fully_frozen_session_is_not_measured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
        gate = {"evaluated": 40, "allowed": False, "halted": True,
                "reasons": {"HALT_MA_MOMENTUM_DAILY_LOSS"}}
        blob = self._write(tmp_path, gate)
        assert blob["entries_halted"] is True
        assert blob["entries_halted_intraday"] is False

    def test_a_clean_session_is_measured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
        gate = {"evaluated": 40, "allowed": True, "halted": False, "reasons": set()}
        blob = self._write(tmp_path, gate)
        assert blob["entries_halted"] is False
        assert blob["entries_halted_intraday"] is False
        assert blob["halt_reasons"] == []

    def test_falls_back_to_a_snapshot_when_no_tick_ever_ran(self, tmp_path, monkeypatch):
        """Clearing a flag at 15:00 after a frozen day must not make a dead
        session count as measured — but with zero evaluations there is nothing
        better than the snapshot."""
        monkeypatch.setattr(r, "DATA_CACHE", tmp_path)
        monkeypatch.setattr(r, "HALT_DAILY_LOSS_PATH", tmp_path / "HALT_MA_MOMENTUM_DAILY_LOSS")
        monkeypatch.setattr(r, "HALT_ALL_PATH", tmp_path / "HALT_ALL")
        monkeypatch.setattr(r, "HALT_NEW_ENTRIES_PATH", tmp_path / "HALT_NEW_ENTRIES")
        monkeypatch.setattr(r, "HALT_ENTRIES_PATH", tmp_path / "HALT_NEW_ENTRIES_ma_momentum")
        (tmp_path / "HALT_MA_MOMENTUM_DAILY_LOSS").write_text("x\n")
        blob = self._write(tmp_path, {"evaluated": 0, "allowed": False,
                                      "halted": False, "reasons": set()})
        assert blob["entries_halted"] is True
