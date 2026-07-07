"""Signal contract (§4) — schema round-trip, worked-example fixtures, and
the §4.12 publisher-side validation rules.

Rule 9: these tests encode WHY the contract matters — an invalid signal on
the bus is a money event downstream, so every rule here is one a real OMS
depends on (closed enums, TTL ordering, structure-ratio hygiene, contract
descriptors that resolve uniquely)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from signal_plane import contract
from signal_plane.contract import (
    Instrument,
    Leg,
    Reference,
    RiskDirective,
    SignalEnvelope,
    Sizing,
    uuid7,
)
from signal_plane.validation import (
    SignalValidationError,
    check_instruments,
    check_ratios,
    check_risk_leg_refs,
    check_schema,
    check_ttl,
    validate_signal,
)

FIXTURES = Path(__file__).parent / "fixtures" / "signals"


def _pair_entry_envelope(**overrides) -> SignalEnvelope:
    """A representative 2-leg pair ENTRY built through the dataclasses."""
    legs = [
        Leg(leg_id="L1",
            instrument=Instrument(exchange="NFO", instrument_class="FUT",
                                  underlying="AAA", expiry="2026-04-28",
                                  tradingsymbol_hint="AAA26APRFUT"),
            side="BUY", ratio=1, quantity_lots=2,
            order_type="MARKETABLE_LIMIT", limit_price=2005.0,
            product="OVERNIGHT", reference_price=2000.0),
        Leg(leg_id="L2",
            instrument=Instrument(exchange="NFO", instrument_class="FUT",
                                  underlying="BBB", expiry="2026-04-28",
                                  tradingsymbol_hint="BBB26APRFUT"),
            side="SELL", ratio=2, quantity_lots=4,
            order_type="MARKETABLE_LIMIT", limit_price=995.0,
            product="OVERNIGHT", reference_price=1000.0),
    ]
    env = SignalEnvelope(
        signal_id=uuid7(),
        position_group_id=uuid7(),
        strategy_id="pair_trading",
        intent="ENTRY",
        created_at="2026-07-07T10:30:00+05:30",
        valid_until="2026-07-07T10:32:00+05:30",
        underlying="AAA/BBB",
        reference=Reference(spot=1000.0,
                            captured_at="2026-07-07T10:30:00+05:30"),
        sequence=0,
        legs=legs,
        sizing=Sizing(method="RISK_PER_TRADE_PCT", base_multiplier=2,
                      risk_per_unit_inr=15000.0),
        risk=[RiskDirective(kind="STOP", scope="STRUCTURE",
                            basis="STRUCTURE_PNL_INR", value=-30000.0,
                            comparator="LTE",
                            placement="MANAGED_BY_PLATFORM")],
    )
    for k, v in overrides.items():
        setattr(env, k, v)
    return env


class TestSchemaRoundTrip:
    def test_envelope_to_wire_validates_and_survives_json(self):
        record = _pair_entry_envelope().to_wire()
        validate_signal(record)  # must not raise
        rehydrated = json.loads(json.dumps(record))
        assert rehydrated == record
        validate_signal(rehydrated)

    def test_wire_never_carries_broker_local_fields(self):
        # §4.1: instrument_token and lot_size are Kite-local; the adapter
        # re-resolves them per broker. Leaking them couples every consumer
        # to Zerodha symbology.
        record = _pair_entry_envelope().to_wire()
        blob = json.dumps(record)
        assert "instrument_token" not in blob
        assert "lot_size" not in blob

    def test_optional_fields_are_omitted_not_null(self):
        record = _pair_entry_envelope().to_wire()
        assert "fraction" not in record
        assert "supersedes" not in record
        assert "rationale" not in record


class TestWorkedExampleFixtures:
    # §4.10 examples pinned as fixtures: if the schema drifts so that the
    # documented examples stop validating, that is a MAJOR-version event,
    # not a refactor detail.
    @pytest.mark.parametrize("name", [
        "entry_taleb_strangle.json",
        "entry_buy_on_gap.json",
        "exit_full_group.json",
        "replace_stop_trail.json",
    ])
    def test_doc_example_validates(self, name):
        record = json.loads((FIXTURES / name).read_text())
        validate_signal(record)


class TestSchemaRejections:
    def test_unknown_intent_is_hard_reject(self):
        # Closed enums (§4.8): a consumer that guesses on an unknown intent
        # could fabricate an order.
        record = _pair_entry_envelope().to_wire()
        record["intent"] = "YOLO"
        assert any("intent" in p or "YOLO" in p for p in check_schema(record))

    def test_entry_without_sizing_rejected(self):
        # An ENTRY without a sizing basis cannot be fanned out — the OMS
        # would have to invent per-user size.
        record = _pair_entry_envelope().to_wire()
        del record["sizing"]
        assert check_schema(record)

    def test_exit_without_fraction_rejected(self):
        record = {
            k: v for k, v in _pair_entry_envelope().to_wire().items()
            if k not in ("legs", "sizing", "risk")
        }
        record["intent"] = "EXIT"
        assert check_schema(record)
        record["fraction"] = 1.0
        assert not check_schema(record)

    def test_unknown_top_level_field_rejected(self):
        record = _pair_entry_envelope().to_wire()
        record["algo_id"] = "XYZ"  # §19.7: algo_id is an OMS concern
        assert check_schema(record)

    def test_missing_reference_rejected(self):
        # reference is required even on CANCEL — slippage attribution and
        # audit need the master's view at every decision.
        record = _pair_entry_envelope().to_wire()
        del record["reference"]
        assert check_schema(record)


class TestSemanticRules:
    def test_ttl_must_be_after_created(self):
        record = _pair_entry_envelope(
            valid_until="2026-07-07T10:30:00+05:30").to_wire()
        assert check_ttl(record)
        with pytest.raises(SignalValidationError):
            validate_signal(record)

    def test_fut_without_expiry_rejected(self):
        env = _pair_entry_envelope()
        env.legs[0].instrument.expiry = None
        problems = check_instruments(env.to_wire())
        assert any("requires expiry" in p for p in problems)

    def test_opt_requires_strike_and_right(self):
        env = _pair_entry_envelope()
        env.legs[0].instrument.instrument_class = "OPT"
        problems = check_instruments(env.to_wire())
        assert any("strike" in p for p in problems)
        assert any("option_type" in p for p in problems)

    def test_nan_reference_price_rejected(self):
        # NaN survives json.dumps (Python emits bare NaN) and jsonschema's
        # "number" type check — only the semantic rule catches it.
        env = _pair_entry_envelope()
        env.legs[0].reference_price = float("nan")
        assert any("not finite" in p for p in check_instruments(env.to_wire()))

    def test_common_factor_ratios_rejected(self):
        # §4.12: a 2:4 that should be 1:2 silently doubles every
        # subscriber's position at multiplier rounding.
        env = _pair_entry_envelope()
        env.legs[0].ratio = 2
        env.legs[1].ratio = 4
        assert check_ratios(env.to_wire())

    def test_coprime_ratios_pass(self):
        assert not check_ratios(_pair_entry_envelope().to_wire())

    def test_leg_scoped_risk_must_name_a_real_leg(self):
        env = _pair_entry_envelope()
        env.risk = [RiskDirective(kind="STOP", scope="LEG", leg_id="L9",
                                  basis="LEG_PRICE", value=1900.0,
                                  comparator="LTE",
                                  placement="RESTING_AT_BROKER",
                                  resting_order_type="SL_M")]
        assert check_risk_leg_refs(env.to_wire())


class TestEnumParity:
    """PR #96 review: the contract module's frozensets and the JSON schema
    each carry a copy of every closed enum. Nothing at runtime cross-checks
    them, so a MINOR bump that edits one and not the other would let a
    consumer built on the frozensets reject schema-valid signals (or vice
    versa). This test IS the parity check."""

    @classmethod
    def _schema(cls):
        with open(contract.schema_path(), encoding="utf-8") as f:
            return json.load(f)

    def test_frozensets_match_schema_enums(self):
        schema = self._schema()
        props = schema["properties"]
        leg = props["legs"]["items"]["properties"]
        inst = leg["instrument"]["properties"]
        risk = props["risk"]["items"]["properties"]
        sizing = props["sizing"]["properties"]
        pairs = [
            (contract.INTENTS, props["intent"]["enum"]),
            (contract.SIDES, leg["side"]["enum"]),
            (contract.ORDER_TYPES, leg["order_type"]["enum"]),
            (contract.PRODUCTS, leg["product"]["enum"]),
            (contract.EXCHANGES, inst["exchange"]["enum"]),
            (contract.INSTRUMENT_CLASSES, inst["instrument_class"]["enum"]),
            # option_type is nullable on the wire; null is not a member.
            (contract.OPTION_TYPES,
             [v for v in inst["option_type"]["enum"] if v is not None]),
            (contract.RISK_KINDS, risk["kind"]["enum"]),
            (contract.RISK_SCOPES, risk["scope"]["enum"]),
            (contract.RISK_BASES, risk["basis"]["enum"]),
            (contract.RISK_COMPARATORS, risk["comparator"]["enum"]),
            (contract.RISK_PLACEMENTS, risk["placement"]["enum"]),
            (contract.RESTING_ORDER_TYPES,
             risk["resting_order_type"]["enum"]),
            (contract.SIZING_METHODS, sizing["method"]["enum"]),
        ]
        for frozen, schema_list in pairs:
            assert frozen == frozenset(schema_list), (
                f"contract frozenset {sorted(frozen)} != schema enum "
                f"{sorted(schema_list)} — bump both together (§4.13)"
            )

    def test_sizing_rounding_const_matches_schema(self):
        schema = self._schema()
        const = schema["properties"]["sizing"]["properties"]["rounding"]["const"]
        assert contract.SIZING_ROUNDING == const


class TestUuid7:
    def test_version_and_variant_bits(self):
        u = uuid7()
        assert u[14] == "7"          # version nibble
        assert u[19] in "89ab"       # RFC 4122 variant

    def test_time_ordering(self):
        # UUIDv7's point: signal_id doubles as a time-sortable key.
        import time
        a = uuid7()
        time.sleep(0.002)
        b = uuid7()
        assert a < b
