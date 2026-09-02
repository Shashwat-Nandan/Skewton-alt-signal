"""Tests for the §6.3 MA-momentum paper strategy.

The historical replay was NO-GO. These tests pin the things that would make
the paper holdout measure the wrong object: a live path, a CMA-ES refit,
or drifted SMA windows.
"""
from __future__ import annotations

import inspect

import numpy as np
import pytest

from strategies import ma_momentum as mm
from runners import run_paper_ma_momentum as r


def test_live_mode_raises_before_a_book_exists():
    """Safety rule 3: paper → live is a human gate. A live branch that
    'just works' would skip the 60-session holdout this runner exists for."""
    with pytest.raises(mm.LiveModeForbidden, match="PAPER ONLY"):
        mm.build_book("NIFTY", mode="live")
    mm.assert_paper_only("paper")
    mm.assert_paper_only("signals")


def test_build_book_uses_frozen_paper_control_windows():
    b = mm.build_book("NIFTY")
    assert b.signal_kind == "ma"
    assert (b.short, b.long) == (34, 53)
    assert b.target_ticks is None
    assert b.lot_size == 75
    assert b.cost_per_unit == 2.5
    bn = mm.build_book("BANKNIFTY")
    assert (bn.short, bn.long) == (27, 109)
    assert bn.lot_size == 15


def test_unknown_symbol_fails_loud():
    with pytest.raises(KeyError, match="FINNIFTY"):
        mm.build_book("FINNIFTY")


def test_seed_closes_fills_the_long_window_without_changing_params():
    b = mm.build_book("NIFTY")
    n = mm.seed_closes(b, list(range(100, 200)))
    assert n == 53                         # long window, not the 100-long series
    assert len(b._closes) == 53
    assert b.n_bars >= 53
    assert (b.short, b.long) == (34, 53)   # seeding is not a fit


def test_reassert_frozen_repairs_a_flat_book_and_refuses_an_open_stop_swap():
    b = mm.build_book("NIFTY")
    b.short, b.long, b.stop_ticks = 3, 8, 1.0
    assert mm.reassert_frozen(b, "NIFTY") is True
    assert (b.short, b.long, b.stop_ticks) == (34, 53, mm.FROZEN_PARAMS["NIFTY"]["stop_ticks"])

    open_b = mm.build_book("NIFTY")
    open_b.pos = 1
    open_b.stop_ticks = 1.0
    mm.reassert_frozen(open_b, "NIFTY")
    assert open_b.stop_ticks == 1.0        # not swapped under an open position


def test_runner_and_strategy_source_never_call_cmaes():
    """§6.3: do not jointly refit. The paper runner's job is to hold the
    frozen windows for 60 sessions, not to rediscover them."""
    for src in (inspect.getsource(mm), inspect.getsource(r)):
        assert "fit_ma_crossover" not in src
        assert "run_cmaes" not in src
        assert "fit_kalman" not in src


def test_seed_closes_rejects_infinite_prices():
    """`x == x` drops NaN but not ±inf. An inf in the seeded window makes both
    SMAs non-finite, so every comparison is False and the book emits no signal
    for `long` bars — silently mute, which is the failure seeding exists to
    prevent."""
    book = mm.build_book("NIFTY")
    n = mm.seed_closes(book, [24000.0, float("inf"), 24010.0,
                              float("-inf"), float("nan"), 24020.0])
    assert n == 3
    assert all(np.isfinite(x) for x in book._closes)


def test_reset_window_clears_a_rolled_contract_out_of_the_sma():
    """A futures roll leaves closes of the EXPIRED contract in the window; the
    roll basis exceeds the frozen dead-band, so a mixed window fabricates a
    signal rather than merely delaying one."""
    book = mm.build_book("BANKNIFTY")
    mm.seed_closes(book, [57000.0 + i for i in range(200)])
    assert not mm.window_is_short(book)
    mm.reset_window(book)
    assert len(book._closes) == 0
    assert mm.window_is_short(book)


def test_window_is_short_flags_an_underfilled_restored_book():
    """reassert_frozen widening `long` (8 → 53) leaves the deque under-filled
    and the restore path does not seed — the caller must be told to re-seed or
    the book is inert for half a session."""
    book = mm.build_book("NIFTY")
    book.short, book.long = 3, 8
    book._closes = type(book._closes)(book._closes, maxlen=8)
    mm.seed_closes(book, [24000.0 + i for i in range(8)])
    assert not mm.window_is_short(book)
    mm.reassert_frozen(book, "NIFTY")
    assert book.long == 53
    assert mm.window_is_short(book), "an 8-deep window cannot feed a 53 SMA"
