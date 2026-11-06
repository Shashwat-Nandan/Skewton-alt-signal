"""Owner rule 2026-11-05: pair trading never opens a pair with a negative
hedge ratio or correlation below 0.75, and such a pair is not even a
candidate. A negative β puts both legs on the same side, so it is a
directional bet and not a hedge. The gate has three layers because any one
alone leaves a door open: the screen's output, the runner's selection from
an older CSV, and a held pair that closes mid-session and could re-enter."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core import screen_pairs as sp
from strategies.pair_trading import PairTradingStrategy


def _row(a, b, beta=1.0, corr=0.9):
    return {"symbol_a": a, "symbol_b": b, "hedge_ratio": beta, "correlation": corr,
            "coint_pvalue": 0.001, "half_life_days": 2.0, "spread_vol_pct": 3.0,
            "rank_score": 0.1}


def test_the_floor_is_the_owner_number():
    assert sp.QUALITY_MIN_CORR == 0.75


def test_candidate_list_drops_negative_beta_and_weak_correlation():
    df = pd.DataFrame([
        _row("GOOD", "PAIR"),
        _row("NEG", "BETA", beta=-0.15),
        _row("WEAK", "CORR", corr=0.7499),
        _row("EDGE", "CORR", corr=0.75),
        _row("ZERO", "BETA", beta=0.0),
        _row("NAN", "CORR", corr=float("nan")),
    ])
    kept = sp.drop_untradeable_candidates(df)
    assert list(kept["symbol_a"]) == ["GOOD", "EDGE"]


def test_the_gate_handles_an_empty_screen():
    assert sp.drop_untradeable_candidates(pd.DataFrame()).empty


def test_runner_refuses_a_bad_row_from_a_csv_written_before_the_gate():
    df = pd.DataFrame([
        _row("GOOD", "PAIR"),
        _row("NEG", "BETA", beta=-0.15),
        _row("WEAK", "CORR", corr=0.70),
    ])
    out = sp.classify_pair_candidates(df, top=5).set_index("symbol_a")
    assert out.loc["GOOD", "skip_reason"] == ""
    assert out.loc["NEG", "skip_reason"] == "beta"
    assert out.loc["WEAK", "skip_reason"] == "quality"


def _panel(seed=7, n=420):
    """Cointegrated legs, plus an anti-moving pair that passes |corr| but has
    a negative β: what the screen's abs() prefilter lets through."""
    rng = np.random.default_rng(seed)
    base = 1000 + np.cumsum(rng.normal(0, 8, n))
    noise = lambda s: rng.normal(0, s, n)  # noqa: E731
    return pd.DataFrame({
        "AAA": base + noise(4),
        "BBB": 0.8 * base + noise(4),
        "CCC": 4000 - base + noise(4),
    }, index=pd.bdate_range("2024-01-01", periods=n))


def test_persistent_screen_output_has_no_negative_beta(monkeypatch):
    panel = _panel()
    unfiltered = sp.screen_pairs(panel.iloc[-130:])
    assert (unfiltered["hedge_ratio"] < 0).any()    # the fixture bites
    out = sp.screen_pairs_persistent(panel, min_persistence=2, window_days=130, step_days=45)
    assert not out.empty
    assert (out["hedge_ratio"] > 0).all() and (out["correlation"] >= 0.75).all()


def test_baseline_cli_does_not_write_a_bad_row(monkeypatch, tmp_path):
    out = tmp_path / "pair_candidates.csv"
    monkeypatch.setattr(sp, "load_front_month_panel", lambda *a, **k: _panel())
    monkeypatch.setattr("sys.argv", ["screen_pairs", "--output", str(out)])
    assert sp.main() in (0, None)
    written = pd.read_csv(out)
    assert not written.empty
    assert (written["hedge_ratio"] > 0).all() and (written["correlation"] >= 0.75).all()


def test_a_closed_orphan_does_not_re_enter():
    """The screen dropped this pair while it was held. Once flat, a fresh z
    signal must not open it again."""
    stub = SimpleNamespace(
        allow_new_entries=False, hedge_ratio=1.0,
        state=SimpleNamespace(position="FLAT", unwind_pending=False, legs=[]),
        _is_in_stop_cooldown=lambda: pytest.fail("must stop before any entry check"),
    )
    assert PairTradingStrategy.scan_and_propose(stub) == []


