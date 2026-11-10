import asyncio
import importlib
import time

import pytest

from novel_downloader.plugins.utils.rate_limiter import TokenBucketRateLimiter

rate_limiter_module = importlib.import_module(
    "novel_downloader.plugins.utils.rate_limiter"
)


class FrozenClock:
    """Deterministic stand-in for the module's ``time`` object.

    The limiter's accounting depends on ``time.monotonic``; on Windows that
    clock only ticks every ~15.6 ms, so the real clock can jump between two
    consecutive ``wait()`` calls that are microseconds apart.  Asserting exact
    float sleeps against the real clock therefore fails intermittently on a
    loaded machine.  Freezing the clock verifies the reservation maths exactly,
    while the real-time test below still exercises wall-clock concurrency.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> FrozenClock:
    clock = FrozenClock()
    monkeypatch.setattr(rate_limiter_module, "time", clock)
    return clock


@pytest.mark.asyncio
async def test_initial_burst_allows_instant_tokens():
    """Bucket starts full -> first N calls should be instant."""
    rl = TokenBucketRateLimiter(rate=1.0, burst=3, jitter_strength=0)

    start = time.perf_counter()

    # 3 tokens available immediately
    for _ in range(3):
        await rl.wait()

    elapsed = time.perf_counter() - start
    assert elapsed < 0.05  # essentially instant


@pytest.mark.asyncio
async def test_wait_requires_sleep(monkeypatch, frozen_clock):
    """When bucket empty, wait() must sleep the correct amount (jitter=0)."""

    rl = TokenBucketRateLimiter(rate=2.0, burst=1, jitter_strength=0)

    await rl.wait()

    slept = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await rl.wait()  # should need ~0.5 sec

    assert slept, "asyncio.sleep must be called"
    assert slept[0] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_jitter_applied(monkeypatch, frozen_clock):
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
async def test_concurrent_waits_safe(monkeypatch, frozen_clock):
    """Ensure concurrent tasks do not corrupt counters (lock works)."""

    rl = TokenBucketRateLimiter(rate=5.0, burst=1, jitter_strength=0.0)

    slept: list[float] = []

    # patch sleep to avoid real delays, but still count the deferrals
    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def do_wait():
        await rl.wait()

    # Run many concurrent wait() calls
    tasks = [asyncio.create_task(do_wait()) for _ in range(10)]
    await asyncio.gather(*tasks)

    # Slots are reserved under the lock: nine of the ten callers hold a
    # reservation (tokens go negative on purpose), never beyond the burst.
    # The frozen clock makes these exact values deterministic.
    assert len(slept) == 9
    assert rl.tokens <= rl.capacity
    assert rl.tokens == -9.0


@pytest.mark.asyncio
async def test_concurrent_waits_get_increasing_slots(monkeypatch, frozen_clock):
    """Concurrent callers reserve distinct slots instead of firing together."""
    rl = TokenBucketRateLimiter(rate=4.0, burst=1, jitter_strength=0)
    await rl.wait()  # spend the initial burst token

    slept: list[float] = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await asyncio.gather(*(rl.wait() for _ in range(4)))

    # rate 4/s -> one slot every 0.25s: 0.25, 0.50, 0.75, 1.00
    assert len(slept) == 4
    assert slept == [
        pytest.approx(0.25),
        pytest.approx(0.5),
        pytest.approx(0.75),
        pytest.approx(1.0),
    ]
    # Fairness in the strong sense: each caller waits strictly longer than
    # the previous one, so the four requests cannot fire together.
    assert all(
        later > earlier for earlier, later in zip(slept, slept[1:], strict=False)
    )


@pytest.mark.asyncio
async def test_elapsed_time_is_credited_before_reserving(monkeypatch, frozen_clock):
    """Time passed between reservations refills tokens by ``rate * elapsed``.

    This is the behaviour the old exact-value assertions tripped over: a real
    clock tick between two calls legitimately shortens every later sleep.
    """
    rl = TokenBucketRateLimiter(rate=4.0, burst=1, jitter_strength=0)
    await rl.wait()  # spend the initial burst token
    frozen_clock.advance(0.016)  # one Windows monotonic tick

    slept: list[float] = []

    async def fake_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await asyncio.gather(*(rl.wait() for _ in range(4)))

    # 0.016 s * 4 tokens/s = 0.064 tokens refunded before the first slot.
    assert slept == [
        pytest.approx(0.234),
        pytest.approx(0.484),
        pytest.approx(0.734),
        pytest.approx(0.984),
    ]


@pytest.mark.asyncio
async def test_cancelled_waiter_gives_its_slot_back(monkeypatch, frozen_clock):
    """A cancelled request must not keep charging the bucket."""
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_sleep(t):
        started.set()
        await never.wait()

    monkeypatch.setattr(asyncio, "sleep", blocking_sleep)

    rl = TokenBucketRateLimiter(rate=1.0, burst=1, jitter_strength=0)
    await rl.wait()  # burst token, no sleep
    assert rl.tokens == 0.0

    waiter = asyncio.create_task(rl.wait())
    await started.wait()
    assert rl.tokens == -1.0

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert rl.tokens == 0.0

    slept: list[float] = []

    async def recording_sleep(t):
        slept.append(t)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    await rl.wait()
    # One interval, not two: the cancelled caller's slot was returned.
    assert slept == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_real_concurrency_serialises_callers_in_time():
    """Wall-clock check on the real event loop, tolerant of timer granularity.

    Four waiters at 20/s must finish ~0.2 s apart in total.  The band is wide
    (Windows timers have ~15.6 ms granularity and CI can be slow) but still
    rejects the historical regression where every waiter fired at once.
    """
    rl = TokenBucketRateLimiter(rate=20.0, burst=1, jitter_strength=0)
    await rl.wait()  # spend the burst token

    start = time.perf_counter()
    await asyncio.gather(*(rl.wait() for _ in range(4)))
    elapsed = time.perf_counter() - start

    assert 0.15 <= elapsed < 2.0
