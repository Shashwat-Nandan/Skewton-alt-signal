#!/usr/bin/env python3
"""Cross-strategy portfolio view — net exposure per underlying, read-only.

Answers the question no single runner can today: *what is the account's
net position in each underlying, across ALL strategies at once?* Each
runner owns its own state file / DB table; a NIFTY futures leg in the pair
book and a NIFTY option book in the Taleb hedger are invisible to each
other. This aggregates them (NautilusTrader eval §4.5 — the read-only seed
of the SaaS per-user portfolio).

Read-only and offline: it reads the same state files / dashboard.db the
runners and the dashboard already produce (no Kite auth, no network, no
order path). Mirrors ``scripts/strategy_scoreboard.py`` — one small reader
per persistence shape.

**Delta-1 vs options.** Futures, stock-futures (pairs / arbitrage legs) and
cash equity are delta-1: their underlying exposure is exactly
``signed_qty × lot_size`` (or shares), known from state. OPTION delta needs
a live spot (Black-Scholes), which is not in any state file — so option
legs are listed and their premium shown, but they are EXCLUDED from the
net delta-1 figure and flagged. Net option delta by underlying is the job
of the live backend router (Phase 2b), which runs alongside quotes.

Usage:
    python -m scripts.portfolio_view              # table
    python -m scripts.portfolio_view --json       # machine-readable
    python -m scripts.portfolio_view --cache-dir data_cache
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


@dataclass
class Contribution:
    """One strategy's exposure to one underlying."""
    underlying: str
    system: str            # e.g. "pair:baseline(paper)", "taleb:NIFTY", "equity_swing"
    kind: str              # "future" | "equity" | "option" | "futures_hedge"
    signed_units: float    # signed contracts (lots) or shares, as booked
    delta1_units: Optional[float]  # underlying-equivalent delta-1 exposure; None for options
    notional: Optional[float]      # signed INR notional at last mark; None if no mark/spot
    detail: str = ""       # tradingsymbol or note


@dataclass
class UnderlyingBook:
    underlying: str
    contributions: List[Contribution] = field(default_factory=list)

    @property
    def net_delta1_units(self) -> float:
        return sum(c.delta1_units for c in self.contributions if c.delta1_units is not None)

    @property
    def net_notional(self) -> float:
        return sum(c.notional for c in self.contributions if c.notional is not None)

    @property
    def systems(self) -> List[str]:
        # preserve first-seen order, de-duplicated
        seen: Dict[str, None] = {}
        for c in self.contributions:
            seen.setdefault(c.system, None)
        return list(seen)

    @property
    def has_options(self) -> bool:
        return any(c.kind == "option" for c in self.contributions)

    @property
    def is_shared(self) -> bool:
        return len(self.systems) > 1


# ── Readers (pure: take already-parsed data, return Contributions) ───────────
# Kept dict-in so tests drive the exact real serialized shapes without disk.

def _as_list(container) -> list:
    """Serialized position/trade containers differ by producer — arbitrage
    writes open_calendars as a LIST, buy_on_gap writes positions as a DICT
    keyed by symbol, pairs write legs as a LIST. Accept any of them (and
    None/garbage) so a reader never crashes on the wrong container type."""
    if isinstance(container, dict):
        return list(container.values())
    if isinstance(container, list):
        return container
    return []


def _sys_label(base: str, blob: dict) -> str:
    """Append the runner mode only when the payload actually carries it
    (pair states do; arbitrage/gap states do not — labelling those
    'arbitrage(?)' is misleading)."""
    mode = blob.get("mode") or (blob.get("state", {}) or {}).get("mode")
    return f"{base}({mode})" if mode else base


def read_pair_blob(blob: dict, system_label: str) -> List[Contribution]:
    """Pair/Kalman-pair state: top-level ``pairs`` list, each with a
    ``state`` whose ``legs`` are open only when position != FLAT. Each leg
    is a delta-1 stock-future; the underlying is ``leg.symbol``."""
    out: List[Contribution] = []
    label = _sys_label(system_label, blob)
    for pair in blob.get("pairs", []):
        st = pair.get("state", {}) or {}
        if (st.get("position") or "FLAT") == "FLAT":
            continue
        for leg in _as_list(st.get("legs")):
            out.append(_future_contribution(leg, label))
    return out


