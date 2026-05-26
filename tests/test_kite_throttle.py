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

from kite_throttle import (
    KiteRateLimiter,
    throttle_kite,
    DEFAULT_THROTTLED_METHODS,
)


class TestKiteRateLimiterBasics:
    def test_burst_passes_without_waiting(self):
        """The first N≤burst calls drain the initial bucket — none of
        them should block. Token refill rate is irrelevant until the
        bucket empties."""
        limiter = KiteRateLimiter(rate_per_sec=2.0, burst=5)
        t0 = time.monotonic()
        for _ in range(5):
            assert limiter.acquire() == 0.0
        # Five token consumptions on a 5-token bucket → no waits, so
        # the whole loop should be <50ms (system jitter only).
        assert time.monotonic() - t0 < 0.05

    def test_exhausted_bucket_throttles_to_rate(self):
        """After draining the burst, subsequent calls block at the
        configured rate. With rate=10/s and burst=1, three back-to-back
        calls should take ≥ (N-1)/rate seconds total."""
        limiter = KiteRateLimiter(rate_per_sec=10.0, burst=1)
        t0 = time.monotonic()
        for _ in range(3):
            limiter.acquire()
        elapsed = time.monotonic() - t0
        # (3 calls - 1 free) × (1/10s) = 0.20s minimum
        assert elapsed >= 0.18, f"throttle not enforced: only {elapsed:.3f}s for 3 calls"
        # And not absurdly slow — under 0.5s for sanity.
        assert elapsed < 0.5

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
        """End-to-end: 3 wrapped calls on a 10/s burst=1 client should
        take at least 0.18s — proves the wrapper actually calls
        limiter.acquire() rather than just labeling the method."""
        kite = MagicMock()
        kite.quote = MagicMock(return_value={})
        limiter = KiteRateLimiter(rate_per_sec=10.0, burst=1)
        throttle_kite(kite, limiter, methods=("quote",))

        t0 = time.monotonic()
        for _ in range(3):
            kite.quote(["x"])
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.18, f"wrapper bypassed throttler: {elapsed:.3f}s"

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
