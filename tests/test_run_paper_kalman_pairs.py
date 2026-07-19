"""Tests for run_paper_kalman_pairs — the Kalman paper runner's Kite-light
pieces (CLAUDE.md Rule 9).

The live tick loop / auth can't run without a broker, so these pin the parts
that CAN break silently and that the forward test depends on: front-month
resolution picks the right contract; build_strategies seeds a filter per pair
and skips non-cointegrated / unresolvable pairs (rather than crashing); the
once-per-session daily step advances the filter; and a full state write→load→
restore round-trips the filter state so a restart continues exactly (the whole
point of persisting the covariance, not just γ).
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

import configparser

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from research import backtest_kalman_pairs as BT
from runners import run_paper_kalman_pairs as R
from strategies.kalman_pair_trading import KalmanPairStrategy


HERE = os.path.dirname(os.path.dirname(__file__))


def test_kalman_pairs_entry_z_defaults_in_sync():
    """entry_z lives as independent literals in the runner argparse, the
    backtest argparse, the strategy cfg.get fallback, and both config
    [kalman_pair_trading] sections — before this guard they drifted silently
    (the 5-min revalidation raised the CODE literals to 1.5 but left the config
    template at the book's 1.0, which reintroduces the rejected noise-churn band
    on any direct-against-config construction). The runner argparse default is
    the value that ACTUALLY governs live paper (the deploy unit passes no
    --entry-z; _write_config writes it into the derived config), so pin every
    copy to it. If a future retune moves one and not the others, this fails."""
    EXPECTED = 1.5

    # Production-governing value: the runner argparse default (the deploy unit
    # inherits it, so a drift here silently changes the LIVE band).
    assert R.build_parser().parse_args([]).entry_z == EXPECTED
    # Backtest argparse default (what the revalidation is run through).
    assert BT.build_parser().parse_args([]).entry_z == EXPECTED
    # Strategy cfg.get fallback (direct construction with no entry_z present).
    assert _strategy_entry_z_default() == EXPECTED
    # Tracked config template's section (host config.ini is gitignored, so the
    # template is the guard). It documents values with inline `#` comments, so
    # parse with inline_comment_prefixes to read just the number.
    cfg = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cfg.read(os.path.join(HERE, "config_template.ini"))
    assert cfg.getfloat("kalman_pair_trading", "entry_z") == EXPECTED


def _strategy_entry_z_default() -> float:
    """The strategy's cfg.get('entry_z', …) fallback, exercised by constructing
    with a config that has the section but no entry_z key."""
    pb = 100.0 + np.cumsum(np.random.default_rng(0).normal(0, 0.5, 200))
    pa = 5.0 + 0.7 * pb + np.random.default_rng(1).normal(0, 0.4, 200)

    real_read = configparser.ConfigParser.read

    def fake_read(self, *a, **k):
        self.read_dict({"strategy": {"total_capital": "500000"},
                        "kalman_pair_trading": {"max_leg_notional": "5000000"}})
        return []
    configparser.ConfigParser.read = fake_read
    try:
        s = KalmanPairStrategy(
            kite=None, mode="paper", symbol_a="PA", symbol_b="PB",
            tradingsymbol_a="PA_FUT", tradingsymbol_b="PB_FUT",
            lot_size_a=50, lot_size_b=50, training_a=pa, training_b=pb,
            model="basic", quote_fn=lambda ts: None)
    finally:
        configparser.ConfigParser.read = real_read
    return s.entry_z


class FakeKite:
    """Returns a fixed last_price per NFO tradingsymbol."""
    def __init__(self, prices):
        self.prices = prices  # {"NFO:SYM_FUT": px}

    def quote(self, keys):
        return {k: {"last_price": self.prices[k]} for k in keys}


def _panel(n=300, seed=1):
    """Two cointegrated price series (raw) over n trading days."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2025-01-01", periods=n)
    pb = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    pa = 5.0 + 0.7 * pb + rng.normal(0, 0.4, n)
    return pd.DataFrame({"AAA": pa, "BBB": pb}, index=idx)


