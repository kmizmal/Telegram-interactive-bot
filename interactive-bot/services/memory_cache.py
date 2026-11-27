"""Small bounded in-process caches.

The bot is a long-lived process; any per-user cache must have both a maximum
entry count and a TTL so a growing user base cannot grow memory forever.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    """A tiny insertion-ordered cache with expiry and a hard size cap."""

    def __init__(self, maxsize: int, ttl_seconds: float):
        self.maxsize = max(1, int(maxsize))
        self.ttl_seconds = float(ttl_seconds)
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key, now: float | None = None):
        moment = time.monotonic() if now is None else now
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= moment:
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return value

    def set(self, key, value, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        expires_at = moment + self.ttl_seconds
        self._items[key] = (expires_at, value)
        self._items.move_to_end(key)
        self._prune(moment)

    def _prune(self, now: float) -> None:
        expired = [key for key, (expires_at, _) in self._items.items() if expires_at <= now]
        for key in expired:
            self._items.pop(key, None)
        while len(self._items) > self.maxsize:
            self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()


avatar_cache = TTLCache(maxsize=2048, ttl_seconds=7 * 24 * 3600)
captcha_image_cache = TTLCache(maxsize=128, ttl_seconds=7 * 24 * 3600)
_message_times = TTLCache(maxsize=4096, ttl_seconds=3600)


def rate_limit_ok(user_id: int, interval: float) -> bool:
    """Return True when this user may send another message now."""
    if interval <= 0:
        return True
    now = time.monotonic()
    last = _message_times.get(user_id, now=now)
    if last is not None and now - last < interval:
        return False
    _message_times.set(user_id, now, now=now)
    return True


def clear_all() -> None:
    """Clear all module caches (tests only)."""
    avatar_cache.clear()
    captcha_image_cache.clear()
    _message_times.clear()
