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
from datetime import date, datetime

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


def _make(quote_a=None, quote_b=None, *, mode="paper", model="basic",
          training=None, **kw):
    """Build a strategy with an injected quote_fn. config.ini provides the
    [kalman_pair_trading] section with max_leg_notional for paper mode.
    `training` overrides the default cointegrated window (e.g. for gate tests)."""
    pa, pb = training if training is not None else _training()
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
            # adf_gate_p=0 disables the regime gate so the band/state tests
            # exercise the threshold logic in isolation; the gate has its own
            # dedicated tests below.
            "kalman_pair_trading": {"max_leg_notional": "5000000",
                                    "exit_debounce_ticks": "1",
                                    "min_edge_multiplier": "0",
                                    "adf_gate_p": "0"},
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


def test_default_entry_band_rejects_book_s0_noise_touch():
    """entry_z defaults to 1.5, not the book's s₀=1: at 5-min resolution the
    z-score touches ±1 on intraday noise, and those shallow entries churn below
    the ~₹1.5k round-trip friction (2026-07-04 5-min revalidation — 1.5 beat
    1.0 on both independent half-windows). A z that the BOOK default would
    trade (|z|=1.2) must NOT enter; past 1.5 it must. If this fails, the
    noise-churn regression is back."""
    strat, quotes = _make()
    assert strat.entry_z == 1.5
    pa, pb = _push_z(strat, 1.2)          # book s₀=1 would enter here
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert strat.scan_and_propose() == []
    pa, pb = _push_z(strat, 1.7)          # beyond the raised band → trade
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert len(strat.scan_and_propose()) == 2


def test_regime_break_above_max_entry_z_refuses():
    """Past max_entry_z the spread has broken its relationship — entering there
    is the runaway-churn failure the static system hit. Must refuse."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 6.0)   # > default max_entry_z=5.0
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert strat.scan_and_propose() == []


def test_full_entry_then_mean_revert_exit_cycle():
    """End-to-end paper cycle: a deep-z entry opens a 2-leg position; when the
    spread reverts to the mean (book exit-at-0, exit_z default 0) the position
    closes and books a trade. Verifies state transitions, not just that methods
    return lists."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)        # z≥entry_z → SHORT_SPREAD
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    assert strat.state.position == "SHORT_SPREAD"
    assert len(strat.state.legs) == 2

    # Spread reverts to/through the mean → MEAN_REVERT close (SHORT unwinds at z≤0).
    pa, pb = _push_z(strat, -0.2)
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
    pa, pb = _push_z(strat, -0.2)       # revert through the mean → SHORT unwinds
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


def test_regime_gate_blocks_entry_on_nonstationary_residual():
    """The ADF regime gate must suppress entries when the recent RAW residual
    window is non-stationary (the pair isn't currently reverting) — the lever the
    s₀-sweep showed dominates the threshold. We drive the gate's contract directly
    with a random-walk window: a deep-z signal that fires with the gate off must
    be REFUSED with it on. The gate reads the RAW residual series on purpose;
    gating the Kalman normalized spread (stationary by construction) is inert.
    See tasks/kalman-pairs-rebase-plan.md."""
    strat, quotes = _make()
    strat.adf_gate_p = 0.05               # enable the gate
    rng = np.random.default_rng(7)
    # Unit-root (random walk) raw window → ADF cannot reject → gate closed.
    strat._raw_spread_history = list(np.cumsum(rng.normal(0, 1.0, 120)))
    strat._refresh_regime_adf()
    assert strat._gate_blocks_entry()
    pa, pb = _push_z(strat, 3.0)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert strat.scan_and_propose() == []   # blocked by the regime gate

    strat.adf_gate_p = 0.0                # gate off → the same signal fires
    assert len(strat.scan_and_propose()) == 2


def test_regime_gate_opens_on_stationary_residual():
    """Mirror of the block test: a stationary (white-noise) raw window means the
    spread IS reverting, so the gate stays OPEN and a deep-z entry fires. Without
    this the block test could pass by always blocking."""
    strat, quotes = _make()
    strat.adf_gate_p = 0.05
    rng = np.random.default_rng(7)
    strat._raw_spread_history = list(rng.normal(0, 1.0, 120))   # stationary
    strat._refresh_regime_adf()
    assert not strat._gate_blocks_entry()
    pa, pb = _push_z(strat, 3.0)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert len(strat.scan_and_propose()) == 2


