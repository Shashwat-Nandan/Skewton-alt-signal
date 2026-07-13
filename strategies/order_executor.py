"""
Shared live-order executor (audit 2026-06-10, task 1.2 step 2)
==============================================================
Marketable-LIMIT place → poll-until-terminal → cancel / partial-reverse,
ported from pair_trading's live path (b1a1725 + H7/H8/M-B4 lineage) so the
taleb and arbitrage strategies can trade live with the same fill semantics
the pair runner proved with real money on 2026-06-11.

pair_trading.py still runs its OWN copy of this logic — migrating it onto
this module is audit task 2.2 (it is the live-money path today and stays
byte-identical until a paper soak validates the swap). Until then, a
behavior fix found in either copy must be applied to both.

Deliberately NOT ported: M-B5 consecutive-failure backoff. That is
pair-strategy state (persisted per pair, decremented per tick by its
execute_proposals); a host strategy that wants it must implement its own
tick cadence around execute(). Revisit when 2.2 unifies the call sites.

Contract: execute(prop) returns the same result dict shape as
pair_trading._live_execute —
    {"order_id", "status", "filled_lots", "average_price", "mode": "live",
     ["error"]}
and "COMPLETE" is the only status on which a caller may mutate state
(C-1 whitelist). Anything else means the order did NOT settle: the
executor has already cancelled / reversed whatever reached the broker, or
logged CRITICAL telling the operator to square off manually.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable, Dict, List, Optional

from trade_proposer import TradeProposal

from .base import OrderValidationError, validate_order

try:  # kiteconnect is the live broker; tests run without it installed
    from kiteconnect.exceptions import (
        TokenException as _TokenException,
        NetworkException as _NetworkException,
        OrderException as _OrderException,
    )
except Exception:  # pragma: no cover
    class _TokenException(Exception):  # type: ignore[no-redef]
        pass

    class _NetworkException(Exception):  # type: ignore[no-redef]
        pass

    class _OrderException(Exception):  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)


class KiteOrderExecutor:
    """One instance per strategy; holds no position state, only the
    instruments-dump tick-size cache.

    order_tag: str, or callable(prop) -> str for per-proposal tags
        (kite truncates at 20 chars; we truncate defensively too).
    get_instruments: callable returning kite.instruments("NFO") rows, used
        for tick-size lookup. Called at most once per executor lifetime
        (the dump is static intraday); failures fall back to the 0.05 NSE
        F&O default. Stock-futures ticks can differ post the 2024 NSE
        cash-alignment circular, hence the lookup matters for arbitrage.
    kite_refresh: callable() -> fresh kite client (H8). On TokenException
        the executor rebinds self.kite from it and retries the failed call
        exactly once; without it a TokenException fails the order.
    """

    def __init__(self, kite, *, order_tag,
                 limit_protection_pct: float = 0.25,
                 exchange: str = "NFO",
                 get_instruments: Optional[Callable[[], List[dict]]] = None,
                 kite_refresh: Optional[Callable[[], object]] = None,
                 poll_timeout_s: float = 10.0,
                 poll_interval_s: float = 1.0):
        self.kite = kite
        self._order_tag = order_tag
        self.limit_protection_pct = float(limit_protection_pct)
        self.exchange = exchange
        self._get_instruments = get_instruments
        self._kite_refresh = kite_refresh
        self.poll_timeout_s = poll_timeout_s
        self.poll_interval_s = poll_interval_s
        self._tick_cache: Dict[str, float] = {}
        self._instruments_rows: Optional[List[dict]] = None

    # ── helpers ─────────────────────────────────────────────────

    def _tag_for(self, prop: TradeProposal) -> str:
        tag = self._order_tag(prop) if callable(self._order_tag) else self._order_tag
        return str(tag)[:20]

    def _try_refresh_kite(self, op: str, ctx: str, err: Exception) -> bool:
        # H8: refresh the kite client via the host-supplied callback.
        if self._kite_refresh is None:
            logger.error(
                "TokenException during %s (%s) but no kite_refresh callback "
                "is configured — cannot recover: %s", op, ctx, err,
            )
            return False
        try:
            self.kite = self._kite_refresh()
            logger.warning(
                "Token refreshed mid-session after %s on %s: %s", op, ctx, err,
            )
            return True
        except Exception as e:
            logger.critical(
                "kite_refresh callback failed during %s (%s): original=%s "
                "refresh_err=%s", op, ctx, err, e,
            )
            return False

    def _get_last_price(self, tradingsymbol: str) -> Optional[float]:
        key = f"{self.exchange}:{tradingsymbol}"
        try:
            quote = self.kite.quote([key])
            return float(quote[key]["last_price"])
        except _TokenException as e:
            if not self._try_refresh_kite("quote", tradingsymbol, e):
                return None
            try:
                quote = self.kite.quote([key])
                return float(quote[key]["last_price"])
            except Exception as e2:
                logger.critical(
                    "quote failed for %s even after token refresh: %s",
                    tradingsymbol, e2,
                )
                return None
        except Exception as e:
            logger.warning("quote failed for %s: %s", tradingsymbol, e)
            return None

    def _tick_size_for(self, tradingsymbol: str) -> float:
        # Tick size from the session NFO dump; 0.05 (the NSE F&O default)
        # when the dump is unavailable or the symbol is missing.
        cached = self._tick_cache.get(tradingsymbol)
        if cached is not None:
            return cached
        tick = 0.05
        try:
            if self._instruments_rows is None and self._get_instruments is not None:
                self._instruments_rows = self._get_instruments() or []
            for row in self._instruments_rows or []:
                if row.get("tradingsymbol") == tradingsymbol:
                    found = float(row.get("tick_size") or 0)
                    if found > 0:
                        tick = found
                    break
        except Exception as e:
            logger.warning("tick_size lookup failed for %s: %s", tradingsymbol, e)
        self._tick_cache[tradingsymbol] = tick
        return tick

    def _protective_limit_price(self, tradingsymbol: str,
                                 transaction_type: str,
                                 fallback_price: float) -> float:
        """Marketable-LIMIT price: fresh LTP padded limit_protection_pct
        toward the aggressive side (BUY above, SELL below), rounded outward
        to tick size so the price stays exchange-valid AND at least as
        aggressive as the pad. Falls back to the proposal's quote price if
        the fresh quote fails — that quote is from the same tick, seconds
        old at worst."""
        base = self._get_last_price(tradingsymbol)
        if base is None or base <= 0:
            base = float(fallback_price)
        tick = self._tick_size_for(tradingsymbol)
        pad = base * self.limit_protection_pct / 100.0
        # round(.., 9) before ceil/floor: float division wobble (1002.5/0.05
        # = 20049.999...) must not push the price a spurious tick outward.
        if transaction_type == "BUY":
            price = math.ceil(round((base + pad) / tick, 9)) * tick
        else:
            price = math.floor(round((base - pad) / tick, 9)) * tick
        return round(max(price, tick), 2)

    # ── place → poll → settle ───────────────────────────────────

    def execute(self, prop: TradeProposal) -> Dict:
        # Marketable LIMIT with protection: Zerodha's API rejects naked
        # MARKET orders on F&O ("Market orders without market protection
        # are not allowed via API"), so we send a LIMIT priced LTP ±
        # limit_protection_pct on the aggressive side — it crosses the
        # book and fills immediately like a market order, with slippage
        # bounded at the pad. State is booked only on a confirmed
        # COMPLETE: _poll_until_terminal cancels anything still open at
        # timeout and reports FAILED so the caller's batch-atomicity
        # logic can reverse a filled sibling leg.
        try:
            validate_order(prop)
        except OrderValidationError as e:
            logger.error("Order rejected pre-submit: %s — %s", e, prop)
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": f"validation: {e}", "mode": "live"}
        limit_price = self._protective_limit_price(
            prop.tradingsymbol, prop.transaction_type, prop.price,
        )

        def _do_place():
            return self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR, exchange=self.exchange,
                tradingsymbol=prop.tradingsymbol,
                transaction_type=(
                    self.kite.TRANSACTION_TYPE_BUY if prop.transaction_type == "BUY"
                    else self.kite.TRANSACTION_TYPE_SELL
                ),
                quantity=abs(prop.quantity) * prop.lot_size,
                product=self.kite.PRODUCT_NRML,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=limit_price,
                validity=self.kite.VALIDITY_DAY,
                tag=self._tag_for(prop),
            )

        try:
            order_id = _do_place()
        except _TokenException as e:
            # H8: token expired mid-session. Refresh once and retry the
            # place_order call exactly once. A second failure is CRITICAL
            # and the order is reported FAILED — the caller's reversal
            # logic handles any already-filled sibling leg.
            if not self._try_refresh_kite("place_order", prop.tradingsymbol, e):
                return {"order_id": None, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"place_order: token-expired ({e})",
                        "mode": "live"}
            try:
                order_id = _do_place()
            except Exception as e2:
                logger.critical(
                    "place_order failed after token refresh for %s: %s",
                    prop.tradingsymbol, e2,
                )
                return {"order_id": None, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"place_order post-refresh: {e2}",
                        "mode": "live"}
        except _NetworkException as e:
            # M-B4: transient kite/network blip. Retry once with a brief
            # delay; if the second attempt also fails, give up for this
            # proposal (caller reverses any already-filled sibling).
            logger.warning("place_order NetworkException for %s: %s — retrying once",
                           prop.tradingsymbol, e)
            time.sleep(1.0)
            try:
                order_id = _do_place()
            except Exception as e2:
                logger.error(
                    "place_order NetworkException retry failed for %s: %s",
                    prop.tradingsymbol, e2,
                )
                return {"order_id": None, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"place_order net-retry: {e2}",
                        "mode": "live"}
        except _OrderException as e:
            # M-B4: broker-side reject (margin, validation, exchange
            # error). Do not retry — the underlying cause is unlikely to
            # clear within seconds and a blind retry can compound an
            # invalid-order issue. Log and return FAILED.
            logger.error("place_order OrderException for %s: %s",
                         prop.tradingsymbol, e)
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": f"place_order rejected: {e}", "mode": "live"}
        except Exception as e:
            logger.exception("place_order failed for %s: %s",
                             prop.tradingsymbol, e)
            return {"order_id": None, "status": "FAILED",
                    "filled_lots": 0, "average_price": 0.0,
                    "error": f"place_order: {e}", "mode": "live"}

        return self._poll_until_terminal(order_id, prop)

    def _poll_until_terminal(self, order_id, prop: TradeProposal) -> Dict:
        # Poll order_history until terminal (COMPLETE / REJECTED /
        # CANCELLED) or timeout. On COMPLETE we report the actual fill
        # (filled_quantity / average_price); on anything else we cancel
        # best-effort and return FAILED so the caller skips its fill
        # booking and (if entry batch) triggers a reversal sweep.
        deadline = time.monotonic() + self.poll_timeout_s
        final_status = "PENDING"
        filled_qty = 0
        avg_price = 0.0
        status_message = ""
        while time.monotonic() < deadline:
            try:
                history = self.kite.order_history(order_id)
                latest = history[-1] if history else {}
                final_status = latest.get("status", "PENDING")
                filled_qty = int(latest.get("filled_quantity", 0))
                avg_price = float(latest.get("average_price") or 0.0)
                # Zerodha's human-readable reject/cancel reason (e.g.
                # "Insufficient funds. Margin required: ..."). Surfaced in the
                # failure path below so the reason lands in the runner logs
                # instead of requiring a manual kite.orders() lookup.
                status_message = (latest.get("status_message")
                                  or latest.get("status_message_raw") or "")
                if final_status in ("COMPLETE", "REJECTED", "CANCELLED"):
                    break
            except Exception as e:
                logger.warning("order_history poll failed for %s: %s",
                               order_id, e)
            time.sleep(self.poll_interval_s)

        requested_shares = abs(prop.quantity) * prop.lot_size
        if final_status == "COMPLETE":
            # H7: refuse ANY partial fill (filled_qty != requested_shares),
            # not just sub-lot ones. A lot-boundary partial would otherwise
            # silently book a half-size leg, breaking the structure's hedge
            # ratios.
            #
            # Returning FAILED keeps the leg out of the caller's state,
            # which means batch-reversal logic (which only reverses
            # COMPLETE siblings) will NOT touch the broker-side partial.
            # We therefore reverse the partial inline — a same-symbol
            # opposite-side order for filled_qty shares — so the batch
            # ends flat on both the strategy and the broker. If the
            # inline reversal fails, log CRITICAL: an operator MUST square
            # this manually before the next session.
            if filled_qty != requested_shares:
                logger.error(
                    "Partial fill not handled: order %s filled %d of %d "
                    "shares (lot %d) — treating as FAILED",
                    order_id, filled_qty, requested_shares, prop.lot_size,
                )
                if filled_qty > 0:
                    self._emergency_reverse_partial(prop, filled_qty, order_id)
                return {"order_id": order_id, "status": "FAILED",
                        "filled_lots": 0, "average_price": 0.0,
                        "error": f"partial-fill {filled_qty}/{requested_shares}",
                        "mode": "live"}
            filled_lots = filled_qty // prop.lot_size
            return {"order_id": order_id, "status": "COMPLETE",
                    "filled_lots": filled_lots, "average_price": avg_price,
                    "mode": "live"}

        reason = status_message or "(no status_message from broker)"
        if final_status in ("REJECTED", "CANCELLED"):
            # Terminal broker rejection/cancel — the order settled fast, so
            # don't mislabel it "non-terminal after Ns". Log and propagate the
            # exchange reason so the caller's failure log carries it too.
            logger.warning(
                "Order %s %s by broker: %s",
                order_id, final_status, reason,
            )
            error = f"{final_status}: {reason}"
        else:
            # Still open at timeout: best-effort cancel so it doesn't fill late.
            try:
                self.kite.cancel_order(
                    variety=self.kite.VARIETY_REGULAR, order_id=order_id,
                )
                logger.warning(
                    "Order %s cancelled after %.1fs (last status=%s)",
                    order_id, self.poll_timeout_s, final_status,
                )
            except Exception as e:
                logger.warning("cancel_order failed for %s: %s",
                               order_id, e)
            error = (f"non-terminal after {self.poll_timeout_s}s: "
                     f"status={final_status}")
        return {"order_id": order_id, "status": "FAILED",
                "filled_lots": 0, "average_price": 0.0,
                "error": error, "mode": "live"}

    def _emergency_reverse_partial(self, prop: TradeProposal,
                                    filled_shares: int,
                                    original_order_id: str) -> None:
        # H7 follow-up: place an opposite-side order for the partial
        # quantity sitting on the broker after we treated the original
        # order as FAILED. Marketable LIMIT, same as execute() — the API
        # rejects naked MARKET orders. Best-effort: if this raises, we
        # cannot recover automatically — log CRITICAL so notify-failure@
        # alerts surface the orphan and the operator squares it manually
        # before reopen.
        reverse_type = "SELL" if prop.transaction_type == "BUY" else "BUY"
        reverse_side = (
            self.kite.TRANSACTION_TYPE_SELL if reverse_type == "SELL"
            else self.kite.TRANSACTION_TYPE_BUY
        )
        try:
            reverse_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR, exchange=self.exchange,
                tradingsymbol=prop.tradingsymbol,
                transaction_type=reverse_side,
                quantity=filled_shares,
                product=self.kite.PRODUCT_NRML,
                order_type=self.kite.ORDER_TYPE_LIMIT,
                price=self._protective_limit_price(
                    prop.tradingsymbol, reverse_type, prop.price,
                ),
                validity=self.kite.VALIDITY_DAY,
                tag=self._tag_for(prop),
            )
            logger.warning(
                "H7 partial-fill recovery: placed reversing %s order %s for "
                "%d shares of %s (original order %s)",
                reverse_side, reverse_id, filled_shares, prop.tradingsymbol,
                original_order_id,
            )
        except Exception as e:
            logger.critical(
                "H7 PARTIAL ORPHAN: failed to reverse %d shares of %s after "
                "partial fill on order %s. MANUAL SQUARE-OFF REQUIRED before "
                "next session. err=%s",
                filled_shares, prop.tradingsymbol, original_order_id, e,
            )
