"""
Tests for backend/routers/positions.py — the dashboard's per-system block.

Pins two things that were wrong and silently misreported the Taleb book:
- The group/label must name the structure that was ACTUALLY traded. Regime
  dispatch routes to straddle / asymmetric_strangle / calendar / … ; the block
  used to hardcode "Taleb straddle" for every trade.
- realized_pnl must be the TRUE net. The taleb runner deducts costs from
  realized_pnl as legs close, so the per-trade `gross_pnl` field is already
  net — adding `costs` back (the old bug) understated every loss by the cost.
"""

import json
from datetime import date

from backend.routers import positions


def _write_state(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(positions, "DATA_CACHE", tmp_path)
    (tmp_path / "taleb_paper_state.json").write_text(json.dumps(payload))


def _payload(structure="asymmetric_strangle", gross_pnl=-12106.19, costs=2551.19):
    # Mirrors the real taleb_paper_state schema: a closed trade carries
    # `gross_pnl` (already net of costs in the runner) + a positive `costs`.
    return {
        "saved_at": "2026-06-17T15:25:00",
        "mode": "paper",
        "state": {
            "positions": [],
            "active_structure_types": [structure],
            "realized_pnl": gross_pnl,
            "unrealized_pnl": 0.0,
            "total_transaction_costs": costs,
            "closed_trades": [{
                "entry_time": "2026-06-17 10:09:34",
                "exit_time": "2026-06-17 14:52:58",
                "structure": structure,
                "gross_pnl": gross_pnl,
                "costs": costs,
                "n_rehedges": 3,
                "holding_minutes": 283,
            }],
        },
    }


class TestStructureNaming:
    def test_label_is_structure_agnostic(self, tmp_path, monkeypatch):
        # The strategy trades five structures — the system label must not claim
        # "straddle".
        _write_state(tmp_path, monkeypatch, _payload())
        block = positions._build_taleb_block(date(2026, 6, 17))
        assert block.label == "Taleb hedger"

    def test_closed_trade_group_names_actual_structure(self, tmp_path, monkeypatch):
        _write_state(tmp_path, monkeypatch, _payload("asymmetric_strangle"))
        block = positions._build_taleb_block(date(2026, 6, 17))
        (ct,) = block.closed_today
        assert ct.group == "NIFTY asymmetric strangle"

    def test_open_position_group_from_active_structures(self, tmp_path, monkeypatch):
        payload = _payload("backspread")
        payload["state"]["positions"] = [{
            "tradingsymbol": "NIFTY26JUN24000CE", "quantity": 8, "lot_size": 25,
            "entry_price": 100.0, "current_price": 90.0,
            "strike": 24000, "expiry": "2026-06-26", "option_type": "CE",
        }]
        _write_state(tmp_path, monkeypatch, payload)
        block = positions._build_taleb_block(date(2026, 6, 17))
        (op,) = block.open_positions
        assert op.group == "NIFTY backspread"

    def test_unknown_structure_falls_back_not_to_straddle(self, tmp_path, monkeypatch):
        # A trade closed before `structure` was recorded must NOT be mislabelled
        # a straddle.
        payload = _payload()
        del payload["state"]["closed_trades"][0]["structure"]
        _write_state(tmp_path, monkeypatch, payload)
        block = positions._build_taleb_block(date(2026, 6, 17))
        assert block.closed_today[0].group == "NIFTY options"


class TestNetPnl:
    def test_realized_pnl_is_net_not_pre_cost(self, tmp_path, monkeypatch):
        # gross_pnl field is ALREADY net (runner does realized_pnl -= cost).
        # The bug reported gross + costs = -9555, hiding ₹2551 of cost.
        _write_state(tmp_path, monkeypatch,
                     _payload(gross_pnl=-12106.19, costs=2551.19))
        block = positions._build_taleb_block(date(2026, 6, 17))
        (ct,) = block.closed_today
        assert ct.realized_pnl == -12106.19         # true net
        assert ct.realized_pnl != -12106.19 + 2551.19  # the old wrong number
        assert ct.transaction_costs == 2551.19

    def test_summary_realized_matches_state_net(self, tmp_path, monkeypatch):
        _write_state(tmp_path, monkeypatch, _payload())
        block = positions._build_taleb_block(date(2026, 6, 17))
        assert block.summary.realized_pnl == -12106.19


class TestKalmanBlock:
    """The Kalman paper book must appear as its own Positions block. Its state
    file is named off the `*paper_state*` glob (to stay out of the live notional
    cap), so list_positions must list it explicitly; it reuses the pair block
    builder, so the live open legs surface like any other pair system."""

    def _write_kalman(self, tmp_path, monkeypatch, *, position, legs, closed=None):
        monkeypatch.setattr(positions, "DATA_CACHE", tmp_path)
        (tmp_path / "kalman_pairs_runner_state.json").write_text(json.dumps({
            "system": "kalman", "mode": "paper",
            "updated_at": "2026-06-29T11:00:00",
            "pairs": [{
                "pair": ["ICICIBANK", "BPCL"],
                "state": {
                    "position": position, "entry_z": 2.1,
                    "entry_time": "2026-06-29T10:20:00",
                    "realized_pnl": -50.0, "unrealized_pnl": 1200.0,
                    "total_transaction_costs": 300.0,
                    "legs": legs, "closed_trades": closed or [],
                },
            }],
        }))

    def test_open_position_surfaces_with_correct_sides(self, tmp_path, monkeypatch):
        self._write_kalman(tmp_path, monkeypatch, position="SHORT_SPREAD", legs=[
            {"symbol": "ICICIBANK", "tradingsymbol": "ICICIBANK29JUNFUT",
             "lot_size": 700, "quantity": -1, "entry_price": 1370.0, "current_price": 1365.0},
            {"symbol": "BPCL", "tradingsymbol": "BPCL29JUNFUT",
             "lot_size": 1800, "quantity": 1, "entry_price": 310.0, "current_price": 311.0},
        ])
        block = next(b for b in positions.list_positions().systems if b.name == "kalman")
        assert block.available and block.mode == "paper"
        assert block.label.startswith("Pair trading — Kalman")
        assert {p.tradingsymbol for p in block.open_positions} == {
            "ICICIBANK29JUNFUT", "BPCL29JUNFUT"}
        short = next(p for p in block.open_positions if p.tradingsymbol == "ICICIBANK29JUNFUT")
        assert short.side == "SHORT" and short.quantity == 1   # signed qty → side
        assert block.summary.unrealized_pnl == 1200.0
        assert block.summary.n_open_positions == 2

    def test_absent_state_file_is_unavailable_not_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(positions, "DATA_CACHE", tmp_path)  # no kalman file written
        block = next(b for b in positions.list_positions().systems if b.name == "kalman")
        assert block.available is False and block.open_positions == []


class TestBankniftyTalebVisibility:
    """Issue #87: the BANKNIFTY instance (#62/PR #86) had NO dashboard
    visibility — positions read only the legacy NIFTY state file. WHY these
    matter: #87's gate ('paper shows tradeable behaviour before the loop is
    built') can only be judged from the dashboard; an invisible book means
    the gate gets decided on vibes. The filename contract mirrors
    run_paper.derive_paths: NIFTY legacy-unsuffixed, others suffixed."""

    def test_banknifty_block_reads_suffixed_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(positions, "DATA_CACHE", tmp_path)
        (tmp_path / "taleb_paper_state_BANKNIFTY.json").write_text(json.dumps({
            "saved_at": "2026-07-07T15:25:00",
            "state": {"positions": [], "closed_trades": [],
                      "realized_pnl": -1234.0, "unrealized_pnl": 10.0,
                      "total_transaction_costs": 55.0,
                      "active_structure_types": ["straddle"]},
        }))
        block = positions._build_taleb_block(date(2026, 7, 7),
                                             underlying="BANKNIFTY")
        assert block.available
        assert block.name == "taleb_banknifty"          # distinct system key
        assert block.state_file == "taleb_paper_state_BANKNIFTY.json"
        assert block.summary.realized_pnl == -1234.0

    def test_banknifty_absent_state_is_unavailable_not_nifty_fallback(
            self, tmp_path, monkeypatch):
        # Only the NIFTY file exists: the BANKNIFTY block must report
        # available=False, NEVER silently render NIFTY's book under the
        # BANKNIFTY label (the orphaned-state hazard from the #62 review).
        monkeypatch.setattr(positions, "DATA_CACHE", tmp_path)
        (tmp_path / "taleb_paper_state.json").write_text(json.dumps({
            "state": {"realized_pnl": 999.0}}))
        block = positions._build_taleb_block(date(2026, 7, 7),
                                             underlying="BANKNIFTY")
        assert not block.available
        assert block.summary.realized_pnl == 0.0

    def test_groups_are_labelled_with_the_underlying(self, tmp_path, monkeypatch):
        assert positions._taleb_group(["straddle"], "BANKNIFTY").startswith("BANKNIFTY")
        assert positions._taleb_group(None, "BANKNIFTY") == "BANKNIFTY options"
        # NIFTY default unchanged (legacy callers pass no underlying).
        assert positions._taleb_group(None) == "NIFTY options"

    def test_list_positions_contains_both_taleb_instances(self, tmp_path, monkeypatch):
        monkeypatch.setattr(positions, "DATA_CACHE", tmp_path)
        names = [s.name for s in positions.list_positions().systems]
        assert "taleb" in names and "taleb_banknifty" in names