def test_regime_gate_open_via_real_seed_on_cointegrated_data():
    """Production default is gate ON. A NORMALLY-constructed strategy seeded from
    cointegrated training (the REAL _seed_spread_history → innovation path, no
    injection) must leave the gate OPEN — _last_adf_p computable (not None) and
    entries permitted. Guards the fail-closed deploy risk: if the seeded raw
    window were unassessable (None → fail-closed), the strategy would silently
    never trade, and every other entry test disables the gate so none would catch
    it."""
    strat, quotes = _make()                 # default _training() is cointegrated
    strat.adf_gate_p = 0.05                  # production default
    strat._refresh_regime_adf()              # recompute from the REAL seeded window
    assert strat._last_adf_p is not None, "seeded window must be assessable, not fail-closed"
    assert not strat._gate_blocks_entry()
    pa, pb = _push_z(strat, 3.0)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    assert len(strat.scan_and_propose()) == 2


def _stationary_source(step_date, clock_dt):
    """A healthy pair with a full STATIONARY raw window (ADF would OPEN the gate),
    last stepped on `step_date`, clock at `clock_dt`. Returned serialized so a
    restore test can reload it into a strategy running later."""
    src, _ = _make(model="basic", clock=lambda: clock_dt)
    src.adf_gate_p = 0.05
    src._raw_spread_history = list(np.random.default_rng(11).normal(0, 1.0, 120))
    src._last_step_date = step_date
    src._refresh_regime_adf()
    return src


def test_restore_from_stale_state_file_degrades_gate():
    """issue #65: restoring a state file whose newest residual predates a long
    outage must NOT wave entries through on a stale-but-confident ADF verdict.
    The restored window is FULL and stationary (p computable — it would open the
    gate), but weeks old, so the gate must fail closed. Without the freshness
    guard the pair silently trades on a regime read from before the gap. This is
    the exact PR#64-review-#7 hole. (Surfacing of the block is asserted in
    test_stale_block_is_logged_with_the_right_reason + the runner's
    warn_if_gate_stale, not at restore — see code-review #65.)"""
    src = _stationary_source(date(2026, 1, 9), datetime(2026, 1, 12))
    assert not src._gate_is_stale() and not src._gate_blocks_entry(), "source is fresh"
    assert src._last_adf_p is not None and src._last_adf_p < 0.05, "gate would be OPEN"
    blob = src.serialize_state()

    tgt, _ = _make(model="basic", clock=lambda: datetime(2026, 2, 20))  # weeks later
    tgt.adf_gate_p = 0.05
    tgt.restore_state(blob)
    # The p-value is still confident (full window) — that is the trap.
    assert tgt._last_adf_p is not None and tgt._last_adf_p < 0.05
    assert tgt._gate_is_stale()
    assert tgt._gate_blocks_entry(), "stale window must fail closed despite a real p"


def test_stale_block_is_logged_with_the_right_reason(caplog):
    """When a stale window blocks an actionable entry signal, the skip log must
    name STALENESS, not the ADF p-value: a stale window can hold a confident p
    (<0.05), so the old 'ADF p=0.01 > 0.050' line was self-contradictory and
    pointed at cointegration instead of the data gap (code-review #65)."""
    src = _stationary_source(date(2026, 1, 9), datetime(2026, 1, 12))
    blob = src.serialize_state()
    tgt, quotes = _make(model="basic", clock=lambda: datetime(2026, 2, 20))
    tgt.adf_gate_p = 0.05
    tgt.restore_state(blob)
    assert tgt._gate_is_stale() and tgt._last_adf_p < 0.05   # the trap: confident p
    pa, pb = _push_z(tgt, 3.0)                                # a real entry signal
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    with caplog.at_level("INFO"):
        assert tgt.scan_and_propose() == []                  # blocked
    msgs = " ".join(r.message for r in caplog.records)
    assert "STALE" in msgs, "block must be attributed to staleness"
    assert "ADF p=" not in msgs, "must NOT misattribute a stale block to the p-value"


def test_future_dated_residual_fails_closed():
    """Backward clock skew / a future-dated state file (newest residual dated
    AFTER now) is unassessable — _trading_days_between returns 0 for end<=start,
    which would silently wave the pair through. The guard must fail CLOSED
    instead (code-review #65)."""
    strat, _ = _make(model="basic", clock=lambda: datetime(2026, 1, 1))
    strat.adf_gate_p = 0.05
    strat._last_step_date = date(2026, 6, 1)   # dated in the future vs the clock
    assert strat._gate_is_stale()
    assert strat._gate_blocks_entry()