def _nfo():
    return [
        {"name": "AAA", "instrument_type": "FUT", "tradingsymbol": "AAA26JANFUT",
         "expiry": "2026-01-29", "lot_size": 50, "instrument_token": 1},
        {"name": "AAA", "instrument_type": "FUT", "tradingsymbol": "AAA26FEBFUT",
         "expiry": "2026-02-26", "lot_size": 50, "instrument_token": 2},
        {"name": "BBB", "instrument_type": "FUT", "tradingsymbol": "BBB26JANFUT",
         "expiry": "2026-01-29", "lot_size": 40, "instrument_token": 3},
    ]


def _config(tmp_path):
    p = tmp_path / "k.ini"
    p.write_text("[strategy]\ntotal_capital = 500000\n\n[kalman_pair_trading]\n"
                 "max_leg_notional = 5000000\nexit_debounce_ticks = 1\n"
                 "min_edge_multiplier = 0\n")
    return str(p)


def test_resolve_front_month_picks_nearest_expiry():
    """Must pick the smallest expiry on/after today — holding a back-month or an
    already-expired contract would mis-price the spread and mis-size legs."""
    info = R.resolve_front_month(_nfo(), "AAA", date(2026, 1, 1))
    assert info["tradingsymbol"] == "AAA26JANFUT" and info["lot_size"] == 50
    # After Jan expiry, it rolls to Feb.
    info2 = R.resolve_front_month(_nfo(), "AAA", date(2026, 2, 1))
    assert info2["tradingsymbol"] == "AAA26FEBFUT"


def test_build_skips_unresolvable_and_seeds_filter(tmp_path, caplog):
    """build_strategies seeds one filter per resolvable, cointegrated pair and
    skips the rest with a log — a missing contract must not crash the runner."""
    pairs = pd.DataFrame({"symbol_a": ["AAA", "AAA"], "symbol_b": ["BBB", "ZZZ"]})
    panel = _panel()
    panel["ZZZ"] = panel["BBB"]  # ZZZ has no FUT in _nfo() → unresolvable
    kite = FakeKite({})
    strats = R.build_strategies(pairs, panel, _nfo(), kite, _config(tmp_path),
                                date(2026, 1, 1), R.logger)
    assert len(strats) == 1
    s = strats[0]
    assert (s.symbol_a, s.symbol_b) == ("AAA", "BBB")
    assert len(s._spread_history) > 0 and np.isfinite(s._gamma_today)


def test_daily_step_and_state_roundtrip(tmp_path, monkeypatch):
    """write→load→restore must continue the filter EXACTLY: after a restart,
    the next daily step yields a byte-identical spread. Proves the runner
    persists the full filter state (covariance included), not just γ."""
    monkeypatch.setattr(R, "DATA_CACHE", tmp_path)
    monkeypatch.setattr(R, "STATE_PATH", tmp_path / "state.json")
    pairs = pd.DataFrame({"symbol_a": ["AAA"], "symbol_b": ["BBB"]})
    panel = _panel()
    kite = FakeKite({"NFO:AAA26JANFUT": 178.0, "NFO:BBB26JANFUT": 100.0})
    cfg = _config(tmp_path)
    strats = R.build_strategies(pairs, panel, _nfo(), kite, cfg, date(2026, 1, 1), R.logger)

    # Advance a few sessions' worth of daily steps, then persist. (Call the
    # strategy directly — step_filters_on_close is idempotent per calendar day,
    # so 5 same-day calls would only step once.)
    for _ in range(5):
        for s in strats:
            s.step_daily_close(178.0, 100.0)
    R.write_state_file(strats, R.logger)

    # Fresh build, restore, then step both originals and restored with the same
    # close — spreads must match to the bit.
    fresh = R.build_strategies(pairs, panel, _nfo(), kite, cfg, date(2026, 1, 1), R.logger)
    R.restore_matching(fresh, R.load_prior_state(R.logger), R.logger)
    s_old, s_new = strats[0], fresh[0]
    for px in (180.0, 176.0, 182.0):
        kite.prices["NFO:AAA26JANFUT"] = px
        a = s_old.step_daily_close(px, 100.0)
        b = s_new.step_daily_close(px, 100.0)
        assert abs(a - b) < 1e-12


