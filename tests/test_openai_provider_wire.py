"""Wire-level fault-injection tests for the OpenAI-compatible provider.

Drives OpenAICompatibleProvider through httpx.MockTransport, i.e. real HTTP
semantics (status lines, chunked SSE bodies, connection resets) without a
network. Covers what deterministic mock providers cannot: malformed upstream
bodies, non-JSON errors, slow/aborted streams.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from aigateway.core.openai_provider import OpenAICompatibleProvider
from aigateway.core.provider import UpstreamStreamError, UpstreamUnavailable

PAYLOAD = {"messages": [{"role": "user", "content": "hi"}], "stream": False}


def sse_body(chunks: list[dict], *, garbage_lines: bool = False) -> bytes:
    lines = []
    if garbage_lines:
        lines += [": this is an sse comment", "event: whatever", "data: not-json", ""]
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode()


def make_provider(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "wire",
        base_url="http://upstream.test/v1",
        transport=httpx.MockTransport(handler),
    )


def completion_body(content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ---- non-streaming ------------------------------------------------------------


async def test_chat_success_parses_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        sent = json.loads(request.content)
        assert sent["model"] == "g" and sent["stream"] is False
        return httpx.Response(200, json=completion_body())

    provider = make_provider(handler)
    result = await provider.chat(model="g", payload=PAYLOAD, timeout=5)
    assert result.ok and result.body["id"] == "chatcmpl-1"


async def test_chat_invalid_json_body_is_wrapped_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>bad gateway</html>")

    provider = make_provider(handler)
    result = await provider.chat(model="g", payload=PAYLOAD, timeout=5)
    assert not result.ok and result.status == 502
    assert "upstream_invalid_body" in str(result.body)


async def test_chat_connect_error_maps_to_upstream_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = make_provider(handler)
    with pytest.raises(UpstreamUnavailable):
        await provider.chat(model="g", payload=PAYLOAD, timeout=5)


async def test_chat_timeout_maps_to_upstream_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    provider = make_provider(handler)
    with pytest.raises(UpstreamUnavailable):
        await provider.chat(model="g", payload=PAYLOAD, timeout=5)


async def test_chat_429_body_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "quota", "code": "rate_limited"}})

    provider = make_provider(handler)
    result = await provider.chat(model="g", payload=PAYLOAD, timeout=5)
    assert result.status == 429
    assert result.body["error"]["message"] == "quota"


# ---- streaming -----------------------------------------------------------------


async def _collect(provider, model="g", payload=None):
    chunks = []
    async for chunk in provider.stream(model=model, payload=payload or PAYLOAD, timeout=5):
        chunks.append(chunk)
    return chunks


async def test_stream_parses_sse_with_comments_and_done():
    chunks = [
        {"choices": [{"delta": {"content": "he"}}]},
        {"choices": [{"delta": {"content": "llo"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["stream"] is True
        return httpx.Response(200, content=sse_body(chunks, garbage_lines=True))

    provider = make_provider(handler)
    got = await _collect(provider)
    assert [c["choices"][0]["delta"].get("content") for c in got] == ["he", "llo", None]


async def test_stream_non_2xx_raises_stream_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    provider = make_provider(handler)
    with pytest.raises(UpstreamStreamError) as excinfo:
        await _collect(provider)
    assert excinfo.value.status_code == 503
    assert "overloaded" in excinfo.value.body_text


async def test_stream_mid_stream_connection_drop():
    """Connection dies mid-stream: caught as UpstreamUnavailable after some chunks."""

    def handler(request: httpx.Request) -> httpx.Response:
        async def body_stream():
            yield sse_body([{"choices": [{"delta": {"content": "partial"}}]}])
            raise httpx.RemoteProtocolError("peer closed connection")

        return httpx.Response(200, content=body_stream())

    provider = make_provider(handler)
    received = []
    with pytest.raises(UpstreamUnavailable):
        async for chunk in provider.stream(model="g", payload=PAYLOAD, timeout=5):
            received.append(chunk)
    assert len(received) == 1  # the first chunk made it out before the drop


async def test_stream_slow_chunks_preserve_order():
    def handler(request: httpx.Request) -> httpx.Response:
        async def body_stream():
            for text in ("a", "b", "c"):
                await asyncio.sleep(0.01)
                yield sse_body([{"choices": [{"delta": {"content": text}}]}])
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, content=body_stream())

    provider = make_provider(handler)
    got = await _collect(provider)
    assert [c["choices"][0]["delta"]["content"] for c in got] == ["a", "b", "c"]


async def test_stream_empty_body_is_empty_iteration():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    provider = make_provider(handler)
    assert await _collect(provider) == []
