"""AsyncRateLimiter tests — deterministic via injected time source."""
from __future__ import annotations

import pytest

from shotcut.research.rate_limit import AsyncRateLimiter


class FakeClock:
    """Controllable monotonic clock. `sleep()` advances the clock and
    returns immediately (no real I/O)."""

    def __init__(self) -> None:
        self._now = 0.0
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._now += seconds

    def tick(self, seconds: float) -> None:
        self._now += seconds


async def test_bucket_starts_full() -> None:
    clock = FakeClock()
    limiter = AsyncRateLimiter(10.0, time_source=clock.now, sleep=clock.sleep)
    assert limiter.tokens == pytest.approx(10.0)


async def test_acquire_consumes_one_token() -> None:
    clock = FakeClock()
    limiter = AsyncRateLimiter(10.0, time_source=clock.now, sleep=clock.sleep)
    await limiter.acquire()
    assert limiter.tokens == pytest.approx(9.0)
    assert clock.sleep_calls == []


async def test_eleventh_acquire_waits() -> None:
    """Ten acquires in zero elapsed time deplete the bucket; the eleventh
    must wait for ~100ms at 10 req/sec."""
    clock = FakeClock()
    limiter = AsyncRateLimiter(10.0, time_source=clock.now, sleep=clock.sleep)

    for _ in range(10):
        await limiter.acquire()
    assert clock.sleep_calls == [], "first ten acquires should not sleep"
    assert limiter.tokens == pytest.approx(0.0)

    await limiter.acquire()
    assert len(clock.sleep_calls) == 1
    # Should sleep ~1/rate = 0.1s.
    assert clock.sleep_calls[0] == pytest.approx(0.1, abs=1e-6)


async def test_tokens_replenish_over_time() -> None:
    clock = FakeClock()
    limiter = AsyncRateLimiter(10.0, time_source=clock.now, sleep=clock.sleep)
    # Drain the bucket.
    for _ in range(10):
        await limiter.acquire()
    # 500ms passes → 5 tokens refill.
    clock.tick(0.5)
    await limiter.acquire()
    # We had 0 + 5 refilled - 1 consumed = 4.
    assert limiter.tokens == pytest.approx(4.0, abs=1e-6)
    # No sleep needed — tokens had refilled.
    assert clock.sleep_calls == []


async def test_rate_is_respected_over_long_run() -> None:
    """Simulated 100 acquires at 10 req/sec should pace themselves."""
    clock = FakeClock()
    limiter = AsyncRateLimiter(10.0, time_source=clock.now, sleep=clock.sleep)
    for _ in range(100):
        await limiter.acquire()
    # After initial 10 free + 90 more at 1/rate each, total simulated
    # time advanced by ~9 seconds.
    assert clock.now() == pytest.approx(9.0, rel=1e-2)


def test_rejects_nonpositive_rate() -> None:
    with pytest.raises(ValueError):
        AsyncRateLimiter(0)
    with pytest.raises(ValueError):
        AsyncRateLimiter(-1)
