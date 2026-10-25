"""/v1/embeddings endpoint tests (pipeline reuse, caching, failover, limits)."""

from __future__ import annotations

import httpx
import pytest

from aigateway.api import create_app
from aigateway.app import GatewayApp
from aigateway.core.dispatcher import Dispatcher
from aigateway.core.provider import Provider, ProviderResult
from aigateway.core.router import ModelRoute
from tests.conftest import base_config, write_config


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_embeddings_single_and_batch(tmp_path):
    client = make_client(tmp_path, base_config())
    try:
        response = await client.post("/v1/embeddings", json={"model": "m1", "input": "hello world"})
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "list"
        assert len(body["data"]) == 1
        assert body["data"][0]["object"] == "embedding"
        assert len(body["data"][0]["embedding"]) == 8
        assert body["usage"]["prompt_tokens"] >= 1

        response = await client.post(
            "/v1/embeddings", json={"model": "m1", "input": ["alpha", "beta", "gamma"]}
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["data"]) == 3
        assert [d["index"] for d in body["data"]] == [0, 1, 2]
        vectors = [d["embedding"] for d in body["data"]]
        assert vectors[0] != vectors[1] != vectors[2]  # deterministic per input
    finally:
        await client.aclose()


async def test_embeddings_deterministic(tmp_path):
    client = make_client(tmp_path, base_config())
    try:
        first = (await client.post("/v1/embeddings", json={"model": "m1", "input": "same"})).json()
        second = (await client.post("/v1/embeddings", json={"model": "m1", "input": "same"})).json()
        assert first["data"][0]["embedding"] == second["data"][0]["embedding"]
    finally:
        await client.aclose()


async def test_embeddings_unknown_model_404(tmp_path):
    client = make_client(tmp_path, base_config())
    try:
        response = await client.post("/v1/embeddings", json={"model": "nope", "input": "x"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found_error"
    finally:
        await client.aclose()


async def test_embeddings_invalid_body_400(tmp_path):
    client = make_client(tmp_path, base_config())
    try:
        response = await client.post("/v1/embeddings", content=b"not json")
        assert response.status_code == 400
        response = await client.post("/v1/embeddings", json={"model": "m1"})
        assert response.status_code == 400  # missing input
    finally:
        await client.aclose()


async def test_embeddings_response_cache_hit(tmp_path):
    config = base_config(plugins={"response-cache-plugin": {"config": {"ttl_s": 60}}})
    client = make_client(tmp_path, config)
    try:
        first = await client.post("/v1/embeddings", json={"model": "m1", "input": "cache me"})
        second = await client.post("/v1/embeddings", json={"model": "m1", "input": "cache me"})
        other = await client.post("/v1/embeddings", json={"model": "m1", "input": "different"})
        assert first.json() == second.json()  # cache hit: identical body
        assert other.json()["data"][0]["embedding"] != first.json()["data"][0]["embedding"]
        metrics = await client.get("/metrics")
        assert 'gateway_cache_requests_total{model="m1",result="hit"}' in metrics.text
    finally:
        await client.aclose()


async def test_embeddings_limit_413(tmp_path):
    config = base_config()
    config["server"] = {"max_message_chars": 10}
    client = make_client(tmp_path, config)
    try:
        response = await client.post("/v1/embeddings", json={"model": "m1", "input": "x" * 100})
        assert response.status_code == 413
    finally:
        await client.aclose()


class ChatOnlyProvider(Provider):
    """Provider without embeddings support (base-class default)."""

    def __init__(self):
        super().__init__("chat-only")
        self.embed_called = False

    async def chat(self, *, model, payload, timeout):
        return ProviderResult(status=200, body={"choices": []}, latency_s=0.0)

    def stream(self, *, model, payload, timeout):
        raise NotImplementedError

    async def health(self):
        return True


class EmbedProvider(ChatOnlyProvider):
    def __init__(self):
        super().__init__()
        self.name = "embedder"

    async def embed(self, *, model, payload, timeout):
        self.embed_called = True
        return ProviderResult(
            status=200,
            body={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.5]}],
                "model": model,
            },
            latency_s=0.0,
        )


async def test_dispatcher_fails_over_to_embedding_capable_deployment():
    chat_only = ChatOnlyProvider()
    embedder = EmbedProvider()
    from aigateway.core.router import Deployment

    route = ModelRoute(
        name="e",
        deployments=[Deployment(provider="chat-only", model="e"), Deployment(provider="embedder", model="e")],
    )
    from aigateway.core.router import Router

    dispatcher = Dispatcher(
        router=Router({"e": route}), providers={"chat-only": chat_only, "embedder": embedder}
    )
    result = await dispatcher.execute_embedding(route, {"input": "x"})
    assert result.body["data"][0]["embedding"] == [0.5]
    assert embedder.embed_called


async def test_all_deployments_lack_embedding_support_503(tmp_path):
    config = base_config(models={"m1": {"deployments": [{"provider": "p1", "model": "m1"}]}})
    app = GatewayApp(write_config(tmp_path, config))
    try:
        from aigateway.core.provider import Provider as _P

        class NoEmbed(_P):
            async def chat(self, *, model, payload, timeout):
                return ProviderResult(status=200, body={}, latency_s=0.0)

            def stream(self, *, model, payload, timeout):
                raise NotImplementedError

            async def health(self):
                return True

        app.state.providers["p1"] = NoEmbed("p1")
        with pytest.raises(Exception, match="all deployments failed"):
            await app.embeddings({"model": "m1", "input": "x"}, {}, "")
    finally:
        await app.aclose()


async def test_embeddings_metrics_recorded(tmp_path):
    config = base_config()
    client = make_client(tmp_path, config)
    try:
        await client.post("/v1/embeddings", json={"model": "m1", "input": "metric me"})
        metrics = await client.get("/metrics")
        assert "gateway_requests_total" in metrics.text
        assert "gateway_tokens_total" in metrics.text
    finally:
        await client.aclose()


def test_embedding_request_payload_and_text():
    from aigateway.schemas import EmbeddingRequest, embedding_request_text

    request = EmbeddingRequest(model="m", input=["a", "b"], encoding_format="float")
    payload = request.upstream_payload()
    assert "model" not in payload
    assert payload["input"] == ["a", "b"]
    assert embedding_request_text(payload) == "a\nb"
    assert embedding_request_text({"input": "solo"}) == "solo"
    assert embedding_request_text({"input": [[1, 2, 3]]}) == "1 2 3"


def test_cache_key_separates_embedding_inputs():
    from aigateway.plugins.response_cache import cache_key

    assert cache_key("m", {"input": "a"}) != cache_key("m", {"input": "b"})
    assert cache_key("m", {"input": "a"}) == cache_key("m", {"input": "a"})


async def test_embeddings_passes_through_plugin_auth(tmp_path):
    config = base_config(
        plugins={
            "auth-plugin": {
                "config": {"api_keys": [{"key": "sk-x", "name": "t", "models": ["m1"]}]},
            }
        }
    )
    client = make_client(tmp_path, config)
    try:
        denied = await client.post("/v1/embeddings", json={"model": "m2", "input": "x"})
        assert denied.status_code == 401 or denied.status_code == 403
        ok = await client.post(
            "/v1/embeddings", json={"model": "m1", "input": "x"}, headers={"Authorization": "Bearer sk-x"}
        )
        assert ok.status_code == 200
    finally:
        await client.aclose()
