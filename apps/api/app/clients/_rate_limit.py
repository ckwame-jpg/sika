"""Process-level token bucket rate limiter, shared across clients.

Mirrors the existing ``_TokenBucket`` in :mod:`app.clients.kalshi` but exposes
a named registry so multiple advanced-stats clients (NBA Stats, MLB Stats,
Baseball Savant, weather) can share a single bucket per upstream host. This
matters because each upstream has its own rate budget — a bucket per name
keeps them isolated, and a process-level singleton serializes bursts across
threads.

Usage::

    from app.clients._rate_limit import shared_bucket

    bucket = shared_bucket("nba_stats", rps=0.6, burst=2.0)
    bucket.acquire()  # blocks until a token is available
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_second: float, burst: float) -> None:
        self._rate = float(rate_per_second)
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate if self._rate > 0 else 0.1
            time.sleep(max(wait, 0.0))


_REGISTRY: dict[str, TokenBucket] = {}
_REGISTRY_LOCK = threading.Lock()


def shared_bucket(name: str, rps: float, burst: float) -> TokenBucket:
    """Return the process-singleton bucket for ``name``.

    The first call with a given name installs the bucket using the supplied
    ``rps`` and ``burst``; subsequent calls return the same instance and
    ignore the parameters (so callers cannot accidentally widen another
    caller's rate). To change parameters, restart the process.
    """
    with _REGISTRY_LOCK:
        bucket = _REGISTRY.get(name)
        if bucket is None:
            bucket = TokenBucket(rps, burst)
            _REGISTRY[name] = bucket
        return bucket


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header into seconds, supporting integer-seconds form."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return seconds


def reset_for_tests() -> None:
    """Reset the registry. Intended for test fixtures only."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
