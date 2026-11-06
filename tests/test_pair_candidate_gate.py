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
        allow_new_entries=False, hedge_ratio=1.0, symbol_a="A", symbol_b="B",
        state=SimpleNamespace(position="FLAT", unwind_pending=False, legs=[]),
        _is_in_stop_cooldown=lambda: pytest.fail("must stop before any entry check"),
    )
    assert PairTradingStrategy.scan_and_propose(stub) == []


def _orphan(monkeypatch, beta=-0.123, reentry_pairs=None):
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
        logging.getLogger("t"), reentry_pairs=reentry_pairs)


def test_an_orphan_the_screen_dropped_is_managed_but_cannot_re_enter(monkeypatch):
    orphans = _orphan(monkeypatch)
    assert [s.symbol_a for s in orphans] == ["KOTAKBANK"]
    assert orphans[0].hedge_ratio == -0.123       # still managed on its saved β
    assert orphans[0].allow_new_entries is False


def test_an_orphan_that_only_missed_the_cutoff_keeps_trading_in_either_order(monkeypatch):
    """Below --top or at the leg cap is not the owner's rule. The screen can
    also list the pair the other way round on a later day."""
    import runners.run_paper_pairs as r
    orphans = _orphan(monkeypatch, beta=1.2, reentry_pairs={r.pair_id("HINDUNILVR", "KOTAKBANK")})
    assert orphans[0].allow_new_entries is True


def _select(tmp_path, rows, top):
    import logging

    import runners.run_paper_pairs as r
    csv = tmp_path / "c.csv"
    pd.DataFrame(rows).assign(latest_z_score=0.0).to_csv(csv, index=False)
    out = set()
    picks = r.select_pairs(top, logging.getLogger("t"), candidates_path=csv,
                           max_age_days=0, reentry_pairs=out)
    return r, picks, out


def test_only_rows_skipped_by_the_cutoff_may_re_enter(tmp_path):
    """A row the quality filters refused, or one at the leg cap, must not get
    back in through the orphan path, past the floor or the concentration cap."""
    ranked = [("AAA", "BBB"), ("AAA", "CCC"), ("AAA", "DDD"), ("EEE", "FFF"), ("GGG", "HHH")]
    rows = [{**_row(a, b), "coint_pvalue": 0.001 * (i + 1), "half_life_days": 1.0 + i,
             "spread_vol_pct": 10.0 - i, "correlation": 0.95 - 0.01 * i}
            for i, (a, b) in enumerate(ranked)]
    rows += [{**_row("SLOW", "HL"), "half_life_days": 12.0},
             _row("NEG", "BETA", beta=-1.0), _row("WEAK", "CORR", corr=0.7)]
    r, picks, out = _select(tmp_path, rows, top=3)
    # AAA is at its cap of 2 after BBB and CCC, so DDD is leg_cap; FFF fills --top.
    assert list(picks["symbol_b"]) == ["BBB", "CCC", "FFF"]
    assert out == {r.pair_id("GGG", "HHH")}


def _manage_only(monkeypatch, tmp_path, prior, csv_text=None):
    import logging

    import runners.run_paper_pairs as r
    alerts = []
    monkeypatch.setattr(r, "load_prior_state", lambda *a: prior)
    monkeypatch.setattr(r, "alert_operator", lambda log, text: alerts.append(text))
    path = tmp_path / "c.csv"
    if csv_text is not None:
        path.write_text(csv_text)
    args = SimpleNamespace(top=3, candidates=str(path), max_csv_age_days=0,
                           quality_max_pvalue=None, system="persistent")
    picks = r.select_pairs_or_manage_only(args, logging.getLogger("t"), seed_leg_count=None,
                                          reentry_pairs=set())
    return picks, alerts


_HELD = {"A/B": {"state": {"position": "LONG_SPREAD", "legs": [1, 2]}}}
_FLAT = {"A/B": {"state": {"position": "FLAT", "legs": []}}}
_EMPTY_LIST = ("symbol_a,symbol_b,correlation,hedge_ratio,coint_pvalue,half_life_days,"
               "spread_vol_pct,last_data_date\n")


