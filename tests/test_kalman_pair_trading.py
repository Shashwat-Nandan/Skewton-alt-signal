"""Tests for strategies.kalman_pair_trading.KalmanPairStrategy — Phase 1.

These encode the *intent* of the strategy layer (CLAUDE.md Rule 9), the parts a
"returns a list" test would not catch: the rolling-z entry/exit thresholds fire
on the right side of the band; the spread is strictly causal (today's predicted
state, no look-ahead); a full serialize→restore round-trip is identity INCLUDING
the Kalman filter's covariance (restoring only γ would silently corrupt
tracking); and the β-lock holds an open position's hedge ratio fixed even as the
filter keeps drifting.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.kalman_pair_trading import KalmanPairStrategy


def _training(n=300, gamma=0.7, mu=5.0, seed=1):
    """A cointegrated training window (raw prices, as the strategy uses)."""
    rng = np.random.default_rng(seed)
    pb = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    pa = mu + gamma * pb + rng.normal(0, 0.4, n)
    return pa, pb


def _make(quote_a=None, quote_b=None, *, mode="paper", model="basic", **kw):
    """Build a strategy with an injected quote_fn. config.ini provides the
    [kalman_pair_trading] section with max_leg_notional for paper mode."""
    pa, pb = _training()
    quotes = {"PA_FUT": quote_a, "PB_FUT": quote_b}
    strat = KalmanPairStrategy(
        kite=None, mode=mode,
        symbol_a="PA", symbol_b="PB",
        tradingsymbol_a="PA_FUT", tradingsymbol_b="PB_FUT",
        lot_size_a=50, lot_size_b=50,
        training_a=pa, training_b=pb,
        model=model,
        quote_fn=lambda ts: quotes.get(ts),
        **kw,
    )
    return strat, quotes


# config.ini on disk may lack our section / max_leg_notional; inject via a
# monkeypatched config so paper-mode construction succeeds deterministically.
@pytest.fixture(autouse=True)
def _cap_config(monkeypatch):
    import configparser
    real_read = configparser.ConfigParser.read

    def fake_read(self, *a, **k):
        self.read_dict({
            "strategy": {"total_capital": "500000"},
            # min_edge_multiplier=0 disables the cost-hurdle so the band/state
            # tests exercise the threshold logic in isolation; the hurdle has
            # its own dedicated test below.
            "kalman_pair_trading": {"max_leg_notional": "5000000",
                                    "exit_debounce_ticks": "1",
                                    "min_edge_multiplier": "0"},
        })
        return []
    monkeypatch.setattr(configparser.ConfigParser, "read", fake_read)
    yield
    monkeypatch.setattr(configparser.ConfigParser, "read", real_read)


def _push_z(strat, target_z):
    """Set live quotes so the observed normalized spread lands at ~target_z.
    Log-price model: spread = (log pa - γ·log pb - μ)/(1+γ); fix pb, solve pa."""
    mean, std = strat._rolling_window_stats()
    want_spread = mean + target_z * std
    mu, gamma = strat._current_state()
    pb = 100.0
    # spread = (log pa − γ·log pb − μ)/(1+|γ|); invert for pa.
    log_pa = want_spread * (1 + abs(gamma)) + gamma * np.log(pb) + mu
    return float(np.exp(log_pa)), pb


def test_entry_fires_on_correct_side_of_band():
    """z ≤ −entry_z must propose LONG_SPREAD (buy A / sell B); z ≥ +entry_z must
    propose SHORT_SPREAD. A flat |z| inside the band proposes nothing. This is
    the core entry contract — wrong side = systematically backwards trades."""
    strat, quotes = _make()
    # Deep negative z → LONG_SPREAD.
    pa, pb = _push_z(strat, -3.0)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    props = strat.scan_and_propose()
    assert len(props) == 2
    a = next(p for p in props if p.tradingsymbol == "PA_FUT")
    b = next(p for p in props if p.tradingsymbol == "PB_FUT")
    assert a.transaction_type == "BUY" and b.transaction_type == "SELL"

    # Inside the band → nothing.
    strat2, q2 = _make()
    pa, pb = _push_z(strat2, 0.5)
    q2["PA_FUT"], q2["PB_FUT"] = pa, pb
    assert strat2.scan_and_propose() == []

    # Deep positive z → SHORT_SPREAD.
    strat3, q3 = _make()
    pa, pb = _push_z(strat3, 3.0)
    q3["PA_FUT"], q3["PB_FUT"] = pa, pb
    props = strat3.scan_and_propose()
    a = next(p for p in props if p.tradingsymbol == "PA_FUT")
    assert a.transaction_type == "SELL"


def test_regime_break_above_max_entry_z_refuses():
    """Past max_entry_z the spread has broken its relationship — entering there
    is the runaway-churn failure the static system hit. Must refuse."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 6.0)   # > default max_entry_z=5.0
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert strat.scan_and_propose() == []


