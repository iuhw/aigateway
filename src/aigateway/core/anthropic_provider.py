"""Native Anthropic Messages API upstream provider.

The gateway's internal protocol is OpenAI chat completions; this provider
translates outbound requests to the Anthropic Messages API and inbound
responses (including SSE events) back to OpenAI format. It is the inverse of
`aigateway.anthropic_api` (which serves Anthropic *clients*), so one
deployment chain can mix Anthropic and OpenAI upstreams behind a single
public model.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aigateway.core.provider import (
    Provider,
    ProviderResult,
    UpstreamStreamError,
    UpstreamUnavailable,
)
from aigateway.schemas import message_text

ANTHROPIC_STOP_REASON_TO_OPENAI = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
    "refusal": "content_filter",
}

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_API_VERSION = "2023-06-01"


def to_anthropic_request(
    payload: dict[str, Any], *, default_max_tokens: int = 4096
) -> dict[str, Any]:
    """OpenAI chat-completions payload -> Anthropic Messages request body."""
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []

    def _append(role: str, content: list[dict[str, Any]]) -> None:
        anthropic_messages.append({"role": role, "content": content or [{"type": "text", "text": ""}]})

    for message in payload.get("messages") or []:
        role = message.get("role")
        if role == "system":
            system_parts.append(message_text(message))
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": message.get("content") or "",
            }
            # Consecutive tool results belong to one user turn in Anthropic.
            if anthropic_messages and anthropic_messages[-1]["role"] == "user":
                last_blocks = anthropic_messages[-1]["content"]
                if last_blocks and all(b.get("type") == "tool_result" for b in last_blocks):
                    last_blocks.append(block)
                    continue
            _append("user", [block])
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = message.get("content")
            if text:
                blocks.append(
                    {"type": "text", "text": text if isinstance(text, str) else message_text(message)}
                )
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": arguments if isinstance(arguments, dict) else {},
                    }
                )
            _append("assistant", blocks)
            continue
        _append("user", [{"type": "text", "text": message_text(message)}])

    fallback = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    body: dict[str, Any] = {"messages": anthropic_messages or fallback}
    if system_parts:
        body["system"] = "\n".join(system_parts)
    body["max_tokens"] = int(payload.get("max_tokens") or default_max_tokens)
    for key in ("temperature", "top_p"):
        if payload.get(key) is not None:
            body[key] = payload[key]
    if payload.get("stop"):
        body["stop_sequences"] = payload["stop"]
    tools = payload.get("tools")
    if tools:
        body["tools"] = [
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
            for function in (
                (tool.get("function") or {}) if tool.get("type") == "function" else tool for tool in tools
            )
        ]
    tool_choice = payload.get("tool_choice")
    if tool_choice == "auto":
        body["tool_choice"] = {"type": "auto"}
    elif tool_choice == "required":
        body["tool_choice"] = {"type": "any"}
    elif tool_choice == "none":
        body["tool_choice"] = {"type": "none"}
    elif isinstance(tool_choice, dict) and (tool_choice.get("function") or {}).get("name"):
        body["tool_choice"] = {"type": "tool", "name": tool_choice["function"]["name"]}
    return body


def from_anthropic_response(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Anthropic message body -> OpenAI chat-completion body."""
    blocks = body.get("content") or []
    text = "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
    tool_calls = [
        {
            "id": str(b.get("id") or ""),
            "type": "function",
            "function": {
                "name": str(b.get("name") or ""),
                "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
            },
        }
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = body.get("usage") or {}
    return {
        "id": f"chatcmpl-{str(body.get('id') or 'anthropic')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": ANTHROPIC_STOP_REASON_TO_OPENAI.get(body.get("stop_reason"), "stop"),
            }
        ],
        "usage": _openai_usage(
            input_tokens=usage.get("input_tokens"),
            cache_read=usage.get("cache_read_input_tokens"),
            cache_write=usage.get("cache_creation_input_tokens"),
            completion_tokens=usage.get("output_tokens"),
        ),
    }


def _openai_usage(
    *,
    input_tokens: int | None,
    cache_read: int | None,
    cache_write: int | None,
    completion_tokens: int | None,
) -> dict[str, int]:
    """OpenAI-style usage where prompt_tokens covers the whole prompt, with the
    cached portions broken out (`cache_read_tokens` / `cache_write_tokens` —
    the gateway convention for tiered pricing). Anthropic/Bedrock report the
    uncached input separately from cache reads/writes; OpenAI/Gemini include
    cache reads in prompt_tokens already."""
    input_tokens = int(input_tokens or 0)
    cache_read = int(cache_read or 0)
    cache_write = int(cache_write or 0)
    completion = int(completion_tokens or 0)
    prompt = input_tokens + cache_read + cache_write
    usage: dict[str, int] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cache_read or cache_write:
        usage["cache_read_tokens"] = cache_read
        usage["cache_write_tokens"] = cache_write
    return usage


