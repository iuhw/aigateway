"""AWS Bedrock upstream provider (Converse API) with SigV4 signing.

Translates the gateway's OpenAI payloads to the Bedrock Converse API
(`POST /model/{modelId}/converse`, `.../converse-stream`) and converts
responses back. Credentials come from config or the standard AWS environment
variables; signing is stdlib-only (see `sigv4.py`).
"""

from __future__ import annotations

import json
import os
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
from aigateway.core.sigv4 import amz_date_now, sigv4_headers
from aigateway.schemas import message_text

BEDROCK_STOP_REASON_TO_OPENAI = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "content_filtered": "content_filter",
    "guardrail_intervened": "content_filter",
}

DEFAULT_MAX_TOKENS = 4096


def default_base_url(region: str) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com"


def to_converse_request(
    payload: dict[str, Any], *, default_max_tokens: int = DEFAULT_MAX_TOKENS
) -> dict[str, Any]:
    """OpenAI chat-completions payload -> Bedrock Converse request body."""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    def _append(role: str, content: list[dict[str, Any]]) -> None:
        messages.append({"role": role, "content": content or [{"text": ""}]})

    for message in payload.get("messages") or []:
        role = message.get("role")
        if role == "system":
            system_parts.append(message_text(message))
            continue
        if role == "tool":
            block = {
                "toolResult": {
                    "toolUseId": str(message.get("tool_call_id") or ""),
                    "content": [{"text": message.get("content") or ""}],
                    "status": "success",
                }
            }
            # Consecutive tool results belong to one user turn.
            if messages and messages[-1]["role"] == "user":
                last_blocks = messages[-1]["content"]
                if last_blocks and all("toolResult" in b for b in last_blocks):
                    last_blocks.append(block)
                    continue
            _append("user", [block])
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = message.get("content")
            if text:
                blocks.append({"text": text if isinstance(text, str) else message_text(message)})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": str(call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "input": args if isinstance(args, dict) else {},
                        }
                    }
                )
            _append("assistant", blocks)
            continue
        _append("user", [{"text": message_text(message)}])

    body: dict[str, Any] = {"messages": messages or [{"role": "user", "content": [{"text": ""}]}]}
    if system_parts:
        body["system"] = [{"text": "\n".join(system_parts)}]

    inference_config: dict[str, Any] = {"maxTokens": int(payload.get("max_tokens") or default_max_tokens)}
    for source, target in (("temperature", "temperature"), ("top_p", "topP")):
        if payload.get(source) is not None:
            inference_config[target] = payload[source]
    body["inferenceConfig"] = inference_config
    if payload.get("stop"):
        body["additionalModelRequestFields"] = {"stop_sequences": payload["stop"]}

    tools = payload.get("tools")
    if tools:
        specs = []
        for tool in tools:
            function = (tool.get("function") or {}) if tool.get("type") == "function" else tool
            specs.append(
                {
                    "toolSpec": {
                        "name": function.get("name", ""),
                        "description": function.get("description", ""),
                        "inputSchema": {
                            "json": function.get("parameters") or {"type": "object", "properties": {}}
                        },
                    }
                }
            )
        tool_config: dict[str, Any] = {"tools": specs}
        tool_choice = payload.get("tool_choice")
        if tool_choice == "auto":
            tool_config["toolChoice"] = {"auto": {}}
        elif tool_choice == "required":
            tool_config["toolChoice"] = {"any": {}}
        elif tool_choice == "none":
            # Converse has no explicit "none": declaring no tools is equivalent.
            tool_config = {}
        elif isinstance(tool_choice, dict) and (tool_choice.get("function") or {}).get("name"):
            tool_config["toolChoice"] = {"tool": {"name": tool_choice["function"]["name"]}}
        body["toolConfig"] = tool_config
    return body


