"""Native Google Gemini (generativelanguage.googleapis.com) upstream provider.

Translates the gateway's OpenAI payloads to `models/{model}:generateContent`
(and `:streamGenerateContent?alt=sse` for streaming) and converts responses
back. Function calling maps to Gemini's functionDeclarations / functionCall /
functionResponse.
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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"

GEMINI_FINISH_TO_OPENAI = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "BLOCKLIST": "content_filter",
    "SPII": "content_filter",
    "RECURRENCE": "stop",
    "OTHER": "stop",
}

_TOOL_CHOICE_MODE = {"auto": "AUTO", "required": "ANY", "none": "NONE"}


def to_gemini_request(payload: dict[str, Any]) -> dict[str, Any]:
    """OpenAI chat-completions payload -> Gemini generateContent request body."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    tool_name_by_id: dict[str, str] = {}  # OpenAI tool_call_id -> function name

    def _append(role: str, parts: list[dict[str, Any]]) -> None:
        contents.append({"role": role, "parts": parts})

    for message in payload.get("messages") or []:
        role = message.get("role")
        if role == "system":
            system_parts.append(message_text(message))
            continue
        if role == "tool":
            # Gemini expects function responses inside a user turn; consecutive
            # tool messages merge into one turn with several functionResponse parts.
            call_id = str(message.get("tool_call_id") or "")
            name = tool_name_by_id.get(call_id, call_id)
            raw_content = message.get("content")
            result: Any
            if isinstance(raw_content, str) and raw_content.strip().startswith("{"):
                try:
                    result = json.loads(raw_content)
                except json.JSONDecodeError:
                    result = {"content": raw_content}
            else:
                result = {"content": raw_content if isinstance(raw_content, str) else message_text(message)}
            response = result if isinstance(result, dict) else {"result": result}
            part = {"functionResponse": {"name": name, "response": response}}
            if contents and contents[-1]["role"] == "user" and contents[-1]["parts"] and all(
                "functionResponse" in p for p in contents[-1]["parts"]
            ):
                contents[-1]["parts"].append(part)
                continue
            _append("user", [part])
            continue
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            text = message.get("content")
            if text:
                parts.append({"text": text if isinstance(text, str) else message_text(message)})
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_name_by_id[str(call.get("id") or "")] = str(function.get("name") or "")
                parts.append(
                    {
                        "functionCall": {
                            "name": function.get("name") or "",
                            "args": args if isinstance(args, dict) else {},
                        }
                    }
                )
            _append("model", parts or [{"text": ""}])
            continue
        _append("user", [{"text": message_text(message)}])

    body: dict[str, Any] = {"contents": contents or [{"role": "user", "parts": [{"text": ""}]}]}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}

    generation_config: dict[str, Any] = {}
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "topP"),
        ("max_tokens", "maxOutputTokens"),
    ):
        if payload.get(source) is not None:
            generation_config[target] = payload[source]
    if payload.get("stop"):
        generation_config["stopSequences"] = payload["stop"]
    if generation_config:
        body["generationConfig"] = generation_config

    tools = payload.get("tools")
    if tools:
        declarations = []
        for tool in tools:
            function = (tool.get("function") or {}) if tool.get("type") == "function" else tool
            declarations.append(
                {
                    "name": function.get("name", ""),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        body["tools"] = [{"functionDeclarations": declarations}]
        tool_choice = payload.get("tool_choice")
        if tool_choice is not None:
            config: dict[str, Any] = {}
            if isinstance(tool_choice, str):
                config["mode"] = _TOOL_CHOICE_MODE.get(tool_choice, "AUTO")
            elif isinstance(tool_choice, dict) and (tool_choice.get("function") or {}).get("name"):
                config = {"mode": "ANY", "allowedFunctionNames": [tool_choice["function"]["name"]]}
            body["toolConfig"] = {"functionCallingConfig": config}
    return body


def _candidate_of(body: dict[str, Any]) -> dict[str, Any]:
    candidates = body.get("candidates") or []
    return candidates[0] if candidates and isinstance(candidates[0], dict) else {}


def from_gemini_response(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    """Gemini generateContent body -> OpenAI chat-completion body."""
    candidate = _candidate_of(body)
    parts = ((candidate.get("content") or {}).get("parts")) or []
    text = "".join(str(part.get("text", "")) for part in parts if isinstance(part.get("text"), str))
    tool_calls = []
    for i, part in enumerate(parts):
        call = part.get("functionCall")
        if isinstance(call, dict):
            tool_calls.append(
                {
                    "id": f"call_{i}_{call.get('name', '')}",
                    "type": "function",
                    "function": {
                        "name": str(call.get("name") or ""),
                        "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    usage = body.get("usageMetadata") or {}
    return {
        "id": f"chatcmpl-gemini-{body.get('responseId') or 'unknown'}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": GEMINI_FINISH_TO_OPENAI.get(candidate.get("finishReason"), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("promptTokenCount", 0),
            "completion_tokens": usage.get("candidatesTokenCount", 0),
            "total_tokens": usage.get("totalTokenCount", 0),
        },
    }


class GeminiProvider(Provider):
    """Speaks the native Google Gemini API upstream."""

    def __init__(
        self,
        name: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        api_version: str = "v1beta",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(name)
        self.api_version = api_version
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["x-goog-api-key"] = api_key
        client_kwargs: dict[str, Any] = {
            "base_url": base_url.rstrip("/"),
            "headers": headers,
            "timeout": httpx.Timeout(connect=5.0, read=120.0, write=30.0, pool=120.0),
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**client_kwargs)

    def _path(self, model: str, *, stream: bool) -> str:
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"/{self.api_version}/models/{model}:{action}"

    async def chat(self, *, model: str, payload: dict[str, Any], timeout: float) -> ProviderResult:
        body = to_gemini_request(payload)
        started = time.perf_counter()
        try:
            response = await self._client.post(self._path(model, stream=False), json=body, timeout=timeout)
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
            parsed = from_gemini_response(parsed, model=model)
        return ProviderResult(status=response.status_code, body=parsed, latency_s=latency)

    async def stream(
        self, *, model: str, payload: dict[str, Any], timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        body = to_gemini_request(payload)
        chunk_id = f"chatcmpl-gemini-{model.replace(':', '_')}"
        created = int(time.time())
        next_call_index = 0
        try:
            async with self._client.stream(
                "POST", self._path(model, stream=True), json=body, timeout=timeout
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
                    candidate = _candidate_of(event)
                    delta: dict[str, Any] = {}
                    finish_reason = GEMINI_FINISH_TO_OPENAI.get(candidate.get("finishReason"))
                    parts = ((candidate.get("content") or {}).get("parts")) or []
                    for part in parts:
                        if isinstance(part.get("text"), str) and part["text"]:
                            delta.setdefault("content", "")
                            delta["content"] += part["text"]
                        call = part.get("functionCall")
                        if isinstance(call, dict):
                            delta.setdefault("tool_calls", []).append(
                                {
                                    "index": next_call_index,
                                    "id": f"call_{next_call_index}_{call.get('name', '')}",
                                    "type": "function",
                                    "function": {
                                        "name": str(call.get("name") or ""),
                                        "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                                    },
                                }
                            )
                            next_call_index += 1
                    usage_meta = event.get("usageMetadata")
                    if delta or finish_reason:
                        yield {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
                        }
                    if usage_meta:
                        # OpenAI include_usage style: usage lands in a final
                        # standalone chunk after the finish_reason chunk.
                        yield {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                            "usage": {
                                "prompt_tokens": usage_meta.get("promptTokenCount", 0),
                                "completion_tokens": usage_meta.get("candidatesTokenCount", 0),
                                "total_tokens": usage_meta.get("totalTokenCount", 0),
                            },
                        }
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get(f"/{self.api_version}/models", timeout=5.0)
            return response.status_code < 500
        except (httpx.TimeoutException, httpx.TransportError):
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
