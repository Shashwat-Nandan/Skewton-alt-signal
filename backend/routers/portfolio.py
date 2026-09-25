"""Cross-strategy portfolio view — live-augmented (NautilusTrader eval §4.5b).

Extends the offline aggregator (``scripts.portfolio_view``, read-only) with
the two things that need live data and therefore cannot live in the CLI:

  * **net OPTION delta per underlying** — Black-Scholes delta needs a live
    spot; we fetch it (``kite.ltp``) and run ``core.greeks_engine`` over the
    Taleb option book, so "what is the account's net NIFTY delta right now?"
    finally has an answer that includes the options, not just the futures.
  * **broker truth** — ``kite.positions()`` net book, surfaced alongside our
    per-strategy state so divergence is visible.

Read-only: no orders, ever (the dashboard never trades — AGENTS.md). When no
broker session is cached it **degrades gracefully** to the exact offline
delta-1 view (option delta null, broker section omitted) rather than 401 —
the exposure tab stays useful signed-out.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from core.greeks_engine import GreeksEngine, OptionContract, time_to_expiry
from scripts.portfolio_view import aggregate, collect, taleb_option_positions
# Canonical index→spot-quote map lives with the strategy that owns spot
# resolution; import it rather than duplicate (drift risk, review §4.5b).
from strategies.taleb_karpathy import _INDEX_SPOT_SYMBOLS

from core.broker import get_broker, read_broker_name
from core.broker.errors import BrokerConfigError

from .. import kite_oauth
from ..settings import REPO_ROOT, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portfolio", tags=["portfolio"])

DATA_CACHE = REPO_ROOT / "data_cache"


def _spot_symbol(underlying: str) -> str:
    return _INDEX_SPOT_SYMBOLS.get(underlying, f"NSE:{underlying}")


class UnderlyingExposure(BaseModel):
    underlying: str
    net_delta1_units: float          # futures + equity + futures-hedge (exact, offline)
    net_option_delta: Optional[float]  # from live greeks; None when no session
    net_total_delta: Optional[float]   # delta1 + option delta; None when no session
    net_notional: float
    systems: List[str]
    shared: bool
    has_options: bool


class BrokerPosition(BaseModel):
    tradingsymbol: str
    exchange: str
    quantity: int
    average_price: float
    pnl: float


class PortfolioResponse(BaseModel):
    live: bool                       # True = a broker session augmented this view
    note: str
    underlyings: List[UnderlyingExposure]
    broker_net: Optional[List[BrokerPosition]]


def _net_option_delta_by_underlying(kite) -> Dict[str, float]:
    """Compute net option delta per underlying from the Taleb option book
    using a live spot. Best-effort per underlying: a quote/greeks failure on
    one underlying drops just that one (logged), never the whole view."""
    engine = GreeksEngine()
    out: Dict[str, float] = {}
    for underlying, positions in taleb_option_positions(DATA_CACHE).items():
        try:
            quote = kite.ltp([_spot_symbol(underlying)]) or {}
            row = next(iter(quote.values()), None)
            spot = float(row["last_price"]) if row else 0.0
            if spot <= 0:
                # empty quote (pre-open / unknown symbol) or a bad print —
                # can't price options; leave this underlying's delta absent
                # (→ total None downstream) rather than log(0)-crash or fake 0.
                logger.warning("portfolio: no usable spot for %s (quote=%r) — "
                               "option delta omitted", underlying, quote)
                continue
            contracts, per_leg_T = [], {}
            for p in positions:
                ts = p.get("tradingsymbol", "")
                contracts.append(OptionContract(
                    tradingsymbol=ts, instrument_token=int(p.get("instrument_token", 0)),
                    strike=float(p.get("strike", 0)), expiry=p.get("expiry", ""),
                    option_type=(p.get("option_type") or "CE").upper(),
                    lot_size=int(p.get("lot_size", 0)), quantity=int(p.get("quantity", 0)),
                    entry_price=float(p.get("entry_price", 0)),
                    current_price=float(p.get("current_price", 0)),
                    iv=float(p.get("iv", 0) or 0),
                ))
                per_leg_T[ts] = time_to_expiry(p.get("expiry", ""))
            default_T = next(iter(per_leg_T.values()), 0.0)
            # net_delta is analytic per-leg (independent of the price grid);
            # price_steps=3 skips the 33-point pnl/gamma profile we don't read.
            greeks = engine.compute_portfolio_greeks(
                contracts, spot, default_T, per_leg_T=per_leg_T, price_steps=3,
            )
            out[underlying] = greeks.net_delta
        except Exception as e:  # noqa: BLE001 — one bad underlying must not sink the view
            logger.warning("portfolio: option delta for %s skipped (%s: %s)",
                           underlying, type(e).__name__, e)
    return out


def _broker_net(kite) -> Optional[List[BrokerPosition]]:
    try:
        net = kite.positions().get("net", [])
        return [
            BrokerPosition(
                tradingsymbol=p.get("tradingsymbol", ""),
                exchange=p.get("exchange", ""),
                quantity=int(p.get("quantity", 0)),
                average_price=float(p.get("average_price", 0)),
                pnl=float(p.get("pnl", 0)),
            )
            for p in net if int(p.get("quantity", 0)) != 0
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("portfolio: broker positions skipped (%s: %s)", type(e).__name__, e)
        return None


def _session_client():
    """Cached broker client for the configured adapter. Zerodha stays on
    kite_oauth so existing tests that patch that boundary keep working."""
    name = read_broker_name(str(get_settings().config_path))
    if name != "zerodha":
        try:
            return get_broker(str(get_settings().config_path)).cached_client()
        except BrokerConfigError:
            return None
    return kite_oauth.get_authenticated_kite()


@router.get("/exposure", response_model=PortfolioResponse)
def get_portfolio_exposure() -> PortfolioResponse:
    """Net exposure per underlying across all strategies, augmented with live
    option delta + broker truth when a broker session is available."""
    books = aggregate(collect(DATA_CACHE))

    kite = _session_client()
    session_present = kite is not None
    broker = _broker_net(kite) if session_present else None
    option_delta = _net_option_delta_by_underlying(kite) if session_present else {}
    # Broker access tokens expire daily (~06:00 IST) and get_authenticated_kite
    # does NOT verify — a cached-but-rejected token yields a non-None client
    # whose every call raises. Treat the view as "live" only if a broker call
    # actually succeeded (None = it raised = token bad), so a stale token
    # degrades honestly to the offline label instead of claiming a join that
    # never happened (Rule 12).
    live = session_present and broker is not None

    # Union underlyings from the aggregated books AND any that priced an
    # option delta: if collect()'s per-source isolation ever skips a taleb
    # file, taleb_option_positions still reads it — without the union that
    # options position would silently vanish from the exposure view.
    rows: List[UnderlyingExposure] = []
    for u in set(books) | set(option_delta):
        b = books.get(u)
        delta1 = b.net_delta1_units if b else 0.0
        has_opts = (b.has_options if b else False) or (u in option_delta)
        if not has_opts:
            opt_d = 0.0            # no options → option delta is exactly 0 (offline too)
        elif live:
            opt_d = option_delta.get(u)   # None if this underlying failed to price
        else:
            opt_d = None           # options present but no live session to price them
        total = (delta1 + opt_d) if opt_d is not None else None
        rows.append(UnderlyingExposure(
            underlying=u,
            net_delta1_units=round(delta1, 4),
            net_option_delta=(round(opt_d, 4) if opt_d is not None else None),
            net_total_delta=(round(total, 4) if total is not None else None),
            net_notional=round(b.net_notional if b else 0.0, 2),
            systems=(b.systems if b else [f"taleb:{u}"]),
            shared=(b.is_shared if b else False),
            has_options=has_opts,
        ))
    rows.sort(key=lambda r: -abs(r.net_notional))

    if live:
        note = "live: option delta from broker spot + broker net joined"
    elif session_present:
        note = ("Broker session present but calls failed (token likely expired "
                "~06:00 IST) — showing offline delta-1 only")
    else:
        note = "offline: no broker session — option delta excluded, delta-1 only"
    return PortfolioResponse(live=live, note=note, underlyings=rows, broker_net=broker)
