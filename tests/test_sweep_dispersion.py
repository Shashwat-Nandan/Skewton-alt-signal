"""The dispersion grid's selection must read the training cycles only.

A selection that peeks at the holdout, or that crowns a config on a handful
of cycles, turns a sign check into a fitted backtest. These pin the
pre-registered rule from tasks/todo.md (2026-10-02).
"""
from __future__ import annotations

import pandas as pd
import pytest

from research import backtest_dispersion as bd
from research.sweep_dispersion import (
    CONFIG_COLS,
    Config,
    applied,
    grid,
    select,
    split,
)


def _rows(cfg: dict, nets, expiries):
    return [
        {**cfg, "expiry": e, "net": n, "status": "ok"}
        for e, n in zip(expiries, nets)
    ]


def _cfg(**kw):
    base = {"variant": "matched_ff", "flatten_dte": 2, "m_rho_q": 0.8,
            "coverage": "base", "book": "A", "exit_mode": "expiry", "hedge": "future"}
    base.update(kw)
    return base


TRAIN = [f"2025-{m:02d}-28" for m in range(1, 11)]
HOLD = ["2026-02-24", "2026-03-30"]


def test_selection_ignores_the_holdout():
    # B wins the train by a little; A has a huge holdout. A holdout-reading
    # selector would pick A.
    frame = pd.DataFrame(
        _rows(_cfg(hedge="none"), [10] * 10 + [10_000] * 2, TRAIN + HOLD)
        + _rows(_cfg(hedge="future"), [11] * 10 + [-5] * 2, TRAIN + HOLD)
    )
    train, hold = split(frame)
    assert len(hold) == 4
    pick = select(train)
    assert pick["hedge"] == "future"


def test_too_few_training_cycles_cannot_win():
    # Book B opened on three cycles and made a fortune. Three is not a test.
    frame = pd.DataFrame(
        _rows(_cfg(book="B"), [1_000_000] * 3, TRAIN[:3])
        + _rows(_cfg(book="A"), [1] * 10, TRAIN)
    )
    pick = select(frame)
    assert pick["book"] == "A"
    assert select(pd.DataFrame(_rows(_cfg(), [1] * 3, TRAIN[:3]))) is None


def test_ties_go_to_the_smaller_worst_loss():
    frame = pd.DataFrame(
        _rows(_cfg(exit_mode="flatten"), [100, -90] + [0] * 8, TRAIN)
        + _rows(_cfg(exit_mode="expiry"), [10, 0] + [0] * 8, TRAIN)
    )
    assert select(frame)["exit_mode"] == "expiry"


def test_config_is_applied_then_restored():
    before = (bd.FLATTEN_DTE, bd.M_RHO_QUANTILE, bd.MIN_COVERED_WEIGHT,
              bd.MIN_COVERED_NAMES, bd.MAX_COVERED_NAMES)
    with applied(Config("short_vol", 9, 0.5, "wide")):
        assert bd.FLATTEN_DTE == 9
        assert bd.M_RHO_QUANTILE == 0.5
        assert bd.MIN_COVERED_WEIGHT == 0.50
        assert (bd.MIN_COVERED_NAMES, bd.MAX_COVERED_NAMES) == (0.50, 0.60)
    after = (bd.FLATTEN_DTE, bd.M_RHO_QUANTILE, bd.MIN_COVERED_WEIGHT,
             bd.MIN_COVERED_NAMES, bd.MAX_COVERED_NAMES)
    assert after == before
    with pytest.raises(ValueError):
        with applied(Config("short_vol", 2, 0.8, "huge")):
            pass
    assert bd.FLATTEN_DTE == before[0]


def test_grid_is_the_registered_36():
    g = grid()
    assert len(g) == 36 == len(set(g))
    assert {c.variant for c in g} == {"short_vol", "matched_equal", "matched_ff"}
    assert set(CONFIG_COLS) >= {"book", "exit_mode", "hedge"}
