"""pair_trading → signal contract: the §4.14 mapper and the
execute_proposals publish hooks (issue #90 increment 1).

Rule 9: the mapper tests pin the STRUCTURE semantics (one signal per pair
decision, gcd ratios, broker-agnostic instruments) because those are the
contract-level defenses against the JUN/JUL lot-mismatch failure; the
integration tests prove every book-mutating path of the persistent runner
lands on the bus — the Phase 0 exit criterion."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from signal_plane.contract import uuid7
from signal_plane.publisher import SignalPublisher
from signal_plane import pair_trading_signals as sigmap
from tests.test_pair_trading import _make_strategy
from core.trade_proposer import TradeProposal


def _prop(tradingsymbol, lot_size, qty, price, side,
          expiry="2026-04-28 00:00:00", rationale="test"):
    return TradeProposal(
        tradingsymbol=tradingsymbol, instrument_token=111,
        strike=0.0, expiry=expiry, option_type="FUT",
        lot_size=lot_size, quantity=qty, price=price,
        transaction_type=side, iv=0.0, bid_ask_spread_pct=0.0,
        margin_required=price * lot_size * qty * 0.2,
        rationale=rationale,
    )


def _entry_props(qty_a=1, qty_b=2, price_a=2000.0, price_b=1000.0):
    return [
        _prop("AAA26APRFUT", 100, qty_a, price_a, "BUY"),
        _prop("BBB26APRFUT", 200, qty_b, price_b, "SELL"),
    ]


def _strategy_with_history(**kw):
    # 30 spread observations, std > 0, mean 0 — enough for z/risk math.
    history = [(-1) ** i * 5.0 for i in range(30)]
    s = _make_strategy(spread_history=history, **kw)
    s._signal_publisher = None
    s.signal_system_tag = "persistent"
    s._pending_entry_z = None
    return s


def _publisher(tmp_path):
    return SignalPublisher(strategy_id="pair_trading",
                           bus_dir=tmp_path / "signal-bus",
                           state_dir=tmp_path / "data_cache")


def _bus_records(pub):
    path = pub.bus_file()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# ──────────────────────────────────────────────────────────
# Mapper
# ──────────────────────────────────────────────────────────

class TestEntryMapping:
    def test_two_proposals_become_one_structure_signal(self):
        s = _strategy_with_history()
        env = sigmap.build_entry_signal(s, _entry_props(), z=-2.5)
        record = env.to_wire()
        assert record["intent"] == "ENTRY"
        assert record["underlying"] == "AAA/BBB"
        assert [leg["leg_id"] for leg in record["legs"]] == ["L1", "L2"]
        assert [leg["side"] for leg in record["legs"]] == ["BUY", "SELL"]

    def test_gcd_moves_into_base_multiplier(self):
        # 2:4 lots must publish as ratio 1:2 × base_multiplier 2 — §4.12's
        # coprime rule; per-user scaling multiplies the whole structure.
        s = _strategy_with_history()
        env = sigmap.build_entry_signal(
            s, _entry_props(qty_a=2, qty_b=4), z=-2.5)
        record = env.to_wire()
        assert [leg["ratio"] for leg in record["legs"]] == [1, 2]
        assert [leg["quantity_lots"] for leg in record["legs"]] == [2, 4]
        assert record["sizing"]["base_multiplier"] == 2

    def test_instrument_is_broker_agnostic(self):
        s = _strategy_with_history()
        record = sigmap.build_entry_signal(s, _entry_props(), z=-2.5).to_wire()
        inst = record["legs"][0]["instrument"]
        # §4.14: ISO date, not the kite '2026-04-28 00:00:00' form, and the
        # NFO symbol is demoted to a hint.
        assert inst["expiry"] == "2026-04-28"
        assert inst["tradingsymbol_hint"] == "AAA26APRFUT"
        assert inst["instrument_class"] == "FUT"
        assert "instrument_token" not in json.dumps(record)

    def test_marketable_limit_pads_toward_aggressive_side(self):
        # Mirrors KiteOrderExecutor: BUY pads above reference, SELL below —
        # a subscriber chasing with the wrong sign would never fill.
        s = _strategy_with_history()
        record = sigmap.build_entry_signal(s, _entry_props(), z=-2.5).to_wire()
        buy, sell = record["legs"]
        assert buy["limit_price"] > buy["reference_price"]
        assert sell["limit_price"] < sell["reference_price"]

    def test_risk_directive_models_loss_to_effective_stop(self):
        # z=-2.5, stop_z=4, buffer 0.75 → planned stop 4.0; band=1.5σ.
        # σ of ±5 alternating history is ~5.08; risk ≈ band × A-shares.
        s = _strategy_with_history()
        record = sigmap.build_entry_signal(s, _entry_props(), z=-2.5).to_wire()
        assert record["sizing"]["method"] == "RISK_PER_TRADE_PCT"
        risk_unit = record["sizing"]["risk_per_unit_inr"]
        _mean, std = s._rolling_window_stats()
        expected = round((4.0 - 2.5) * std * 1 * 100, 2)
        assert risk_unit == pytest.approx(expected)
        stop = record["risk"][0]
        assert stop["basis"] == "STRUCTURE_PNL_INR"
        assert stop["value"] == pytest.approx(-risk_unit)
        assert stop["placement"] == "MANAGED_BY_PLATFORM"
        assert record["tags"]["effective_stop_z"] == 4.0

    def test_no_rolling_std_falls_back_to_fixed_lots(self):
        # Rule 12: rather than inventing a risk number, the signal says
        # FIXED_LOTS and tags the fallback.
        s = _strategy_with_history()
        s._spread_history = []
        record = sigmap.build_entry_signal(s, _entry_props(), z=-2.5).to_wire()
        assert record["sizing"]["method"] == "FIXED_LOTS"
        assert "risk" not in record
        assert record["tags"]["sizing_fallback"] == "no_rolling_std"

    def test_reference_spot_is_the_spread(self):
        s = _strategy_with_history(hedge_ratio=0.5)
        record = sigmap.build_entry_signal(s, _entry_props(), z=-2.5).to_wire()
        assert record["reference"]["spot"] == pytest.approx(
            2000.0 - 0.5 * 1000.0)
        assert record["tags"]["spot_basis"] == "spread"


class TestExitMapping:
    def test_exit_names_stored_contracts_full_fraction(self):
        s = _strategy_with_history()
        props = [
            _prop("AAA26APRFUT", 100, 1, 1990.0, "SELL"),
            _prop("BBB26APRFUT", 200, 2, 1010.0, "BUY"),
        ]
        env = sigmap.build_exit_signal(s, props, reason="STOP",
                                       group_id=uuid7())
        record = env.to_wire()
        assert record["intent"] == "EXIT"
        assert record["fraction"] == 1.0
        assert record["tags"]["exit_reason"] == "STOP"
        # With complete contract terms the exit names its legs (roll-safety
        # cross-check for the OMS).
        hints = [leg["instrument"]["tradingsymbol_hint"]
                 for leg in record["legs"]]
        assert hints == ["AAA26APRFUT", "BBB26APRFUT"]

    def test_exit_without_expiry_metadata_omits_legs(self):
        # PairLeg state doesn't persist expiry, so real runner exits carry
        # expiry="" — a FUT descriptor that can't uniquely resolve (§4.12).
        # The §4.3-blessed shape is legs omitted: the OMS derives them from
        # the group's open position. The omission is tagged, never silent.
        s = _strategy_with_history()
        props = [
            _prop("AAA26APRFUT", 100, 1, 1990.0, "SELL", expiry=""),
            _prop("BBB26APRFUT", 200, 2, 1010.0, "BUY", expiry=""),
        ]
        env = sigmap.build_exit_signal(s, props, reason="STOP",
                                       group_id=uuid7())
        record = env.to_wire()
        assert "legs" not in record
        assert record["fraction"] == 1.0
        assert record["tags"]["legs_omitted"] == "no_expiry_metadata"


# ──────────────────────────────────────────────────────────
# execute_proposals integration (paper mode, real publisher)
# ──────────────────────────────────────────────────────────

class TestExecuteProposalsPublishes:
    def _enter(self, s, pub, qty_a=1, qty_b=2):
        s._signal_publisher = pub
        s._pending_entry_z = -2.5
        s.execute_proposals(_entry_props(qty_a=qty_a, qty_b=qty_b))
        return _bus_records(pub)

    def test_entry_publishes_and_adopts_group(self, tmp_path):
        s = _strategy_with_history()
        pub = _publisher(tmp_path)
        records = self._enter(s, pub)
        assert [r["intent"] for r in records] == ["ENTRY"]
        assert s.state.position == "LONG_SPREAD"
        # §4.11 correlation: the strategy remembers the group so its exit
        # resolves to exactly this entry.
        assert s.state.position_group_id == records[0]["position_group_id"]
        assert records[0]["tags"]["system"] == "persistent"

    def test_every_exit_reason_reaches_the_bus(self, tmp_path):
        # Phase 0 exit criterion: no in-process exit the OMS can't see.
        for reason in ("MEAN_REVERT", "STOP", "MAX_HOLD", "EXPIRY",
                       "OPS_FORCE"):
            s = _strategy_with_history()
            pub = _publisher(tmp_path / reason)
            self._enter(s, pub)
            prices = {"AAA": 1990.0, "BBB": 1010.0}
            exit_props = s._build_exit_proposals(reason, 0.1, prices)
            s.execute_proposals(exit_props)
            records = _bus_records(pub)
            assert [r["intent"] for r in records] == ["ENTRY", "EXIT"]
            assert records[1]["tags"]["exit_reason"] == reason
            assert records[1]["position_group_id"] == \
                records[0]["position_group_id"]
            # PR #96 review: legs stored at fill time now persist expiry,
            # so real exits name their exact contracts (roll safety) with
            # the ISO date instead of dropping legs.
            exit_legs = records[1]["legs"]
            assert [leg["instrument"]["tradingsymbol_hint"]
                    for leg in exit_legs] == ["AAA26APRFUT", "BBB26APRFUT"]
            assert all(leg["instrument"]["expiry"] == "2026-04-28"
                       for leg in exit_legs)
            assert s.state.position == "FLAT"
            assert s.state.position_group_id is None

    def test_reversed_partial_entry_publishes_cancel(self, tmp_path):
        # If leg B rejects and leg A is reversed, the master ends FLAT —
        # subscribers must not be left holding the structure (§5).
        s = _strategy_with_history()
        pub = _publisher(tmp_path)
        s._signal_publisher = pub
        s._pending_entry_z = -2.5
        props = _entry_props()
        props[1].tradingsymbol = "bad symbol!"  # fails validate_order
        s.execute_proposals(props)
        records = _bus_records(pub)
        assert [r["intent"] for r in records] == ["ENTRY", "CANCEL"]
        assert records[1]["supersedes"] == records[0]["signal_id"]
        assert records[1]["position_group_id"] == \
            records[0]["position_group_id"]
        assert s.state.position == "FLAT"
        assert not s.state.legs

    def test_margin_refused_entry_publishes_nothing(self, tmp_path):
        # PR #96 review: the ENTRY used to be published BEFORE the H15
        # margin pre-check, whose `return []` skipped the CANCEL reconcile
        # — subscribers were left holding a structure the master never
        # opened, re-published with a fresh group every tick. The gate now
        # runs first: no funds on the master, no signal at all.
        s = _strategy_with_history()
        s.mode = "live"
        pub = _publisher(tmp_path)
        s._signal_publisher = pub
        s._pending_entry_z = -2.5
        s._margin_precheck_ok = lambda proposals: False
        results = s.execute_proposals(_entry_props())
        assert results == []
        assert _bus_records(pub) == []
        assert pub.last_sequence == -1  # no sequence burned either
        assert pub.open_groups == {}

    def test_backoff_window_entry_publishes_nothing(self, tmp_path):
        # PR #96 review: during an armed M-B5 backoff every leg is FAILED
        # by _live_execute's short-circuit, so publishing the ENTRY only
        # produced an ENTRY+CANCEL whipsaw per tick. No intent the master
        # can act on → no signal (same reasoning as HALT_NEW_ENTRIES).
        s = _strategy_with_history()
        s.mode = "live"
        s._place_order_skip_ticks_left = 2
        pub = _publisher(tmp_path)
        s._signal_publisher = pub
        s._pending_entry_z = -2.5
        s._margin_precheck_ok = lambda proposals: True
        s.execute_proposals(_entry_props())
        assert _bus_records(pub) == []
        assert s.state.position == "FLAT"

    def test_publish_failure_never_blocks_trading(self, tmp_path):
        # The live book's safety outranks the bus: a broken publisher logs
        # CRITICAL but fills still happen and state stays consistent.
        class ExplodingPublisher:
            def publish(self, *a, **kw):
                raise RuntimeError("bus on fire")

        s = _strategy_with_history()
        s._signal_publisher = ExplodingPublisher()
        s._pending_entry_z = -2.5
        results = s.execute_proposals(_entry_props())
        assert [r["status"] for r in results] == ["COMPLETE", "COMPLETE"]
        assert s.state.position == "LONG_SPREAD"
        assert s.state.position_group_id is None  # nothing on the bus

    def test_bootstrap_exit_for_pre_history_position(self, tmp_path):
        # A position restored from a pre-signal-plane state file has no
        # group id; its exit must still be published (exits are money
        # events), flagged via the publisher's unmatched-entry escape.
        s = _strategy_with_history()
        pub = _publisher(tmp_path)
        self._enter(s, pub)
        # Simulate the pre-upgrade restore: position open, no group id.
        s.state.position_group_id = None
        prices = {"AAA": 1990.0, "BBB": 1010.0}
        s.execute_proposals(s._build_exit_proposals("STOP", 4.2, prices))
        records = _bus_records(pub)
        assert records[-1]["intent"] == "EXIT"
        # Fresh group, not the entry's — the correlation was lost with the
        # old state file, and the signal doesn't pretend otherwise.
        assert records[-1]["position_group_id"] != \
            records[0]["position_group_id"]


class TestGroupIdPersistence:
    def test_group_id_round_trips_through_state_file(self):
        s = _strategy_with_history()
        s.state.position = "LONG_SPREAD"
        s.state.entry_time = datetime(2026, 4, 21, 10, 30)
        gid = uuid7()
        s.state.position_group_id = gid
        blob = s.serialize_state()
        assert blob["state"]["position_group_id"] == gid

        fresh = _strategy_with_history()
        fresh.restore_state(blob)
        assert fresh.state.position_group_id == gid

    def test_pre_upgrade_state_file_restores_as_none(self):
        # Older state files have no position_group_id key — restore must
        # not fail loud here; None routes exits through the bootstrap path.
        s = _strategy_with_history()
        s.state.position = "LONG_SPREAD"
        s.state.entry_time = datetime(2026, 4, 21, 10, 30)
        blob = s.serialize_state()
        del blob["state"]["position_group_id"]
        fresh = _strategy_with_history()
        fresh.restore_state(blob)
        assert fresh.state.position_group_id is None