def read_kalman_pairs_blob(blob: dict) -> List[Contribution]:
    """Kalman pairs runner stores a single pair-shaped ``state`` (no outer
    ``pairs`` list on some versions). Handle both layouts defensively."""
    if "pairs" in blob:
        return read_pair_blob(blob, "kalman_pairs")
    st = blob.get("state", {}) or {}
    if (st.get("position") or "FLAT") == "FLAT":
        return []
    label = _sys_label("kalman_pairs", blob)
    return [_future_contribution(leg, label) for leg in _as_list(st.get("legs"))]


def read_arbitrage_blob(blob: dict) -> List[Contribution]:
    """Arbitrage state: ``state.open_calendars`` is a dict keyed by
    underlying → CalendarTrade with delta-1 future ``legs``."""
    out: List[Contribution] = []
    label = _sys_label("arbitrage", blob)
    st = blob.get("state", {}) or {}
    # open_calendars is serialized as a LIST of trade dicts (arbitrage.py:550),
    # though it is a dict in memory — _as_list handles either.
    for trade in _as_list(st.get("open_calendars")):
        for leg in _as_list(trade.get("legs")):
            out.append(_future_contribution(leg, label))
    return out


def read_taleb_blob(blob: dict, underlying: str) -> List[Contribution]:
    """Taleb hedger state: options keyed by tradingsymbol (underlying comes
    from the FILE identity, not the position). Options are flagged and
    excluded from delta-1; the book-level ``futures_hedge_delta`` IS a
    delta-1 contribution."""
    out: List[Contribution] = []
    st = blob.get("state", {}) or {}
    # Taleb state.positions holds ONLY options (CE/PE) — the futures hedge is
    # NOT a position here, it lives in the book-level futures_hedge_delta
    # (taleb_karpathy.py). So there is no FUT-in-positions branch and no
    # hedge double-count: options (delta excluded, needs live spot) below,
    # the delta-1 hedge added once after the loop.
    for p in _as_list(st.get("positions")):
        # CE/PE only — same filter as taleb_option_positions(), so has_options
        # here and the greeks input there can never diverge (a non-CE/PE row,
        # which taleb never writes, must not set has_options while being
        # excluded from pricing → a total with silently-missing delta).
        if (p.get("option_type") or "").upper() not in ("CE", "PE"):
            continue
        lots = float(p.get("quantity", 0) or 0)
        lot_size = float(p.get("lot_size", 0) or 0)
        mark = p.get("current_price")
        premium = (lots * lot_size * mark) if mark is not None else None
        out.append(Contribution(
            underlying=underlying, system=f"taleb:{underlying}", kind="option",
            signed_units=lots, delta1_units=None, notional=premium,
            detail=p.get("tradingsymbol", ""),
        ))
    hedge_delta = float(st.get("futures_hedge_delta", 0) or 0)
    if hedge_delta:
        out.append(Contribution(
            underlying=underlying, system=f"taleb:{underlying}", kind="futures_hedge",
            signed_units=hedge_delta, delta1_units=hedge_delta, notional=None,
            detail="futures_hedge_delta (notional needs live spot)",
        ))
    return out


def read_equity_positions(rows, system: str) -> List[Contribution]:
    """Cash-equity longs (buy_on_gap / equity_swing / mp). ``symbol`` IS the
    underlying; delta-1 with 1 delta per share. ``rows`` may be a list
    (dashboard.db) or a dict keyed by symbol (buy_on_gap JSON)."""
    out: List[Contribution] = []
    for r in _as_list(rows):
        if (r.get("status") or "OPEN") != "OPEN":
            continue
        sym = r.get("symbol")
        qty = float(r.get("qty", 0) or 0)
        if not sym or qty == 0:
            continue
        mark = r.get("last_mtm_px") or r.get("entry_px") or r.get("entry_price")
        notional = (qty * mark) if mark else None
        out.append(Contribution(
            underlying=sym, system=system, kind="equity",
            signed_units=qty, delta1_units=qty, notional=notional, detail=sym,
        ))
    return out


