"""Async token bucket for SEC fair-access compliance.

SEC's fair-access policy allows up to 10 requests per second per
requester (with an identifying User-Agent). The `EdgarClient` owns an
instance of `AsyncRateLimiter` and awaits `acquire()` before every
SEC call.

Token bucket semantics:
- Bucket starts full (`rate_per_second` tokens).
- Tokens replenish continuously at `rate_per_second`/sec.
- `acquire()` consumes one token, blocking only if none are available.

The time source is injectable (`time_source` / `sleep`) so tests can
drive deterministic schedules without real sleeps.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


_DefaultTime = Callable[[], float]
_DefaultSleep = Callable[[float], Awaitable[None]]


class AsyncRateLimiter:
    def __init__(
        self,
        rate_per_second: float,
        *,
        time_source: _DefaultTime | None = None,
        sleep: _DefaultSleep | None = None,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._tokens = rate_per_second
        self._time = time_source or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._last = self._time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        async with self._lock:
            self._refill()
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await self._sleep(wait)
                self._refill()
            self._tokens -= 1

    def _refill(self) -> None:
        now = self._time()
        elapsed = now - self._last
        self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
        self._last = now

    @property
    def tokens(self) -> float:
        """Current token count. Primarily for tests / observability."""
        return self._tokens
