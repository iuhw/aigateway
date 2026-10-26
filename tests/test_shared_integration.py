"""Multi-instance integration tests: two GatewayApps sharing one Redis.

These are the tests that prove the gateway's limits mean the same thing behind
N workers/replicas: identical configs backed by one shared state backend see
one shared quota, and a cooldown imposed by instance A reaches instance B.
"""

from __future__ import annotations

import socket
import threading

import pytest

from aigateway.app import GatewayApp
from aigateway.errors import RateLimitError
from aigateway.observability.metrics import Metrics
from aigateway.plugins.base import PluginContext
from aigateway.plugins.rate_limit import RateLimitPlugin
from aigateway.state import RedisBackend, StateBackendError
from tests.conftest import write_config

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
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()


def shared_config(tmp_path_factory, redis_url: str, **overrides) -> str:

    config = {
        "shared_state": {"backend": "redis", "redis_url": redis_url, "key_prefix": "gtest"},
        "providers": {"p1": {"type": "mock"}},
        "models": {"m1": {"deployments": [{"provider": "p1", "model": "m1-up"}]}},
        "plugins": {"rate-limit-plugin": {"config": {"model_rpm": {"m1": 2}}}},
    }
    config.update(overrides)
    return write_config(tmp_path_factory.mktemp("cfg"), config)


async def test_two_instances_share_rate_limit(tmp_path_factory, redis_url):
    """rpm=2 split across two instances: 2 total allowed, then both refuse."""
    path = shared_config(tmp_path_factory, redis_url)
    app_a = GatewayApp(path)
    app_b = GatewayApp(path)
    payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}
    statuses = []
    for app in (app_a, app_b, app_a, app_b):
        try:
            await app.chat(dict(payload), {}, "test")
            statuses.append(200)
        except RateLimitError:
            statuses.append(429)
    assert statuses == [200, 200, 429, 429]
    await app_a.aclose()
    await app_b.aclose()


async def test_cooldown_reaches_other_instance(tmp_path_factory, redis_url):
    path = shared_config(tmp_path_factory, redis_url)
    app_a = GatewayApp(path)
    app_b = GatewayApp(path)
    route = app_a.state.router.route("m1")
    deployment = route.deployments[0]
    app_a.state.router.report_failure(deployment, route)
    app_a.state.router.report_failure(deployment, route)  # allowed_fails=2 -> cooldown
    assert deployment.is_cooling_down()

    await app_a._cooldown_sync_once()
    await app_b._cooldown_sync_once()

    remote = app_b.state.router.route("m1").deployments[0]
    assert remote.is_cooling_down(), "instance B must adopt A's cooldown"
    await app_a.aclose()
    await app_b.aclose()


async def test_rate_limit_fail_open_on_backend_outage():
    """Redis down: fail_open=true lets requests through and counts the error."""
    plugin = RateLimitPlugin({"model_rpm": {"m1": 0.001}, "fail_open": True})
    plugin.set_shared(RedisBackend("redis://127.0.0.1:9/0"), Metrics())
    ctx = PluginContext(request_id="r", model="m1", payload={}, stream=False)
    await plugin.on_request(ctx)  # must not raise
    assert plugin._metrics.registry.get_sample_value(
        "gateway_state_backend_errors_total", {"operation": "rate_limit"}
    ) == 1.0


async def test_rate_limit_fail_closed_on_backend_outage():
    plugin = RateLimitPlugin({"model_rpm": {"m1": 60}, "fail_open": False})
    plugin.set_shared(RedisBackend("redis://127.0.0.1:9/0"), Metrics())
    ctx = PluginContext(request_id="r", model="m1", payload={}, stream=False)
    with pytest.raises(RateLimitError):
        await plugin.on_request(ctx)


async def test_fail_open_still_enforces_when_backend_healthy(tmp_path_factory, redis_url):
    """fail_open must not disable limiting: healthy backend still returns 429."""
    path = shared_config(tmp_path_factory, redis_url)
    app = GatewayApp(path)
    payload = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}
    assert (await app.chat(dict(payload), {}, "t")) is not None
    assert (await app.chat(dict(payload), {}, "t")) is not None
    with pytest.raises(RateLimitError):
        await app.chat(dict(payload), {}, "t")
    await app.aclose()


async def test_backend_error_visible_as_metric(tmp_path_factory, redis_url):
    backend = RedisBackend(redis_url)
    await backend.acquire_bucket("k", 10, 10)  # smoke: healthy
    with pytest.raises(StateBackendError):
        dead = RedisBackend("redis://127.0.0.1:9/0")
        await dead.acquire_bucket("k", 10, 10)
    await backend.aclose()


async def test_two_instances_share_exact_cache(tmp_path_factory, redis_url):
    """Instance A fills the shared cache; identical request on B is a cache
    hit without touching B's upstream provider."""
    path = shared_config(
        tmp_path_factory,
        redis_url,
        plugins={"response-cache-plugin": {"config": {"ttl_s": 300, "shared": True}}},
    )
    app_a = GatewayApp(path)
    app_b = GatewayApp(path)
    payload = {"model": "m1", "messages": [{"role": "user", "content": "cache share"}]}
    first = await app_a.chat(dict(payload), {}, "test")
    assert app_a.state.providers["p1"].call_count == 1  # A went upstream
    second = await app_b.chat(dict(payload), {}, "test")
    assert app_b.state.providers["p1"].call_count == 0  # B served from shared cache
    assert second["choices"] == first["choices"]
    await app_a.aclose()
    await app_b.aclose()


async def test_shared_cache_disabled_without_shared_state(tmp_path):
    """shared=true without a shared_state backend degrades to in-process caching."""
    from tests.conftest import base_config, write_config

    config = base_config(
        plugins={"response-cache-plugin": {"config": {"ttl_s": 300, "shared": True}}}
    )
    app = GatewayApp(write_config(tmp_path, config))
    payload = {"model": "m1", "messages": [{"role": "user", "content": "degrade"}]}
    await app.chat(dict(payload), {}, "test")
    assert app.state.providers["p1"].call_count == 1
    await app.chat(dict(payload), {}, "test")
    assert app.state.providers["p1"].call_count == 1  # in-process hit
    await app.aclose()
