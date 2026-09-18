"""Token buckets, for the door that faces the internet.

On the tailnet, not rate limiting was defensible: every caller had already been
authenticated by tailscaled, and the set of them was small and known. A route
exposed publicly has neither property. Anyone can reach it, the hostname is in
certificate transparency logs so it is not even obscure, and every request that
arrives costs a signature verification whether or not it is genuine.

Two buckets, because they answer different questions:

  per route   is this endpoint being hammered, by anyone at all? Protects the
              process regardless of where it comes from.
  per caller  is this one source misbehaving? Lets a flood from one address be
              shed without penalising everyone else.

A rejected request costs one dictionary lookup and no HMAC, which is the point:
the cheap check has to come before the expensive one, or the limiter becomes the
amplifier.

Deliberately in memory. A restart forgets, and that is the right trade for a
single-process daemon — persisting it would buy very little and add a write to
the hot path of every request.
"""

from __future__ import annotations

import threading
from typing import Any

from . import clock

__all__ = ["TokenBucket", "RateLimiter"]


class TokenBucket:
    """Classic leaky bucket: ``capacity`` burst, refilled at ``rate`` per second."""

    __slots__ = ("capacity", "rate", "_tokens", "_last")

    def __init__(self, capacity: float, rate: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(rate)
        self._tokens = float(capacity)
        self._last = clock.monotonic()

    def take(self, cost: float = 1.0) -> bool:
        now = clock.monotonic()
        # Monotonic, so a clock correction cannot hand out a windfall of tokens
        # or freeze the bucket until the wall clock catches up.
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def retry_after(self, cost: float = 1.0) -> float:
        if self.rate <= 0:
            return 0.0
        missing = max(0.0, cost - self._tokens)
        return missing / self.rate


class RateLimiter:
    """Per-route and per-caller buckets, with a bound on how many it will track."""

    def __init__(
        self,
        *,
        route_burst: float = 60,
        route_rate: float = 2.0,
        caller_burst: float = 20,
        caller_rate: float = 0.5,
        max_callers: int = 4096,
    ) -> None:
        self.route_burst, self.route_rate = route_burst, route_rate
        self.caller_burst, self.caller_rate = caller_burst, caller_rate
        self.max_callers = max_callers
        self._routes: dict[str, TokenBucket] = {}
        self._callers: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _caller_bucket(self, key: str) -> TokenBucket:
        bucket = self._callers.get(key)
        if bucket is None:
            if len(self._callers) >= self.max_callers:
                # A flood from many addresses must not become a memory leak, so
                # the table is capped and the oldest-touched entries are dropped.
                # Worst case everyone shares the route bucket, which still holds.
                stale = sorted(self._callers.items(), key=lambda item: item[1]._last)[: self.max_callers // 4]
                for name, _ in stale:
                    self._callers.pop(name, None)
            bucket = TokenBucket(self.caller_burst, self.caller_rate)
            self._callers[key] = bucket
        return bucket

    def check(self, route: str, caller: str) -> tuple[bool, float, str]:
        """Allow or refuse. Returns (allowed, retry_after_seconds, which_limit)."""
        with self._lock:
            route_bucket = self._routes.setdefault(route, TokenBucket(self.route_burst, self.route_rate))
            caller_bucket = self._caller_bucket(f"{route}|{caller}")
            if not route_bucket.take():
                return False, route_bucket.retry_after(), "route"
            if not caller_bucket.take():
                # The route token is already spent. Not refunding is deliberate:
                # a caller who is being shed should still count towards the
                # endpoint's own pressure, because that pressure is real.
                return False, caller_bucket.retry_after(), "caller"
            return True, 0.0, ""

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"routes_tracked": len(self._routes), "callers_tracked": len(self._callers)}
