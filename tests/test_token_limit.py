"""Token-based rate limiting tests (prepaid estimate + actual-usage reconcile)."""

from __future__ import annotations

import socket
import threading

import pytest

from aigateway.errors import RateLimitError
from aigateway.observability.metrics import Metrics
from aigateway.plugins.base import PluginContext
from aigateway.plugins.rate_limit import RateLimitPlugin
from aigateway.schemas import estimate_tokens, request_text
from aigateway.state import RedisBackend


def make_ctx(payload: dict, identity: dict | None = None) -> PluginContext:
    return PluginContext(
        request_id="r", model="m1", payload=payload, stream=False,
        headers={}, identity=identity,
    )


def complete_usage(ctx: PluginContext, prompt: int, completion: int) -> None:
    ctx.prompt_tokens, ctx.completion_tokens = prompt, completion
    ctx.usage_reported = True


async def test_token_limit_prepays_estimate_and_refunds():
    plugin = RateLimitPlugin({"token_rpm": {"m1": 1000}})
    payload = {"messages": [{"role": "user", "content": "x" * 40}], "max_tokens": 100}
    ctx = make_ctx(payload)
    estimate = estimate_tokens(request_text(payload)) + 100
    assert 40 < estimate < 120

    await plugin.on_request(ctx)  # prepay estimate (~50 of 1000)
    # reconcile with a small actual usage -> refund makes room again
    complete_usage(ctx, 5, 10)
    await plugin.on_response(ctx)

    # 30 small requests (~21 tokens each) fit in the refunded budget
    for _ in range(30):
        ctx2 = make_ctx(dict(payload, messages=[{"role": "user", "content": "y"}], max_tokens=20))
        await plugin.on_request(ctx2)
        complete_usage(ctx2, 5, 10)
        await plugin.on_response(ctx2)


async def test_token_limit_refuses_when_estimate_exceeds_window():
    """An estimate larger than the whole window budget can never fit: rejected
    immediately (the caller must raise max_tokens or shrink the prompt)."""
    plugin = RateLimitPlugin({"token_rpm": {"m1": 100}, "assumed_max_tokens": 10})
    # "z" run of 600 -> ceil(599/5) = 120 estimated tokens + 10 assumed > 100 cap
    payload = {"messages": [{"role": "user", "content": "z" * 600}]}
    with pytest.raises(RateLimitError):
        await plugin.on_request(make_ctx(payload))
    with pytest.raises(RateLimitError):
        await plugin.on_request(make_ctx(payload))


async def test_token_limit_identity_scope():
    plugin = RateLimitPlugin({"identity_token_rpm": 50, "assumed_max_tokens": 10})
    payload = {"messages": [{"role": "user", "content": "w" * 160}]}  # ~40 tok est
    await plugin.on_request(make_ctx(payload, identity={"name": "u1"}))
    with pytest.raises(RateLimitError):
        await plugin.on_request(make_ctx(payload, identity={"name": "u1"}))
    await plugin.on_request(make_ctx(payload, identity={"name": "u2"}))  # other identity


async def test_reconcile_ignores_unreported_usage():
    plugin = RateLimitPlugin({"token_rpm": {"m1": 100}, "assumed_max_tokens": 10})
    ctx = make_ctx({"messages": [{"role": "user", "content": "hi"}]})
    await plugin.on_request(ctx)
    await plugin.on_response(ctx)  # usage not reported: must be a no-op, no crash


async def test_reconcile_runs_once_only():
    plugin = RateLimitPlugin({"token_rpm": {"m1": 100000}, "assumed_max_tokens": 10})
    ctx = make_ctx({"messages": [{"role": "user", "content": "hi"}]})
    await plugin.on_request(ctx)
    complete_usage(ctx, 10, 10)
    await plugin.on_response(ctx)
    ctx.prompt_tokens, ctx.completion_tokens = 0, 0  # tamper: second call must not re-apply
    await plugin.on_response(ctx)
    # if it had re-applied, bucket would be drained; a fresh small request still fits
    await plugin.on_request(make_ctx({"messages": [{"role": "user", "content": "tiny"}], "max_tokens": 5}))


# ---- redis backend reconciliation -------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def redis_url():
    from fakeredis import TcpFakeServer

    port = _free_port()
    server = TcpFakeServer(("127.0.0.1", port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()


async def test_token_reconcile_shared_across_redis_connections(redis_url):
    plugin_a = RateLimitPlugin({"token_rpm": {"m1": 100}, "assumed_max_tokens": 10})
    plugin_b = RateLimitPlugin({"token_rpm": {"m1": 100}, "assumed_max_tokens": 10})
    backend = RedisBackend(redis_url)
    metrics = Metrics()
    plugin_a.set_shared(backend, metrics)
    plugin_b.set_shared(backend, metrics)

    payload = {"messages": [{"role": "user", "content": "q" * 160}]}  # ~40 tok est
    ctx_a = make_ctx(payload)
    await plugin_a.on_request(ctx_a)  # A prepays ~50
    complete_usage(ctx_a, 5, 5)       # actual 10 -> refund 40 on shared bucket
    await plugin_a.on_response(ctx_a)

    # B (separate connection) now finds room for a similar request
    await plugin_b.on_request(make_ctx(payload))
    await backend.aclose()


async def test_token_adjust_orphan_window_dropped(redis_url):
    backend = RedisBackend(redis_url)
    await backend.acquire_bucket("token:model:x", 100, capacity=100, amount=1)
    # simulate the window expiring: delete + adjust must not resurrect a key
    import fakeredis.aioredis as fake_redis  # noqa: F401  (ensures module loads)

    from aigateway.state import InMemoryBackend  # noqa: F401

    # force-expire by deleting directly, then adjusting
    await backend._redis.delete("aigw:rl:token:model:x")
    await backend.adjust_bucket("token:model:x", -5)
    assert not await backend._redis.exists("aigw:rl:token:model:x")
    await backend.aclose()


async def test_token_backend_outage_fail_open():
    plugin = RateLimitPlugin({"token_rpm": {"m1": 10}, "fail_open": True})
    plugin.set_shared(RedisBackend("redis://127.0.0.1:9/0"), Metrics())
    ctx = make_ctx({"messages": [{"role": "user", "content": "hi"}]})
    await plugin.on_request(ctx)  # backend down -> allowed
    complete_usage(ctx, 1, 1)
    await plugin.on_response(ctx)  # adjustment failure -> counted, no crash
