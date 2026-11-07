import asyncio

import pytest

from novel_downloader.plugins.utils.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_initial_burst_allows_instant_tokens():
    """Bucket starts full -> first N calls should be instant."""
    rl = TokenBucketRateLimiter(rate=1.0, burst=3, jitter_strength=0)

    start = asyncio.get_event_loop().time()

    # 3 tokens available immediately
    for _ in range(3):
        await rl.wait()

    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.01  # essentially instant


@pytest.mark.asyncio
async def test_wait_requires_sleep(monkeypatch):
    """When bucket empty, wait() must sleep the correct amount (jitter=0)."""

    rl = TokenBucketRateLimiter(rate=2.0, burst=1, jitter_strength=0)

    await rl.wait()

    slept = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await rl.wait()  # should need ~0.5 sec

    assert slept, "asyncio.sleep must be called"
    assert abs(slept[0] - 0.5) < 0.05


@pytest.mark.asyncio
async def test_jitter_applied(monkeypatch):
    """Ensure jitter affects sleep duration."""

    rl = TokenBucketRateLimiter(rate=1.0, burst=1, jitter_strength=0.3)

    # consume initial token
    await rl.wait()

    slept = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await rl.wait()

    # Base wait = 1s (rate=1)
    # jitter in [-0.3, +0.3]

    assert slept, "asyncio.sleep should be called"
    assert 0.7 <= slept[0] <= 1.3


@pytest.mark.asyncio
async def test_concurrent_waits_safe(monkeypatch):
    """Ensure concurrent tasks do not corrupt counters (lock works)."""

    rl = TokenBucketRateLimiter(rate=5.0, burst=1, jitter_strength=0.0)

    # patch sleep to avoid real delays
    async def fake_sleep(t):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def do_wait():
        await rl.wait()

    # Run many concurrent wait() calls
    tasks = [asyncio.create_task(do_wait()) for _ in range(10)]
    await asyncio.gather(*tasks)

    # Slots are reserved under the lock: nine of the ten callers hold a
    # reservation (tokens go negative on purpose), never beyond the burst.
    assert rl.tokens <= rl.capacity
    assert rl.tokens == -9.0


@pytest.mark.asyncio
async def test_concurrent_waits_get_increasing_slots(monkeypatch):
    """Concurrent callers reserve distinct slots instead of firing together."""
    rl = TokenBucketRateLimiter(rate=4.0, burst=1, jitter_strength=0)
    await rl.wait()  # spend the initial burst token

    slept: list[float] = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await asyncio.gather(*(rl.wait() for _ in range(4)))

    # rate 4/s -> one slot every 0.25s: 0.25, 0.50, 0.75, 1.00
    assert slept == [0.25, 0.5, 0.75, 1.0]
