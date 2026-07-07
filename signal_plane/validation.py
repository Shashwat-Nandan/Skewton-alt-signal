"""
Publisher-side validation (§4.9 + §4.12) — fail loud, never emit an invalid
signal.

Two layers, both mandatory before a record reaches the bus:
  1. JSON Schema (draft 2020-12) — the checked-in signal-1.0.json.
  2. Semantic rules the schema cannot express (§4.12): TTL ordering,
     FUT/OPT contract-term completeness, ratio hygiene, LEG-scoped risk
     directives referencing real legs.

Stateful rules (sequence strictly monotonic, EXIT only for a group the
master actually opened, signal_id idempotency) live in
signal_plane.publisher — they need the publisher's persisted state.

Each rule is its own function returning a list of problem strings so tests
can exercise them individually (issue #90: "unit-testable checks").
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from functools import lru_cache
from typing import Callable, Dict, List

from jsonschema import Draft202012Validator

from signal_plane.contract import schema_path


class SignalValidationError(ValueError):
    """An outgoing signal failed validation — it must NOT be published."""

    def __init__(self, problems: List[str]):
        self.problems = list(problems)
        super().__init__(
            "invalid signal (%d problem%s): %s"
            % (len(problems), "" if len(problems) == 1 else "s",
               "; ".join(problems))
        )


@lru_cache(maxsize=1)
def _schema_validator() -> Draft202012Validator:
    with open(schema_path(), encoding="utf-8") as f:
        schema = json.load(f)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def check_schema(record: Dict) -> List[str]:
    """Layer 1: the record validates against the checked-in JSON Schema."""
    return [
        "schema: %s: %s" % ("/".join(str(p) for p in e.absolute_path) or "$",
                            e.message)
        for e in _schema_validator().iter_errors(record)
    ]


def _parse_ts(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def check_ttl(record: Dict) -> List[str]:
    """§4.12: valid_until must be strictly after created_at (and both must
    parse — jsonschema treats `format` as annotation-only by default)."""
    created = _parse_ts(record.get("created_at"))
    valid_until = _parse_ts(record.get("valid_until"))
    problems = []
    if created is None:
        problems.append("created_at is not a parseable RFC3339 timestamp")
    if valid_until is None:
        problems.append("valid_until is not a parseable RFC3339 timestamp")
    if created and valid_until and valid_until <= created:
        problems.append(
            f"valid_until ({record['valid_until']}) must be after "
            f"created_at ({record['created_at']})"
        )
    return problems


def check_instruments(record: Dict) -> List[str]:
    """§4.12: every leg's descriptor must uniquely resolve a real contract —
    FUT/OPT need an expiry, OPT needs strike + option_type, and EQ must not
    carry contract terms. reference_price must be finite (NaN/Inf survive
    json.dumps, so the schema alone can't catch them)."""
    problems = []
    for i, leg in enumerate(record.get("legs", [])):
        label = leg.get("leg_id") or f"legs[{i}]"
        inst = leg.get("instrument", {})
        cls = inst.get("instrument_class")
        if cls in ("FUT", "OPT") and not inst.get("expiry"):
            problems.append(f"{label}: {cls} instrument requires expiry")
        if cls == "OPT":
            if inst.get("strike") in (None, 0):
                problems.append(f"{label}: OPT instrument requires strike")
            if not inst.get("option_type"):
                problems.append(f"{label}: OPT instrument requires option_type")
        if cls in ("EQ", "FUT") and inst.get("option_type"):
            problems.append(f"{label}: option_type is meaningless for {cls}")
        for price_field in ("reference_price", "limit_price", "trigger_price"):
            v = leg.get(price_field)
            if v is not None and not math.isfinite(v):
                problems.append(f"{label}: {price_field} is not finite ({v})")
    return problems


def check_ratios(record: Dict) -> List[str]:
    """§4.12: ratios are coprime-or-intended — a 2:2 that should be 1:1 is a
    sizing bug. The publisher's mappers always reduce by gcd, so a common
    factor here is a hard reject; a deliberately non-reduced structure would
    need a new mapper decision, not a silent pass."""
    legs = record.get("legs", [])
    if len(legs) < 2:
        return []
    ratios = [leg.get("ratio") for leg in legs]
    if any(not isinstance(r, int) or r < 1 for r in ratios):
        return []  # schema layer already reports these
    g = ratios[0]
    for r in ratios[1:]:
        g = math.gcd(g, r)
    if g > 1:
        return [f"leg ratios {ratios} share a common factor {g} — reduce to "
                f"the coprime structure and move the factor into "
                f"sizing.base_multiplier"]
    return []


def check_risk_leg_refs(record: Dict) -> List[str]:
    """A LEG-scoped risk directive must name a leg_id present in this
    envelope (structure-scoped directives carry no leg_id)."""
    leg_ids = {leg.get("leg_id") for leg in record.get("legs", [])}
    problems = []
    for i, r in enumerate(record.get("risk", [])):
        if r.get("scope") == "LEG":
            if not r.get("leg_id"):
                problems.append(f"risk[{i}]: scope=LEG requires leg_id")
            elif record.get("legs") and r["leg_id"] not in leg_ids:
                problems.append(
                    f"risk[{i}]: leg_id {r['leg_id']!r} not in this "
                    f"envelope's legs {sorted(leg_ids)}"
                )
    return problems


PUBLISHER_CHECKS: List[Callable[[Dict], List[str]]] = [
    check_schema,
    check_ttl,
    check_instruments,
    check_ratios,
    check_risk_leg_refs,
]


def validate_signal(record: Dict) -> None:
    """Run every publisher-side check; raise SignalValidationError with the
    full problem list if any fail. Called by the publisher on EVERY outgoing
    signal — an invalid signal never reaches the bus (Rule 12)."""
    problems: List[str] = []
    for check in PUBLISHER_CHECKS:
        problems.extend(check(record))
    if problems:
        raise SignalValidationError(problems)
