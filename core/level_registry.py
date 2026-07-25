"""Persistent Market-Profile level registry — reversal engine Phase A2.

The registry is the plan's highest-value new infrastructure (§4.2 of
docs/research/auction-orderflow-reversal-engine-2026-07-22.md): it turns L4
(first-test / retest discipline) from a memory exercise into mechanical
bookkeeping. A `Level` is a price the auction has marked — a value-area edge, a
volume node, an excess tail, a single-print — that *persists across sessions*,
accrues an outcome every time price tests it, and ages out when it stops
mattering. The strategy (Phase D) reads a level's `test_count` and outcome
history to decide whether a touch is a fresh first-test (tradeable) or a tired
re-test (skip).

Sources map 1:1 onto fields the profile layer already produces
(`DayProfile`, `CompositeProfile`, `DayIndicators`, and `market_profile.hvn_lvn`
over the A1 tape volume-at-price). The one piece of judgment is the **lunch
rule** (§2.5 of the original spec): 11:30–13:30 IST is low-participation, so a
thin print there must not mint a Low-Volume Node daily — LVN/HVN derivation runs
on a volume profile with the lunch window excluded.

Pure and offline: no kite, no order path. Persistence is JSON via the repo's
atomic-write primitive; the strategy owns *when* to save/restore.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.market_profile import (
    Bar,
    CompositeProfile,
    DayIndicators,
    DayProfile,
    _bin_mid,
    _ceil_to_tick,
    _floor_to_tick,
    auto_tick_size,
    hvn_lvn,
)
from core.runner_common import durable_write_text

# Every source tag a Level can carry. Each maps to a field the profile layer
# already computes — the registry invents no new geometry (Rule 5).
LEVEL_SOURCES = frozenset({
    "weekly_vah", "weekly_val", "composite_poc",
    "session_poc", "session_vah", "session_val",
    "ib_high", "ib_low",
    "excess_high", "excess_low", "poor_high", "poor_low",
    "single_print", "session_hvn", "session_lvn",
})

# India's lunch lull: the exchange is open but participation collapses, so a
# thin volume trough here is an artifact, not a level (§2.5).
LUNCH_START = time(11, 30)
LUNCH_END = time(13, 30)


# ──────────────────────────────────────────────────────────
# Level & test-outcome records
# ──────────────────────────────────────────────────────────

@dataclass
class TestOutcome:
    """One test of a level: price came to it and either held or gave way.

    MFE/MAE are signed *magnitudes* of the reaction the caller measured after
    the touch (favorable = away from the level in the trade's direction, adverse
    = penetration through it); `absorbed` is True when the level held (a
    reaction) and False when price accepted through it (the level failed). The
    registry stores what the strategy measured — it does not compute excursions.
    """
    ts: datetime
    mfe: float
    mae: float
    absorbed: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts.isoformat(), "mfe": self.mfe,
                "mae": self.mae, "absorbed": self.absorbed}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TestOutcome":
        return cls(ts=datetime.fromisoformat(d["ts"]), mfe=float(d["mfe"]),
                   mae=float(d["mae"]), absorbed=bool(d["absorbed"]))


@dataclass
class Level:
    """A persistent price level and its test history."""
    price: float
    source: str
    instrument: str
    created_at: datetime
    test_count: int = 0
    tests: List[TestOutcome] = field(default_factory=list)
    last_tested_at: Optional[datetime] = None

    def register_test(self, outcome: TestOutcome) -> None:
        self.tests.append(outcome)
        self.test_count += 1
        if self.last_tested_at is None or outcome.ts > self.last_tested_at:
            self.last_tested_at = outcome.ts

    def last_active_at(self) -> datetime:
        """Most recent moment this level mattered — its last test, else birth."""
        return self.last_tested_at or self.created_at

    def age_days(self, now: datetime) -> float:
        """Days since the level was last active (tested, else created)."""
        return (now - self.last_active_at()).total_seconds() / 86400.0

    def staleness(self, now: datetime, *, half_life_days: float = 10.0) -> float:
        """Decay weight in (0, 1]: 1.0 fresh, halving every `half_life_days`.

        A tested level resets toward fresh (age is measured from the last
        test), so a repeatedly-defended level stays alive while an untouched one
        fades — the staleness the strategy weights a touch by.
        """
        if half_life_days <= 0:
            return 1.0
        return 0.5 ** (max(self.age_days(now), 0.0) / half_life_days)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "price": self.price,
            "source": self.source,
            "instrument": self.instrument,
            "created_at": self.created_at.isoformat(),
            "test_count": self.test_count,
            "tests": [t.to_dict() for t in self.tests],
            "last_tested_at": (self.last_tested_at.isoformat()
                               if self.last_tested_at is not None else None),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Level":
        source = str(d["source"])
        # Same guard upsert enforces (Rule 12): a persisted level with a
        # renamed/typo'd source must fail loud on load, not silently deserialize
        # and get re-emitted as if valid (2026-07-25 review).
        if source not in LEVEL_SOURCES:
            raise ValueError(f"unknown level source {source!r} in persisted registry")
        lt = d.get("last_tested_at")
        return cls(
            price=float(d["price"]), source=source,
            instrument=str(d["instrument"]),
            created_at=datetime.fromisoformat(d["created_at"]),
            test_count=int(d.get("test_count", 0)),
            tests=[TestOutcome.from_dict(t) for t in d.get("tests", [])],
            last_tested_at=datetime.fromisoformat(lt) if lt else None,
        )


# ──────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────

class LevelRegistry:
    """A persistent, multi-instrument collection of `Level`s.

    `price_tol` is the absolute price window used both to dedupe an incoming
    level against an existing one of the same (instrument, source) and to decide
    which levels a price *touch* tests. It defaults small; callers on a wide
    instrument pass their own.
    """

    def __init__(self, price_tol: float = 5.0) -> None:
        self.price_tol = price_tol
        self._levels: List[Level] = []

    # -- mutation ------------------------------------------------------

    def upsert(
        self, price: float, source: str, instrument: str,
        created_at: datetime, *, price_tol: Optional[float] = None,
    ) -> Level:
        """Add a level, or return the existing one it coincides with.

        Re-deriving the same source near an existing price (a value-area edge
        that drifts a few points week to week) must NOT mint a duplicate or
        reset the original's age — the persistence *is* the point. Match is by
        same instrument + same source within `price_tol`; a match keeps the
        original's identity (created_at, test history) but **re-centers its
        price to the latest derivation** so `level.price` tracks where the
        auction is actually marking now, not where the edge first appeared
        (Phase D reads `level.price` to place entries/stops — 2026-07-25
        review). Age is measured from creation/last-test, so re-centering does
        not reset staleness.
        """
        if source not in LEVEL_SOURCES:
            raise ValueError(f"unknown level source {source!r}")
        tol = self.price_tol if price_tol is None else price_tol
        for lvl in self._levels:
            if (lvl.instrument == instrument and lvl.source == source
                    and abs(lvl.price - price) <= tol):
                lvl.price = float(price)   # re-center to the current edge
                return lvl
        lvl = Level(price=price, source=source, instrument=instrument,
                    created_at=created_at)
        self._levels.append(lvl)
        return lvl

    def record_test(
        self, price: float, ts: datetime, instrument: str,
        *, mfe: float, mae: float, absorbed: bool,
        price_tol: Optional[float] = None,
    ) -> List[Level]:
        """Register a touch: every level within `price_tol` of `price` is tested.

        A price zone tests *all* the levels sitting in it (a value-area edge and
        a coincident volume node are both being probed by the same touch), not
        just the nearest — the L4 semantics the strategy needs. Returns the
        levels that were tested (empty if the touch hit none).
        """
        tol = self.price_tol if price_tol is None else price_tol
        hit = [lvl for lvl in self._levels
               if lvl.instrument == instrument and abs(lvl.price - price) <= tol]
        for lvl in hit:
            lvl.register_test(TestOutcome(ts=ts, mfe=mfe, mae=mae, absorbed=absorbed))
        return hit

    def prune_stale(self, now: datetime, *, max_age_days: float) -> int:
        """Drop levels untouched for longer than `max_age_days`. Returns count.

        Age is from last activity, so a still-defended level survives regardless
        of how long ago it was born.
        """
        before = len(self._levels)
        self._levels = [lvl for lvl in self._levels
                        if lvl.age_days(now) <= max_age_days]
        return before - len(self._levels)

    # -- query ---------------------------------------------------------

    def levels_near(
        self, price: float, *, instrument: Optional[str] = None,
        price_tol: Optional[float] = None,
    ) -> List[Level]:
        """Levels within `price_tol` of `price`, nearest first."""
        tol = self.price_tol if price_tol is None else price_tol
        hit = [lvl for lvl in self._levels
               if (instrument is None or lvl.instrument == instrument)
               and abs(lvl.price - price) <= tol]
        return sorted(hit, key=lambda lvl: abs(lvl.price - price))

    def all_levels(self, instrument: Optional[str] = None) -> List[Level]:
        return [lvl for lvl in self._levels
                if instrument is None or lvl.instrument == instrument]

    def __len__(self) -> int:
        return len(self._levels)

    # -- persistence ---------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        # Levels are emitted in a deterministic order (instrument, source,
        # price) so the serialized JSON is byte-stable across replays regardless
        # of insertion order — the A2 determinism gate depends on this.
        ordered = sorted(self._levels,
                         key=lambda lvl: (lvl.instrument, lvl.source, lvl.price))
        return {"price_tol": self.price_tol,
                "levels": [lvl.to_dict() for lvl in ordered]}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LevelRegistry":
        reg = cls(price_tol=float(d.get("price_tol", 5.0)))
        reg._levels = [Level.from_dict(x) for x in d.get("levels", [])]
        return reg

    def save(self, path: Path) -> None:
        durable_write_text(Path(path),
                           json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: Path) -> "LevelRegistry":
        return cls.from_dict(json.loads(Path(path).read_text()))


# ──────────────────────────────────────────────────────────
# Derivation — profile fields → level sources (1:1)
# ──────────────────────────────────────────────────────────

def _volume_profile_from_bars(
    bars: Sequence[Bar], tick_size: float,
) -> tuple[List[float], List[float]]:
    """Volume-at-price histogram from OHLCV bars, on a tick-snapped grid.

    Each bar spreads its volume uniformly across the price bins its [low, high]
    range spans — the coarse-but-deterministic volume profile used for node
    derivation (the fine tick-level profile lives in `research.tape_vap`; here
    bars are the input because they carry the timestamps the lunch rule needs
    and make the 20-session replay deterministic). Bars with zero volume (e.g.
    the index spot) contribute nothing, so an index yields no nodes.

    The bin lattice mirrors `compute_day_profile` exactly — same `_floor_to_tick`
    (with its float-epsilon guard) and the same `//` + boundary-clamp crossing —
    so a node price lands where the canonical TPO profile bins it, not half a
    tick above or one tick below (2026-07-25 review).
    """
    if not bars:
        return [], []
    lo = min(b.low for b in bars)
    hi = max(b.high for b in bars)
    if hi <= lo:
        return [], []
    base = _floor_to_tick(lo, tick_size)
    n_bins = max(int(round((_ceil_to_tick(hi, tick_size) - base) / tick_size)), 1)
    counts = [0.0] * n_bins
    for bar in bars:
        if bar.volume <= 0:
            continue
        lo_idx = max(int((bar.low - base) // tick_size), 0)
        hi_idx = min(int((bar.high - base) // tick_size), n_bins - 1)
        if bar.high == base + (hi_idx + 1) * tick_size:
            hi_idx = min(hi_idx + 1, n_bins - 1)
        spread = hi_idx - lo_idx + 1
        share = bar.volume / spread
        for i in range(lo_idx, hi_idx + 1):
            counts[i] += share
    mids = [_bin_mid(base, tick_size, i) for i in range(n_bins)]
    return mids, counts


def _deweight_lunch(bars: Sequence[Bar], weight: float) -> List[Bar]:
    """Scale 11:30–13:30 bars' volume by `weight` (§2.5).

    Deweight, not drop: at `weight=0` the lunch window contributes nothing (a
    thin lull mints no node — the rule's purpose), but the default `weight` only
    *discounts* it, so a session that genuinely transacts through lunch (a
    trend/news day) keeps a real acceptance shelf instead of having it erased
    into a spurious LVN — the inversion a hard exclude risks (2026-07-25 review).
    """
    if weight >= 1.0:
        return list(bars)
    return [replace(b, volume=int(b.volume * weight))
            if (LUNCH_START <= b.ts.time() < LUNCH_END) else b
            for b in bars]


def session_nodes(
    bars: Sequence[Bar],
    *,
    lunch_weight: float = 0.25,
    tick_size: Optional[float] = None,
    smoothing_bins: int = 2,
    min_prominence: float = 0.10,
) -> tuple[List[float], List[float]]:
    """(HVNs, LVNs) for one session's bars, lunch-deweighted by default.

    `lunch_weight` scales the 11:30–13:30 window's volume before node detection
    (0 = fully excluded, 1 = untouched, default 0.25 = discounted) so a lunch
    lull can't mint a daily fake LVN without erasing a genuine lunch shelf
    (§2.5). `tick_size` should be a **fixed** per-instrument value when nodes are
    compared across sessions (see `ingest_session`); left None it auto-sizes to
    this session's range, which is fine for a one-off but drifts the grid day to
    day. Returns `market_profile.hvn_lvn`'s output.
    """
    use = _deweight_lunch(bars, lunch_weight)
    if not use:
        return [], []
    ts = tick_size or auto_tick_size(
        [b.high for b in use] + [b.low for b in use])
    mids, vols = _volume_profile_from_bars(use, ts)
    return hvn_lvn(mids, vols, smoothing_bins=smoothing_bins,
                   min_prominence=min_prominence)


def ingest_session(
    registry: LevelRegistry,
    *,
    instrument: str,
    created_at: datetime,
    day_profile: Optional[DayProfile] = None,
    indicators: Optional[DayIndicators] = None,
    composite: Optional[CompositeProfile] = None,
    session_bars: Optional[Sequence[Bar]] = None,
    lunch_weight: float = 0.25,
    node_tick_size: Optional[float] = None,
    node_min_prominence: float = 0.10,
) -> LevelRegistry:
    """Mint every level source a session produces and upsert them.

    Each argument that is supplied contributes its levels; all are optional so a
    caller with only a `DayProfile` still works. `created_at` stamps the birth
    of any *new* level (an upsert onto an existing one leaves its birth alone).
    Returns the same registry for chaining.

    `node_tick_size` fixes the grid HVN/LVN derivation bins on. It defaults to
    the registry's `price_tol` — a **stable, absolute** grid — so the same
    physical node lands on the same bin-mid every session and `upsert` dedups it
    across days. Letting `session_nodes` auto-size the tick per session (its own
    default) would put the same node ~a tick apart on a wide day vs a narrow one
    and defeat cross-session dedup — the exact L4 error the registry exists to
    prevent (2026-07-25 review).
    """
    def add(price: Optional[float], source: str) -> None:
        if price is not None:
            registry.upsert(float(price), source, instrument, created_at)

    if composite is not None:
        add(composite.vah, "weekly_vah")
        add(composite.val, "weekly_val")
        add(composite.poc, "composite_poc")

    if day_profile is not None:
        add(day_profile.poc, "session_poc")
        add(day_profile.vah, "session_vah")
        add(day_profile.val, "session_val")
        add(day_profile.ib_high, "ib_high")
        add(day_profile.ib_low, "ib_low")

    if indicators is not None:
        if indicators.excess_high:
            add(indicators.high, "excess_high")
        if indicators.excess_low:
            add(indicators.low, "excess_low")
        if indicators.poor_high:
            add(indicators.high, "poor_high")
        if indicators.poor_low:
            add(indicators.low, "poor_low")
        for lvl in indicators.single_print_levels:
            add(lvl, "single_print")

    if session_bars:
        hvns, lvns = session_nodes(
            session_bars, lunch_weight=lunch_weight,
            tick_size=node_tick_size if node_tick_size is not None else registry.price_tol,
            min_prominence=node_min_prominence)
        for h in hvns:
            add(h, "session_hvn")
        for lvn in lvns:
            add(lvn, "session_lvn")

    return registry