def from_converse_response(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Bedrock Converse body -> OpenAI chat-completion body."""
    blocks = (((body.get("output") or {}).get("message") or {}).get("content")) or []
    text = "".join(str(b.get("text", "")) for b in blocks if "text" in b)
    tool_calls = [
        {
            "id": str((b.get("toolUse") or {}).get("toolUseId") or ""),
            "type": "function",
            "function": {
                "name": str((b.get("toolUse") or {}).get("name") or ""),
                "arguments": json.dumps((b.get("toolUse") or {}).get("input") or {}, ensure_ascii=False),
            },
        }
        for b in blocks
        if "toolUse" in b
    ]
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = body.get("usage") or {}
    return {
        "id": f"chatcmpl-bedrock-{body.get('metrics', {}).get('latencyMs', 'unknown')}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": BEDROCK_STOP_REASON_TO_OPENAI.get(body.get("stopReason"), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("inputTokens", 0),
            "completion_tokens": usage.get("outputTokens", 0),
            "total_tokens": usage.get("totalTokens", 0),
        },
    }


class BedrockProvider(Provider):
    """Speaks the AWS Bedrock Converse API upstream."""

    def __init__(
        self,
        name: str,
        *,
        region: str,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        base_url: str | None = None,
        default_max_tokens: int = DEFAULT_MAX_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(name)
        self.region = region
        self.access_key_id = access_key_id or os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.secret_access_key = secret_access_key or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        self.session_token = session_token or os.environ.get("AWS_SESSION_TOKEN") or None
        self.default_max_tokens = default_max_tokens
        resolved_base = base_url or default_base_url(region)
        client_kwargs: dict[str, Any] = {
            "base_url": resolved_base,
            "timeout": httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=120.0),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    def _signed_headers(self, method: str, url: str, body: bytes) -> dict[str, str]:
        return sigv4_headers(
            method=method,
            url=url,
            body=body,
            access_key_id=self.access_key_id,
            secret_access_key=self.secret_access_key,
            session_token=self.session_token,
            region=self.region,
            service="bedrock",
            amz_date=amz_date_now(),
        )

    async def chat(self, *, model: str, payload: dict[str, Any], timeout: float) -> ProviderResult:
        body_bytes = json.dumps(
            to_converse_request(payload, default_max_tokens=self.default_max_tokens)
        ).encode("utf-8")
        path = f"/model/{model}/converse"
        url = f"{self._client.base_url}model/{model}/converse"
        started = time.perf_counter()
        try:
            response = await self._client.post(
                path,
                content=body_bytes,
                headers={**self._signed_headers("POST", url, body_bytes), "Content-Type": "application/json"},
                timeout=timeout,
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
            parsed = from_converse_response(parsed, model=model)
        return ProviderResult(status=response.status_code, body=parsed, latency_s=latency)

    async def stream(
        self, *, model: str, payload: dict[str, Any], timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        body_bytes = json.dumps(
            to_converse_request(payload, default_max_tokens=self.default_max_tokens)
        ).encode("utf-8")
        path = f"/model/{model}/converse-stream"
        url = f"{self._client.base_url}model/{model}/converse-stream"
        chunk_id = f"chatcmpl-bedrock-{model.replace(':', '_').replace('/', '-')}"
        created = int(time.time())
        block_to_call: dict[int, int] = {}
        next_call_index = 0
        finish_reason: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            async with self._client.stream(
                "POST",
                path,
                content=body_bytes,
                headers={**self._signed_headers("POST", url, body_bytes), "Content-Type": "application/json"},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")[:512]
                    raise UpstreamStreamError(response.status_code, text)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    block_index = int(event.get("contentBlockIndex") or 0)
                    if "start" in event and isinstance(event["start"].get("toolUse"), dict):
                        tool_use = event["start"]["toolUse"]
                        call_index = next_call_index
                        next_call_index += 1
                        block_to_call[block_index] = call_index
                        yield _chunk(
                            chunk_id,
                            model,
                            created,
                            {
                                "tool_calls": [
                                    {
                                        "index": call_index,
                                        "id": str(tool_use.get("toolUseId") or ""),
                                        "type": "function",
                                        "function": {
                                            "name": str(tool_use.get("name") or ""),
                                            "arguments": "",
                                        },
                                    }
                                ]
                            },
                        )
                    elif isinstance(event.get("delta"), dict):
                        delta = event["delta"]
                        if delta.get("text"):
                            yield _chunk(chunk_id, model, created, {"content": delta["text"]})
                        tool_delta = delta.get("toolUse") or {}
                        if tool_delta.get("input"):
                            call_index = block_to_call.get(block_index)
                            if call_index is not None:
                                yield _chunk(
                                    chunk_id,
                                    model,
                                    created,
                                    {
                                        "tool_calls": [
                                            {
                                                "index": call_index,
                                                "function": {"arguments": tool_delta["input"]},
                                            }
                                        ]
                                    },
                                )
                    elif isinstance(event.get("messageDelta"), dict):
                        message_delta = event["messageDelta"]
                        stop = (message_delta.get("delta") or {}).get("stopReason")
                        finish_reason = BEDROCK_STOP_REASON_TO_OPENAI.get(stop, finish_reason)
                        if isinstance(message_delta.get("usage"), dict):
                            output_tokens = message_delta["usage"].get("outputTokens", output_tokens)
                    elif isinstance(event.get("metadata"), dict):
                        usage_meta = event["metadata"].get("usage") or {}
                        input_tokens = usage_meta.get("inputTokens", input_tokens)
                        output_tokens = usage_meta.get("outputTokens", output_tokens)

                usage = None
                if input_tokens is not None or output_tokens is not None:
                    usage = {
                        "prompt_tokens": input_tokens or 0,
                        "completion_tokens": output_tokens or 0,
                        "total_tokens": (input_tokens or 0) + (output_tokens or 0),
                    }
                final = _chunk(chunk_id, model, created, {}, finish_reason or "stop")
                if usage:
                    final["usage"] = usage
                yield final
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get("/", timeout=5.0)
            return response.status_code < 500  # any HTTP answer proves connectivity
        except (httpx.TimeoutException, httpx.TransportError):
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


def _chunk(
    chunk_id: str, model: str, created: int, delta: dict[str, Any], finish_reason: str | None = None
) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
