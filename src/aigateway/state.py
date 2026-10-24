"""Shared state backends for multi-instance deployments.

The gateway is single-process by default (in-memory token buckets, in-process
cooldowns). Behind N workers / replicas those limits must be shared:

- **Rate limiting** must be shared across instances -> Redis fixed-window
  counter (atomic ``INCR`` + ``PEXPIRE``, no scripting required).
- **Deployment cooldowns** are eventually consistent across instances: each
  process exports active cooldowns to Redis and adopts remote ones every few
  seconds. Exactness is not required — each instance independently fails over
  anyway; sharing only avoids re-hammering a dead upstream.

Redis unavailability policy (``fail_open``): when the backend cannot be
reached, rate limiting allows the request and logs (availability over strict
limiting — a stalled Redis must not take the gateway down). Set
``redis_fail_open: false`` to fail closed (429) instead.

The redis client is imported lazily; the dependency is optional
(``pip install aigateway[redis]``). Tests use fakeredis, which speaks the same
protocol.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("aigateway.state")


class StateBackendError(Exception):
    """Raised when the shared-state backend is unreachable."""


class StateBackend(ABC):
    """Async primitives for shared rate-limiting and cooldown state."""

    @abstractmethod
    async def acquire_bucket(
        self, name: str, rate_per_minute: float, capacity: float, amount: float = 1.0
    ) -> tuple[bool, float]:
        """Atomically take `amount` from the named token bucket.

        Returns (allowed, retry_after_s) — retry_after_s is 0.0 when allowed.
        """

    @abstractmethod
    async def set_cooldown(self, name: str, remaining_s: float) -> None:
        """Publish a cooldown that expires `remaining_s` from now."""

    @abstractmethod
    async def fetch_cooldowns(self, prefix: str) -> dict[str, float]:
        """All active cooldowns under `prefix` as {name: remaining_seconds}."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release connections."""


class InMemoryBackend(StateBackend):
    """Same-process backend; equivalent to the pre-Redis behavior."""

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float, float]] = {}  # name -> (tokens, rate_per_s, updated)
        self._cooldowns: dict[str, float] = {}  # name -> expiry (monotonic)

    async def acquire_bucket(
        self, name: str, rate_per_minute: float, capacity: float, amount: float = 1.0
    ) -> tuple[bool, float]:
        now = time.monotonic()
        tokens, rate, updated = self._buckets.get(name, (capacity, rate_per_minute / 60.0, now))
        tokens = min(capacity, tokens + (now - updated) * rate)
        if tokens >= amount:
            tokens -= amount
            allowed, retry_after = True, 0.0
        else:
            allowed = False
            retry_after = (amount - tokens) / rate if rate > 0 else 60.0
        self._buckets[name] = (tokens, rate_per_minute / 60.0, now)
        return allowed, retry_after

    async def set_cooldown(self, name: str, remaining_s: float) -> None:
        self._cooldowns[name] = time.monotonic() + remaining_s

    async def fetch_cooldowns(self, prefix: str) -> dict[str, float]:
        now = time.monotonic()
        active = {k: v - now for k, v in self._cooldowns.items() if v > now}
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}
        return {k: v for k, v in active.items() if k.startswith(prefix)}

    async def aclose(self) -> None:
        self._buckets.clear()
        self._cooldowns.clear()


class RedisBackend(StateBackend):
    """Redis-backed shared state (production backend for N instances).

    Rate limiting uses a fixed-window counter (atomic ``INCR`` + ``PEXPIRE``):
    unlike the in-memory token bucket it allows up to 2x ``capacity`` across a
    window boundary — the standard trade-off for lock-free distributed
    limiting (same approach as Kong's rate-limiting). Cooldowns are keys with
    a TTL; ``fetch_cooldowns`` reports remaining TTLs.
    """

    WINDOW_MS = 60_000

    def __init__(self, url: str, *, key_prefix: str = "aigw"):
        import redis.asyncio as aioredis  # optional dependency

        self.key_prefix = key_prefix
        self._redis = aioredis.from_url(
            url, decode_responses=True, socket_connect_timeout=2.0, socket_timeout=2.0
        )

    def _bucket_key(self, name: str) -> str:
        return f"{self.key_prefix}:rl:{name}"

    def _cooldown_prefix(self) -> str:
        return f"{self.key_prefix}:cd:"

    async def acquire_bucket(
        self, name: str, rate_per_minute: float, capacity: float, amount: float = 1.0
    ) -> tuple[bool, float]:
        try:
            key = self._bucket_key(name)
            limit = max(1, int(capacity))
            pipe = self._redis.pipeline(transaction=False)
            pipe.incrby(key, max(1, int(amount)))
            pipe.ttl(key)
            count, ttl = await pipe.execute()
            if ttl is None or ttl < 0:
                # First hit in this window (or TTL was lost): arm the window.
                await self._redis.pexpire(key, self.WINDOW_MS)
                ttl = self.WINDOW_MS
        except Exception as exc:  # noqa: BLE001 - network/protocol failures become StateBackendError
            raise StateBackendError(f"redis acquire_bucket failed: {exc}") from exc
        if count <= limit:
            return True, 0.0
        return False, ttl / 1000.0

    async def set_cooldown(self, name: str, remaining_s: float) -> None:
        try:
            ttl_ms = max(1000, int(remaining_s * 1000))
            await self._redis.set(f"{self._cooldown_prefix()}{name}", "1", px=ttl_ms)
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis set_cooldown failed: {exc}") from exc

    async def fetch_cooldowns(self, prefix: str) -> dict[str, float]:
        try:
            full_prefix = f"{self._cooldown_prefix()}{prefix}"
            names: list[str] = []
            cursor: int | str = 0
            while True:
                cursor, keys = await self._redis.scan(cursor=cursor, match=f"{full_prefix}*", count=100)
                names.extend(keys)
                if cursor == 0:
                    break
            result: dict[str, float] = {}
            for key in names:
                ttl_ms = await self._redis.pttl(key)
                if ttl_ms and ttl_ms > 0:
                    result[key.removeprefix(self._cooldown_prefix())] = ttl_ms / 1000.0
            return result
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis fetch_cooldowns failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._redis.aclose()


def build_backend(spec: dict[str, Any] | None) -> StateBackend | None:
    """Build a backend from the `shared_state` config section. None = disabled."""
    spec = spec or {}
    backend = spec.get("backend", "memory")
    if backend == "memory":
        return InMemoryBackend()
    if backend == "redis":
        return RedisBackend(spec.get("redis_url", "redis://127.0.0.1:6379/0"),
                            key_prefix=str(spec.get("key_prefix", "aigw")))
    raise ValueError(f"unknown shared_state backend {backend!r}")
