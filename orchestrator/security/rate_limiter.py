"""Per-agent rate limiting — token bucket algorithm.

Prevents any single agent from monopolising resources or executing
excessive calls within a time window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    capacity: float
    refill_rate: float  # tokens per second

    def try_consume(self, amount: float) -> bool:
        """Refill and try to consume. Returns True if allowed."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class AgentRateLimiter:
    """Token bucket rate limiter per agent."""

    def __init__(self, *, calls_per_minute: int = 30, tokens_per_minute: int = 50000):
        self._calls_per_minute = calls_per_minute
        self._tokens_per_minute = tokens_per_minute
        self._call_buckets: dict[str, _Bucket] = {}
        self._token_buckets: dict[str, _Bucket] = {}

    def _get_call_bucket(self, agent_name: str) -> _Bucket:
        if agent_name not in self._call_buckets:
            self._call_buckets[agent_name] = _Bucket(
                tokens=float(self._calls_per_minute),
                last_refill=time.monotonic(),
                capacity=float(self._calls_per_minute),
                refill_rate=self._calls_per_minute / 60.0,
            )
        return self._call_buckets[agent_name]

    def _get_token_bucket(self, agent_name: str) -> _Bucket:
        if agent_name not in self._token_buckets:
            self._token_buckets[agent_name] = _Bucket(
                tokens=float(self._tokens_per_minute),
                last_refill=time.monotonic(),
                capacity=float(self._tokens_per_minute),
                refill_rate=self._tokens_per_minute / 60.0,
            )
        return self._token_buckets[agent_name]

    def acquire(self, agent_name: str, tokens: int = 1) -> bool:
        """Try to acquire a call slot and token budget. Returns True if allowed."""
        call_bucket = self._get_call_bucket(agent_name)
        if not call_bucket.try_consume(1):
            return False

        if tokens > 0:
            token_bucket = self._get_token_bucket(agent_name)
            if not token_bucket.try_consume(float(tokens)):
                # Refund the call slot
                call_bucket.tokens = min(call_bucket.capacity, call_bucket.tokens + 1)
                return False

        return True

    def reset(self, agent_name: str | None = None) -> None:
        """Reset bucket(s). If agent_name is None, resets all."""
        if agent_name is None:
            self._call_buckets.clear()
            self._token_buckets.clear()
        else:
            self._call_buckets.pop(agent_name, None)
            self._token_buckets.pop(agent_name, None)