def test_step_filters_isolates_bad_quote(tmp_path):
    """A NaN/halted close quote on one pair must NOT abort the others' daily
    step (and, in main(), the EOD state/sidecar writes that follow). The NaN
    must be caught — `nan <= 0` is False, so it would otherwise reach
    step_daily_close and raise mid-loop (cloud-review bug_004)."""
    panel = _panel()
    panel["CCC"] = panel["BBB"] * 1.01
    nfo = _nfo() + [{"name": "CCC", "instrument_type": "FUT",
                     "tradingsymbol": "CCC26JANFUT", "expiry": "2026-01-29",
                     "lot_size": 30, "instrument_token": 9}]
    pairs = pd.DataFrame({"symbol_a": ["AAA", "AAA"], "symbol_b": ["BBB", "CCC"]})
    # AAA leg quotes NaN (halted); the AAA/BBB and AAA/CCC pairs both touch it.
    kite = FakeKite({"NFO:AAA26JANFUT": float("nan"),
                     "NFO:BBB26JANFUT": 100.0, "NFO:CCC26JANFUT": 101.0})
    strats = R.build_strategies(pairs, panel, nfo, kite, _config(tmp_path),
                                date(2026, 1, 1), R.logger)
    before = [len(s._spread_history) for s in strats]
    # Must not raise; pairs with the NaN leg are skipped, not crashed.
    R.step_filters_on_close(strats, date(2026, 1, 15), R.logger)
    after = [len(s._spread_history) for s in strats]
    assert after == before  # no pair stepped (both share the NaN leg), no raise


def test_step_filters_on_close_is_idempotent_per_day(tmp_path):
    """A restart in the 15:25–15:30 window after a clean session-end must NOT
    re-step today's close. step_filters_on_close skips any pair already stepped
    today (_last_step_date == today) — else the z-window gets a duplicate spread
    and the Kalman state is double-advanced for one day."""
    panel = _panel()
    kite = FakeKite({"NFO:AAA26JANFUT": 178.0, "NFO:BBB26JANFUT": 100.0})
    strats = R.build_strategies(pd.DataFrame({"symbol_a": ["AAA"], "symbol_b": ["BBB"]}),
                                panel, _nfo(), kite, _config(tmp_path),
                                date(2026, 1, 1), R.logger)
    today = date(2026, 1, 15)
    strats[0]._clock = lambda: datetime(2026, 1, 15)  # so step sets _last_step_date=today
    n0 = len(strats[0]._spread_history)
    R.step_filters_on_close(strats, today, R.logger)
    assert len(strats[0]._spread_history) == n0 + 1   # stepped once
    R.step_filters_on_close(strats, today, R.logger)  # restart same day
    assert len(strats[0]._spread_history) == n0 + 1   # NOT stepped again


def test_catch_up_filters_steps_missed_days(tmp_path):
    """After a restart with a stale saved filter, catch_up_filters must replay
    the bhavcopy days that elapsed since _last_step_date so the filter and its
    z-window are current — the once-per-missed-day catch-up step_daily_close
    documents. Days already stepped (≤ last_step_date) and today must NOT be
    re-stepped."""
    panel = _panel(n=300)
    kite = FakeKite({"NFO:AAA26JANFUT": 178.0, "NFO:BBB26JANFUT": 100.0})
    cfg = _config(tmp_path)
    pairs = pd.DataFrame({"symbol_a": ["AAA"], "symbol_b": ["BBB"]})
    s = R.build_strategies(pairs, panel, _nfo(), kite, cfg, date(2026, 1, 1), R.logger)[0]
    # Simulate a saved filter last stepped 3 trading days before the panel end.
    last_idx = panel.index[-4]
    s._last_step_date = last_idx.date()
    hist_before = len(s._spread_history)
    last_before = s._last_step_date

    # "today" is after the panel end, so the 3 days panel[-3:] are missed.
    today = (panel.index[-1] + pd.Timedelta(days=5)).date()
    R.catch_up_filters([s], panel, today, R.logger)

    # Exactly the 3 days strictly after last_step_date and before today.
    assert len(s._spread_history) == hist_before + 3
    assert s._last_step_date == panel.index[-1].date() and s._last_step_date > last_before


