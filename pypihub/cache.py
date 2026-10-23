#!/usr/bin/env python3

# File: pypihub/cache.py
# -*- coding: utf-8 -*-
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Description: Small key/value cache with a TTL: in-process by default, Redis when REDIS_URL is set
# License: LGPL-3.0-or-later

"""
Shared by the upstream index cache, the statistics cache and the rate limiter's storage.

The in-process cache is per worker. Redis makes the cache shared by every worker and every
replica. A Redis failure never fails a request: reads become misses and writes are dropped.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple

log = logging.getLogger(__name__)


class MemoryCache:
    """Thread-safe, size-bounded cache. Least recently used entries are dropped first."""

    kind = "memory"

    def __init__(self, max_items: int = 4096) -> None:
        self._items: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_items

    def get(self, key: str) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires <= now:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return value

    def set(self, key: str, value: str, ttl: float) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)

    def ping(self) -> bool:
        return True


class RedisCache:
    kind = "redis"

    def __init__(self, url: str, prefix: str = "pypihub:") -> None:
        import redis

        self._redis = redis.Redis.from_url(
            url,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
            health_check_interval=30,
            decode_responses=True,
        )
        self._errors = (redis.exceptions.RedisError, OSError)
        self._prefix = prefix
        self._last_warning = 0.0

    def _warn(self, action: str, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_warning > 30:  # one line per 30 s, not one per request
            self._last_warning = now
            log.warning("Redis %s failed, continuing without the cache: %s", action, exc)

    def get(self, key: str) -> Optional[str]:
        try:
            return self._redis.get(self._prefix + key)
        except self._errors as exc:
            self._warn("read", exc)
            return None

    def set(self, key: str, value: str, ttl: float) -> None:
        try:
            self._redis.set(self._prefix + key, value, px=max(1, int(ttl * 1000)))
        except self._errors as exc:
            self._warn("write", exc)

    def delete(self, key: str) -> None:
        try:
            self._redis.delete(self._prefix + key)
        except self._errors as exc:
            self._warn("delete", exc)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except self._errors:
            return False


def make_cache(redis_url: Optional[str] = None):
    """Redis when a URL is given, otherwise an in-process cache."""
    if redis_url:
        return RedisCache(redis_url)
    return MemoryCache()
