"""Semantic cache tests: partitioning, threshold boundaries, fail-open, E2E.

The mock provider derives embeddings from SHA-256 of the input text, so:
identical text -> cosine 1.0 (hit); different text -> unrelated vector (miss).
Threshold boundaries use the plugin directly with hand-built vectors.
"""

from __future__ import annotations

import httpx

from aigateway.api import create_app
from aigateway.app import GatewayApp
from aigateway.plugins.semantic_cache import SemanticCachePlugin, messages_text, partition_key
from aigateway.state import InMemoryBackend
from tests.conftest import base_config, write_config

# ---- unit: partitioning & text extraction --------------------------------------------


def test_partition_key_ignores_prompt_but_not_params():
    base = {"messages": [{"role": "user", "content": "hello"}]}
    different_prompt = {**base, "messages": [{"role": "user", "content": "different"}]}
    assert partition_key("m", base) == partition_key("m", dict(base))
    # different prompt text, same shape -> SAME partition (text varies within it)
    assert partition_key("m", base) == partition_key("m", different_prompt)
    assert partition_key("m", base) != partition_key("m", {**base, "temperature": 0.7})
    assert partition_key("m", base) != partition_key("m", {**base, "seed": 42})
    assert partition_key("m", base) != partition_key("m", {**base, "max_tokens": 99})
    assert partition_key("m", base) != partition_key("other", base)
    assert partition_key("m", base) != partition_key("m", {**base, "tools": [{"type": "function"}]})


def test_messages_text_joins_all_roles():
    payload = {
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    }
    assert messages_text(payload) == "be terse\nhi\nhello"
    assert messages_text({"messages": []}) == ""


def test_missing_embed_model_self_disables():
    plugin = SemanticCachePlugin({"ttl_s": 60})
    assert plugin.enabled is False
    plugin = SemanticCachePlugin({"embed_model": "embed"})
    assert plugin.enabled is True


def test_invalid_threshold_clamped():
    plugin = SemanticCachePlugin({"embed_model": "embed", "similarity_threshold": 1.7})
    assert plugin.threshold == 0.95
    plugin = SemanticCachePlugin({"embed_model": "embed", "similarity_threshold": -1})
    assert plugin.threshold == 0.95


# ---- unit: plugin behavior with a fake embed_fn ---------------------------------------


def make_plugin(**config) -> SemanticCachePlugin:
    plugin = SemanticCachePlugin({"embed_model": "embed", "ttl_s": 300, **config})
    vectors = {"same meaning": [1.0, 0.0], "same meaning paraphrased": [0.99, 0.14], "unrelated": [0.0, 1.0]}

    async def embed(texts):
        return [vectors.get(text, [0.98, 0.2]) for text in texts]  # unknown text ~ near "paraphrased"

    plugin.set_embed_fn(embed)
    return plugin


async def test_plugin_hit_on_same_text_and_threshold_boundary():
    plugin = make_plugin(similarity_threshold=0.95)
    payload = {"messages": [{"role": "user", "content": "same meaning"}]}

    ctx_fill = _ctx(payload)
    await plugin.on_request(ctx_fill)
    assert not ctx_fill.cache_hit  # miss on empty store
    ctx_fill.response = {"answer": "Paris"}
    ctx_fill.status_code = 200
    await plugin.on_response(ctx_fill)

    ctx_hit = _ctx(payload)
    await plugin.on_request(ctx_hit)
    assert ctx_hit.cache_hit and ctx_hit.response == {"answer": "Paris"}

    # cosine([1,0],[0.99,0.14]) ~= 0.990 > 0.95 -> paraphrase hits
    ctx_paraphrase = _ctx({"messages": [{"role": "user", "content": "same meaning paraphrased"}]})
    await plugin.on_request(ctx_paraphrase)
    assert ctx_paraphrase.cache_hit

    # cosine([1,0],[0.0,1.0]) = 0 < 0.95 -> unrelated misses
    ctx_unrelated = _ctx({"messages": [{"role": "user", "content": "unrelated"}]})
    await plugin.on_request(ctx_unrelated)
    assert not ctx_unrelated.cache_hit


async def test_plugin_threshold_boundary_excluded():
    # threshold 0.999: paraphrase (0.990) must NOT hit
    plugin = make_plugin(similarity_threshold=0.999)
    payload = {"messages": [{"role": "user", "content": "same meaning"}]}
    ctx_fill = _ctx(payload)
    await plugin.on_request(ctx_fill)
    ctx_fill.response, ctx_fill.status_code = {"a": 1}, 200
    await plugin.on_response(ctx_fill)

    ctx = _ctx({"messages": [{"role": "user", "content": "same meaning paraphrased"}]})
    await plugin.on_request(ctx)
    assert not ctx.cache_hit  # 0.990 < 0.999


async def test_plugin_partition_isolation():
    plugin = make_plugin()
    fill = _ctx({"messages": [{"role": "user", "content": "same meaning"}]})
    await plugin.on_request(fill)
    fill.response, fill.status_code = {"a": 1}, 200
    await plugin.on_response(fill)

    # same text but different temperature -> different partition -> no hit
    other = _ctx({"messages": [{"role": "user", "content": "same meaning"}], "temperature": 0.9})
    await plugin.on_request(other)
    assert not other.cache_hit


async def test_plugin_skips_streaming_and_embeddings_requests():
    plugin = make_plugin()
    stream_ctx = _ctx({"messages": [{"role": "user", "content": "same meaning"}]})
    stream_ctx.stream = True
    await plugin.on_request(stream_ctx)
    assert not stream_ctx.cache_hit and stream_ctx.state == {}

    embed_ctx = _ctx({"input": "same meaning"})  # embeddings request shape
    await plugin.on_request(embed_ctx)
    assert not embed_ctx.cache_hit and embed_ctx.state == {}


