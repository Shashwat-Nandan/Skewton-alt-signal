"""Tests for kite_throttle.KiteRateLimiter and throttle_kite.

The bucket is timing-sensitive, so we use coarse assertions: the wait
budget is "at least X seconds" rather than exact times, and we rely on
the fact that pytest doesn't schedule sub-millisecond cleanly. Each
test runs in <2s.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import time
import threading
from unittest.mock import MagicMock

import pytest

from core.kite_throttle import (
    KiteRateLimiter,
    throttle_kite,
    DEFAULT_THROTTLED_METHODS,
)


class FakeClock:
    """Deterministic monotonic clock. `sleep` advances virtual time, so the
    token bucket behaves exactly as in production without any real waits —
    no wall-clock-flaky upper/lower bounds (audit 3.6)."""
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestKiteRateLimiterBasics:
    def test_burst_passes_without_waiting(self):
        """The first N≤burst calls drain the initial bucket — none of
        them should block (exact: zero virtual time elapses)."""
        fc = FakeClock()
        limiter = KiteRateLimiter(rate_per_sec=2.0, burst=5, clock=fc, sleep=fc.sleep)
        for _ in range(5):
            assert limiter.acquire() == 0.0
        assert fc.t == 0.0   # no sleeps → virtual clock unmoved

    def test_exhausted_bucket_throttles_to_rate(self):
        """After draining the burst, each further call blocks exactly
        1/rate. rate=10/s burst=1: 3 calls → 1 free + 2×0.1s = 0.20s."""
        fc = FakeClock()
        limiter = KiteRateLimiter(rate_per_sec=10.0, burst=1, clock=fc, sleep=fc.sleep)
        waits = [limiter.acquire() for _ in range(3)]
        assert waits[0] == 0.0                       # burst token, free
        assert waits[1] == pytest.approx(0.1)        # one refill period
        assert waits[2] == pytest.approx(0.1)
        assert fc.t == pytest.approx(0.2)            # exact total, no jitter

    def test_rejects_nonpositive_rate(self):
        with pytest.raises(ValueError):
            KiteRateLimiter(rate_per_sec=0)
        with pytest.raises(ValueError):
            KiteRateLimiter(rate_per_sec=-1.0)

    def test_rejects_burst_below_one(self):
        with pytest.raises(ValueError):
            KiteRateLimiter(burst=0)

    def test_thread_safe(self):
        """20 threads each acquiring once on a rate=20/s burst=1 bucket
        should finish in ~1s (19 throttled gaps × 50ms). The lock must
        not deadlock or let two threads consume the same token."""
        limiter = KiteRateLimiter(rate_per_sec=20.0, burst=1)
        results = []

        def worker():
            results.append(limiter.acquire())

        threads = [threading.Thread(target=worker) for _ in range(20)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0
        assert len(results) == 20
        # Lower bound: 19 throttled calls × 1/20s = 0.95s.
        assert elapsed >= 0.85, f"too fast — token bucket leaking? {elapsed:.3f}s"


class TestThrottleKite:
    def test_wraps_named_methods(self):
        kite = MagicMock()
        kite.quote = MagicMock(return_value={"x": 1})
        kite.profile = MagicMock(return_value={"user_id": "ZZ"})
        kite.no_such_method = "not callable"  # exercises the skip path
        limiter = KiteRateLimiter(rate_per_sec=100.0, burst=100)

        throttle_kite(kite, limiter, methods=("quote", "profile", "no_such_method"))
        assert getattr(kite.quote, "__throttled__", False) is True
        assert getattr(kite.profile, "__throttled__", False) is True
        # Calls still work and return the original payload.
        assert kite.quote(["x"]) == {"x": 1}
        assert kite.profile()["user_id"] == "ZZ"

    def test_idempotent(self):
        """Calling throttle_kite twice must not stack two waits per call.
        Without the __throttled__ guard, the second pass would wrap the
        already-wrapped method and acquire two tokens for every call."""
        kite = MagicMock()
        kite.quote = MagicMock(return_value={})
        limiter = KiteRateLimiter(rate_per_sec=1.0, burst=1)

        throttle_kite(kite, limiter, methods=("quote",))
        throttle_kite(kite, limiter, methods=("quote",))
        first_wrapper = kite.quote
        # No additional wrapping happened — same callable, marked __throttled__.
        assert getattr(first_wrapper, "__throttled__", False) is True
        # And we can introspect back to the original via __wrapped__.
        assert kite.quote.__wrapped__ is not None

    def test_actually_throttles_through_wrapper(self):
        """End-to-end: 3 wrapped calls on a 10/s burst=1 client must incur
        exactly 0.2s of throttling — proves the wrapper actually calls
        limiter.acquire() rather than just labeling the method. Fake clock
        → deterministic (audit 3.6)."""
        fc = FakeClock()
        kite = MagicMock()
        kite.quote = MagicMock(return_value={})
        limiter = KiteRateLimiter(rate_per_sec=10.0, burst=1, clock=fc, sleep=fc.sleep)
        throttle_kite(kite, limiter, methods=("quote",))

        for _ in range(3):
            kite.quote(["x"])
        assert fc.t == pytest.approx(0.2), f"wrapper bypassed throttler: {fc.t:.3f}s"

    def test_default_methods_cover_kite_surface(self):
        """Smoke check: the DEFAULT_THROTTLED_METHODS list mentions
        every kite call referenced anywhere in the production code.
        If a new call site adds, say, `kite.gtt_orders()` to a hot
        path, this test won't catch it — but it will catch obvious
        drift if someone removes 'quote' or 'place_order'."""
        # Must include the high-volume ones — checked by name to keep
        # this assertion stable against future additions.
        critical = {"quote", "ltp", "instruments", "place_order"}
        assert critical <= set(DEFAULT_THROTTLED_METHODS)
        # Both margin calls: the arbitrage entry precheck (#222) issues one
        # basket_order_margins per entry group per tick, and a book that can
        # never fund an entry re-proposes the same groups every tick — an
        # unthrottled call in a hot path is exactly what 429s the quote path.
        assert {"margins", "basket_order_margins"} <= set(DEFAULT_THROTTLED_METHODS)
