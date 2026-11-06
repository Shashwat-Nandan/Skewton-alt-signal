"""
Tests for strategies/order_executor.py (audit 2026-06-10, task 1.2 step 2).

The executor is the ported pair_trading live path: marketable LIMIT →
poll-until-terminal → cancel / partial-reverse. These tests pin the parts
that protect real money:
- COMPLETE is reported only on a confirmed exact fill (H7: any partial is
  FAILED + reversed inline, so the broker and the strategy end flat).
- An order that never goes terminal is cancelled, not abandoned.
- The exception taxonomy (token-refresh-once / network-retry-once /
  broker-reject-no-retry) matches what the pair runner proved live on
  2026-06-11 — a blind retry on a broker reject can compound an
  invalid-order issue.
- The protective LIMIT price stays exchange-valid (tick-rounded) AND at
  least as aggressive as the pad — a price rounded the wrong way could
  rest in the book unfilled, which is exactly the 2026-05-21 incident.
"""

import time as time_module

from strategies import order_executor
from core.broker.errors import BrokerNetworkError, BrokerOrderError, BrokerTokenError
from strategies.order_executor import (
    ADOPT_MAX_AGE_S,
    LOST_SEND_CLEAR_S,
    OrderExecutor,
    _NetworkException,
    _OrderException,
    _TokenException,
)
from core.trade_proposer import TradeProposal


class FakeKite:
    VARIETY_REGULAR = "regular"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"
    PRODUCT_NRML = "NRML"
    ORDER_TYPE_LIMIT = "LIMIT"
    VALIDITY_DAY = "DAY"

    def __init__(self, ltp=100.0, history=None, place_raises=(),
                 quote_raises=False):
        self.ltp = ltp
        self.history = history if history is not None else [
            {"status": "COMPLETE", "filled_quantity": 50,
             "average_price": 100.0},
        ]
        self.placed = []
        self.cancelled = []
        self._place_raises = list(place_raises)
        self._quote_raises = quote_raises

    def quote(self, keys):
        if self._quote_raises:
            raise RuntimeError("quote down")
        return {keys[0]: {"last_price": self.ltp}}

    def place_order(self, **kwargs):
        if self._place_raises:
            exc = self._place_raises.pop(0)
            if exc is not None:
                raise exc
        self.placed.append(kwargs)
        return f"OID{len(self.placed)}"

    def order_history(self, order_id):
        return self.history

    def cancel_order(self, variety, order_id):
        self.cancelled.append(order_id)


def _prop(side="BUY", price=100.0, quantity=2, lot_size=25):
    return TradeProposal(
        tradingsymbol="NIFTY26403CE22000", instrument_token=1,
        strike=22000, expiry="2026-04-03", option_type="CE",
        lot_size=lot_size, quantity=quantity, price=price,
        transaction_type=side, iv=0.15, bid_ask_spread_pct=0.5,
        margin_required=15000,
    )


def _executor(kite, **kwargs):
    kwargs.setdefault("order_tag", "test-tag")
    kwargs.setdefault("poll_timeout_s", 0.05)
    kwargs.setdefault("poll_interval_s", 0.01)
    return OrderExecutor(kite, **kwargs)


class TestCompleteFill:
    def test_exact_fill_reports_actual_average_price(self):
        kite = FakeKite(history=[
            {"status": "COMPLETE", "filled_quantity": 50,
             "average_price": 100.45},
        ])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "COMPLETE"
        assert result["filled_lots"] == 2
        assert result["average_price"] == 100.45
        assert result["mode"] == "live"

    def test_order_is_a_marketable_limit_with_tag(self):
        kite = FakeKite(ltp=100.0)
        _executor(kite, order_tag="x" * 30).execute(_prop())
        (order,) = kite.placed
        assert order["order_type"] == "LIMIT"
        assert order["quantity"] == 50          # 2 lots × 25
        assert order["exchange"] == "NFO"
        assert order["validity"] == "DAY"
        # Broker 20-char cap; the readable tag leads, then a per-order
        # suffix (Kotak client order ids must be unique — 2026-10-09).
        assert len(order["tag"]) <= 20 and order["tag"].startswith("x" * 13)

    def test_callable_order_tag_receives_proposal(self):
        kite = FakeKite()
        _executor(kite, order_tag=lambda p: f"t-{p.transaction_type}",
                  ).execute(_prop("SELL"))
        assert kite.placed[0]["tag"].startswith("t-SELL-")


