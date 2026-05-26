"""Token-bucket throttler for the Kite Connect SDK.

Kite's published per-key ceiling is 10 req/s; sustained excess gets 429s
that downstream code interprets as "quote failed" → no z-score → no
trade. H14 from the live-readiness audit: with 12 pairs clustered at
second-0 of each minute issuing 2 quote calls each plus per-symbol
instrument lookups, a real session crosses the ceiling routinely.

The throttler wraps named methods on a KiteConnect instance with a
shared token bucket. Default is 8 req/s with a burst of 8 — leaves
headroom for the dashboard process and retry storms. Calls block on the
bucket rather than raising; out-of-band 429s only show up when the
ceiling itself moves (broker-side incident, not our problem).

Threading model: the bucket uses a single Lock around the token-count
update; the sleep happens OUTSIDE the lock so other threads can drain
the bucket while one waits. Recursive acquire keeps the path simple at
the cost of one redundant lock acquisition per wait — fine at 8 r/s.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

logger = logging.getLogger(__name__)

# Methods worth throttling. Restricted to the ones we actually use across
# strategies + the dashboard backend + the runners — `unsubscribe`,
# `ticker`, etc. don't go through the REST surface and shouldn't be in
# the bucket. Update this list if a new call site appears in a hot path.
DEFAULT_THROTTLED_METHODS = (
    "quote",
    "ltp",
    "ohlc",
    "historical_data",
    "instruments",
    "place_order",
    "modify_order",
    "cancel_order",
    "orders",
    "order_history",
    "trades",
    "positions",
    "holdings",
    "margins",
    "profile",
)

# Default budget. Kite ceiling is 10 r/s; we settle 2 r/s below to keep
# headroom for retries (the place_order path can retry once on transient
# failures). Burst = rate so the very first batch of N≤8 calls doesn't
# block at all — only sustained pressure does.
DEFAULT_RATE_PER_SEC = 8.0
DEFAULT_BURST = 8
# Slow-call alert threshold. Sleeps shorter than this are too noisy to
# log — they happen at the start of every minute when 12 strategies
# fire quote calls back-to-back. Above this means the bucket was empty
# for a non-trivial stretch — useful operational signal.
SLOW_WAIT_LOG_THRESHOLD_S = 0.5


class KiteRateLimiter:
    """Thread-safe token bucket. acquire() blocks until a token is free."""

    def __init__(
        self, rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        burst: int = DEFAULT_BURST,
    ):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._rate = float(rate_per_sec)
        self._burst = int(burst)
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a token is available; return the time waited (s).
        Refills are lazy — we add tokens proportional to elapsed wall
        time on each call rather than running a background thread."""
        total_wait = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._last_refill = now
                self._tokens = min(
                    self._burst, self._tokens + elapsed * self._rate
                )
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    if total_wait >= SLOW_WAIT_LOG_THRESHOLD_S:
                        logger.info(
                            "kite throttle: waited %.2fs for a token "
                            "(rate=%.1f/s, burst=%d)",
                            total_wait, self._rate, self._burst,
                        )
                    return total_wait
                # Compute how long until the next whole token, release
                # the lock, then sleep — letting other threads keep the
                # bucket honest while we wait.
                wait = (1.0 - self._tokens) / self._rate
            time.sleep(wait)
            total_wait += wait


def throttle_kite(
    kite, limiter: KiteRateLimiter,
    methods: Iterable[str] = DEFAULT_THROTTLED_METHODS,
):
    """Monkey-patch each named method on `kite` so it awaits a token
    before delegating to the original. Idempotent: repeated application
    sees the `__throttled__` flag and skips.

    Returns `kite` for chaining. Methods that don't exist on the
    instance are silently skipped (KiteConnect surface evolves; we don't
    want to crash a runner because a method was renamed)."""
    for name in methods:
        original = getattr(kite, name, None)
        if not callable(original):
            continue
        if getattr(original, "__throttled__", False):
            continue
        wrapped = _make_throttled(original, limiter)
        setattr(kite, name, wrapped)
    return kite


def _make_throttled(original, limiter: KiteRateLimiter):
    def wrapper(*args, **kwargs):
        limiter.acquire()
        return original(*args, **kwargs)
    wrapper.__wrapped__ = original  # type: ignore[attr-defined]
    wrapper.__throttled__ = True   # type: ignore[attr-defined]
    wrapper.__name__ = getattr(original, "__name__", "throttled")
    return wrapper
