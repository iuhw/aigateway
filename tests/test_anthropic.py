"""Anthropic Messages API compatibility tests (/v1/messages)."""

from __future__ import annotations

import json

import httpx
import pytest

from aigateway.anthropic_api import (
    error_body,
    stream_events,
    translate_request,
    translate_response,
)
from aigateway.api import create_app
from aigateway.errors import NotFoundError, RateLimitError
from tests.conftest import base_config, write_config


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def anthropic_body(**overrides) -> dict:
    body = {
        "model": "m1",
        "max_tokens": 128,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


# ---- request translation -------------------------------------------------------


def test_translate_request_minimal():
    payload = translate_request(anthropic_body())
    assert payload["model"] == "m1"
    assert payload["max_tokens"] == 128
    assert payload["messages"] == [{"role": "user", "content": "hello"}]
    assert "system" not in payload


def test_translate_request_system_and_blocks():
    payload = translate_request(
        anthropic_body(
            system="be brief",
            messages=[
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}],
                }
            ],
        )
    )
    assert payload["messages"][0] == {"role": "system", "content": "be brief"}
    assert payload["messages"][1]["content"] == "part1part2"


def test_translate_request_stop_sequences_and_sampling():
    payload = translate_request(
        anthropic_body(stop_sequences=["STOP"], temperature=0.5, top_p=0.9)
    )
    assert payload["stop"] == ["STOP"]
    assert payload["temperature"] == 0.5 and payload["top_p"] == 0.9


def test_translate_request_rejects_missing_max_tokens():
    body = anthropic_body()
    del body["max_tokens"]
    with pytest.raises(Exception, match="max_tokens"):
        translate_request(body)


def test_translate_request_rejects_tools():
    with pytest.raises(Exception, match="tools"):
        translate_request(anthropic_body(tools=[{"name": "x"}]))


def test_translate_request_rejects_images():
    body = anthropic_body(messages=[{"role": "user", "content": [{"type": "image", "source": {}}]}])
    with pytest.raises(Exception, match="unsupported content block"):
        translate_request(body)


def test_translate_request_rejects_bad_role():
    body = anthropic_body(messages=[{"role": "system", "content": "x"}])
    with pytest.raises(Exception, match="role"):
        translate_request(body)


# ---- response translation ---------------------------------------------------------


def test_translate_response_roundtrip():
    openai_body = {
        "id": "chatcmpl-9",
        "model": "m1-up",
        "choices": [{"message": {"role": "assistant", "content": "hi there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
    }
    body = translate_response(openai_body)
    assert body["id"] == "msg_chatcmpl-9"
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "hi there"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_stop_reason_length_maps_to_max_tokens():
    body = translate_response(
        {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}], "usage": {}}
    )
    assert body["stop_reason"] == "max_tokens"


def test_error_body_mapping():
    assert error_body(RateLimitError("slow down"))["error"]["type"] == "rate_limit_error"
    assert error_body(NotFoundError("nope"))["error"]["type"] == "not_found_error"
    assert error_body(Exception("boom"))["error"]["type"] == "api_error"


# ---- streaming translation ----------------------------------------------------------


async def _collect_events(chunks: list[dict], model="m1") -> list[tuple[str, dict]]:
    async def openai_sse():
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    events = []
    async for event in stream_events(openai_sse(), model=model, prompt_text="hello", max_tokens=128):
        lines = event.strip().splitlines()
        name = next(ln for ln in lines if ln.startswith("event: "))[7:]
        data = json.loads(next(ln for ln in lines if ln.startswith("data: "))[6:])
        events.append((name, data))
    return events


async def test_stream_event_sequence_and_shape():
    chunks = [
        {"choices": [{"delta": {"content": "he"}}]},
        {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
    ]
    events = await _collect_events(chunks)
    names = [name for name, _ in events]
    assert names == [
        "message_start", "content_block_start", "content_block_delta",
        "content_block_delta", "content_block_stop", "message_delta", "message_stop",
    ]
    start = events[0][1]["message"]
    assert start["role"] == "assistant" and start["usage"]["input_tokens"] > 0
    deltas = [d for n, d in events if n == "content_block_delta"]
    assert "".join(d["delta"]["text"] for d in deltas) == "hello"
    final = events[-2][1]
    assert final["delta"]["stop_reason"] == "end_turn"
    assert final["usage"]["output_tokens"] == 2  # upstream-reported usage


async def test_stream_mid_stream_error_event():
    chunks = [
        {"choices": [{"delta": {"content": "par"}}]},
        {"error": {"message": "upstream died", "type": "upstream_error", "code": "upstream_error"}},
    ]
    events = await _collect_events(chunks)
    assert events[-1][0] == "error"
    assert events[-1][1]["error"]["type"] == "api_error"
    # no message_stop after an error
    assert "message_stop" not in [n for n, _ in events]


# ---- end-to-end over HTTP -----------------------------------------------------------


async def test_http_non_stream_anthropic_roundtrip(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        response = await client.post("/v1/messages", json=anthropic_body())
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "message"
        assert "mock reply" in body["content"][0]["text"]
        assert body["stop_reason"] == "end_turn"
        assert body["usage"]["output_tokens"] > 0


async def test_http_stream_anthropic_events(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        events = []
        async with client.stream(
            "POST", "/v1/messages", json=anthropic_body(stream=True)
        ) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            current_event = None
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    current_event = line[7:]
                elif line.startswith("data: ") and current_event:
                    events.append((current_event, json.loads(line[6:])))
    names = [n for n, _ in events]
    assert "message_start" == names[0]
    assert "message_stop" == names[-1]
    deltas = [d for n, d in events if n == "content_block_delta"]
    assert "".join(d["delta"]["text"] for d in deltas if d["delta"].get("text")) != ""


async def test_http_anthropic_error_envelope(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        response = await client.post(
            "/v1/messages", json=anthropic_body(model="no-such-model")
        )
        assert response.status_code == 404
        body = response.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "not_found_error"


async def test_http_anthropic_auth_and_limits_apply(tmp_path):
    """The Anthropic surface goes through the same plugin chain."""
    config = base_config(
        plugins={
            "auth-plugin": {"config": {"api_keys": [{"key": "sk-a", "name": "a"}]}},
            "rate-limit-plugin": {"config": {"model_rpm": {"m1": 1}}},
        }
    )
    async with make_client(tmp_path, config) as client:
        unauthorized = await client.post("/v1/messages", json=anthropic_body())
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["type"] == "authentication_error"

        ok = await client.post(
            "/v1/messages", headers={"Authorization": "Bearer sk-a"}, json=anthropic_body()
        )
        assert ok.status_code == 200
        limited = await client.post(
            "/v1/messages", headers={"Authorization": "Bearer sk-a"}, json=anthropic_body()
        )
        assert limited.status_code == 429
        assert limited.json()["error"]["type"] == "rate_limit_error"