class TestProtectiveLimitPrice:
    # 0.25% pad on the aggressive side, rounded OUTWARD to tick so the
    # price never lands inside the pad (a too-passive price can rest
    # unfilled — the 2026-05-21 incident class).

    def test_buy_pads_up_and_ceils_to_tick(self):
        kite = FakeKite(ltp=1002.5)
        _executor(kite).execute(_prop("BUY"))
        # 1002.5 × 1.0025 = 1005.00625 → ceil to 0.05 → 1005.05
        assert kite.placed[0]["price"] == 1005.05

    def test_sell_pads_down_and_floors_to_tick(self):
        kite = FakeKite(ltp=1002.5)
        _executor(kite).execute(_prop("SELL"))
        # 1002.5 × 0.9975 = 999.99375 → floor to 0.05 → 999.95
        assert kite.placed[0]["price"] == 999.95

    def test_tick_size_comes_from_instruments_dump(self):
        kite = FakeKite(ltp=100.0)
        calls = []

        def get_instruments():
            calls.append(1)
            return [{"tradingsymbol": "NIFTY26403CE22000", "tick_size": 0.10}]

        ex = _executor(kite, get_instruments=get_instruments)
        ex.execute(_prop("BUY"))
        # 100 × 1.0025 = 100.25 → ceil to 0.10 → 100.3
        assert kite.placed[0]["price"] == 100.3
        ex.execute(_prop("BUY"))
        assert len(calls) == 1  # dump fetched once, tick cached per symbol

    def test_quote_failure_falls_back_to_proposal_price(self):
        kite = FakeKite(quote_raises=True)
        _executor(kite).execute(_prop("BUY", price=200.0))
        # 200 × 1.0025 = 200.5 — already on-tick
        assert kite.placed[0]["price"] == 200.5


class TestPartialFill:
    def test_partial_fill_is_failed_and_reversed_inline(self):
        # H7: 25 of 50 shares filled at COMPLETE → FAILED (caller books
        # nothing) + an opposite-side order for exactly the filled shares
        # so the broker ends flat too.
        kite = FakeKite(history=[
            {"status": "COMPLETE", "filled_quantity": 25,
             "average_price": 100.0},
        ])
        result = _executor(kite).execute(_prop("BUY"))
        assert result["status"] == "FAILED"
        assert "partial-fill 25/50" in result["error"]
        assert len(kite.placed) == 2
        reverse = kite.placed[1]
        assert reverse["transaction_type"] == "SELL"
        assert reverse["quantity"] == 25

    def test_zero_fill_complete_is_failed_without_reverse(self):
        # Contradictory, and how a missing fldQty reads through the Kotak
        # adapter: it may be a real fill, so it must not allow a resend.
        kite = FakeKite(history=[
            {"status": "COMPLETE", "filled_quantity": 0,
             "average_price": 0.0},
        ])
        ex = _executor(kite)
        assert ex.execute(_prop())["status"] == "UNKNOWN"
        assert ex.execute(_prop())["status"] == "UNKNOWN"
        assert len(kite.placed) == 1  # no reverse, and no second order