def test_recent_restore_keeps_gate_live():
    """The mirror: restoring after a NORMAL short gap (the runner's catch_up has
    brought the window current) must leave the gate LIVE — otherwise the guard
    would suppress every restart. A residual dated one session back is not stale."""
    src = _stationary_source(date(2026, 1, 9), datetime(2026, 1, 12))
    blob = src.serialize_state()
    tgt, _ = _make(model="basic", clock=lambda: datetime(2026, 1, 12))  # ~1 day on
    tgt.adf_gate_p = 0.05
    tgt.restore_state(blob)
    assert not tgt._gate_is_stale()
    assert not tgt._gate_blocks_entry(), "a current window must keep trading"


def test_fresh_seed_pair_never_treated_as_stale():
    """A first-launch pair (never stepped → _last_step_date is None) trades on its
    training seed, which is current by construction (fresh bhavcopy panel each
    run). The staleness guard must EXEMPT it — else a brand-new pair would never
    trade. Guards against keying staleness off the seed instead of a real gap."""
    strat, _ = _make(model="basic")
    assert strat._last_step_date is None
    assert strat._gate_stale_trading_days() == 0
    assert not strat._gate_is_stale()


def test_stale_gate_self_heals_after_fresh_close():
    """After a stale restore, a single fresh daily close (current clock) brings
    _last_step_date current, so the gate re-opens — the guard is self-healing as
    the runner's catch_up / live stepping refills the window, not a permanent
    lockout."""
    src = _stationary_source(date(2026, 1, 9), datetime(2026, 1, 12))
    blob = src.serialize_state()
    tgt, _ = _make(model="basic", clock=lambda: datetime(2026, 2, 20))
    tgt.adf_gate_p = 0.05
    tgt.restore_state(blob)
    assert tgt._gate_blocks_entry()                     # stale → closed
    # A fresh close dated "now" (what catch_up / the live session does).
    tgt.step_daily_close(101.0, 100.0)
    assert not tgt._gate_is_stale()
    assert not tgt._gate_blocks_entry(), "gate must re-open once the window is current"


