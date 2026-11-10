#!/usr/bin/env python3
"""
novel_downloader.plugins.utils.rate_limiter
-------------------------------------------

An asyncio-compatible token bucket rate limiter.
"""

import asyncio
import random
import time


class TokenBucketRateLimiter:
    def __init__(
        self,
        rate: float,
        burst: int = 10,
        jitter_strength: float = 0.3,
    ):
        """
        A simple asyncio-compatible token bucket rate limiter.

        :param rate: Tokens added per second.
        :param burst: Maximum bucket size (burst capacity).
        :param jitter_strength: Jitter range in seconds (+/-).
        """
        self.rate = rate
        self.capacity = burst
        self.tokens = float(burst)
        self.timestamp = time.monotonic()
        self.lock = asyncio.Lock()
        self.jitter_strength = jitter_strength

    async def wait(self) -> None:
        """
        Block until this caller owns a slot.

        The slot is *reserved* under the lock: the bucket may go negative and
        each caller sleeps for its own deficit (``-tokens / rate``), so the
        callers wake one interval apart.

        The previous implementation slept outside the lock and only took a
        token afterwards, so every concurrent waiter computed the same delay
        and fired at the same instant — ``max_rps`` was silently ignored as
        soon as ``workers`` > 1.

        A caller cancelled while sleeping returns its reservation, so a
        cancelled request does not keep costing later callers a slot.
        """
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.timestamp
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.timestamp = now

            self.tokens -= 1.0
            if self.tokens >= 0:
                return

            wait_time = -self.tokens / self.rate
            jitter = random.uniform(-self.jitter_strength, self.jitter_strength)
            total_wait = max(0.0, wait_time + jitter)

        try:
            await asyncio.sleep(total_wait)
        except asyncio.CancelledError:
            # The slot was reserved above, so a caller cancelled while
            # sleeping would keep charging the bucket for a request that is
            # never sent (every later caller waits one extra interval, and
            # repeated cancellations throttle the whole session).  Give the
            # reservation back; the bucket may legitimately hold other
            # callers' reservations, hence the ``min`` rather than a reset.
            async with self.lock:
                self.tokens = min(self.capacity, self.tokens + 1.0)
            raise