class TestNonTerminal:
    def test_timeout_cancels_open_order(self):
        kite = FakeKite(history=[
            {"status": "OPEN", "filled_quantity": 0, "average_price": 0.0},
        ])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "UNKNOWN"      # may yet fill: not "nothing filled"
        assert "non-terminal" in result["error"]
        assert kite.cancelled == ["OID1"]

    def test_rejected_is_failed_without_cancel(self):
        kite = FakeKite(history=[
            {"status": "REJECTED", "filled_quantity": 0, "average_price": 0.0},
        ])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "FAILED"
        assert kite.cancelled == []

    def test_reject_reason_surfaced_in_error(self):
        # 2026-07-13: a margin reject's reason lived only in kite.orders(),
        # never the runner logs. The broker's status_message must ride out on
        # the FAILED result's error so the caller's failure log carries it,
        # and a terminal reject must NOT be mislabeled "non-terminal after Ns".
        kite = FakeKite(history=[
            {"status": "REJECTED", "filled_quantity": 0, "average_price": 0.0,
             "status_message": "Insufficient funds. Margin required: 918886.29"},
        ])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "FAILED"
        assert "Insufficient funds" in result["error"]
        assert "REJECTED" in result["error"]
        assert "non-terminal" not in result["error"]
        assert kite.cancelled == []

    def test_reject_without_status_message_has_placeholder(self):
        # Reason absent (older/edge broker payloads) must degrade to a clear
        # placeholder, not an empty/misleading error.
        kite = FakeKite(history=[
            {"status": "REJECTED", "filled_quantity": 0, "average_price": 0.0},
        ])
        result = _executor(kite).execute(_prop())
        assert result["error"] == "REJECTED: (no status_message from broker)"


class TestPlaceExceptionTaxonomy:
    def test_order_exception_fails_without_retry(self):
        kite = FakeKite(place_raises=[_OrderException("margin")])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "FAILED"
        assert "rejected" in result["error"]
        assert kite.placed == []  # the one attempt raised; no blind retry

    def test_network_exception_retries_once_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("strategies.order_executor.time.sleep",
                            lambda s: None)
        kite = FakeKite(place_raises=[_NetworkException("blip"), None])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "COMPLETE"
        assert len(kite.placed) == 1

    def test_network_exception_twice_fails(self, monkeypatch):
        monkeypatch.setattr("strategies.order_executor.time.sleep",
                            lambda s: None)
        kite = FakeKite(place_raises=[_NetworkException("blip"),
                                      _NetworkException("blip")])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "FAILED"
        assert "net-retry" in result["error"]

    def test_token_exception_without_refresh_callback_fails(self):
        kite = FakeKite(place_raises=[_TokenException("expired")])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "FAILED"
        assert "token-expired" in result["error"]

    def test_token_exception_refreshes_and_retries_once(self):
        stale = FakeKite(place_raises=[_TokenException("expired")])
        fresh = FakeKite()
        ex = _executor(stale, broker_refresh=lambda: fresh)
        result = ex.execute(_prop())
        assert result["status"] == "COMPLETE"
        assert len(fresh.placed) == 1
        assert ex.client is fresh  # rebound for subsequent calls


class TestValidation:
    def test_invalid_proposal_never_reaches_the_broker(self):
        kite = FakeKite()
        result = _executor(kite).execute(_prop(price=-5.0))
        assert result["status"] == "FAILED"
        assert "validation" in result["error"]
        assert kite.placed == []


class _Scripted(FakeKite):
    """order_history answers from a script: a row list, or an Exception to
    raise. The last entry repeats. cancel_order can refuse, as Kotak does for
    an order that has already filled."""

    def __init__(self, reads, cancel_raises=None, place_raises=()):
        super().__init__(place_raises=place_raises)
        self.reads = list(reads)
        self.cancel_raises = cancel_raises

    def order_history(self, order_id):
        step = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(step, Exception):
            raise step
        return step

    def cancel_order(self, variety, order_id):
        self.cancelled.append(order_id)
        if self.cancel_raises:
            raise self.cancel_raises


_OPEN = [{"status": "PENDING", "filled_quantity": 0, "average_price": 0.0}]
_DONE = [{"status": "COMPLETE", "filled_quantity": 50, "average_price": 101.5}]
_GONE = [{"status": "CANCELLED", "filled_quantity": 0, "average_price": 0.0}]
_PART = [{"status": "CANCELLED", "filled_quantity": 25, "average_price": 100.0}]
_REVERSED = [{"status": "COMPLETE", "filled_quantity": 25, "average_price": 100.0}]
_TIMEOUT = BrokerNetworkError("read timed out")
_REFUSED = BrokerOrderError("order is completed")


