#!/usr/bin/env python3
"""
novel_downloader.plugins.utils.rate_limiter
-------------------------------------------

An asyncio-compatible token bucket rate limiter.
"""

import asyncio
import random
import threading
import time

#: A server may ask for an hour-long pause; a shared cooldown that long would
#: stall the whole worker pool.  The fetcher already bounds ``Retry-After`` at
#: 30 s, so this only matters for direct callers.
MAX_COOLDOWN = 60.0

#: Upward-only jitter added to a cooldown so workers that all received the
#: same ``Retry-After`` do not resume in lockstep and earn another 429.
_COOLDOWN_JITTER_MAX = 1.0
_COOLDOWN_JITTER_FRACTION = 0.1


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
        # A plain lock, not ``asyncio.Lock``: every critical section below is
        # synchronous (no ``await``), so it also protects the state when
        # ``penalize`` is called from another thread (e.g. an executor).
        self._lock = threading.Lock()
        self.jitter_strength = jitter_strength
        #: Monotonic deadline until which every ``wait()`` caller pauses.
        self.cooldown_until = 0.0

    # ------------------------------------------------------------------ #
    # shared cooldown
    # ------------------------------------------------------------------ #
    def penalize(self, seconds: float, *, jitter: bool = True) -> float:
        """
        Pause every caller of :meth:`wait` for (at least) ``seconds``.

        A 429/503 is evidence about the *site*, not about one request: the
        worker that received it must make its peers back off too.  Unlike
        sleeping locally, this state lives on the limiter shared by all
        workers, so a caller that is already asleep inside :meth:`wait`
        re-checks the deadline when it wakes and sleeps again if needed.

        Cooldowns do not stack additively; the later deadline wins, so a fleet
        that receives the same ``Retry-After`` does not extend the pause once
        per worker.

        :param seconds: Requested pause.  Clamped to :data:`MAX_COOLDOWN`.
        :param jitter: Add upward-only jitter (bounded to 10 % / 1 s) so
            workers do not resume simultaneously.
        :return: The seconds actually applied (after clamping and jitter).
            Callers can compare this with the delay they asked for to know
            whether they still owe a local sleep.  Returns ``0.0`` for
            non-positive or non-numeric input.
        """
        try:
            requested = float(seconds)
        except (TypeError, ValueError):
            return 0.0
        # ``not requested > 0`` also rejects NaN.
        if not requested > 0:
            return 0.0

        applied = min(requested, MAX_COOLDOWN)
        if jitter:
            applied += random.uniform(
                0.0,
                min(_COOLDOWN_JITTER_MAX, applied * _COOLDOWN_JITTER_FRACTION),
            )

        deadline = time.monotonic() + applied
        with self._lock:
            self.cooldown_until = max(self.cooldown_until, deadline)
        return applied

    def cooldown_remaining(self) -> float:
        """Seconds left before the shared cooldown expires (never negative)."""
        with self._lock:
            return max(0.0, self.cooldown_until - time.monotonic())

    # ------------------------------------------------------------------ #
    # token bucket
    # ------------------------------------------------------------------ #
    async def wait(self) -> None:
        """
        Block until this caller owns a slot *and* any shared cooldown expired.

        The slot is *reserved* under the lock: the bucket may go negative and
        each caller sleeps for its own deficit (``-tokens / rate``), so the
        callers wake one interval apart.

        The previous implementation slept outside the lock and only took a
        token afterwards, so every concurrent waiter computed the same delay
        and fired at the same instant — ``max_rps`` was silently ignored as
        soon as ``workers`` > 1.

        A caller cancelled while sleeping returns its reservation, so a
        cancelled request does not keep costing later callers a slot.

        The wait is a loop because :meth:`penalize` may extend the cooldown
        while a caller is sleeping: on waking it re-reads the deadline and
        keeps waiting.  A caller that already owns a reservation does not
        reserve a second token while doing so.
        """
        reserved = False
        try:
            while True:
                with self._lock:
                    now = time.monotonic()
                    elapsed = now - self.timestamp
                    self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                    self.timestamp = now

                    cooldown = self.cooldown_until - now
                    if cooldown > 0:
                        # Do not consume a token while the site is cooling
                        # down: the slot is only needed once the pause ends.
                        sleep_for = cooldown
                    elif reserved:
                        return
                    else:
                        self.tokens -= 1.0
                        reserved = True
                        if self.tokens >= 0:
                            return
                        wait_time = -self.tokens / self.rate
                        jitter = random.uniform(
                            -self.jitter_strength, self.jitter_strength
                        )
                        sleep_for = max(0.0, wait_time + jitter)

                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            # The slot was reserved above, so a caller cancelled while
            # sleeping would keep charging the bucket for a request that is
            # never sent (every later caller waits one extra interval, and
            # repeated cancellations throttle the whole session).  Give the
            # reservation back; the bucket may legitimately hold other
            # callers' reservations, hence the ``min`` rather than a reset.
            if reserved:
                with self._lock:
                    self.tokens = min(self.capacity, self.tokens + 1.0)
            raise
