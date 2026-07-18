"""Phase-1 fitness-redesign instrumentation (2026-07-18).

Each test encodes a Phase-0 failure these metrics exist to prevent:
  F1 — lifetime counters / undated floats made sessions un-auditable;
  F2 — the book kept losing on 0.5-1.5% "middle" moves its profile was short;
  F4 — the WW rehedge gate froze gamma-scalp attribution for weeks.
If one of these tests fails, the fitness objective built on top of the
metrics is being fed garbage again — that is the regression being guarded,
not the arithmetic itself.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from greeks_engine import PortfolioGreeks
from strategies.taleb_karpathy import HedgeState, TalebKarpathyStrategy

FIXED_NOW = datetime(2026, 7, 18, 11, 0)


def _mk_hedger(capital: float = 1_000_000.0) -> TalebKarpathyStrategy:
    h = TalebKarpathyStrategy.__new__(TalebKarpathyStrategy)
    h.state = HedgeState()
    h.mode = "paper"
    h.underlying = "NIFTY"
    h._clock = lambda: FIXED_NOW
    h.immutable_params = {"total_capital": capital}
    h.tunable_params = {}
    h.greeks = MagicMock()
    return h


# ── breakeven_move_pct ──────────────────────────────────────


def _breakeven(theta_day: float, gamma: float, spot: float) -> float:
    h = _mk_hedger()
    h.state._last_scalp_anchor_spot = spot
    h.state.portfolio_greeks = PortfolioGreeks(
        net_shadow_theta=theta_day, net_shadow_gamma=gamma)
    return h.get_strategy_metrics()["breakeven_move_pct"]


def test_breakeven_move_scales_with_theta_rent():
    # WHY: the metric answers "how big a daily move re-earns the theta rent".
    # If theta rent doubles, the required move must grow by √2 — a fitness
    # objective using a mis-scaled breakeven would tolerate structures whose
    # rent the market's actual move distribution can never repay.
    b1 = _breakeven(theta_day=2000.0, gamma=0.02, spot=24000.0)
    b2 = _breakeven(theta_day=4000.0, gamma=0.02, spot=24000.0)
    assert b1 > 0
    assert abs(b2 / b1 - 2 ** 0.5) < 1e-9
    # Sanity of magnitude: ½·0.02·(24000·r)² = 2000 → r ≈ 1.86%
    assert 1.5 < b1 < 2.2


def test_breakeven_zero_for_short_gamma_or_flat_book():
    # WHY: a short-gamma (middle-short) book has no "breakeven move" — moves
    # only hurt it. Emitting a number here (or NaN) would let the optimizer
    # treat a short-vol book as an efficient long-vol one (Phase-0 F2).
    assert _breakeven(theta_day=2000.0, gamma=-0.05, spot=24000.0) == 0.0
    # Short-premium book EARNING theta: also undefined.
    assert _breakeven(theta_day=-500.0, gamma=0.02, spot=24000.0) == 0.0
    # Flat book (no spot anchor).
    h = _mk_hedger()
    assert h.get_strategy_metrics()["breakeven_move_pct"] == 0.0


# ── middle_band_worst_pnl ───────────────────────────────────


def test_middle_band_worst_pnl_flags_short_middle():
    # WHY: Phase-0 F2 — on 07-10 a +1.02% move cost ₹11.7k because the
    # backspread was short the ±1.5% middle while its far wings looked great.
    # The metric must report the worst P&L INSIDE the band and must NOT be
    # rescued by wing payoffs outside it.
    h = _mk_hedger()
    spot = 24000.0
    h.state._last_scalp_anchor_spot = spot
    profile = {
        23000.0: +50_000.0,   # far down wing (outside band)
        23760.0: -9_000.0,    # −1.0% (inside)
        24000.0: 0.0,         # at spot
        24240.0: -12_000.0,   # +1.0% (inside)
        24340.0: -14_000.0,   # ~+1.42% (inside — the worst in-band)
        25000.0: +80_000.0,   # far up wing (outside band)
    }
    h.state.portfolio_greeks = PortfolioGreeks(pnl_profile=profile)
    m = h.get_strategy_metrics()
    assert m["middle_band_worst_pnl"] == -14_000.0


def test_middle_band_worst_pnl_nonnegative_for_long_convexity():
    # WHY: a genuinely long-convexity book (long straddle) never loses much
    # inside the band beyond premium bleed already booked at 0 — the metric
    # must not manufacture a penalty for the structure the strategy is
    # SUPPOSED to hold.
    h = _mk_hedger()
    h.state._last_scalp_anchor_spot = 24000.0
    h.state.portfolio_greeks = PortfolioGreeks(pnl_profile={
        23760.0: 200.0, 24000.0: 0.0, 24240.0: 250.0, 25000.0: 30_000.0,
    })
    assert h.get_strategy_metrics()["middle_band_worst_pnl"] == 0.0


# ── theoretical_scalp_pnl accrual (F1/F4 guard) ─────────────


def _mk_ticking_hedger(gamma: float):
    h = _mk_hedger()
    h.state.positions = [SimpleNamespace(expiry="", tradingsymbol="X")]
    h.greeks.compute_portfolio_greeks = MagicMock(
        return_value=PortfolioGreeks(net_shadow_gamma=gamma))
    return h


def test_theoretical_scalp_accrues_without_any_rehedge():
    # WHY: Phase-0 F1/F4 — gamma_scalp_pnl froze for 7 straight sessions
    # because it only accrues when a rehedge is EMITTED and the WW gate
    # blocked every rehedge. The theoretical accrual must tick on every
    # greeks update regardless, or scalp-capture efficiency is unmeasurable.
    h = _mk_ticking_hedger(gamma=0.02)
    spots = iter([24000.0, 24100.0, 24100.0])
    h._get_spot_price = lambda: next(spots)
    h._update_portfolio_greeks()          # anchors at 24000, no accrual yet
    assert h.state.theoretical_scalp_pnl == 0.0
    h._update_portfolio_greeks()          # ΔS=100 → ½·0.02·100² = ₹100
    assert abs(h.state.theoretical_scalp_pnl - 100.0) < 1e-9
    h._update_portfolio_greeks()          # ΔS=0 → no further accrual
    assert abs(h.state.theoretical_scalp_pnl - 100.0) < 1e-9
    # No rehedge was ever emitted:
    assert h.state.rehedge_count == 0
    assert h.state.gamma_scalp_pnl == 0.0


def test_theoretical_scalp_signed_for_short_gamma():
    # WHY: a short-gamma book LOSES to realized variance. abs()-ing the gamma
    # would book that loss as a gain and bias the objective toward the losing
    # short-middle structures (same defect class as the pre-a7e3005 theta bug).
    h = _mk_ticking_hedger(gamma=-0.02)
    spots = iter([24000.0, 24100.0])
    h._get_spot_price = lambda: next(spots)
    h._update_portfolio_greeks()
    h._update_portfolio_greeks()
    assert abs(h.state.theoretical_scalp_pnl - (-100.0)) < 1e-9


def test_scalp_anchor_resets_when_flat():
    # WHY: a flat gap (exit at 24000, re-enter days later at 24500) is not
    # realized variance the book held gamma through; accruing across it would
    # inflate capture capacity and flatter churny configs (Phase-0 F3).
    h = _mk_ticking_hedger(gamma=0.02)
    spots = iter([24000.0, 24500.0])
    h._get_spot_price = lambda: next(spots)
    h._update_portfolio_greeks()          # anchor 24000
    h.state.positions = []
    h._update_portfolio_greeks()          # flat → anchor cleared
    assert h.state._last_scalp_anchor_spot is None
    h.state.positions = [SimpleNamespace(expiry="", tradingsymbol="X")]
    h._update_portfolio_greeks()          # re-anchor at 24500, NO accrual
    assert h.state.theoretical_scalp_pnl == 0.0


# ── session attribution record (F1 guard) ───────────────────


def test_session_attribution_reports_deltas_not_lifetime():
    # WHY: Phase-0 F1 — lifetime counters (theta ₹2.5M legacy artifact) are
    # unusable; the sidecar must report THIS session's deltas over the anchor,
    # with a date, or the fitness objective inherits the same garbage.
    h = _mk_hedger()
    h.state.total_pnl = -147_000.0
    h.state.theta_decay_paid = 2_500_000.0
    h.state.total_transaction_costs = 75_000.0
    anchor = h.snapshot_attribution_counters()
    # ... session happens:
    h.state.total_pnl += 5_000.0
    h.state.theta_decay_paid += 1_200.0
    h.state.total_transaction_costs += 800.0
    rec = h.get_session_attribution(anchor)
    assert rec["date"] == "2026-07-18"
    assert rec["session_total_pnl"] == 5_000.0
    assert rec["session_theta_decay_paid"] == 1_200.0
    assert rec["session_total_transaction_costs"] == 800.0
    # Lifetime kept separate, clearly labeled:
    assert rec["lifetime_total_pnl"] == -142_000.0
    assert json.dumps(rec, default=str)   # must be JSONL-serializable


def test_state_roundtrip_and_backcompat():
    # WHY: pre-2026-07-18 state files lack the new fields; restore must
    # default them (not crash the live-paper morning restore) and a roundtrip
    # must not lose accrued attribution.
    h = _mk_hedger()
    h.state.theoretical_scalp_pnl = 42.5
    h.state._last_scalp_anchor_spot = 24123.0
    payload = h.serialize_state()
    h2 = _mk_hedger()
    h2.restore_state(payload)
    assert h2.state.theoretical_scalp_pnl == 42.5
    assert h2.state._last_scalp_anchor_spot == 24123.0
    # Old state file: strip the new keys entirely.
    old = json.loads(json.dumps(payload, default=str))
    old["state"].pop("theoretical_scalp_pnl")
    old["state"].pop("_last_scalp_anchor_spot")
    h3 = _mk_hedger()
    h3.restore_state(old)
    assert h3.state.theoretical_scalp_pnl == 0.0
    assert h3.state._last_scalp_anchor_spot is None


# ── runner sidecar append (dated, append-only) ──────────────


def test_end_of_session_appends_dated_jsonl(tmp_path):
    # WHY: the sidecar is the dated record Phase-0 proved was missing. Two
    # sessions must yield two lines (append, never overwrite), each parseable
    # with its own date — this is what makes future sessions auditable.
    import run_paper

    class _FakeHedger:
        def __init__(self):
            self.state = SimpleNamespace(positions=[], futures_lots=0,
                                         futures_hedge_delta=0.0)
            self._n = 0

        def generate_eod_report(self):
            return {"status": "no_positions"}

        def get_session_attribution(self, anchor):
            self._n += 1
            return {"date": f"2026-07-{17 + self._n}", "session_total_pnl": 1.0}

        def _save_iv_history(self):
            pass

        def serialize_state(self):
            return {"saved_at": "x", "state": {}}

        def legs_expire_on(self, today):
            return False

    hedger = _FakeHedger()
    args = SimpleNamespace(force_flatten_on_exit=False)
    log = logging.getLogger("test_attr_sidecar")
    state_file = tmp_path / "state.json"
    attr_file = tmp_path / "taleb_attribution.jsonl"
    from datetime import date
    for _ in range(2):
        run_paper.end_of_session(hedger, date(2026, 7, 18), args, log,
                                 state_file, {"total_pnl": 0.0}, attr_file)
    lines = attr_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["date"] == "2026-07-18"
    assert json.loads(lines[1])["date"] == "2026-07-19"


def test_attribution_write_failure_does_not_block_state_persist(tmp_path):
    # WHY: attribution is measurement, state persistence is safety. A broken
    # sidecar path must never cost the book its restore file (Rule 12: the
    # exception is logged loudly instead).
    import run_paper

    class _FakeHedger:
        def __init__(self):
            self.state = SimpleNamespace(positions=[], futures_lots=0,
                                         futures_hedge_delta=0.0)

        def generate_eod_report(self):
            return {"status": "no_positions"}

        def get_session_attribution(self, anchor):
            raise RuntimeError("attribution exploded")

        def _save_iv_history(self):
            pass

        def serialize_state(self):
            return {"saved_at": "x", "state": {}}

        def legs_expire_on(self, today):
            return False

    from datetime import date
    state_file = tmp_path / "state.json"
    run_paper.end_of_session(
        _FakeHedger(), date(2026, 7, 18),
        SimpleNamespace(force_flatten_on_exit=False),
        logging.getLogger("test_attr_fail"), state_file,
        {"total_pnl": 0.0}, tmp_path / "attr.jsonl")
    assert state_file.exists()   # state persisted despite attribution failure