def _future_contribution(leg: dict, system: str) -> Contribution:
    lots = float(leg.get("quantity", 0) or 0)
    lot_size = float(leg.get("lot_size", 0) or 0)
    units = lots * lot_size
    mark = leg.get("current_price") or leg.get("entry_price")
    notional = (units * mark) if mark else None
    return Contribution(
        underlying=leg.get("symbol", "?"), system=system, kind="future",
        signed_units=lots, delta1_units=units, notional=notional,
        detail=leg.get("tradingsymbol", ""),
    )


# ── Aggregation ──────────────────────────────────────────────────────────────

def aggregate(contribs: List[Contribution]) -> Dict[str, UnderlyingBook]:
    books: Dict[str, UnderlyingBook] = {}
    for c in contribs:
        books.setdefault(c.underlying, UnderlyingBook(c.underlying)).contributions.append(c)
    return books


# ── Loaders (thin disk/DB layer over the pure readers) ───────────────────────

def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _taleb_underlying(path: Path) -> str:
    """taleb_paper_state.json → NIFTY; taleb_paper_state_BANKNIFTY.json → BANKNIFTY."""
    stem = path.stem  # taleb_paper_state[_UNDERLYING]
    suffix = stem[len("taleb_paper_state"):].lstrip("_")
    return suffix or "NIFTY"


def taleb_option_positions(cache_dir: Path) -> Dict[str, List[dict]]:
    """Raw open OPTION position dicts per underlying, from the Taleb state
    file(s). The offline view can only report these as premium (no delta —
    Black-Scholes needs a live spot); the live backend router feeds them to
    core.greeks_engine with a fetched spot to get net option delta. Kept
    here so taleb-state location/identity lives in ONE place (this module),
    not re-globbed in the backend. Returns ``{underlying: [pos_dict, ...]}``
    where each pos_dict carries the OptionContract fields except spot."""
    out: Dict[str, List[dict]] = {}
    for path in sorted(cache_dir.glob("taleb_paper_state*.json")):
        blob = _load_json(path)
        if not blob:
            continue
        underlying = _taleb_underlying(path)
        st = blob.get("state", {}) or {}
        opts = [p for p in _as_list(st.get("positions"))
                if (p.get("option_type") or "").upper() in ("CE", "PE")]
        if opts:
            out.setdefault(underlying, []).extend(opts)
    return out


def _load_sqlite_positions(db_path: Path, table: str) -> List[dict]:
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                f"SELECT * FROM {table} WHERE status = 'OPEN'"  # noqa: S608 (fixed table names)
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def collect(cache_dir: Path) -> List[Contribution]:
    """Read every known state source under ``cache_dir`` into Contributions.
    Missing / malformed / flat sources contribute nothing. Each source is
    isolated: a reader that raises (e.g. a state-file shape change) is
    skipped with a loud stderr warning rather than blanking the whole
    portfolio or crashing (never-raises contract + Rule 12 — fail loud but
    keep going, so one broken strategy can't hide the rest of the book)."""
    contribs: List[Contribution] = []

    def _src(label: str, fn):
        try:
            contribs.extend(fn())
        except Exception as e:  # noqa: BLE001 — best-effort per-source isolation
            print(f"portfolio_view: skipped {label} ({type(e).__name__}: {e})",
                  file=sys.stderr)

    for path in sorted(cache_dir.glob("pair_paper_state_*.json")):
        if blob := _load_json(path):
            _src(path.name, lambda b=blob: read_pair_blob(
                b, f"pair:{b.get('system', 'pair')}"))

    kp = cache_dir / "kalman_pairs_runner_state.json"
    if kp.exists() and (blob := _load_json(kp)):
        _src(kp.name, lambda b=blob: read_kalman_pairs_blob(b))

    for path in sorted(cache_dir.glob("arbitrage_paper_state_*.json")):
        if blob := _load_json(path):
            _src(path.name, lambda b=blob: read_arbitrage_blob(b))

    for path in sorted(cache_dir.glob("taleb_paper_state*.json")):
        if blob := _load_json(path):
            _src(path.name, lambda b=blob, p=path: read_taleb_blob(b, _taleb_underlying(p)))

    for path in sorted(cache_dir.glob("buy_on_gap_paper_state_*.json")):
        if blob := _load_json(path):
            _src(path.name, lambda b=blob: read_equity_positions(
                (b.get("state", {}) or {}).get("positions"), "buy_on_gap"))

    db = cache_dir / "dashboard.db"
    _src("dashboard.db:equity_positions",
         lambda: read_equity_positions(_load_sqlite_positions(db, "equity_positions"), "equity_swing"))
    _src("dashboard.db:mp_trend_positions",
         lambda: read_equity_positions(_load_sqlite_positions(db, "mp_trend_positions"), "mp_trend"))

    return contribs