def test_eod_sidecar_shape(tmp_path, monkeypatch):
    """The EOD sidecar must carry per-pair reports the verifier/dashboard read,
    with the same field shape as the static system's EOD."""
    monkeypatch.setattr(R, "DATA_CACHE", tmp_path)
    pairs = pd.DataFrame({"symbol_a": ["AAA"], "symbol_b": ["BBB"]})
    kite = FakeKite({"NFO:AAA26JANFUT": 178.0, "NFO:BBB26JANFUT": 100.0})
    strats = R.build_strategies(pairs, _panel(), _nfo(), kite, _config(tmp_path),
                                date(2026, 1, 1), R.logger)
    R.write_eod_sidecar(strats, date(2026, 1, 15), R.logger)
    import json
    blob = json.loads((tmp_path / "pair_paper_kalman_eod_2026-01-15.json").read_text())
    assert blob["system"] == "kalman" and len(blob["pairs"]) == 1
    rep = blob["pairs"][0]
    for k in ("pair", "hedge_ratio", "position", "realized_pnl",
              "session_realized_delta", "gamma_filter"):
        assert k in rep


# ──────────────────────────────────────────────────────────────────
# Expiry-day flatten — the gap was: this runner carried open STF legs through
# contract expiry (the static runner force-flattens; this one originally didn't).
# These tests are the A/B that the fix flattens ON expiry day and ONLY then.
# ──────────────────────────────────────────────────────────────────
def _entered_strategy(tmp_path, today):
    """One AAA/BBB strategy driven into a real LONG_SPREAD entry, so it holds
    genuine legs (AAA26JANFUT / BBB26JANFUT, both expiring 2026-01-29)."""
    pairs = pd.DataFrame({"symbol_a": ["AAA"], "symbol_b": ["BBB"]})
    kite = FakeKite({"NFO:AAA26JANFUT": 178.0, "NFO:BBB26JANFUT": 100.0})
    s = R.build_strategies(pairs, _panel(), _nfo(), kite, _config(tmp_path),
                           today, R.logger)[0]
    spread, prices = s._observe_spread()
    props = s._build_entry_proposals("LONG_SPREAD", -1.5, spread, prices)
    assert props, "entry should propose legs (min_edge_multiplier=0 in test cfg)"
    s.execute_proposals(props)
    assert s.state.position == "LONG_SPREAD" and len(s.state.legs) == 2
    return s, kite


def test_flatten_expiring_legs_squares_off_on_expiry_day(tmp_path):
    """A/B (expiry side): on the leg's last trading day the session-close expiry
    flatten squares the pair off — closing the bug where the Kalman runner
    carried single-stock-futures legs into cash settlement."""
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    R.flatten_expiring_legs([s], _nfo(), date(2026, 1, 29), R.logger)  # 26JAN expiry
    assert s.state.position == "FLAT" and not s.state.legs
    assert s.state.closed_trades, "a closed trade should be recorded on flatten"


def test_flatten_expiring_legs_holds_when_not_expiry(tmp_path):
    """A/B (control side): on a non-expiry day the same path leaves the position
    untouched — the flatten fires ONLY on the contract's last trading day, so it
    can't square off healthy positions early."""
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    R.flatten_expiring_legs([s], _nfo(), date(2026, 1, 15), R.logger)
    assert s.state.position == "LONG_SPREAD" and len(s.state.legs) == 2


