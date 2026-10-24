"""Tests for auth, rate-limit, and cache plugins."""

from __future__ import annotations

import pytest

from aigateway.errors import AuthenticationError, PermissionDeniedError, RateLimitError
from aigateway.plugins.auth import AuthPlugin
from aigateway.plugins.base import PluginContext
from aigateway.plugins.rate_limit import RateLimitPlugin, TokenBucket
from aigateway.plugins.response_cache import ResponseCachePlugin, cache_key


def ctx(model="m1", headers=None, identity=None) -> PluginContext:
    return PluginContext(
        request_id="r", model=model, payload={"messages": []}, stream=False,
        headers=headers or {}, identity=identity,
    )


# ---- auth --------------------------------------------------------------------


def test_auth_missing_key_rejected():
    plugin = AuthPlugin({"api_keys": [{"key": "k1", "name": "a"}]})
    with pytest.raises(AuthenticationError):
        plugin.authenticate({})


def test_auth_invalid_key_rejected():
    plugin = AuthPlugin({"api_keys": [{"key": "k1", "name": "a"}]})
    with pytest.raises(AuthenticationError):
        plugin.authenticate({"Authorization": "Bearer wrong"})


def test_auth_valid_key_passes():
    plugin = AuthPlugin({"api_keys": [{"key": "sk-abc", "name": "team-a"}]})
    identity = plugin.authenticate({"authorization": "Bearer sk-abc"})
    assert identity["name"] == "team-a"


def test_auth_hashed_key_supported():
    import hashlib

    digest = hashlib.sha256(b"sk-hashed").hexdigest()
    plugin = AuthPlugin({"api_keys": [{"key": f"sha256:{digest}", "name": "h"}]})
    assert plugin.authenticate({"Authorization": "Bearer sk-hashed"})["name"] == "h"


def test_auth_allow_anonymous():
    plugin = AuthPlugin({"allow_anonymous": True})
    assert plugin.authenticate({})["name"] == "anonymous"


async def test_auth_model_restriction():
    plugin = AuthPlugin({"api_keys": [{"key": "k", "name": "a", "models": ["allowed"]}]})
    ok = ctx(model="allowed", headers={"Authorization": "Bearer k"})
    await plugin.on_request(ok)
    with pytest.raises(PermissionDeniedError):
        await plugin.on_request(ctx(model="denied", headers={"Authorization": "Bearer k"}))


# ---- rate limiting --------------------------------------------------------------


def test_token_bucket_basic():
    bucket = TokenBucket(rate_per_minute=60, capacity=1)
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False
    assert bucket.retry_after_s() > 0


async def test_rate_limit_model_rpm():
    plugin = RateLimitPlugin({"model_rpm": {"m1": 2}, "default_rpm": 1000})
    await plugin.on_request(ctx("m1"))
    await plugin.on_request(ctx("m1"))
    with pytest.raises(RateLimitError):
        await plugin.on_request(ctx("m1"))
    # other models unaffected
    await plugin.on_request(ctx("m2"))


async def test_rate_limit_identity_rpm():
    plugin = RateLimitPlugin({"identity_rpm": 1})
    await plugin.on_request(ctx(identity={"name": "u1"}))
    with pytest.raises(RateLimitError):
        await plugin.on_request(ctx(identity={"name": "u1"}))
    await plugin.on_request(ctx(identity={"name": "u2"}))


# ---- response cache ---------------------------------------------------------------


async def test_cache_hit_and_miss():
    plugin = ResponseCachePlugin({"ttl_s": 60})
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    context = PluginContext(request_id="r", model="m", payload=payload, stream=False)
    await plugin.on_request(context)
    assert context.cache_hit is False
    context.status_code = 200
    context.response = {"choices": [{"message": {"content": "cached!"}}]}
    await plugin.on_response(context)

    context2 = PluginContext(request_id="r2", model="m", payload=payload, stream=False)
    await plugin.on_request(context2)
    assert context2.cache_hit is True
    assert context2.response["choices"][0]["message"]["content"] == "cached!"


async def test_cache_key_distinguishes_inputs():
    a = cache_key("m", {"messages": [{"role": "user", "content": "a"}]})
    b = cache_key("m", {"messages": [{"role": "user", "content": "b"}]})
    c = cache_key("m2", {"messages": [{"role": "user", "content": "a"}]})
    d = cache_key("m", {"messages": [{"role": "user", "content": "a"}], "temperature": 0.1})
    assert len({a, b, c, d}) == 4


async def test_cache_ttl_expiry(monkeypatch):
    plugin = ResponseCachePlugin({"ttl_s": 1})
    payload = {"messages": [{"role": "user", "content": "x"}]}
    context = PluginContext(request_id="r", model="m", payload=payload, stream=False)
    await plugin.on_request(context)
    context.status_code = 200
    context.response = {"choices": [{"message": {"content": "v"}}]}
    await plugin.on_response(context)

    # simulate time travel
    key = cache_key("m", payload)
    deadline, _ = plugin._store[key]
    plugin._store[key] = (deadline - 10, _)
    context2 = PluginContext(request_id="r2", model="m", payload=payload, stream=False)
    await plugin.on_request(context2)
    assert context2.cache_hit is False


async def test_cache_max_entries_eviction():
    plugin = ResponseCachePlugin({"max_entries": 2})
    for i in range(3):
        payload = {"messages": [{"role": "user", "content": f"msg-{i}"}]}
        context = PluginContext(request_id=str(i), model="m", payload=payload, stream=False)
        await plugin.on_request(context)
        context.status_code = 200
        context.response = {"choices": [{"message": {"content": f"v{i}"}}]}
        await plugin.on_response(context)
    assert len(plugin._store) == 2
    assert cache_key("m", {"messages": [{"role": "user", "content": "msg-0"}]}) not in plugin._store