# ── Rendering ────────────────────────────────────────────────────────────────

def to_json(books: Dict[str, UnderlyingBook]) -> dict:
    return {
        "underlyings": [
            {
                "underlying": b.underlying,
                "net_delta1_units": round(b.net_delta1_units, 4),
                "net_notional": round(b.net_notional, 2),
                "systems": b.systems,
                "shared": b.is_shared,
                "has_options": b.has_options,
                "contributions": [
                    {
                        "system": c.system, "kind": c.kind,
                        "signed_units": c.signed_units,
                        "delta1_units": c.delta1_units,
                        "notional": (round(c.notional, 2) if c.notional is not None else None),
                        "detail": c.detail,
                    }
                    for c in b.contributions
                ],
            }
            for b in sorted(books.values(), key=lambda x: -abs(x.net_notional))
        ],
        "shared_underlyings": sorted(u for u, b in books.items() if b.is_shared),
    }


def render_table(books: Dict[str, UnderlyingBook]) -> str:
    lines: List[str] = []
    lines.append("=" * 92)
    lines.append("CROSS-STRATEGY PORTFOLIO — net exposure per underlying (read-only, offline)")
    lines.append("=" * 92)
    if not books:
        lines.append("  No open positions across any strategy.")
        return "\n".join(lines)
    lines.append(f"  {'Underlying':<14}{'Net Δ1 units':>14}{'Net notional':>16}"
                 f"  {'#sys':>4}  Systems (flags)")
    lines.append("-" * 92)
    for b in sorted(books.values(), key=lambda x: -abs(x.net_notional)):
        flags = []
        if b.is_shared:
            flags.append("SHARED")
        if b.has_options:
            flags.append("opt-Δ-excl")
        flag_s = f"  [{','.join(flags)}]" if flags else ""
        lines.append(f"  {b.underlying:<14}{b.net_delta1_units:>14,.1f}"
                     f"{b.net_notional:>16,.0f}  {len(b.systems):>4}  "
                     f"{', '.join(b.systems)}{flag_s}")
    lines.append("-" * 92)
    shared = [b.underlying for b in books.values() if b.is_shared]
    if shared:
        lines.append(f"  ⚠ {len(shared)} underlying(s) held by >1 strategy "
                     f"(net-exposure/margin overlap): {', '.join(sorted(shared))}")
    if any(b.has_options for b in books.values()):
        lines.append("  note: option delta EXCLUDED from Net Δ1 (needs live spot — "
                     "see the live portfolio router, Phase 2b).")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-strategy portfolio view (read-only)")
    ap.add_argument("--cache-dir", default="data_cache", type=Path)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    books = aggregate(collect(args.cache_dir))
    if args.json:
        print(json.dumps(to_json(books), indent=2))
    else:
        print(render_table(books))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
