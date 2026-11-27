"""Concurrency controls for update processing.

Concurrent updates are off by default: enable them with
``PERF_CONCURRENT_UPDATES=2|4|8`` after the canary checks in the performance
plan. Same-user messages always run under an asyncio lock so message order and
per-user state stay intact.
"""

import asyncio
import os
import time
import weakref

_user_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def concurrency_limit() -> int:
    """Return the configured update concurrency (0 keeps PTB's default)."""
    try:
        return max(0, int(os.getenv("PERF_CONCURRENT_UPDATES", "0")))
    except ValueError:
        return 0


def configure_builder(builder):
    """Apply concurrent_updates only when explicitly enabled."""
    limit = concurrency_limit()
    if limit > 0:
        builder = builder.concurrent_updates(limit)
    return builder


def user_lock(context, user_id) -> asyncio.Lock:
    """Return a stable per-user lock while the caller holds a reference."""
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def lock_count() -> int:
    """Return the number of live per-user locks (tests/diagnostics)."""
    return len(_user_locks)


class TokenBucket:
    """Simple async token bucket for outbound Telegram API calls."""

    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = float(rate)
        self.capacity = float(capacity or rate)
        self.tokens = self.capacity
        self.updated = time.monotonic()

    async def acquire(self) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(
                self.capacity, self.tokens + (now - self.updated) * self.rate
            )
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            await asyncio.sleep((1 - self.tokens) / self.rate)
