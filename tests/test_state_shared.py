"""Shared-state backend tests: in-memory semantics, Redis sharing, fail-open.

Uses fakeredis's TCP server so tests exercise real RESP over real sockets —
two backend instances genuinely share counters through separate connections,
which is the property multi-worker/replica deployments depend on.
"""

from __future__ import annotations

import socket
import threading

import pytest

from aigateway.state import (
    InMemoryBackend,
    RedisBackend,
    StateBackendError,
    build_backend,
)

pytest.importorskip("fakeredis", reason="fakeredis not installed (dev extra)")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def redis_url():
    from fakeredis import TcpFakeServer

    port = _free_port()
    server = TcpFakeServer(("127.0.0.1", port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()
    thread.join(timeout=5)


# ---- in-memory ----------------------------------------------------------------


async def test_memory_bucket_allows_burst_then_denies():
    backend = InMemoryBackend()
    results = [await backend.acquire_bucket("m", rate_per_minute=2, capacity=2) for _ in range(3)]
    assert [r[0] for r in results] == [True, True, False]
    assert results[2][1] > 0


async def test_memory_bucket_refills_over_time(monkeypatch):
    backend = InMemoryBackend()
    assert (await backend.acquire_bucket("m", rate_per_minute=60, capacity=1))[0] is True
    assert (await backend.acquire_bucket("m", rate_per_minute=60, capacity=1))[0] is False
    # simulate 10s passing -> 60/min refills 10 tokens
    import aigateway.state as state_mod

    real = state_mod.time.monotonic
    monkeypatch.setattr(state_mod.time, "monotonic", lambda: real() + 10)
    assert (await backend.acquire_bucket("m", rate_per_minute=60, capacity=1))[0] is True


async def test_memory_cooldown_roundtrip():
    backend = InMemoryBackend()
    await backend.set_cooldown("m\x1fp1/k", 5.0)
    active = await backend.fetch_cooldowns("")
    assert "m\x1fp1/k" in active and active["m\x1fp1/k"] > 4


# ---- redis (real RESP over TCP via fakeredis) -----------------------------------


async def test_redis_bucket_limit_and_refuse(redis_url):
    backend = RedisBackend(redis_url)
    results = [await backend.acquire_bucket("m", rate_per_minute=3, capacity=3) for _ in range(5)]
    assert [r[0] for r in results] == [True, True, True, False, False]
    assert results[3][1] > 0  # retry_after within the window
    await backend.aclose()


async def test_redis_bucket_shared_across_connections(redis_url):
    """The property multi-worker deployments depend on: two independent backend
    instances (separate TCP connections) share one counter."""
    backend_a = RedisBackend(redis_url)
    backend_b = RedisBackend(redis_url)
    first = await backend_a.acquire_bucket("shared", rate_per_minute=2, capacity=2)
    second = await backend_b.acquire_bucket("shared", rate_per_minute=2, capacity=2)
    third = await backend_a.acquire_bucket("shared", rate_per_minute=2, capacity=2)
    assert (first[0], second[0], third[0]) == (True, True, False)
    await backend_a.aclose()
    await backend_b.aclose()


async def test_redis_key_prefixes_isolate_tenants(redis_url):
    backend_a = RedisBackend(redis_url, key_prefix="tenant-a")
    backend_b = RedisBackend(redis_url, key_prefix="tenant-b")
    assert (await backend_a.acquire_bucket("m", 1, 1))[0] is True
    assert (await backend_b.acquire_bucket("m", 1, 1))[0] is True  # separate counter
    assert (await backend_a.acquire_bucket("m", 1, 1))[0] is False
    await backend_a.aclose()
    await backend_b.aclose()


async def test_redis_cooldown_roundtrip(redis_url):
    backend = RedisBackend(redis_url)
    await backend.set_cooldown("m\x1fp1/k", 4.0)
    active = await backend.fetch_cooldowns("")
    assert "m\x1fp1/k" in active and 3 < active["m\x1fp1/k"] <= 4
    assert await backend.fetch_cooldowns("other") == {}  # prefix filter
    await backend.aclose()


async def test_redis_unreachable_raises_state_backend_error():
    # Port 9 (discard) on loopback: connection refused immediately.
    backend = RedisBackend("redis://127.0.0.1:9/0")
    with pytest.raises(StateBackendError):
        await backend.acquire_bucket("m", 60, 60)


async def test_build_backend_dispatch():
    assert isinstance(build_backend(None), InMemoryBackend)
    assert isinstance(build_backend({"backend": "memory"}), InMemoryBackend)
    with pytest.raises(ValueError):
        build_backend({"backend": "memcached"})


# ---- response cache -----------------------------------------------------------------


async def test_memory_cache_roundtrip_and_expiry(monkeypatch):
    backend = InMemoryBackend()
    await backend.cache_put("k", {"answer": 42}, ttl_s=60)
    assert (await backend.cache_get("k")) == {"answer": 42}
    import aigateway.state as state_mod

    real = state_mod.time.monotonic
    monkeypatch.setattr(state_mod.time, "monotonic", lambda: real() + 61)
    assert (await backend.cache_get("k")) is None  # expired


async def test_redis_cache_roundtrip(redis_url):
    backend = RedisBackend(redis_url)
    try:
        await backend.cache_put("k", {"id": "x", "choices": []}, ttl_s=60)
        assert (await backend.cache_get("k")) == {"id": "x", "choices": []}
        assert (await backend.cache_get("missing")) is None
    finally:
        await backend.aclose()


async def test_redis_cache_shared_across_instances(redis_url):
    backend_a = RedisBackend(redis_url)
    backend_b = RedisBackend(redis_url, key_prefix="other")  # different tenant prefix
    backend_c = RedisBackend(redis_url)
    try:
        await backend_a.cache_put("k", {"from": "a"}, ttl_s=60)
        assert (await backend_c.cache_get("k")) == {"from": "a"}  # same prefix: shared
        assert (await backend_b.cache_get("k")) is None  # other prefix: isolated
    finally:
        await backend_a.aclose()
        await backend_b.aclose()
        await backend_c.aclose()


async def test_redis_cache_corrupted_entry_is_miss(redis_url):
    backend = RedisBackend(redis_url)
    try:
        await backend._redis.set(backend._cache_key("bad"), "not-json{")
        assert (await backend.cache_get("bad")) is None
    finally:
        await backend.aclose()