def test_full_entry_then_mean_revert_exit_cycle():
    """End-to-end paper cycle: a deep-z entry opens a 2-leg position; when the
    spread reverts inside exit_z the position closes and books a trade. Verifies
    state transitions, not just that methods return lists."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    assert strat.state.position in ("LONG_SPREAD", "SHORT_SPREAD")
    assert len(strat.state.legs) == 2

    # Spread reverts into the exit band → MEAN_REVERT close.
    pa, pb = _push_z(strat, 0.1)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.check_and_rehedge())
    assert strat.state.position == "FLAT"
    assert len(strat.state.legs) == 0
    assert len(strat.state.closed_trades) == 1
    assert strat.state.closed_trades[0]["exit_reason"] == "MEAN_REVERT"


def test_closed_trade_pnl_includes_entry_costs():
    """A closed trade's reported realized_pnl and transaction_costs must net
    BOTH entry and exit costs. The baseline is captured PRE-entry-fill, so after
    a single round trip the per-trade costs equal the full cumulative costs
    (all four leg-sides). Capturing it post-fill (the bug) would report only
    exit-side costs (~half) and overstate win%."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    pa, pb = _push_z(strat, 0.1)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.check_and_rehedge())
    assert strat.state.position == "FLAT"
    t = strat.state.closed_trades[0]
    # One round trip → the trade's costs are the FULL cumulative costs (entry+exit).
    assert t["transaction_costs"] == pytest.approx(strat.state.total_transaction_costs)
    assert t["transaction_costs"] > 0
    # And the per-trade realized is net of all costs (cumulative realized − 0 baseline).
    assert t["realized_pnl"] == pytest.approx(strat.state.realized_pnl)


def test_size_legs_skips_when_cap_binds():
    """When the ratio-correct leg notional exceeds max_leg_notional, the entry is
    skipped entirely (returns no proposals) rather than int-truncating both legs
    into a distorted, non-neutral hedge."""
    strat, quotes = _make()
    strat.max_leg_notional = 1000.0   # any real leg (lot 50 × price) blows this
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert strat.scan_and_propose() == []


def test_spread_uses_predicted_state_no_lookahead():
    """The intraday spread must price off the day's PREDICTED Kalman state
    (α_{t+1|t}), captured before today's prices are seen — never a state that
    incorporates the current observation. Pin it: the strategy's normalized
    spread equals the closed-form (pa − γ_today·pb − μ_today)/(1+γ_today)."""
    strat, _ = _make()
    mu, gamma = strat._mu_today, strat._gamma_today
    pa, pb = 175.0, 100.0
    expected = (np.log(pa) - gamma * np.log(pb) - mu) / (1 + gamma)
    assert abs(strat._normalized_spread(pa, pb) - expected) < 1e-12


def test_daily_update_advances_filter_and_window():
    """step_daily_close must advance γ and grow the z-window by exactly one —
    the once-per-day update (D1) the whole design rests on."""
    strat, _ = _make()
    n0 = len(strat._spread_history)
    g0 = strat._gamma_today
    for _ in range(30):
        strat.step_daily_close(180.0, 100.0)  # push γ toward 0.8-ish
    assert len(strat._spread_history) == n0 + 30
    assert strat._gamma_today != g0  # filter actually moved


def test_serialize_roundtrip_preserves_filter_covariance():
    """A restart must restore the FULL filter state. We prove it the only way
    that matters: after serialize→restore, the next daily update produces a
    byte-identical spread to the un-restarted strategy. If only γ were saved
    (not P), the covariance would reset and the spreads would diverge — the
    exact silent corruption the plan calls out."""
    strat, _ = _make()
    for _ in range(15):
        strat.step_daily_close(170.0, 100.0)

    clone, _ = _make()
    clone.restore_state(strat.serialize_state())
    # Drive both with the same new closes; spreads must stay identical.
    for px in (172.0, 168.0, 174.0):
        s1 = strat.step_daily_close(px, 100.0)
        s2 = clone.step_daily_close(px, 100.0)
        assert abs(s1 - s2) < 1e-12


def test_restore_rejects_wrong_pair():
    """Loading a state file for a different pair must fail loud, not silently
    run the wrong instruments (Rule 12)."""
    strat, _ = _make()
    blob = strat.serialize_state()
    blob["pair"] = ["XX", "YY"]
    with pytest.raises(ValueError):
        strat.restore_state(blob)


