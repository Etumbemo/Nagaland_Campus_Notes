"""Small in-process sliding-window rate limiter.

Scope: one uvicorn worker. If the API is ever run with multiple workers each
worker keeps its own counters, so the effective limit multiplies. The module is
deliberately dependency-free; swap the backing store for a shared one (Redis, or
a MySQL table) before running more than one process.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    retry_after: int


class SlidingWindowLimiter:
    """Allow at most ``limit`` events per ``window_seconds`` for each key."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("limit and window_seconds must be positive")
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.setdefault(key, deque())
        cutoff = now - self._window
        while events and events[0] <= cutoff:
            events.popleft()
        return events

    def check(self, key: str) -> LimitResult:
        """Record one event for ``key`` and report whether it is allowed."""
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, now)
            if len(events) >= self._limit:
                retry_after = max(1, int(round(events[0] + self._window - now)))
                return LimitResult(False, retry_after)
            events.append(now)
            return LimitResult(True, 0)

    def peek(self, key: str) -> LimitResult:
        """Report the current state without recording an event."""
        now = time.monotonic()
        with self._lock:
            events = self._prune(key, now)
            if len(events) >= self._limit:
                retry_after = max(1, int(round(events[0] + self._window - now)))
                return LimitResult(False, retry_after)
            return LimitResult(True, 0)

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)

    def clear(self) -> None:
        """Drop all state. Used by tests."""
        with self._lock:
            self._events.clear()


def client_address(request) -> str:
    """Best-effort client identity for throttling.

    Only the socket address is trusted. ``X-Forwarded-For`` is deliberately not
    read: without a configured trusted proxy it is trivially spoofable, which
    would let an attacker bypass every limit. Configure a proxy-aware key here
    once you actually deploy behind one.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host or "unknown")