def test_zero_crossing_exit_closes_on_overshoot_past_mean():
    """Exit is entry-side aware (book exit-at-mean, §15.5.1), NOT a symmetric
    |z|≤exit_z band: a LONG entered deep-negative must CLOSE when the spread
    overshoots to strongly positive (it has reverted past the mean → take profit),
    instead of holding a now-reversed position until the far-side stop. A
    symmetric band would miss the overshoot and ride the reversal into a loss."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, -2.5)         # deep negative z → LONG_SPREAD
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    assert strat.state.position == "LONG_SPREAD"
    # Overshoot well past the mean to the opposite side (|z| > exit_z, z > 0).
    pa, pb = _push_z(strat, 3.0)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.check_and_rehedge())
    assert strat.state.position == "FLAT"
    assert strat.state.closed_trades[-1]["exit_reason"] == "MEAN_REVERT"


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


def test_rolled_leg_fill_is_booked_on_its_own_leg():
    """A fill carrying the contract a leg was OPENED in must be booked on THAT
    leg, even after the strategy has been re-seeded on the next front month.

    Why this matters: exit proposals are built from `leg.tradingsymbol`, but the
    runner re-seeds strategies on the current front month every morning. The old
    `symbol_a if prop.tradingsymbol == self.tradingsymbol_a else symbol_b`
    resolution missed for every leg-A fill after a roll and dumped it onto leg B.
    On 2026-08-28 that applied ~₹1,892 BHARTIARTL fills against a ₹399 COALINDIA
    leg and manufactured ₹528,705,886 of realized P&L on a pair that never closed
    a trade. The tell is asymmetric: leg A is never touched while leg B's
    entry_price drifts toward leg A's price.
    """
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    leg_a = next(l for l in strat.state.legs if l.symbol == "PA")
    leg_b = next(l for l in strat.state.legs if l.symbol == "PB")
    b_entry_before, b_qty_before = leg_b.entry_price, leg_b.quantity

    # The overnight roll: the strategy is re-seeded on the NEXT month while the
    # legs still hold the contracts they were opened in.
    strat.tradingsymbol_a, strat.tradingsymbol_b = "PA_FUT_NEXT", "PB_FUT_NEXT"

    # A fill for leg A's OWN (now off-front-month) contract.
    close_a = strat._make_proposal(leg_a.tradingsymbol, leg_a.lot_size,
                                   abs(leg_a.quantity), pa,
                                   "SELL" if leg_a.quantity > 0 else "BUY", "exit A")
    strat._apply_fill(close_a, pa)

    # Leg A closed; leg B untouched — not the reverse.
    assert not [l for l in strat.state.legs if l.symbol == "PA"], \
        "leg A's own fill did not close leg A"
    leg_b = next(l for l in strat.state.legs if l.symbol == "PB")
    assert leg_b.quantity == b_qty_before
    assert leg_b.entry_price == pytest.approx(b_entry_before), \
        "leg B absorbed a leg-A fill — fills are misattributed across legs"


def test_exit_is_refused_for_rolled_legs_rather_than_mispriced(caplog):
    """When the held legs are no longer the contracts the strategy is seeded on,
    an exit must be REFUSED, not booked at the front month's quote.

    Closing a JAN leg at the FEB price books the calendar basis as P&L, so
    run_paper_kalman_pairs deliberately strands rolled legs for manual square-off
    (its `test_flatten_strands_rolled_leg_it_cannot_square`). Refusing at the
    proposal layer keeps that policy AND stops the churn: check_and_rehedge
    re-proposes the same exit every tick, which on 2026-08-28 ran ~263 times in a
    single session.
    """
    import logging
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    assert strat.state.position == "SHORT_SPREAD"

    strat.tradingsymbol_a, strat.tradingsymbol_b = "PA_FUT_NEXT", "PB_FUT_NEXT"
    quotes["PA_FUT_NEXT"], quotes["PB_FUT_NEXT"] = quotes["PA_FUT"], quotes["PB_FUT"]
    pa, pb = _push_z(strat, -0.2)       # would otherwise MEAN_REVERT-exit
    for k in ("PA_FUT", "PA_FUT_NEXT"):
        quotes[k] = pa
    for k in ("PB_FUT", "PB_FUT_NEXT"):
        quotes[k] = pb

    with caplog.at_level(logging.WARNING):
        assert strat.check_and_rehedge() == []
        # Second tick: still refused, but not re-logged (263 identical warnings
        # is how the real incident buried itself).
        assert strat.check_and_rehedge() == []
    assert strat.state.position == "SHORT_SPREAD"   # left open for the operator
    warns = [r for r in caplog.records if "exit refused" in r.message]
    assert len(warns) == 1, f"expected exactly one refusal warning, got {len(warns)}"


def test_unmappable_fill_raises_instead_of_defaulting_to_leg_b():
    """A proposal whose tradingsymbol matches neither a held leg nor either
    configured contract must RAISE, not silently land on leg B (Rule 12). The
    old `if a else b` resolution had no failure mode at all, which is why the
    roll bug booked ₹52.9 crore in silence."""
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    stray = strat._make_proposal("WHO_KNOWS_FUT", 50, 1, 100.0, "SELL", "stray")
    with pytest.raises(ValueError, match="cannot be mapped"):
        strat._apply_fill(stray, 100.0)


def test_exit_that_leaves_legs_open_is_surfaced(caplog):
    """An exit that does not reach FLAT must be logged loudly. Without this the
    strategy silently re-proposes the same exit on the next tick, forever: on
    2026-08-28 a single mis-booked exit repeated ~263 times between 10:02 and
    15:25, compounding a one-tick error into ₹52.9 crore."""
    import logging
    strat, quotes = _make()
    pa, pb = _push_z(strat, 2.5)
    quotes["PA_FUT"], quotes["PB_FUT"] = pa, pb
    strat.execute_proposals(strat.scan_and_propose())
    # Exit only leg A, leaving leg B open — a half-closed book.
    leg_a = next(l for l in strat.state.legs if l.symbol == "PA")
    half = strat._make_proposal(leg_a.tradingsymbol, leg_a.lot_size,
                                abs(leg_a.quantity), pa, "BUY", "half exit")
    with caplog.at_level(logging.WARNING):
        strat.execute_proposals([half])
    assert any("did not reach FLAT" in r.message for r in caplog.records), \
        "a half-closed exit must be surfaced, not silently retried next tick"