def test_old_close_path_carried_expiring_leg(tmp_path):
    """Regression anchor: the OLD close path (step_filters_on_close alone) does
    NOT flatten an expiring leg — this is exactly the gap the fix closes, so if a
    future refactor drops the flatten call this test documents the pre-fix bug."""
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    R.step_filters_on_close([s], date(2026, 1, 29), R.logger)
    assert s.state.position == "LONG_SPREAD"  # carried into expiry (the bug)


def test_flatten_on_missed_expiry_day(tmp_path):
    """Hardening (<= today): if the runner MISSED the expiry close (didn't run /
    crashed) and the contract is still on the chain the next session, the leg
    (expiry 26JAN) is still squared off — `== today` would have carried it."""
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    # today is AFTER 26JAN expiry (2026-01-29) but AAA26JANFUT is still listed.
    R.flatten_expiring_legs([s], _nfo(), date(2026, 2, 10), R.logger)
    assert s.state.position == "FLAT" and not s.state.legs


def test_flatten_raises_on_off_chain_held_leg(tmp_path):
    """Hardening (off-chain): a held leg that has dropped off the NFO chain can't
    be quoted to auto-flatten, so the runner RAISES (operator-alert) rather than
    silently carrying it — the exact state the JUN pairs land in next session."""
    import pytest
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    # NFO no longer lists the 26JAN legs (only a later contract) → legs off-chain.
    feb_only = [{"name": "AAA", "instrument_type": "FUT",
                 "tradingsymbol": "AAA26FEBFUT", "expiry": "2026-02-26",
                 "lot_size": 50, "instrument_token": 2}]
    with pytest.raises(RuntimeError):
        R.flatten_expiring_legs([s], feb_only, date(2026, 2, 10), R.logger)
    assert s.state.position == "LONG_SPREAD"  # left open for manual square-off


def test_flatten_expiring_raises_on_empty_nfo_with_open_book(tmp_path):
    """H18 parity: an open book plus an unusable NFO list must RAISE rather than
    silently return — carrying a contract into settlement is the worst outcome."""
    import pytest
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    with pytest.raises(RuntimeError):
        R.flatten_expiring_legs([s], [], date(2026, 1, 29), R.logger)


def test_flatten_strands_pair_when_leg_unquotable(tmp_path):
    """Silent-carry guard: if an expiring leg can't be quoted at the close (an
    illiquid contract returns last_price=0), flatten_one is a no-op — so the
    runner must re-check the book, find it still open, STRAND it and RAISE rather
    than exit clean and carry the contract into settlement."""
    import pytest
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    s._quote_fn = lambda ts: 0.0  # halted/illiquid → rejected as no-quote
    with pytest.raises(RuntimeError):
        R.flatten_expiring_legs([s], _nfo(), date(2026, 1, 29), R.logger)
    assert s.state.position == "LONG_SPREAD"  # left open, surfaced for the operator


def test_flatten_strands_rolled_leg_it_cannot_square(tmp_path):
    """A rolled leg (held contract no longer the front month) must NOT be silently
    mis-flattened. The strategy's fill path resolves a leg by matching the front-
    month tradingsymbol, so it can't re-map a rolled leg and the book won't reach
    FLAT — the re-check must STRAND it (raise) for manual square-off rather than
    book the exit at the wrong contract's price (finding 3)."""
    import pytest
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    # Front month rolled to FEB while the open legs still hold the JAN contracts;
    # both contracts are quotable (so the flatten is attempted, not a no-op).
    s.tradingsymbol_a, s.tradingsymbol_b = "AAA26FEBFUT", "BBB26FEBFUT"
    px = {"AAA26JANFUT": 178.0, "BBB26JANFUT": 100.0,
          "AAA26FEBFUT": 181.0, "BBB26FEBFUT": 102.0}
    s._quote_fn = lambda ts: px.get(ts)
    with pytest.raises(RuntimeError):
        R.flatten_expiring_legs([s], _nfo(), date(2026, 1, 29), R.logger)
    assert s.state.position != "FLAT"  # surfaced for manual square-off, not silent


