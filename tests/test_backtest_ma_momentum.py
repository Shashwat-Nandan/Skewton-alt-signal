"""Tests for the §6.3 MA-momentum harness.

The control already won the Kalman A/B. This module exists to freeze those
windows and score a pre-registered kill — not to discover new SMA lengths.
A test that would still pass after someone wires `fit_ma_crossover` back in
is the exact class of test this file is here to prevent.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from research import backtest_ma_momentum as m
from research import optimize_kalman_trend as opt


def test_frozen_params_are_the_paper_control():
    """The +₹40,310 scoreboard number was produced by THESE windows. Changing
    them is a new strategy, not a replay of the control."""
    n = m.FROZEN_PARAMS["NIFTY"]
    b = m.FROZEN_PARAMS["BANKNIFTY"]
    assert (n["short"], n["long"]) == (34, 53)
    assert (b["short"], b["long"]) == (27, 109)
    assert n["target_ticks"] is None and b["target_ticks"] is None
    assert m.LOT_SIZE == {"NIFTY": 75, "BANKNIFTY": 15}
    assert m.COST_PER_UNIT_POINTS == 2.5
    assert m.FIT_START == date(2026, 6, 5)


def test_harness_source_does_not_call_cmaes():
    """§6.3: do not jointly refit SMA lengths. A replay that silently
    re-optimizes is how Kalman-trend overfit."""
    src = inspect.getsource(m)
    assert "fit_ma_crossover" not in src
    assert "run_cmaes" not in src
    assert "fit_kalman" not in src


def test_kill_fires_when_nothing_traded():
    """No trades is not a free pass — it is 'no edge to measure'."""
    empty = m.score_slice(pd.Series(dtype=float), n_trades=0, lot=75)
    reasons = m.kill_reasons(empty)
    assert reasons == ["OOS-prior trades = 0"]


def test_kill_fires_on_nonpositive_sharpe_and_cost_hurdle():
    """E2: a losing or cost-eaten prior must not promote. Sharpe of a
    steadily-losing daily series is negative; net also misses 2×RT×n."""
    daily = pd.Series([-100.0, -80.0, -140.0, -90.0] * 5)
    scored = m.score_slice(daily, n_trades=4, lot=75)
    assert scored["sharpe"] < 0
    assert scored["net"] < scored["hurdle"]
    reasons = m.kill_reasons(scored)
    assert any("Sharpe" in r for r in reasons)
    assert any("2×RT×n" in r for r in reasons)


def test_kill_clears_a_real_positive_prior():
    daily = pd.Series([400.0, -50.0, 300.0, 200.0, 100.0] * 8)
    scored = m.score_slice(daily, n_trades=2, lot=75)
    # 2 trades × 2× ₹375 RT = ₹1,500 hurdle; net is well above.
    assert scored["sharpe"] > 0
    assert scored["net"] >= scored["hurdle"]
    assert m.kill_reasons(scored) == []


def test_split_prior_is_the_40d_warmup_boundary():
    """The Jul-15 refit trained on ~40 calendar days of 5-min. Dates inside
    that window are in-sample for these params and must not be the gate."""
    rows = []
    for d in pd.bdate_range("2026-05-01", "2026-07-10"):
        rows.append({"session": d.date(), "close": 1.0})
    df = pd.DataFrame(rows)
    prior, fit, post = m.split_prior_fit(df)
    assert prior["session"].max() < date(2026, 6, 5)
    assert fit["session"].min() >= date(2026, 6, 5)
    assert fit["session"].max() < date(2026, 7, 15)
    assert post.empty


def test_post_refit_sessions_are_oos_not_in_sample():
    """An unbounded fit window absorbs every session the tape gains later, so
    real post-refit OOS evidence gets printed as 'FIT WINDOW (IS)' and is
    excluded from the only slice kill_reasons() reads."""
    rows = [{"session": d.date(), "close": 1.0}
            for d in pd.bdate_range("2026-05-01", "2026-08-20")]
    df = pd.DataFrame(rows)
    prior, fit, post = m.split_prior_fit(df)
    assert fit["session"].max() < date(2026, 7, 15)
    assert not post.empty
    assert post["session"].min() >= date(2026, 7, 15)
    # the gate is unchanged: post-refit rows are in neither prior nor fit
    assert len(prior) + len(fit) + len(post) == len(df)


def _bars(n_days, *, start, px0, drift, bars_per_day=8) -> pd.DataFrame:
    rows = []
    px = float(px0)
    d = start
    while len({r["session"] for r in rows}) < n_days:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        for i in range(bars_per_day):
            px += drift
            ts = datetime(d.year, d.month, d.day, 9, 15) + timedelta(minutes=5 * i)
            rows.append({
                "datetime": ts, "session": d,
                "open": px, "high": px + 0.2, "low": px - 0.2, "close": px,
            })
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def test_replay_models_session_flatten_and_does_not_refit(monkeypatch):
    """The 15:25 flatten is the paper runner's execution. A close-only
    multi-day hold is the #121 bug this replay must not reintroduce. Also
    pins that the frozen short/long actually reach ma_direction."""
    seen = {}

    real_ma = opt.ma_direction
    real_sim = opt.simulate

    def wrap_ma(prices, *, short, long, offset, warmup=5):
        seen["short"] = short
        seen["long"] = long
        return real_ma(prices, short=short, long=long, offset=offset, warmup=warmup)

    def wrap_sim(*a, **k):
        seen["session_ends"] = k.get("session_ends")
        seen["target_ticks"] = k.get("target_ticks")
        seen["highs"] = k.get("highs")
        return real_sim(*a, **k)

    monkeypatch.setattr(opt, "ma_direction", wrap_ma)
    monkeypatch.setattr(opt, "simulate", wrap_sim)
    # Keep the harness looking up the wrappers.
    monkeypatch.setattr(m.opt, "ma_direction", wrap_ma)
    monkeypatch.setattr(m.opt, "simulate", wrap_sim)

    df = _bars(15, start=date(2026, 1, 5), px0=100.0, drift=0.4)
    params = {"short": 3, "long": 8, "offset": 0.0,
              "stop_ticks": 50.0, "target_ticks": None}
    out = m.replay(df, params, lot=75)
    assert seen["short"] == 3 and seen["long"] == 8
    assert seen["target_ticks"] is None
    assert seen["session_ends"] is not None
    assert int(np.asarray(seen["session_ends"]).sum()) == 15
    assert seen["highs"] is not None
    assert out["n_trades"] >= 1


def test_replay_reports_calendar_days_not_bar_count():
    """simulate() annualizes as if each 5-min bar were a day. The gate must
    use calendar-day ₹ or a quiet-but-churny session looks like a huge Sharpe."""
    df = _bars(10, start=date(2026, 1, 5), px0=200.0, drift=0.3)
    params = {"short": 2, "long": 5, "offset": 0.0,
              "stop_ticks": 80.0, "target_ticks": None}
    out = m.replay(df, params, lot=75)
    assert out["n_days"] == 10
    assert len(out["daily"]) == 10


def test_run_symbol_gates_on_the_prior_slice_not_the_fit_window():
    """The Jul-15 windows are in-sample from 2026-06-05. A fit-window
    jackpot must not be the thing `kills` reads."""
    prior_df = _bars(12, start=date(2026, 4, 1), px0=250.0, drift=0.3)
    fit_df = _bars(8, start=date(2026, 6, 8), px0=200.0, drift=0.8)
    df = pd.concat([prior_df, fit_df], ignore_index=True)
    r = m.run_symbol("NIFTY", df=df)
    assert r["prior"]["n_days"] == 12
    assert r["fit"]["n_days"] == 8
    assert r["kills"] == m.kill_reasons(r["prior"])
    assert r["params"]["short"] == 34 and r["params"]["long"] == 53
