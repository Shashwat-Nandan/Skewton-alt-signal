"""
§4.14 mapper: pair_trading TradeProposals → signal contract envelopes.

One pair decision = one signal. The two per-leg TradeProposals that
_build_entry_proposals / _build_exit_proposals emit are grouped into a single
ENTRY / EXIT envelope with legs[] and integer ratios — sizing scales the
STRUCTURE, never a leg in isolation (§4.1 principle 4).

Mapping decisions (documented once, here):
  - `underlying` = "A/B" pair label. §4.3 calls the field a routing/
    monitoring grouping; for a cross-stock pair the pair itself is the
    grouping key — neither leg alone identifies the structure.
  - `reference.spot` = the observed SPREAD (A − β·B at proposal prices),
    the structure's own "underlying level"; tags.spot_basis says so.
  - leg ratios = master lots reduced by gcd; the gcd moves into
    sizing.base_multiplier (keeps §4.12 coprime rule + STRUCTURE_PRESERVING
    scaling exact).
  - sizing = RISK_PER_TRADE_PCT: risk_per_unit_inr is the modeled ₹ loss if
    the spread runs from the entry z to the planned effective stop
    (max(stop_z, |z| + safety_buffer)) for ONE structure unit — the same
    Varsity share-count P&L model the strategy's own cost hurdle uses
    (P&L per unit spread move = A-leg share count). Falls back to
    FIXED_LOTS (tagged) when the rolling std is unavailable.
  - risk = one STRUCTURE-scoped STRUCTURE_PNL_INR stop at
    −risk_per_unit_inr × base_multiplier, MANAGED_BY_PLATFORM — a z-score
    stop cannot rest at a broker. The z-band context (entry_z, effective
    stop, exit_z, max_holding_days) rides in tags for analytics; the master
    remains the authority and emits the actual EXIT signal.
  - EXIT envelopes NAME their legs (stored contract tradingsymbols), because
    a position held across a roll must exit the original contract, not
    today's front-month — same reason _build_exit_proposals uses stored legs.
  - order_type = MARKETABLE_LIMIT with limit_price = reference ±
    limit_protection_pct (the live executor's proven fill model);
    product = OVERNIGHT (pair positions persist across sessions).

Timestamps are RFC3339 with the local (IST in production) offset.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from signal_plane.contract import (
    Instrument,
    Leg,
    Reference,
    RiskDirective,
    SignalEnvelope,
    Sizing,
    Slippage,
    uuid7,
)

STRATEGY_ID = "pair_trading"

# TTLs (§4.3 valid_until). Entries go stale fast — a pair entry chased
# minutes late is a different trade (z has moved). Exits get a wide window
# and §4.12 guarantees consumers execute them even past TTL.
ENTRY_TTL = timedelta(minutes=2)
EXIT_TTL = timedelta(minutes=30)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _rfc3339(ts: datetime) -> str:
    """RFC3339 with utcoffset. Strategy clocks are naive wall-clock IST
    (systemd sets TZ=Asia/Kolkata); astimezone() stamps the local offset."""
    if ts.tzinfo is None:
        ts = ts.astimezone()
    return ts.isoformat()


def _iso_expiry(raw: str) -> Optional[str]:
    """TradeProposal.expiry is str(fut['expiry']) — 'YYYY-MM-DD' or
    'YYYY-MM-DD HH:MM:SS' depending on the kite client. §4.14: the wire
    carries the ISO date, never the broker-formatted 26JUN form."""
    if raw and _ISO_DATE_RE.match(raw):
        return raw[:10]
    return None


def _fut_instrument(tradingsymbol: str, underlying: str,
                    expiry_raw: str) -> Instrument:
    return Instrument(
        exchange="NFO",
        instrument_class="FUT",
        underlying=underlying,
        expiry=_iso_expiry(expiry_raw),
        tradingsymbol_hint=tradingsymbol,
    )


def _spread_at(prop_a, prop_b, hedge_ratio: float) -> float:
    return prop_a.price - hedge_ratio * prop_b.price


def _with_system_tag(strategy, tags: Dict) -> Dict:
    """Runner-owned provenance tag (e.g. 'persistent') — which system/book
    emitted this. Optional; the runner sets strategy.signal_system_tag."""
    system_tag = getattr(strategy, "signal_system_tag", None)
    if system_tag:
        tags["system"] = system_tag
    return tags


def _limit_price(prop, protection_pct: float) -> float:
    pad = prop.price * protection_pct / 100.0
    price = prop.price + pad if prop.transaction_type == "BUY" else prop.price - pad
    return round(price, 2)


def _entry_legs(strategy, proposals) -> List[Leg]:
    """Two entry proposals (A first, B second — _build_entry_proposals
    order) → legs with gcd-reduced ratios."""
    qty = [int(p.quantity) for p in proposals]
    g = math.gcd(*qty) if len(qty) == 2 else 1
    legs = []
    for i, (prop, symbol) in enumerate(
        zip(proposals, (strategy.symbol_a, strategy.symbol_b))
    ):
        legs.append(Leg(
            leg_id=f"L{i + 1}",
            instrument=_fut_instrument(prop.tradingsymbol, symbol, prop.expiry),
            side=prop.transaction_type,
            ratio=qty[i] // g,
            quantity_lots=qty[i],
            order_type="MARKETABLE_LIMIT",
            limit_price=_limit_price(prop, strategy.limit_protection_pct),
            product="OVERNIGHT",
            reference_price=float(prop.price),
        ))
    return legs


def _risk_per_unit_inr(strategy, z: float, ratio_a_lots: int,
                       lot_size_a: int) -> Optional[float]:
    """Modeled ₹ loss-to-stop for one structure unit (Varsity share-count
    model: P&L per unit spread move ≈ A-leg share count). None when the
    rolling std is unavailable — callers fall back to FIXED_LOTS."""
    stats = strategy._rolling_window_stats()
    if stats is None:
        return None
    _mean, std = stats
    if not (std and math.isfinite(std) and std > 0):
        return None
    planned_stop_z = max(strategy.stop_z, abs(z) + strategy.safety_buffer)
    band = (planned_stop_z - abs(z)) * std
    if band <= 0:
        return None
    return round(band * ratio_a_lots * lot_size_a, 2)


def build_entry_signal(strategy, proposals, *, z: float,
                       group_id: Optional[str] = None) -> SignalEnvelope:
    """ENTRY envelope from the two entry proposals. `z` is the z-score the
    decision fired on (scan_and_propose passes it to _build_entry_proposals).
    Returns an envelope with a fresh position_group_id unless one is given;
    sequence is left for the publisher."""
    if len(proposals) != 2:
        raise ValueError(
            f"pair entry must be exactly 2 proposals, got {len(proposals)}"
        )
    prop_a, prop_b = proposals
    now = strategy._clock()
    legs = _entry_legs(strategy, proposals)
    spread = _spread_at(prop_a, prop_b, strategy.hedge_ratio)

    base_multiplier = int(prop_a.quantity) // legs[0].ratio
    risk_unit = _risk_per_unit_inr(
        strategy, z, legs[0].ratio, int(prop_a.lot_size),
    )
    tags: Dict = {
        "pair": [strategy.symbol_a, strategy.symbol_b],
        "hedge_ratio": strategy.hedge_ratio,
        "z": z,
        "entry_z_threshold": strategy.entry_z,
        "exit_z": strategy.exit_z,
        "effective_stop_z": max(strategy.stop_z,
                                abs(z) + strategy.safety_buffer),
        "max_holding_days": strategy.max_holding_days,
        "spot_basis": "spread",
    }
    tags = _with_system_tag(strategy, tags)
    if risk_unit is not None:
        sizing = Sizing(method="RISK_PER_TRADE_PCT",
                        base_multiplier=base_multiplier,
                        risk_per_unit_inr=risk_unit)
        risk = [RiskDirective(
            kind="STOP", scope="STRUCTURE", basis="STRUCTURE_PNL_INR",
            value=-round(risk_unit * base_multiplier, 2), comparator="LTE",
            placement="MANAGED_BY_PLATFORM",
        )]
    else:
        # No rolling std (thin seed window) — the trade itself was allowed
        # only because the cost hurdle is disabled in that state, so keep
        # the signal honest: fixed lots, no modeled stop, tagged fallback.
        sizing = Sizing(method="FIXED_LOTS", base_multiplier=base_multiplier)
        risk = []
        tags["sizing_fallback"] = "no_rolling_std"

    group = group_id or uuid7()
    return SignalEnvelope(
        signal_id=uuid7(),
        position_group_id=group,
        strategy_id=STRATEGY_ID,
        intent="ENTRY",
        created_at=_rfc3339(now),
        valid_until=_rfc3339(now + ENTRY_TTL),
        underlying=f"{strategy.symbol_a}/{strategy.symbol_b}",
        reference=Reference(spot=round(spread, 4), captured_at=_rfc3339(now)),
        legs=legs,
        sizing=sizing,
        risk=risk,
        max_slippage=Slippage(
            bps=round(strategy.limit_protection_pct * 100, 2),
            abs_inr=round(max(prop_a.price, prop_b.price)
                          * strategy.limit_protection_pct / 100.0, 2),
        ),
        rationale=prop_a.rationale or None,
        tags=tags,
    )


def build_exit_signal(strategy, proposals, *, reason: str,
                      group_id: str) -> SignalEnvelope:
    """EXIT envelope (full close — pair exits always flatten the whole
    structure).

    Legs are named from the exit proposals when the contract terms are
    complete, so the OMS can cross-check the exact stored contracts even
    across a roll. But exit proposals are built from PairLeg state, which
    does NOT persist expiry (`_make_exit_proposal_from_leg` sets expiry="")
    — a FUT descriptor without expiry cannot uniquely resolve a contract
    (§4.12), so in that case the legs are OMITTED entirely, the §4.3-blessed
    shape for exits: the OMS derives them from the group's open position,
    which correlation (`position_group_id`) identifies precisely. Tagged so
    the omission is visible, never silent (Rule 12)."""
    if not proposals:
        raise ValueError("exit signal needs at least one proposal")
    now = strategy._clock()
    qty = [int(p.quantity) for p in proposals]
    g = qty[0]
    for q in qty[1:]:
        g = math.gcd(g, q)
    g = max(g, 1)

    legs = []
    prices: Dict[str, float] = {}
    for i, prop in enumerate(proposals):
        symbol = strategy._symbol_from_tradingsymbol(prop.tradingsymbol)
        prices[symbol] = prop.price
        legs.append(Leg(
            leg_id=f"L{i + 1}",
            instrument=_fut_instrument(prop.tradingsymbol, symbol, prop.expiry),
            side=prop.transaction_type,
            ratio=qty[i] // g,
            quantity_lots=qty[i],
            order_type="MARKETABLE_LIMIT",
            limit_price=_limit_price(prop, strategy.limit_protection_pct),
            product="OVERNIGHT",
            reference_price=float(prop.price),
        ))

    exit_tags: Dict = {
        "pair": [strategy.symbol_a, strategy.symbol_b],
        "exit_reason": reason,
        "spot_basis": "spread",
    }
    if any(leg.instrument.expiry is None for leg in legs):
        legs = []
        exit_tags["legs_omitted"] = "no_expiry_metadata"

    spread = (
        prices[strategy.symbol_a]
        - strategy.hedge_ratio * prices[strategy.symbol_b]
        if strategy.symbol_a in prices and strategy.symbol_b in prices
        else next(iter(prices.values()))
    )
    return SignalEnvelope(
        signal_id=uuid7(),
        position_group_id=group_id,
        strategy_id=STRATEGY_ID,
        intent="EXIT",
        created_at=_rfc3339(now),
        valid_until=_rfc3339(now + EXIT_TTL),
        underlying=f"{strategy.symbol_a}/{strategy.symbol_b}",
        reference=Reference(spot=round(spread, 4), captured_at=_rfc3339(now)),
        fraction=1.0,
        legs=legs,
        rationale=proposals[0].rationale or None,
        tags=_with_system_tag(strategy, exit_tags),
    )


def build_cancel_signal(strategy, *, group_id: str, entry_signal_id: str,
                        entry_spot: float, detail: str) -> SignalEnvelope:
    """CANCEL: the published ENTRY did not establish a master position
    (every leg rejected, or a partial batch was reversed to flat).
    Subscribers must not be left holding a structure the master never
    opened (§5)."""
    now = strategy._clock()
    return SignalEnvelope(
        signal_id=uuid7(),
        position_group_id=group_id,
        strategy_id=STRATEGY_ID,
        intent="CANCEL",
        created_at=_rfc3339(now),
        valid_until=_rfc3339(now + EXIT_TTL),
        underlying=f"{strategy.symbol_a}/{strategy.symbol_b}",
        reference=Reference(spot=entry_spot, captured_at=_rfc3339(now)),
        supersedes=entry_signal_id,
        rationale=f"entry batch failed to establish a position: {detail}",
        tags=_with_system_tag(strategy, {
            "pair": [strategy.symbol_a, strategy.symbol_b],
        }),
    )