def test_malformed_expiry_strands_leg_not_silently_carried(tmp_path):
    """A held leg whose NFO row has a junk/empty expiry must NOT silently slip the
    flatten: _expiry_by_tradingsymbol drops it (not a real date), so it shows as
    off-chain → stranded+raise, rather than mapping to a value that fails
    `<= today` and carrying the contract (finding 6)."""
    import pytest
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    bad = [{"name": "AAA", "instrument_type": "FUT", "tradingsymbol": "AAA26JANFUT",
            "expiry": "", "lot_size": 50, "instrument_token": 1},          # junk
           {"name": "BBB", "instrument_type": "FUT", "tradingsymbol": "BBB26JANFUT",
            "expiry": "2026-01-29", "lot_size": 40, "instrument_token": 3}]
    with pytest.raises(RuntimeError):
        R.flatten_expiring_legs([s], bad, date(2026, 1, 29), R.logger)
    assert s.state.position != "FLAT"


def test_expiry_by_tradingsymbol_drops_non_dates():
    """Map only well-formed expiries; junk/empty strings are dropped (so the leg
    later reads as off-chain, the safe fail-loud path)."""
    m = R._expiry_by_tradingsymbol([
        {"tradingsymbol": "AAA26JANFUT", "expiry": "2026-01-29"},
        {"tradingsymbol": "BAD1", "expiry": ""},
        {"tradingsymbol": "BAD2", "expiry": "not-a-date"},
        {"tradingsymbol": "NOEXP"},
    ])
    assert m == {"AAA26JANFUT": date(2026, 1, 29)}


def test_warn_if_long_break_warns_on_open_book_before_gap(tmp_path, caplog):
    """M-R1: an open book before a ≥3-day market break must emit a WARNING (this
    runner carries non-expiring positions, so they sit unmonitored across it)."""
    import logging
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    # Fri 2026-01-30, Mon 2026-02-02 is a holiday → next trading day is Tue
    # 2026-02-03, 4 calendar days away (≥3) with an open book → warn.
    with caplog.at_level(logging.WARNING, logger="kalman_pairs"):
        R.warn_if_long_break([s], date(2026, 1, 30), {date(2026, 2, 2)}, R.logger)
    assert any("M-R1" in r.message for r in caplog.records)


def test_warn_if_long_break_silent_when_flat_or_short_gap(tmp_path, caplog):
    """No false alarm: a flat book, or a normal overnight gap, must not warn."""
    import logging
    s, _ = _entered_strategy(tmp_path, date(2026, 1, 1))
    # (1) Flat book before the same long weekend → no warning.
    s.state.position = "FLAT"
    with caplog.at_level(logging.WARNING, logger="kalman_pairs"):
        R.warn_if_long_break([s], date(2026, 1, 30), {date(2026, 2, 2)}, R.logger)
    assert not any("M-R1" in r.message for r in caplog.records)
    caplog.clear()
    # (2) Open book but only a normal overnight gap (Thu 01-29 → Fri 01-30, gap 1);
    # holidays non-empty so warn_if_long_break doesn't early-return.
    s.state.position = "LONG_SPREAD"
    with caplog.at_level(logging.WARNING, logger="kalman_pairs"):
        R.warn_if_long_break([s], date(2026, 1, 29), {date(2026, 1, 1)}, R.logger)
    assert not any("M-R1" in r.message for r in caplog.records)