def test_serialize_preserves_risk_band_for_open_position():
    """A restart mid-open-position must not blank the structure risk band. It's
    computed only at entry, so it has to round-trip through serialize/restore —
    else the EOD/dashboard shows no stop/target for a live position."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    assert strat.state.position != "FLAT" and strat._last_risk_band is not None
    clone, _ = _make()
    clone.restore_state(strat.serialize_state())
    assert clone._last_risk_band == strat._last_risk_band


def test_beta_lock_holds_open_position_hedge_ratio():
    """Once a position is open, the hedge ratio used to manage it must stay
    pinned to entry-γ even as the filter keeps tracking and γ drifts. Otherwise
    a drifting filter moves the exit band out from under a live position."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    entry_gamma = strat.state.entry_gamma
    assert strat.hedge_ratio == entry_gamma

    # Drive the filter hard so its live γ drifts well away from entry.
    for _ in range(40):
        strat.step_daily_close(220.0, 100.0)
    assert strat._gamma_today != entry_gamma          # filter moved
    assert strat.hedge_ratio == entry_gamma           # but the lock holds
    # And the open position's spread still prices with the locked state.
    mu_lock, g_lock = strat._current_state()
    assert (mu_lock, g_lock) == (strat.state.entry_mu, strat.state.entry_gamma)


def test_negative_gamma_routes_both_legs_same_side():
    """For an inversely-cointegrated pair (γ<0, which passes the |γ| guard),
    ∂spread/∂log p_b flips sign, so leg B must take the SAME side as leg A —
    otherwise both legs are wrong-way and double the exposure. A hardcoded
    BUY-A/SELL-B would silently trade backwards (cloud-review bug_007)."""
    strat, quotes = _make()
    # Force a negative locked elasticity (strat is FLAT → hedge_ratio uses this).
    strat._gamma_today = -0.5
    strat._mu_today = 0.0
    pa, pb = _push_z(strat, -3.0)   # deep negative z → LONG_SPREAD
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    props = strat.scan_and_propose()
    assert len(props) == 2
    a = next(p for p in props if p.tradingsymbol == "PA_FUT")
    b = next(p for p in props if p.tradingsymbol == "PB_FUT")
    assert a.transaction_type == "BUY" and b.transaction_type == "BUY"

    # SHORT_SPREAD with γ<0 → both SELL.
    strat2, q2 = _make()
    strat2._gamma_today = -0.5
    strat2._mu_today = 0.0
    pa, pb = _push_z(strat2, 3.0)
    q2["PA_FUT"], q2["PB_FUT"] = pa, pb
    props = strat2.scan_and_propose()
    assert all(p.transaction_type == "SELL" for p in props)


def test_zero_or_halted_quote_rejected():
    """A 0.0 quote (Kite returns last_price=0 for halted instruments) must NOT
    drive a position or unrealized-P&L update — `nan <= 0` is False so an
    isfinite-only guard would let it through and book a phantom loss into the
    state/EOD the A/B test reads (cloud-review bug_005)."""
    strat, quotes = _make()
    quotes["PA_FUT"], quotes["PB_FUT"] = 0.0, 100.0
    spread, prices = strat._observe_spread()
    assert spread is None and prices == {}
    # And the full paths short-circuit cleanly rather than corrupting state.
    assert strat.scan_and_propose() == []
    assert strat.check_and_rehedge() == []


def test_cost_hurdle_blocks_marginal_entry():
    """The cost-hurdle must refuse an entry whose expected ₹ move to the exit
    band is below min_edge_multiplier × round-trip cost — the production
    feature that stops cost-bleed churn. With a high multiplier even a deep-z
    signal is refused; with it disabled the same signal fires."""
    strat, quotes = _make()
    strat.min_edge_multiplier = 50.0   # absurdly high → nothing clears it
    pa, pb = _push_z(strat, 3.0)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert strat.scan_and_propose() == []   # blocked by the hurdle

    strat.min_edge_multiplier = 0.0          # disabled → same signal fires
    assert len(strat.scan_and_propose()) == 2


def test_paper_mode_requires_notional_cap(monkeypatch):
    """Paper/live must refuse to construct without max_leg_notional — a
    misconfigured γ could otherwise size leg B unbounded (Rule 12)."""
    import configparser
    monkeypatch.setattr(configparser.ConfigParser, "read",
                        lambda self, *a, **k: [])  # empty config, no section
    pa, pb = _training()
    with pytest.raises(ValueError):
        KalmanPairStrategy(
            kite=None, mode="paper", symbol_a="PA", symbol_b="PB",
            tradingsymbol_a="PA_FUT", tradingsymbol_b="PB_FUT",
            lot_size_a=50, lot_size_b=50, training_a=pa, training_b=pb,
            quote_fn=lambda ts: None,
        )