@pytest.mark.parametrize("csv_text", [None, "", "symbol_a,symbol_b\nA,B\n", _EMPTY_LIST],
                         ids=["missing", "truncated-to-nothing", "missing-columns", "empty-list"])
def test_a_refused_selection_still_manages_open_positions_and_pages(monkeypatch, tmp_path, csv_text):
    """Dying here leaves held live pairs with no stop and no exit. A quiet log
    line would leave the book idle for days with nobody told."""
    picks, alerts = _manage_only(monkeypatch, tmp_path, _HELD, csv_text)
    assert picks.empty and {"symbol_a", "symbol_b"} <= set(picks.columns)
    assert len(alerts) == 1 and "MANAGE-ONLY" in alerts[0]


def test_an_empty_list_with_nothing_held_is_an_idle_day_not_a_crash(monkeypatch, tmp_path):
    """A crash here would latch HALT_ALL over a legitimately empty list."""
    picks, alerts = _manage_only(monkeypatch, tmp_path, _FLAT, _EMPTY_LIST)
    assert picks.empty and len(alerts) == 1


def test_a_missing_list_with_nothing_held_still_fails_loud(monkeypatch, tmp_path):
    with pytest.raises(FileNotFoundError):
        _manage_only(monkeypatch, tmp_path, _FLAT)


def test_the_alert_never_logs_the_bot_token(monkeypatch, caplog):
    import logging

    import runners.run_paper_pairs as r
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET-TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")

    def boom(url, **k):
        raise OSError(f"failed: {url}")

    monkeypatch.setattr(r.urllib.request, "urlopen", boom)
    r.alert_operator(logging.getLogger("t"), "x")
    assert "SECRET-TOKEN" not in caplog.text and "OSError" in caplog.text


def test_a_nan_hedge_ratio_fails_closed():
    stub = SimpleNamespace(
        allow_new_entries=True, hedge_ratio=float("nan"), symbol_a="A", symbol_b="B",
        state=SimpleNamespace(position="FLAT", unwind_pending=False, legs=[]),
        _is_in_stop_cooldown=lambda: pytest.fail("must stop before any entry check"),
    )
    assert PairTradingStrategy.scan_and_propose(stub) == []


def test_a_saved_negative_beta_cannot_open_a_new_position():
    """Today's screen can list a held pair with β +0.4 while the position was
    opened at -0.5. It keeps the saved β, so once flat it must not re-open."""
    stub = SimpleNamespace(
        allow_new_entries=True, hedge_ratio=-0.5, symbol_a="A", symbol_b="B",
        state=SimpleNamespace(position="FLAT", unwind_pending=False, legs=[]),
        _is_in_stop_cooldown=lambda: pytest.fail("must stop before any entry check"),
    )
    assert PairTradingStrategy.scan_and_propose(stub) == []


def test_a_list_the_gate_empties_replaces_the_old_file(monkeypatch, tmp_path):
    """Yesterday's rows carry yesterday's numbers and would pass again, so they
    must not outlive a screen that rejected them. Baseline runs first under
    `set -e`, so its exit 0 lets the live list refresh; persistent fails loud."""
    out = tmp_path / "pair_candidates.csv"
    out.write_text("old")
    monkeypatch.setattr(sp, "load_front_month_panel", lambda *a, **k: _panel())
    monkeypatch.setattr(sp, "QUALITY_MIN_CORR", 1.01)
    monkeypatch.setattr("sys.argv", ["screen_pairs", "--output", str(out)])
    assert sp.main() == 0
    written = pd.read_csv(out)
    assert written.empty and "hedge_ratio" in written.columns
    assert not list(tmp_path.glob("*.tmp"))


def test_a_screen_that_could_not_judge_keeps_the_old_file_and_fails_loud(monkeypatch, tmp_path):
    out = tmp_path / "pair_candidates.csv"
    out.write_text("old")
    monkeypatch.setattr(sp, "load_front_month_panel", lambda *a, **k: _panel(n=60))
    monkeypatch.setattr("sys.argv", ["screen_pairs", "--output", str(out),
                                     "--persistence-min", "2"])
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