def _openai_chunk(
    chunk_id: str,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


async def anthropic_stream_to_openai(
    lines: AsyncIterator[str], *, model: str
) -> AsyncIterator[dict[str, Any]]:
    """Anthropic SSE events (parsed `data:` JSON payloads) -> OpenAI chunk dicts."""
    chunk_id = "chatcmpl-anthropic-stream"
    created = int(time.time())
    block_to_call: dict[int, int] = {}
    next_call_index = 0
    input_tokens: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None

    async for data in lines:
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")

        if event_type == "message_start":
            message = event.get("message") or {}
            chunk_id = f"chatcmpl-{str(message.get('id') or 'anthropic-stream')}"
            start_usage = message.get("usage") or {}
            input_tokens = start_usage.get("input_tokens")
            cache_read = start_usage.get("cache_read_input_tokens")
            cache_write = start_usage.get("cache_creation_input_tokens")
            yield _openai_chunk(chunk_id, model, {"role": "assistant", "content": ""}, created=created)
        elif event_type == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                call_index = next_call_index
                next_call_index += 1
                block_to_call[int(event.get("index") or 0)] = call_index
                yield _openai_chunk(
                    chunk_id,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": call_index,
                                "id": str(block.get("id") or ""),
                                "type": "function",
                                "function": {"name": str(block.get("name") or ""), "arguments": ""},
                            }
                        ]
                    },
                    created=created,
                )
        elif event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield _openai_chunk(chunk_id, model, {"content": delta["text"]}, created=created)
            elif delta.get("type") == "input_json_delta" and delta.get("partial_json"):
                call_index = block_to_call.get(int(event.get("index") or 0))
                if call_index is not None:
                    yield _openai_chunk(
                        chunk_id,
                        model,
                        {
                            "tool_calls": [
                                {"index": call_index, "function": {"arguments": delta["partial_json"]}}
                            ]
                        },
                        created=created,
                    )
        elif event_type == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                finish_reason = ANTHROPIC_STOP_REASON_TO_OPENAI.get(delta["stop_reason"], "stop")
            if isinstance(event.get("usage"), dict):
                output_tokens = event["usage"].get("output_tokens")
        elif event_type == "error":
            from aigateway.errors import UpstreamError

            error = event.get("error") or {}
            raise UpstreamError(
                f"anthropic stream error: {error.get('message') or event}",
                status_code=error.get("status_code") if isinstance(error.get("status_code"), int) else 502,
            )

    usage: dict[str, int] | None = None
    if input_tokens is not None or output_tokens is not None:
        usage = _openai_usage(
            input_tokens=input_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            completion_tokens=output_tokens,
        )
    final = _openai_chunk(chunk_id, model, {}, finish_reason or "stop", created=created)
    if usage:
        final["usage"] = usage
    yield final


class AnthropicProvider(Provider):
    """Speaks the native Anthropic Messages API upstream."""

    def __init__(
        self,
        name: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        default_max_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        self.default_max_tokens = default_max_tokens
        headers = {"Content-Type": "application/json", "anthropic-version": api_version}
        if api_key:
            headers["x-api-key"] = api_key
        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "headers": headers,
            "timeout": httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=120.0),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    async def chat(self, *, model: str, payload: dict[str, Any], timeout: float) -> ProviderResult:
        body = to_anthropic_request(payload, default_max_tokens=self.default_max_tokens)
        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/v1/messages", json={**body, "model": model, "stream": False}, timeout=timeout
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        latency = time.perf_counter() - started
        try:
            parsed = response.json()
        except ValueError:
            parsed = {"error": {"message": response.text[:512], "type": "upstream_invalid_body"}}
        if not isinstance(parsed, dict):
            parsed = {"error": {"message": str(parsed)[:512], "type": "upstream_invalid_body"}}
        if response.status_code < 400:
            parsed = from_anthropic_response(parsed, model=model)
        return ProviderResult(status=response.status_code, body=parsed, latency_s=latency)

    async def stream(
        self, *, model: str, payload: dict[str, Any], timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        body = to_anthropic_request(payload, default_max_tokens=self.default_max_tokens)
        try:
            async with self._client.stream(
                "POST",
                "/v1/messages",
                json={**body, "model": model, "stream": True},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")[:512]
                    raise UpstreamStreamError(response.status_code, text)
                data_lines = _sse_data_lines(response.aiter_lines())
                async for chunk in anthropic_stream_to_openai(data_lines, model=model):
                    yield chunk
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    async def health(self) -> bool:
        try:
            # Anthropic has no cheap GET probe; a 4xx still proves connectivity.
            response = await self._client.post(
                "/v1/messages",
                json={
                    "model": "health-probe",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=5.0,
            )
            return response.status_code < 500
        except (httpx.TimeoutException, httpx.TransportError):
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


async def _sse_data_lines(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Yield the JSON payload of each SSE `data:` line."""
    async for line in lines:
        line = line.strip()
        if line.startswith("data:"):
            yield line[5:].strip()
