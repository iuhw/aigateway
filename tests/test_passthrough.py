"""The /v1/upstream passthrough tunnel (v1.2).

Covers: default-off, identity governance (auth/whitelist), raw relay of
status/body/headers, model rewriting in JSON bodies, query forwarding, SSE
relay, unsupported provider types, and the passthrough metric.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aigateway.api import create_app
from aigateway.core.openai_provider import OpenAICompatibleProvider
from tests.conftest import base_config, write_config

UPSTREAM = "http://upstream.test/v1"


def passthrough_config(**overrides: Any) -> dict[str, Any]:
    config = base_config(
        server={"passthrough": {"enabled": True}},
        providers={
            "p1": {"type": "openai", "base_url": UPSTREAM, "api_key": "sk-up"},
            "p2": {"type": "mock", "latency_s": 0.0},
        },
    )
    config.update(overrides)
    return config


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def app_with_handler(tmp_path, handler, **overrides: Any) -> httpx.AsyncClient:
    """Client whose p1 openai provider talks to an in-process MockTransport."""
    client = make_client(tmp_path, passthrough_config(**overrides))
    gateway = client._transport.app.state.gateway  # type: ignore[attr-defined]
    gateway.state.providers["p1"] = OpenAICompatibleProvider(
        "p1", base_url=UPSTREAM, api_key="sk-up", transport=httpx.MockTransport(handler)
    )
    return client


async def test_disabled_by_default_404(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        response = await client.post(
            "/v1/upstream/m1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 404
    assert "passthrough" in response.json()["error"]["message"]


async def test_disabled_via_config_404(tmp_path):
    config = passthrough_config(server={"passthrough": {"enabled": False}})
    async with make_client(tmp_path, config) as client:
        response = await client.post("/v1/upstream/m1/chat/completions", json={"model": "m1"})
    assert response.status_code == 404


async def test_unknown_model_404(tmp_path):
    async with app_with_handler(tmp_path, lambda request: httpx.Response(200, json={})) as client:
        response = await client.post("/v1/upstream/nope/chat/completions", json={"model": "nope"})
    assert response.status_code == 404


async def test_relays_path_rewrites_model_and_sends_upstream_auth(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"served": json.loads(request.content)["model"]})

    async with app_with_handler(tmp_path, handler) as client:
        response = await client.post(
            "/v1/upstream/m1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2},
        )
    assert response.status_code == 200
    assert response.json() == {"served": "m1-up"}
    request = seen[0]
    assert str(request.url) == f"{UPSTREAM}/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-up"
    body = json.loads(request.content)
    assert body["model"] == "m1-up"  # virtual name rewritten to the upstream model
    assert body["temperature"] == 0.2


async def test_non_dict_json_body_relayed_verbatim(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=request.content)

    async with app_with_handler(tmp_path, handler) as client:
        response = await client.post("/v1/upstream/m1/things", json=[1, 2, 3])
    assert response.status_code == 200
    assert response.json() == [1, 2, 3]
    assert seen[0].content == b"[1,2,3]"  # not a dict: no model rewrite, bytes untouched


async def test_invalid_json_body_400(tmp_path):
    async with app_with_handler(tmp_path, lambda request: httpx.Response(200)) as client:
        response = await client.post(
            "/v1/upstream/m1/chat/completions", content=b"{nope", headers={"content-type": "application/json"}
        )
    assert response.status_code == 400


async def test_query_forwarded_on_get(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"files": []})

    async with app_with_handler(tmp_path, handler) as client:
        response = await client.get("/v1/upstream/m1/files", params={"limit": "5"})
    assert response.status_code == 200
    assert seen[0].method == "GET"
    assert seen[0].url.params["limit"] == "5"


async def test_upstream_error_relayed_as_is(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"type": "not_found_error", "message": "nope"}})

    async with app_with_handler(tmp_path, handler) as client:
        response = await client.post("/v1/upstream/m1/whatever", json={"model": "m1"})
    # tunnel semantics: upstream status/body pass through unmodified
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found_error"


async def test_sse_stream_relayed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"delta": "a"}\n\ndata: {"delta": "b"}\n\ndata: [DONE]\n\n',
        )

    async with app_with_handler(tmp_path, handler) as client:
        async with client.stream("POST", "/v1/upstream/m1/responses", json={"model": "m1"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            lines = [line async for line in response.aiter_lines() if line.startswith("data:")]
    assert lines[-1] == "data: [DONE]"
    assert len(lines) == 3


async def test_auth_required(tmp_path):
    config = passthrough_config(
        plugins={"auth-plugin": {"config": {"api_keys": [{"key": "sk-x", "name": "a"}]}}}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with app_with_handler(tmp_path, handler, plugins=config["plugins"]) as client:
        denied = await client.post("/v1/upstream/m1/chat/completions", json={"model": "m1"})
        assert denied.status_code == 401
        allowed = await client.post(
            "/v1/upstream/m1/chat/completions",
            headers={"Authorization": "Bearer sk-x"},
            json={"model": "m1"},
        )
        assert allowed.status_code == 200


async def test_model_whitelist_enforced(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    plugins = {
        "auth-plugin": {
            "config": {"api_keys": [{"key": "sk-x", "name": "a", "models": ["m2"]}]}
        }
    }
    async with app_with_handler(tmp_path, handler, plugins=plugins) as client:
        response = await client.post(
            "/v1/upstream/m1/chat/completions",
            headers={"Authorization": "Bearer sk-x"},
            json={"model": "m1"},
        )
    assert response.status_code == 403


async def test_unsupported_provider_400(tmp_path):
    # m2 is served by the mock provider, which has no raw-request support
    async with app_with_handler(tmp_path, lambda request: httpx.Response(200)) as client:
        response = await client.post("/v1/upstream/m2/thing", json={"model": "m2"})
    assert response.status_code == 400
    assert "passthrough" in response.json()["error"]["message"]


async def test_unhealthy_deployment_503(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with app_with_handler(tmp_path, handler) as client:
        gateway = client._transport.app.state.gateway  # type: ignore[attr-defined]
        route = gateway.state.router.route("m1")
        for deployment in route.deployments:
            gateway.state.router.impose_cooldown("m1", deployment.key, 60.0)
        response = await client.post("/v1/upstream/m1/chat/completions", json={"model": "m1"})
    assert response.status_code == 503


async def test_passthrough_metric_counts(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with app_with_handler(tmp_path, handler) as client:
        await client.post("/v1/upstream/m1/chat/completions", json={"model": "m1"})
        metrics = (await client.get("/metrics")).text
    line = next(
        line
        for line in metrics.splitlines()
        if line.startswith("gateway_passthrough_requests_total{")
    )
    assert 'model="m1"' in line
    assert line.endswith("1.0")


@pytest.mark.parametrize("path", ["chat/completions", "a/b/c", "responses"])
async def test_nested_subpaths_relayed(tmp_path, path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"path": request.url.path})

    async with app_with_handler(tmp_path, handler) as client:
        response = await client.post(f"/v1/upstream/m1/{path}", json={"model": "m1"})
    assert response.status_code == 200
    assert seen[0].url.path == f"/v1/{path}"