# ──────────────────────────────────────────────────────────────────
# Entry suppression near expiry (issue #70) — don't OPEN a position so close to
# front-month expiry it has no room to revert; held positions still exit/flatten.
# (A near-expiry guard, not a max-hold guarantee — see entry_suppressed's note.)
# ──────────────────────────────────────────────────────────────────
def test_entry_suppressed_near_front_month_expiry():
    """NEW entries are suppressed when the front-month future is within the cutoff
    of expiry; outside the cutoff, on a disabled cutoff, or for an undatable
    (off-chain) leg they are not. The earlier-expiring leg drives the decision."""
    class _S:  # plain (id-hashable) stub; entry_suppressed only reads tradingsymbol_a/b
        def __init__(self, ta, tb):
            self.symbol_a, self.symbol_b = "AAA", "BBB"
            self.tradingsymbol_a, self.tradingsymbol_b = ta, tb

    nfo = _nfo()  # AAA26JANFUT/BBB26JANFUT exp 2026-01-29; AAA26FEBFUT exp 02-26
    es = R.entry_suppressed
    jan = _S("AAA26JANFUT", "BBB26JANFUT")
    feb = _S("AAA26FEBFUT", "AAA26FEBFUT")
    # 2 days before JAN expiry, cutoff 3 → JAN pair suppressed, FEB pair not.
    sup = es([jan, feb], nfo, date(2026, 1, 27), 3)
    assert jan in sup and feb not in sup
    # Exactly `cutoff` days out counts as within → suppressed.
    assert jan in es([jan], nfo, date(2026, 1, 26), 3)
    # 4 days out is outside the cutoff → not suppressed.
    assert jan not in es([jan], nfo, date(2026, 1, 25), 3)
    # cutoff 0 disables entirely.
    assert es([jan], nfo, date(2026, 1, 28), 0) == set()
    # The earlier-expiring leg (JAN) drives a mixed-month pair → suppressed.
    mixed = _S("AAA26JANFUT", "AAA26FEBFUT")
    assert mixed in es([mixed], nfo, date(2026, 1, 28), 3)
    # An off-chain tradingsymbol can't be dated → not suppressed (handled elsewhere).
    off = _S("X26JANFUT", "Y26JANFUT")
    assert off not in es([off], nfo, date(2026, 1, 28), 3)


def test_warn_if_gate_stale_flags_only_stale_pairs_after_catchup(caplog):
    """After catch_up, a pair whose gate is STILL stale is genuinely behind (the
    DATA is stale, not just the runner) → one loud WARNING per restart; a current
    pair stays silent. This is the non-false-alarm surfacing that replaced the
    per-refresh WARN removed in code-review #65."""
    import logging

    class _S:
        def __init__(self, a, stale, days):
            self.symbol_a, self.symbol_b = a, "BBB"
            self._STALE_GATE_MAX_TRADING_DAYS = 5
            self._stale, self._days = stale, days

        def _gate_is_stale(self):
            return self._stale

        def _gate_stale_trading_days(self):
            return self._days

    stale, fresh = _S("STALEPAIR", True, 30), _S("FRESHPAIR", False, 1)
    with caplog.at_level(logging.WARNING):
        out = R.warn_if_gate_stale([stale, fresh], R.logger)
    assert out == [stale]
    msgs = " ".join(r.message for r in caplog.records)
    assert "STALEPAIR" in msgs and "STALE" in msgs
    assert "FRESHPAIR" not in msgs, "a current pair must not warn"


def test_tick_one_halt_new_suppresses_entry_but_allows_exit():
    """The suppression rides the existing halt_new path: with halt_new=True
    tick_one must NOT scan for entries but MUST still rehedge/exit — that's why
    entry-suppression is safe for a held near-expiry position (issue #70)."""
    calls = []

    class _Spy:
        symbol_a, symbol_b = "AAA", "BBB"

        def scan_and_propose(self):
            calls.append("scan")
            return []

        def check_and_rehedge(self):
            calls.append("rehedge")
            return []

        def execute_proposals(self, props):
            calls.append("execute")

    s = _Spy()
    R.tick_one(s, R.logger, halt_new=True)
    assert "scan" not in calls and "rehedge" in calls   # entries blocked, exits run
    calls.clear()
    R.tick_one(s, R.logger, halt_new=False)
    assert "scan" in calls and "rehedge" in calls        # both run when not halted
