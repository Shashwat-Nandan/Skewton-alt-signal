"""
Signal contract v1.0 — the §4 object model from docs/platform-architecture.md.

A signal is NOT "place this order". It is "the master book intends this state
change", carrying everything a downstream OMS needs to reproduce it safely:
structure (legs + integer ratios), sizing basis, stop/target directives, and
the master's reference snapshot for slippage attribution.

This module is the versioned wire format (issue #90 part A):
  - dataclasses for Envelope / Leg / RiskDirective / Sizing / Reference /
    Instrument / Slippage (§4.2–4.7)
  - the closed enums (§4.8) — an unknown member is a hard reject at the
    consumer, never a default
  - SCHEMA_VERSION and the §4.13 MAJOR/MINOR rules
  - the §4.15 lifecycle states (owned by the OMS per user; the enum lives
    with the contract so both planes share one vocabulary)

Serialization: `.to_wire()` produces the JSON-ready dict. Optional fields are
OMITTED (not null) so the schema's `additionalProperties: false` plus the
required-lists stay the single source of truth. Kite-local fields
(instrument_token, lot_size) intentionally never cross the wire (§4.1).

Validation lives in signal_plane.validation (schema + §4.12 rules);
publishing, sequence assignment and ordering in signal_plane.publisher.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── Versioning (§4.13) ──
# MAJOR.MINOR. Adding an optional field or enum member = MINOR bump.
# Removing/renaming/retyping a field or changing a required set = MAJOR bump;
# consumers reject unknown MAJORs rather than guessing.
SCHEMA_VERSION = "1.0"

# ── Closed enums (§4.8) ──
INTENTS = frozenset({"ENTRY", "ADD", "REDUCE", "EXIT", "EXIT_ALL",
                     "REPLACE_STOP", "CANCEL"})
SIDES = frozenset({"BUY", "SELL"})
ORDER_TYPES = frozenset({"MARKET", "LIMIT", "MARKETABLE_LIMIT", "SL", "SL_M"})
PRODUCTS = frozenset({"INTRADAY", "OVERNIGHT", "DELIVERY"})
EXCHANGES = frozenset({"NSE", "NFO", "BSE", "BFO", "MCX"})
INSTRUMENT_CLASSES = frozenset({"EQ", "FUT", "OPT"})
OPTION_TYPES = frozenset({"CE", "PE"})
RISK_KINDS = frozenset({"STOP", "TARGET"})
RISK_SCOPES = frozenset({"LEG", "STRUCTURE"})
RISK_BASES = frozenset({"LEG_PRICE", "UNDERLYING_LEVEL", "STRUCTURE_PNL_INR",
                        "PREMIUM_PERCENT", "ATR_MULTIPLE"})
RISK_COMPARATORS = frozenset({"LTE", "GTE"})
RISK_PLACEMENTS = frozenset({"RESTING_AT_BROKER", "MANAGED_BY_PLATFORM"})
RESTING_ORDER_TYPES = frozenset({"SL", "SL_M", "GTT", "OCO"})
SIZING_METHODS = frozenset({"PER_LOT_AT_CAPITAL", "FIXED_LOTS",
                            "RISK_PER_TRADE_PCT", "NOTIONAL_TARGET"})
SIZING_ROUNDING = "STRUCTURE_PRESERVING"  # const — never round legs alone

# ── Lifecycle states (§4.15) ──
# Owned by the OMS per user; every transition is an audit record. SKIPPED is
# a first-class, surfaced outcome — never a silent drop (Rule 12).
#   PUBLISHED → DISTRIBUTED → (per user) EVALUATING
#      → SKIPPED(reason)          margin/lot/TTL/risk-gate/no-consent
#      → EXECUTING → FILLED | PARTIAL | REJECTED
#      → (on exit signal or stop/target) CLOSING → CLOSED
LIFECYCLE_STATES = ("PUBLISHED", "DISTRIBUTED", "EVALUATING", "SKIPPED",
                    "EXECUTING", "FILLED", "PARTIAL", "REJECTED",
                    "CLOSING", "CLOSED")


def uuid7() -> str:
    """RFC 9562 UUIDv7 (time-ordered): 48-bit unix-ms timestamp + random.

    stdlib uuid grows uuid7 only in 3.13+; this repo's venv is 3.11. Signals
    use v7 so signal_id doubles as a time-sortable idempotency key (§4.3).
    """
    ts_ms = int(time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    b = (
        (ts_ms << 80)
        | (0x7 << 76) | (rand_a << 64)       # version 7 + rand_a
        | (0b10 << 62) | rand_b              # RFC 4122 variant + rand_b
    ).to_bytes(16, "big")
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _put_optional(d: Dict, **kv) -> Dict:
    """Set only the keys whose value is not None (optional = omitted)."""
    for k, v in kv.items():
        if v is not None:
            d[k] = v
    return d


@dataclass
class Instrument:
    """Canonical broker-agnostic contract descriptor (§4.4). Enough to
    uniquely identify the contract on any broker; the Kite instrument_token
    is intentionally absent (broker-local)."""
    exchange: str            # EXCHANGES
    instrument_class: str    # INSTRUMENT_CLASSES
    underlying: str
    expiry: Optional[str] = None       # ISO YYYY-MM-DD; required for FUT/OPT
    strike: Optional[float] = None     # required for OPT
    option_type: Optional[str] = None  # CE|PE; None for EQ/FUT
    tradingsymbol_hint: Optional[str] = None  # audit aid; never trusted

    def to_wire(self) -> Dict:
        d = {
            "exchange": self.exchange,
            "instrument_class": self.instrument_class,
            "underlying": self.underlying,
            # expiry/strike/option_type are nullable ON the wire (the schema
            # types them ["string","null"] etc.) so an EQ leg is explicit
            # about having no contract terms, per the §4.10 examples.
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
        }
        return _put_optional(d, tradingsymbol_hint=self.tradingsymbol_hint)


@dataclass
class Leg:
    """One instrument order line within a structure (§4.4). `quantity_lots`
    is the master's realized lots — reference only; the OMS recomputes per
    user from sizing × ratio."""
    leg_id: str
    instrument: Instrument
    side: str          # SIDES
    ratio: int         # relative weight within the structure, >= 1
    quantity_lots: int
    order_type: str    # ORDER_TYPES
    product: str       # PRODUCTS
    reference_price: float
    limit_price: Optional[float] = None    # required for LIMIT; cap for M-LIMIT
    trigger_price: Optional[float] = None  # required for SL/SL_M

    def to_wire(self) -> Dict:
        d = {
            "leg_id": self.leg_id,
            "instrument": self.instrument.to_wire(),
            "side": self.side,
            "ratio": int(self.ratio),
            "quantity_lots": int(self.quantity_lots),
            "order_type": self.order_type,
            "product": self.product,
            "reference_price": float(self.reference_price),
        }
        return _put_optional(d, limit_price=self.limit_price,
                             trigger_price=self.trigger_price)


@dataclass
class RiskDirective:
    """One stop/target condition (§4.5), scoped to a leg or the structure.
    STRUCTURE-scoped directives are MANAGED_BY_PLATFORM by nature — a resting
    broker order cannot express 'net structure loss >= X'."""
    kind: str          # RISK_KINDS
    scope: str         # RISK_SCOPES
    basis: str         # RISK_BASES
    value: float
    comparator: str    # RISK_COMPARATORS
    placement: str     # RISK_PLACEMENTS
    leg_id: Optional[str] = None            # required when scope=LEG
    resting_order_type: Optional[str] = None  # for RESTING_AT_BROKER

    def to_wire(self) -> Dict:
        d = {
            "kind": self.kind,
            "scope": self.scope,
            "basis": self.basis,
            "value": float(self.value),
            "comparator": self.comparator,
            "placement": self.placement,
        }
        return _put_optional(d, leg_id=self.leg_id,
                             resting_order_type=self.resting_order_type)


@dataclass
class Sizing:
    """How the OMS converts master lots into one integer structure multiplier
    per user (§4.6). rounding is always STRUCTURE_PRESERVING — round the
    multiplier, then apply to all legs; never round legs independently
    (the JUN/JUL lot-mismatch defense)."""
    method: str        # SIZING_METHODS
    base_multiplier: int = 1
    reference_capital: Optional[float] = None   # for PER_LOT_AT_CAPITAL
    risk_per_unit_inr: Optional[float] = None   # for RISK_PER_TRADE_PCT
    min_multiplier: Optional[int] = None
    max_multiplier: Optional[int] = None

    def to_wire(self) -> Dict:
        d = {
            "method": self.method,
            "base_multiplier": int(self.base_multiplier),
            "rounding": SIZING_ROUNDING,
        }
        return _put_optional(
            d,
            reference_capital=self.reference_capital,
            risk_per_unit_inr=self.risk_per_unit_inr,
            min_multiplier=self.min_multiplier,
            max_multiplier=self.max_multiplier,
        )


@dataclass
class Reference:
    """Master's observed market snapshot at decision time (§4.7). For
    slippage attribution and audit — opaque to execution logic."""
    spot: float
    captured_at: str   # RFC3339
    greeks: Optional[Dict] = None
    iv: Optional[float] = None
    regime: Optional[str] = None

    def to_wire(self) -> Dict:
        d = {"spot": float(self.spot), "captured_at": self.captured_at}
        return _put_optional(d, greeks=self.greeks, iv=self.iv,
                             regime=self.regime)


@dataclass
class Slippage:
    """Per-signal entry guard (§4.3 max_slippage). Exceeded at a user's
    evaluation → SKIPPED(slippage). Omitted on exits."""
    bps: float
    abs_inr: float

    def to_wire(self) -> Dict:
        return {"bps": float(self.bps), "abs_inr": float(self.abs_inr)}


@dataclass
class SignalEnvelope:
    """One published message (§4.3). sequence is assigned by the publisher
    at publish time (strictly monotonic per strategy_id) — leave it None
    when building."""
    signal_id: str
    position_group_id: str
    strategy_id: str
    intent: str            # INTENTS
    created_at: str        # RFC3339, decision time (not publish time)
    valid_until: str       # RFC3339 TTL; §4.12 asymmetry: stale exits still fire
    underlying: str
    reference: Reference
    sequence: Optional[int] = None
    legs: List[Leg] = field(default_factory=list)
    sizing: Optional[Sizing] = None
    fraction: Optional[float] = None       # REDUCE/EXIT: portion to close
    risk: List[RiskDirective] = field(default_factory=list)
    max_slippage: Optional[Slippage] = None
    supersedes: Optional[str] = None       # REPLACE_STOP/CANCEL back-reference
    rationale: Optional[str] = None        # human audit/UX only, never parsed
    tags: Optional[Dict] = None            # opaque strategy metadata

    def to_wire(self) -> Dict:
        d = {
            "schema_version": SCHEMA_VERSION,
            "signal_id": self.signal_id,
            "position_group_id": self.position_group_id,
            "strategy_id": self.strategy_id,
            "sequence": self.sequence,
            "intent": self.intent,
            "created_at": self.created_at,
            "valid_until": self.valid_until,
            "underlying": self.underlying,
            "reference": self.reference.to_wire(),
        }
        if self.legs:
            d["legs"] = [leg.to_wire() for leg in self.legs]
        if self.sizing is not None:
            d["sizing"] = self.sizing.to_wire()
        if self.risk:
            d["risk"] = [r.to_wire() for r in self.risk]
        if self.max_slippage is not None:
            d["max_slippage"] = self.max_slippage.to_wire()
        return _put_optional(
            d,
            fraction=self.fraction,
            supersedes=self.supersedes,
            rationale=self.rationale,
            tags=self.tags,
        )


def schema_path() -> str:
    """Absolute path of the checked-in JSON Schema for SCHEMA_VERSION.
    The schema dir doubles as the MVP schema registry (§6)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "schema", f"signal-{SCHEMA_VERSION}.json")
