"""End-to-end API tests over the FastAPI app (in-process, mock providers)."""

from __future__ import annotations

import json

import httpx
import pytest

from aigateway.api import create_app
from tests.conftest import base_config, write_config


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        yield client


async def test_models_listing(client):
    response = await client.get("/v1/models")
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()["data"]]
    assert ids == ["m1", "m2"]


async def test_chat_completion_non_streaming(client):
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "m1", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert "mock reply" in body["choices"][0]["message"]["content"]
    assert body["model"] == "m1-up"
    assert body["usage"]["total_tokens"] > 0


async def test_chat_completion_invalid_body(client):
    response = await client.post("/v1/chat/completions", json={"model": "m1", "messages": []})
    assert response.status_code == 400
    assert "error" in response.json()


async def test_unknown_model_404(client):
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found_error"


async def test_chat_streaming_sse(client):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "m1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        chunks = []
        async for line in response.aiter_lines():
            if line.startswith("data:") and "[DONE]" not in line:
                chunks.append(json.loads(line[5:]))
    assert len(chunks) >= 2
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks[:-1])
    assert "mock reply" in text


async def test_keyword_filter_blocks_request(tmp_path):
    config = base_config(
        plugins={"keyword-filter-plugin": {"config": {"words": ["badword"], "mode": "block"}}}
    )
    async with make_client(tmp_path, config) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "has badword inside"}]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "content_filter_rejected"
        # clean request still works
        ok = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "fine"}]},
        )
        assert ok.status_code == 200


async def test_keyword_filter_masks_response_non_stream(tmp_path):
    config = base_config(
        plugins={
            "keyword-filter-plugin": {
                "config": {"words": ["secret"], "mode": "mask", "check": ["response"]}
            }
        },
        # make provider emit the keyword
        providers={
            "p1": {"type": "mock", "content_template": "the {echo} secret value"},
            "p2": {"type": "mock"},
        },
    )
    async with make_client(tmp_path, config) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert "secret" not in response.json()["choices"][0]["message"]["content"]


async def test_keyword_filter_blocks_response_non_stream(tmp_path):
    config = base_config(
        plugins={
            "keyword-filter-plugin": {
                "config": {"words": ["evil"], "mode": "block", "check": ["response"]}
            }
        },
        providers={
            "p1": {"type": "mock", "content_template": "something evil"},
            "p2": {"type": "mock"},
        },
    )
    async with make_client(tmp_path, config) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "content_filter_rejected"


async def test_auth_rejects_missing_key(tmp_path):
    config = base_config(
        plugins={"auth-plugin": {"config": {"api_keys": [{"key": "sk-x", "name": "a"}]}}}
    )
    async with make_client(tmp_path, config) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 401
        ok = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-x"},
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert ok.status_code == 200


async def test_rate_limit_returns_429(tmp_path):
    config = base_config(
        plugins={"rate-limit-plugin": {"config": {"model_rpm": {"m1": 1}}}},
    )
    async with make_client(tmp_path, config) as client:
        first = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert first.status_code == 200
        second = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limit_exceeded"


async def test_response_cache_serves_same_body(tmp_path):
    config = base_config(plugins={"response-cache-plugin": {"config": {"ttl_s": 60}}})
    async with make_client(tmp_path, config) as client:
        payload = {"model": "m1", "messages": [{"role": "user", "content": "cache me"}]}
        first = (await client.post("/v1/chat/completions", json=payload)).json()
        second = (await client.post("/v1/chat/completions", json=payload)).json()
        assert first["id"] == second["id"]


async def test_failover_between_deployments(tmp_path):
    config = base_config(
        providers={
            "p1": {"type": "mock", "fail_next_n": 99, "fail_status": 500},
            "p2": {"type": "mock"},
        },
        models={
            "m1": {
                "deployments": [
                    {"provider": "p1", "model": "m1-up"},
                    {"provider": "p2", "model": "m1-up2"},
                ],
                "retries": 0,
            },
            "m2": {"deployments": [{"provider": "p2", "model": "m2-up"}]},
        },
    )
    async with make_client(tmp_path, config) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json()["model"] == "m1-up2"


async def test_metrics_and_admin_endpoints(client):
    await client.post(
        "/v1/chat/completions",
        json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
    )
    metrics = (await client.get("/metrics")).text
    assert "gateway_requests_total" in metrics
    routes = (await client.get("/admin/routes")).json()["routes"]
    assert "m1" in routes
    plugins = (await client.get("/admin/plugins")).json()["plugins"]
    assert isinstance(plugins, list)


async def test_health_endpoints(client):
    assert (await client.get("/healthz")).json()["status"] == "ok"
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["providers"]["p1"] is True


async def test_request_log_plugin_writes_file(tmp_path):
    log_file = tmp_path / "requests.jsonl"
    config = base_config(
        plugins={
            "request-log-plugin": {"config": {"path": str(log_file), "log_prompt": True}}
        }
    )
    async with make_client(tmp_path, config) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "logged"}]},
        )
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    events = [line["event"] for line in lines]
    assert "request" in events and "response" in events
    request_line = next(line for line in lines if line["event"] == "request")
    assert "logged" in request_line["payload"]