def _orphan(monkeypatch, beta=-0.123, candidate_rows=None):
    import logging

    import runners.run_paper_pairs as r

    class _Fake:
        def __init__(self, **kw):
            self.symbol_a, self.symbol_b = kw["symbol_a"], kw["symbol_b"]
            self.hedge_ratio = kw["hedge_ratio"]
            self.state = SimpleNamespace(position="SHORT_SPREAD", legs=[1, 2])

        def restore_state(self, blob):
            pass

        def log_effective_params(self):
            pass

    monkeypatch.setattr("strategies.pair_trading.PairTradingStrategy", _Fake)
    args = SimpleNamespace(system="persistent", entry_z=2.0, exit_z=0.75, stop_z=4.0,
                           lookback_days=60, max_holding_days=7, lots_per_leg=1,
                           max_leg_notional=1e6, stop_cooldown_minutes=60, mode="paper")
    blob = {"pair": ["KOTAKBANK", "HINDUNILVR"], "hedge_ratio": beta,
            "state": {"position": "SHORT_SPREAD", "legs": [{"x": 1}, {"x": 2}]}}
    return r.build_orphan_strategies(
        {"KOTAKBANK/HINDUNILVR": blob}, set(), args, None, "config.ini",
        logging.getLogger("t"), candidate_rows=candidate_rows)


def test_an_orphan_the_screen_dropped_is_managed_but_cannot_re_enter(monkeypatch):
    orphans = _orphan(monkeypatch)
    assert [s.symbol_a for s in orphans] == ["KOTAKBANK"]
    assert orphans[0].hedge_ratio == -0.123       # still managed on its saved β
    assert orphans[0].allow_new_entries is False


def test_an_orphan_that_only_missed_the_cutoff_keeps_trading(monkeypatch):
    """Below --top or at the leg cap is not the owner's rule: the block must
    not remove trades the rule does not ask to remove."""
    orphans = _orphan(monkeypatch, beta=1.2, candidate_rows={"KOTAKBANK/HINDUNILVR"})
    assert orphans[0].allow_new_entries is True


def test_candidate_keys_lists_only_rows_that_pass_the_rule():
    df = pd.DataFrame([_row("GOOD", "PAIR"), _row("NEG", "BETA", beta=-1.0),
                       _row("WEAK", "CORR", corr=0.7)])
    assert sp.candidate_keys(df) == {"GOOD/PAIR"}
    assert sp.candidate_keys(pd.DataFrame()) == set()


def test_a_saved_negative_beta_cannot_open_a_new_position():
    """Today's screen can list a held pair with β +0.4 while the position was
    opened at -0.5. It keeps the saved β, so once flat it must not re-open."""
    stub = SimpleNamespace(
        allow_new_entries=True, hedge_ratio=-0.5,
        state=SimpleNamespace(position="FLAT", unwind_pending=False, legs=[]),
        _is_in_stop_cooldown=lambda: pytest.fail("must stop before any entry check"),
    )
    assert PairTradingStrategy.scan_and_propose(stub) == []


def test_a_list_the_gate_empties_fails_loud_and_keeps_the_old_file(monkeypatch, tmp_path):
    out = tmp_path / "pair_candidates.csv"
    out.write_text("old")
    monkeypatch.setattr(sp, "load_front_month_panel", lambda *a, **k: _panel())
    monkeypatch.setattr(sp, "QUALITY_MIN_CORR", 1.01)
    monkeypatch.setattr("sys.argv", ["screen_pairs", "--output", str(out)])
    assert sp.main() == 1
    assert out.read_text() == "old"


def test_survivors_are_re_ranked_over_the_rows_that_remain():
    df = pd.DataFrame([{**_row("A", "A1"), "coint_pvalue": 0.04, "rank_score": 0.1},
                       {**_row("B", "B1"), "coint_pvalue": 0.01, "rank_score": 0.9},
                       _row("NEG", "BETA", beta=-1.0)])
    kept = sp.drop_untradeable_candidates(df)
    assert list(kept["symbol_a"]) == ["B", "A"]
    assert kept["rank_score"].tolist() == sorted(kept["rank_score"])


def test_the_module_entry_point_comes_after_everything_main_needs():
    """`python -m core.screen_pairs` is how the nightly screen runs. A guard
    above a helper main() calls raises NameError at run time, though every
    test that imports the module first still passes."""
    tree = ast.parse(Path(sp.__file__).read_text())
    last = tree.body[-1]
    assert isinstance(last, ast.If) and "__name__" in ast.unparse(last.test)
