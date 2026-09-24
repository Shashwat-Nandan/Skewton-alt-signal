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

from strategies.order_executor import (
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
        assert order["tag"] == "x" * 20         # kite's 20-char cap

    def test_callable_order_tag_receives_proposal(self):
        kite = FakeKite()
        _executor(kite, order_tag=lambda p: f"t-{p.transaction_type}",
                  ).execute(_prop("SELL"))
        assert kite.placed[0]["tag"] == "t-SELL"


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
        kite = FakeKite(history=[
            {"status": "COMPLETE", "filled_quantity": 0,
             "average_price": 0.0},
        ])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "FAILED"
        assert len(kite.placed) == 1  # no reverse for nothing filled


class TestNonTerminal:
    def test_timeout_cancels_open_order(self):
        kite = FakeKite(history=[
            {"status": "OPEN", "filled_quantity": 0, "average_price": 0.0},
        ])
        result = _executor(kite).execute(_prop())
        assert result["status"] == "FAILED"
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
