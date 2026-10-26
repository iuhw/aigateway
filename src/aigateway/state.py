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

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("aigateway.state")


class StateBackendError(Exception):
    """Raised when the shared-state backend is unreachable."""


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain cosine; mismatched dimensions or zero vectors score 0.0."""
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


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
    async def adjust_bucket(self, name: str, delta: float) -> None:
        """Correct a bucket after the fact (refund over-prepaid / charge more).

        Used by token-based limiting: requests prepay an estimate and are
        reconciled with actual usage on completion. ``delta`` may be negative.
        If the original window expired meanwhile, the adjustment is dropped.
        """

    @abstractmethod
    async def set_cooldown(self, name: str, remaining_s: float) -> None:
        """Publish a cooldown that expires `remaining_s` from now."""

    @abstractmethod
    async def fetch_cooldowns(self, prefix: str) -> dict[str, float]:
        """All active cooldowns under `prefix` as {name: remaining_seconds}."""

    @abstractmethod
    async def incr_spend(self, key: str, usd: float, window_s: int) -> float:
        """Add `usd` to a budget window counter; returns the new total.

        The key embeds the window identity (e.g. UTC day/month); `window_s`
        only sets the TTL so counters self-clean in shared backends.
        """

    @abstractmethod
    async def get_spend(self, key: str) -> float:
        """Current total of a budget window counter."""

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        """Fetch a cached response body; None on miss (or if unsupported).

        The default returns None so backends that cannot hold cache entries
        simply never produce hits — callers must not treat that as an error.
        """
        return None

    async def cache_put(self, key: str, body: dict[str, Any], ttl_s: int) -> None:
        """Store a response body under `key` for `ttl_s` seconds (no-op default)."""

    async def vector_put(
        self, namespace: str, key: str, vector: list[float], payload: dict[str, Any], ttl_s: int
    ) -> None:
        """Store a vector + payload under (namespace, key) for semantic search.

        No-op default: backends without vector support simply never produce
        semantic hits.
        """

    async def vector_search(
        self, namespace: str, query: list[float], threshold: float, limit: int = 1
    ) -> list[tuple[str, dict[str, Any], float]]:
        """Brute-force cosine search within `namespace`; entries at or above
        `threshold` returned best-first as (key, payload, similarity).

        Empty default: backends without vector support never produce hits.
        """
        return []

    async def get_dynamic_keys(self) -> dict[str, dict[str, Any]]:
        """All admin-created (dynamic) virtual keys as {name: record}.

        Only backends that can persist records implement this; the default
        raises so callers can degrade to config-defined keys only.
        """
        raise StateBackendError("dynamic keys not supported by this backend")

    async def set_dynamic_key(self, name: str, record: dict[str, Any]) -> None:
        """Create or replace a dynamic virtual key record."""
        raise StateBackendError("dynamic keys not supported by this backend")

    async def delete_dynamic_key(self, name: str) -> None:
        """Remove a dynamic virtual key record."""
        raise StateBackendError("dynamic keys not supported by this backend")

    @abstractmethod
    async def aclose(self) -> None:
        """Release connections."""


class InMemoryBackend(StateBackend):
    """Same-process backend; equivalent to the pre-Redis behavior."""

    def __init__(self) -> None:
        # name -> (tokens, rate_per_s, updated, capacity)
        self._buckets: dict[str, tuple[float, float, float, float]] = {}
        self._cooldowns: dict[str, float] = {}  # name -> expiry (monotonic)
        self._spend: dict[str, float] = {}
        self._dynamic_keys: dict[str, dict[str, Any]] = {}
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}  # key -> (deadline, body)
        # namespace -> key -> (deadline, vector, payload)
        self._vectors: dict[str, dict[str, tuple[float, list[float], dict[str, Any]]]] = {}

    async def acquire_bucket(
        self, name: str, rate_per_minute: float, capacity: float, amount: float = 1.0
    ) -> tuple[bool, float]:
        now = time.monotonic()
        entry = self._buckets.get(name)
        if entry is None:
            tokens, rate, updated = capacity, rate_per_minute / 60.0, now
        else:
            tokens, rate, updated = entry[0], entry[1], entry[2]
        tokens = min(capacity, tokens + (now - updated) * rate)
        if tokens >= amount:
            tokens -= amount
            allowed, retry_after = True, 0.0
        else:
            allowed = False
            retry_after = (amount - tokens) / rate if rate > 0 else 60.0
        self._buckets[name] = (tokens, rate_per_minute / 60.0, now, capacity)
        return allowed, retry_after

    async def adjust_bucket(self, name: str, delta: float) -> None:
        entry = self._buckets.get(name)
        if entry is None or delta == 0:
            return
        tokens, rate, updated, capacity = entry
        self._buckets[name] = (max(0.0, min(capacity, tokens + delta)), rate, updated, capacity)

    async def set_cooldown(self, name: str, remaining_s: float) -> None:
        self._cooldowns[name] = time.monotonic() + remaining_s

    async def fetch_cooldowns(self, prefix: str) -> dict[str, float]:
        now = time.monotonic()
        active = {k: v - now for k, v in self._cooldowns.items() if v > now}
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}
        return {k: v for k, v in active.items() if k.startswith(prefix)}

    async def incr_spend(self, key: str, usd: float, window_s: int) -> float:
        self._spend[key] = self._spend.get(key, 0.0) + usd
        return self._spend[key]

    async def get_spend(self, key: str) -> float:
        return self._spend.get(key, 0.0)

    async def get_dynamic_keys(self) -> dict[str, dict[str, Any]]:
        return dict(self._dynamic_keys)

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        deadline, body = entry
        if deadline <= time.monotonic():
            del self._cache[key]
            return None
        return body

    async def cache_put(self, key: str, body: dict[str, Any], ttl_s: int) -> None:
        self._cache[key] = (time.monotonic() + ttl_s, body)

    async def vector_put(
        self, namespace: str, key: str, vector: list[float], payload: dict[str, Any], ttl_s: int
    ) -> None:
        self._vectors.setdefault(namespace, {})[key] = (time.monotonic() + ttl_s, vector, payload)

    async def vector_search(
        self, namespace: str, query: list[float], threshold: float, limit: int = 1
    ) -> list[tuple[str, dict[str, Any], float]]:
        entries = self._vectors.get(namespace)
        if not entries:
            return []
        now = time.monotonic()
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for key, (deadline, vector, payload) in list(entries.items()):
            if deadline <= now:
                del entries[key]  # lazy expiry
                continue
            similarity = cosine_similarity(query, vector)
            if similarity >= threshold:
                scored.append((similarity, key, payload))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(key, payload, similarity) for similarity, key, payload in scored[:limit]]

    async def set_dynamic_key(self, name: str, record: dict[str, Any]) -> None:
        self._dynamic_keys[name] = dict(record)

    async def delete_dynamic_key(self, name: str) -> None:
        self._dynamic_keys.pop(name, None)

    async def aclose(self) -> None:
        self._buckets.clear()
        self._cooldowns.clear()
        self._spend.clear()
        self._dynamic_keys.clear()
        self._cache.clear()
        self._vectors.clear()


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

    def _spend_key(self, key: str) -> str:
        return f"{self.key_prefix}:spend:{key}"

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

    async def adjust_bucket(self, name: str, delta: float) -> None:
        """Reconcile after the fact. ``delta`` follows the InMemoryBackend
        convention: positive = refund budget. The Redis counter counts
        *consumption*, so a refund decrements it."""
        try:
            if delta == 0:
                return
            key = self._bucket_key(name)
            await self._redis.incrby(key, -int(delta))
            ttl = await self._redis.ttl(key)
            if ttl is None or ttl < 0:
                # The original window expired before the adjustment landed:
                # drop the orphan counter rather than polluting the next window.
                await self._redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis adjust_bucket failed: {exc}") from exc

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

    async def incr_spend(self, key: str, usd: float, window_s: int) -> float:
        """INCRBYFLOAT + TTL on first touch of the window key. Redis floats are
        IEEE doubles; USD accumulation at float precision is fine for budgets."""
        try:
            name = self._spend_key(key)
            total = await self._redis.incrbyfloat(name, usd)
            ttl = await self._redis.ttl(name)
            if ttl is None or ttl < 0:
                await self._redis.expire(name, window_s)
            return float(total)
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis incr_spend failed: {exc}") from exc

    async def get_spend(self, key: str) -> float:
        try:
            value = await self._redis.get(self._spend_key(key))
            return float(value or 0.0)
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis get_spend failed: {exc}") from exc

    def _cache_key(self, key: str) -> str:
        return f"{self.key_prefix}:cache:{key}"

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._redis.get(self._cache_key(key))
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis cache_get failed: {exc}") from exc
        if raw is None:
            return None
        try:
            body = json.loads(raw)
        except ValueError:
            return None  # corrupted entry counts as a miss
        return body if isinstance(body, dict) else None

    async def cache_put(self, key: str, body: dict[str, Any], ttl_s: int) -> None:
        try:
            await self._redis.set(self._cache_key(key), json.dumps(body), ex=max(1, int(ttl_s)))
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis cache_put failed: {exc}") from exc

    def _vec_prefix(self, namespace: str) -> str:
        return f"{self.key_prefix}:vec:{namespace}:"

    async def vector_put(
        self, namespace: str, key: str, vector: list[float], payload: dict[str, Any], ttl_s: int
    ) -> None:
        try:
            entry = json.dumps({"v": vector, "p": payload})
            await self._redis.set(
                f"{self._vec_prefix(namespace)}{key}", entry, ex=max(1, int(ttl_s))
            )
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis vector_put failed: {exc}") from exc

    async def vector_search(
        self, namespace: str, query: list[float], threshold: float, limit: int = 1
    ) -> list[tuple[str, dict[str, Any], float]]:
        """Brute-force cosine over all entries in the namespace.

        SCAN + per-entry GET keeps this dependency-free; the blueprint caps
        namespaces at a few hundred entries (plugin max_entries semantics are
        enforced per-instance for the in-memory store; Redis namespaces rely
        on TTL eviction). Revisit with a dedicated index if namespaces grow.
        """
        try:
            prefix = self._vec_prefix(namespace)
            keys: list[str] = []
            cursor: int | str = 0
            while True:
                cursor, batch = await self._redis.scan(cursor=cursor, match=f"{prefix}*", count=100)
                keys.extend(batch)
                if cursor == 0:
                    break
            scored: list[tuple[float, str, dict[str, Any]]] = []
            for full_key in keys:
                raw = await self._redis.get(full_key)
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                vector, payload = entry.get("v"), entry.get("p")
                if not isinstance(vector, list) or not isinstance(payload, dict):
                    continue
                similarity = cosine_similarity(query, vector)
                if similarity >= threshold:
                    scored.append((similarity, full_key.removeprefix(prefix), payload))
            scored.sort(key=lambda item: item[0], reverse=True)
            return [(key, payload, similarity) for similarity, key, payload in scored[:limit]]
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis vector_search failed: {exc}") from exc

    def _dynkeys_name(self) -> str:
        return f"{self.key_prefix}:dynkeys"

    async def get_dynamic_keys(self) -> dict[str, dict[str, Any]]:
        try:
            raw = await self._redis.hgetall(self._dynkeys_name())
            return {name: json.loads(record) for name, record in raw.items()}
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis get_dynamic_keys failed: {exc}") from exc

    async def set_dynamic_key(self, name: str, record: dict[str, Any]) -> None:
        try:
            await self._redis.hset(self._dynkeys_name(), name, json.dumps(record))
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis set_dynamic_key failed: {exc}") from exc

    async def delete_dynamic_key(self, name: str) -> None:
        try:
            await self._redis.hdel(self._dynkeys_name(), name)
        except Exception as exc:  # noqa: BLE001
            raise StateBackendError(f"redis delete_dynamic_key failed: {exc}") from exc

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