class TestAFillIsNeverReportedAsFailed:
    """2026-11-06, BRITANNIA: an exit filled while its status read timed out.
    The cancel was refused with "order is completed", FAILED came back, the
    next tick sent the exit again, and the book was left 125 shares long on
    real money. FAILED must mean nothing filled."""

    def _ex(self, reads, **kw):
        # poll_timeout_s=0 skips the poll loop: the order is "still open at
        # timeout", the state today's status read timeout produced.
        kite = _Scripted(reads, cancel_raises=_REFUSED, **kw)
        return kite, _executor(kite, poll_timeout_s=0)

    def test_order_found_filled_after_the_refused_cancel_is_a_fill(self):
        kite, ex = self._ex([_DONE])
        result = ex.execute(_prop())
        assert result["status"] == "COMPLETE"
        assert result["filled_lots"] == 2 and result["average_price"] == 101.5
        assert len(kite.placed) == 1

    def test_an_unreadable_outcome_blocks_the_resend_and_adopts_the_fill(self):
        """Exactly the incident's retry: it must not reach the broker."""
        kite, ex = self._ex([_TIMEOUT, _TIMEOUT, _TIMEOUT, _DONE])
        first = ex.execute(_prop())
        assert first["status"] == "UNKNOWN"
        second = ex.execute(_prop())
        assert second["status"] == "COMPLETE" and second["order_id"] == "OID1"
        assert second["average_price"] == 101.5
        assert len(kite.placed) == 1
        assert ex.settle_unresolved() == []

    def test_still_unreadable_sends_nothing_and_keeps_cancelling(self):
        """One failed cancel must not leave a live limit that can fill later."""
        kite, ex = self._ex([_TIMEOUT])
        ex.execute(_prop())
        assert ex.execute(_prop())["status"] == "UNKNOWN"
        assert ex.settle_unresolved() == [_prop().tradingsymbol]
        assert len(kite.placed) == 1
        assert len(kite.cancelled) == 3

    def test_a_confirmed_cancel_is_failed_and_allows_the_retry(self):
        """The guard must not freeze exits: nothing filled, so resend."""
        kite, ex = self._ex([_GONE])
        assert ex.execute(_prop())["status"] == "FAILED"
        assert ex.execute(_prop())["status"] == "FAILED"
        assert len(kite.placed) == 2

    def test_unresolved_order_that_did_not_fill_allows_the_retry(self):
        kite, ex = self._ex([_TIMEOUT, _TIMEOUT, _TIMEOUT, _GONE, _DONE])
        assert ex.execute(_prop())["status"] == "UNKNOWN"
        assert ex.execute(_prop())["status"] == "COMPLETE"
        assert len(kite.placed) == 2

    def test_another_contract_is_not_blocked(self):
        kite, ex = self._ex([_TIMEOUT])
        ex.execute(_prop())
        other = _prop()
        other.tradingsymbol = "NIFTY26403PE22000"
        kite.reads = [_DONE]
        assert ex.execute(other)["status"] == "COMPLETE"

    def test_part_filled_then_cancelled_after_the_timeout_is_reversed(self):
        kite, ex = self._ex([_PART, _REVERSED])
        result = ex.execute(_prop("BUY"))
        assert result["status"] == "FAILED"
        assert [o["transaction_type"] for o in kite.placed] == ["BUY", "SELL"]
        assert kite.placed[1]["quantity"] == 25

    def test_part_filled_then_cancelled_inside_the_poll_window_is_reversed(self):
        """Otherwise FAILED, then the next tick resends the full order on top."""
        kite = _Scripted([_PART, _REVERSED])
        result = _executor(kite).execute(_prop("BUY"))
        assert result["status"] == "FAILED"
        assert [o["transaction_type"] for o in kite.placed] == ["BUY", "SELL"]
        assert kite.cancelled == []

    def test_a_fill_on_the_other_side_stays_blocked(self):
        """The other-side order would land on an untracked fill: keep blocking,
        tick after tick, until an operator has looked."""
        kite, ex = self._ex([_TIMEOUT, _TIMEOUT, _TIMEOUT, _DONE])
        ex.execute(_prop("BUY"))
        for _ in range(2):
            result = ex.execute(_prop("SELL"))
            assert result["status"] == "UNKNOWN" and "operator" in result["error"]
        assert len(kite.placed) == 1

    def test_a_stale_fill_is_not_adopted_by_a_later_decision(self, monkeypatch):
        """An hour later a same-side, same-size proposal is a new decision. An
        operator may have squared the old fill; adopting would book a leg the
        broker does not hold while the sibling leg really goes out."""
        kite, ex = self._ex([_TIMEOUT, _TIMEOUT, _TIMEOUT, _DONE])
        ex.execute(_prop())
        order_executor._UNRESOLVED[_prop().tradingsymbol]["since"] -= ADOPT_MAX_AGE_S + 1
        result = ex.execute(_prop())
        assert result["status"] == "UNKNOWN"
        assert len(kite.placed) == 1

    def test_a_different_size_is_not_adopted_and_nothing_is_reversed(self):
        """A 2-lot fill read when the hedge now wants 1 lot is still a good
        fill. It must not be sold as a "partial", or adopted as the wrong size."""
        kite, ex = self._ex([_TIMEOUT, _TIMEOUT, _TIMEOUT, _DONE])
        ex.execute(_prop(quantity=2))
        result = ex.execute(_prop(quantity=1))
        assert result["status"] == "UNKNOWN"
        assert len(kite.placed) == 1                  # no SELL went out

    def test_a_token_expiry_refreshes_rather_than_blocking_forever(self):
        fresh = _Scripted([_DONE])
        kite = _Scripted([BrokerTokenError("403")], cancel_raises=_REFUSED)
        ex = _executor(kite, poll_timeout_s=0, broker_refresh=lambda: fresh)
        result = ex.execute(_prop())
        assert result["status"] == "COMPLETE" and ex.client is fresh

    def test_a_malformed_row_is_a_failed_read_not_a_crash(self):
        bad = [{"status": "COMPLETE", "filled_quantity": None, "average_price": "x"}]
        kite, ex = self._ex([bad])
        assert ex.execute(_prop())["status"] == "UNKNOWN"
        assert ex.settle_unresolved() == [_prop().tradingsymbol]

    def test_a_lost_send_with_an_unreadable_book_is_unknown_not_a_resend(self):
        """Both sends time out and the lookup fails too. The next attempt would
        carry a new client order id, so the broker could not refuse a duplicate."""
        kite = _Scripted([_DONE], place_raises=[_TIMEOUT, _TIMEOUT])
        lookups = [_TIMEOUT, _TIMEOUT, "ORD9"]

        def finder(cid):
            step = lookups.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        kite.find_order_by_tag = finder
        ex = _executor(kite, poll_timeout_s=0)
        monkey_sleep = time_module.sleep
        time_module.sleep = lambda _s: None
        try:
            first = ex.execute(_prop())
            assert first["status"] == "UNKNOWN" and first["order_id"] is None
            second = ex.execute(_prop())
        finally:
            time_module.sleep = monkey_sleep
        assert second["status"] == "COMPLETE" and second["order_id"] == "ORD9"
        assert kite.placed == []

    def test_a_lost_send_the_book_says_never_arrived_can_retry(self):
        kite = _Scripted([_DONE], place_raises=[_TIMEOUT, _TIMEOUT])
        lookups = [_TIMEOUT, _TIMEOUT, None]

        def finder(cid):
            step = lookups.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        kite.find_order_by_tag = finder
        ex = _executor(kite, poll_timeout_s=0)
        monkey_sleep = time_module.sleep
        time_module.sleep = lambda _s: None
        try:
            assert ex.execute(_prop())["status"] == "UNKNOWN"
            order_executor._UNRESOLVED[_prop().tradingsymbol]["since"] -= LOST_SEND_CLEAR_S + 1
            assert ex.execute(_prop())["status"] == "COMPLETE"
        finally:
            time_module.sleep = monkey_sleep
        assert len(kite.placed) == 1

    def test_one_miss_right_after_a_lost_send_does_not_unblock(self):
        """The book may not show an in-flight order yet. A retry under a new
        client id would not be refused as a duplicate, and both would fill."""
        kite = _Scripted([_DONE], place_raises=[_TIMEOUT, _TIMEOUT])
        lookups = [_TIMEOUT, _TIMEOUT, None]

        def finder(cid):
            step = lookups.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        kite.find_order_by_tag = finder
        ex = _executor(kite, poll_timeout_s=0)
        monkey_sleep = time_module.sleep
        time_module.sleep = lambda _s: None
        try:
            ex.execute(_prop())
        finally:
            time_module.sleep = monkey_sleep
        assert ex.settle_unresolved() == [_prop().tradingsymbol]
        assert kite.placed == []

    def test_another_pair_on_the_contract_is_blocked_too(self):
        """The incident's second half: BRITANNIA/MARUTI opened +125 on the
        contract with the unread order. Its fill is not that pair's to adopt."""
        kite, first = self._ex([_TIMEOUT, _TIMEOUT, _TIMEOUT, _DONE])
        first.execute(_prop())
        other_kite = _Scripted([_DONE])
        other = _executor(other_kite, poll_timeout_s=0)
        result = other.execute(_prop())
        assert result["status"] == "UNKNOWN" and "another pair" in result["error"]
        assert other_kite.placed == []
        assert other.settle_unresolved() == [_prop().tradingsymbol]
        assert first.execute(_prop())["status"] == "COMPLETE"     # the owner adopts

    def test_complete_with_nothing_filled_is_not_taken_as_no_fill(self):
        bad = [{"status": "COMPLETE", "filled_quantity": 0, "average_price": 0.0}]
        kite, ex = self._ex([_TIMEOUT, _TIMEOUT, _TIMEOUT, bad, bad])
        ex.execute(_prop())
        assert ex.settle_unresolved() == [_prop().tradingsymbol]

    def test_an_unconfirmed_reversal_blocks_instead_of_allowing_a_full_resend(self):
        """FAILED would resend the full size on a book already holding the part."""
        kite, ex = self._ex([_PART])                 # the reversal never reads as filled
        result = ex.execute(_prop("BUY"))
        assert result["status"] == "UNKNOWN" and "not confirmed" in result["error"]
        for _ in range(2):
            assert ex.execute(_prop("BUY"))["status"] == "UNKNOWN"
        assert len(kite.placed) == 2                 # the order and its reversal only

    def test_a_reversal_that_cannot_be_sent_blocks_too(self):
        kite, ex = self._ex([_PART])
        real_place = kite.place_order
        calls = []

        def place(**kw):
            calls.append(kw)
            if len(calls) == 2:
                raise _TIMEOUT
            return real_place(**kw)

        kite.place_order = place
        assert ex.execute(_prop("BUY"))["status"] == "UNKNOWN"
        assert ex.execute(_prop("BUY"))["status"] == "UNKNOWN"
        assert len(calls) == 2

    def test_a_lookup_that_recovers_says_the_send_never_arrived(self):
        """A failed lookup before the resend must not outlive a good one after."""
        kite = _Scripted([_DONE], place_raises=[_TIMEOUT, _TIMEOUT])
        lookups = [_TIMEOUT, None]

        def finder(cid):
            step = lookups.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        kite.find_order_by_tag = finder
        monkey_sleep = time_module.sleep
        time_module.sleep = lambda _s: None
        try:
            result = _executor(kite, poll_timeout_s=0).execute(_prop())
        finally:
            time_module.sleep = monkey_sleep
        assert result["status"] == "FAILED"