async def test_plugin_fail_open_when_embed_returns_none():
    plugin = make_plugin()

    async def dead_embed(texts):
        return None

    plugin.set_embed_fn(dead_embed)
    ctx = _ctx({"messages": [{"role": "user", "content": "anything"}]})
    await plugin.on_request(ctx)
    assert not ctx.cache_hit and ctx.state == {}  # passthrough, no crash


async def test_plugin_in_memory_capacity_eviction():
    plugin = make_plugin(max_entries=1)
    for text in ("first", "second"):
        ctx = _ctx({"messages": [{"role": "user", "content": text}]})
        await plugin.on_request(ctx)
        ctx.response, ctx.status_code = {"t": text}, 200
        await plugin.on_response(ctx)
    assert plugin._entry_count() == 1  # oldest evicted


def _ctx(payload):
    from aigateway.plugins.base import PluginContext

    return PluginContext(
        request_id="r", model="m1", payload=payload, stream=False
    )


# ---- backend vector search ------------------------------------------------------------


async def test_in_memory_vector_search_threshold_and_expiry(monkeypatch):
    backend = InMemoryBackend()
    await backend.vector_put("ns", "k1", [1.0, 0.0], {"answer": 1}, ttl_s=60)
    hits = await backend.vector_search("ns", [0.99, 0.14], threshold=0.9)
    assert len(hits) == 1 and hits[0][2] > 0.9 and hits[0][1] == {"answer": 1}
    assert (await backend.vector_search("ns", [0.0, 1.0], threshold=0.9)) == []

    import aigateway.state as state_mod

    real = state_mod.time.monotonic
    monkeypatch.setattr(state_mod.time, "monotonic", lambda: real() + 61)
    assert (await backend.vector_search("ns", [1.0, 0.0], threshold=0.9)) == []


async def test_in_memory_vector_namespace_isolation():
    backend = InMemoryBackend()
    await backend.vector_put("ns-a", "k", [1.0], {"from": "a"}, ttl_s=60)
    assert (await backend.vector_search("ns-b", [1.0], threshold=0.5)) == []
    assert (await backend.vector_search("ns-a", [1.0], threshold=0.5))[0][1] == {"from": "a"}


# ---- E2E through the gateway ----------------------------------------------------------


def semantic_config(**overrides):
    config = base_config(
        providers={
            "p1": {"type": "mock", "latency_s": 0.0},
            "pembed": {"type": "mock", "latency_s": 0.0},
        },
        models={
            "m1": {"deployments": [{"provider": "p1", "model": "m1-up"}]},
            "embed-mdl": {"deployments": [{"provider": "pembed", "model": "embed-up"}]},
        },
        plugins={
            "semantic-cache-plugin": {
                "config": {"embed_model": "embed-mdl", "similarity_threshold": 0.99, "ttl_s": 300}
            }
        },
    )
    config.update(overrides)
    return config


async def test_e2e_semantic_hit_and_upstream_savings(tmp_path):
    app = GatewayApp(write_config(tmp_path, semantic_config()))
    try:
        ask = {"model": "m1", "messages": [{"role": "user", "content": "same meaning"}]}
        first = await app.chat(dict(ask), {}, "test")
        assert app.state.providers["p1"].call_count == 1  # went upstream
        second = await app.chat(dict(ask), {}, "test")
        assert app.state.providers["p1"].call_count == 1  # semantic hit, no upstream
        assert second["choices"] == first["choices"]

        # different content: mock SHA embeddings are unrelated -> miss -> upstream
        other = {"model": "m1", "messages": [{"role": "user", "content": "completely different subject"}]}
        await app.chat(dict(other), {}, "test")
        assert app.state.providers["p1"].call_count == 2
    finally:
        await app.aclose()


async def test_e2e_semantic_hit_metric(tmp_path):
    path = write_config(tmp_path, semantic_config())
    app = create_app(path, config_watcher=False)
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            ask = {"model": "m1", "messages": [{"role": "user", "content": "same meaning"}]}
            await client.post("/v1/chat/completions", json=ask)
            await client.post("/v1/chat/completions", json=ask)
            metrics = (await client.get("/metrics")).text
            assert "gateway_semantic_cache_hits_total" in metrics
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()


async def test_e2e_semantic_cache_across_instances(tmp_path_factory):
    import socket as socket_mod
    import threading

    from fakeredis import TcpFakeServer

    with socket_mod.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = TcpFakeServer(("127.0.0.1", port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        config = semantic_config()
        config["shared_state"] = {"backend": "redis", "redis_url": f"redis://127.0.0.1:{port}/0"}
        path = write_config(tmp_path_factory.mktemp("sc"), config)
        app_a = GatewayApp(path)
        app_b = GatewayApp(path)
        try:
            ask = {"model": "m1", "messages": [{"role": "user", "content": "same meaning"}]}
            await app_a.chat(dict(ask), {}, "test")
            assert app_a.state.providers["p1"].call_count == 1
            result_b = await app_b.chat(dict(ask), {}, "test")
            assert app_b.state.providers["p1"].call_count == 0  # served from A's fill
            assert "choices" in result_b
        finally:
            await app_a.aclose()
            await app_b.aclose()
    finally:
        server.shutdown()


async def test_e2e_semantic_cache_not_configured_changes_nothing(tmp_path):
    app = GatewayApp(write_config(tmp_path, base_config()))
    try:
        ask = {"model": "m1", "messages": [{"role": "user", "content": "same meaning"}]}
        await app.chat(dict(ask), {}, "test")
        assert app.state.providers["p1"].call_count == 1
        await app.chat(dict(ask), {}, "test")
        assert app.state.providers["p1"].call_count == 2  # no caching of any kind
    finally:
        await app.aclose()
