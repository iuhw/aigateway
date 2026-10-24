"""Anthropic Messages API compatibility surface.

Exposes the gateway to Anthropic-protocol clients (`POST /v1/messages`):
requests are translated to the OpenAI chat-completions payload the gateway
dispatches, and OpenAI responses (including SSE chunks) are translated back to
Anthropic format. This is the same protocol-conversion job Higress `ai-proxy`
does, scoped to text-only completions.

Supported: system (string/blocks), text content blocks, stop_sequences,
temperature/top_p/max_tokens, streaming (message_start / content_block_* /
message_delta / message_stop events). Not supported yet: tools, images,
citations — rejected with a clear error rather than silently mangled.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from aigateway.errors import BadRequestError
from aigateway.schemas import estimate_tokens

_STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    None: None,
}

_OPENAI_STATUS_TO_ANTHROPIC_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    429: "rate_limit_error",
}

#: Gateway error codes (the `type` field of our OpenAI-style errors) mapped to
#: Anthropic error types for mid-stream error events.
_GATEWAY_CODE_TO_ANTHROPIC_ERROR_TYPE = {
    "rate_limit_exceeded": "rate_limit_error",
    "authentication_error": "authentication_error",
    "permission_denied": "permission_error",
    "not_found_error": "not_found_error",
    "content_filter_rejected": "invalid_request_error",
    "invalid_request_error": "invalid_request_error",
    "payload_too_large": "invalid_request_error",
    "no_healthy_deployment": "api_error",
    "upstream_error": "api_error",
    "upstream_unavailable": "api_error",
}


def _text_from_content(content: Any, where: str) -> str:
    """Flatten an Anthropic content field (string or text blocks) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                raise BadRequestError(f"{where}: content blocks must be objects")
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            else:
                raise BadRequestError(
                    f"{where}: unsupported content block type {block_type!r} "
                    "(only text is supported by this gateway)"
                )
        return "".join(parts)
    raise BadRequestError(f"{where}: content must be a string or a list of blocks")


def translate_request(body: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages request -> OpenAI chat-completions payload."""
    if not isinstance(body, dict):
        raise BadRequestError("request body must be a JSON object")
    model = body.get("model")
    if not model:
        raise BadRequestError("'model' is required")
    if body.get("max_tokens") is None:
        raise BadRequestError("'max_tokens' is required by the Anthropic protocol")
    if not isinstance(body.get("max_tokens"), int) or body["max_tokens"] < 1:
        raise BadRequestError("'max_tokens' must be a positive integer")
    if body.get("tools") or body.get("tool_choice"):
        raise BadRequestError("tools are not supported by this gateway yet")

    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _text_from_content(system, "system")})

    anthropic_messages = body.get("messages")
    if not isinstance(anthropic_messages, list) or not anthropic_messages:
        raise BadRequestError("'messages' must be a non-empty list")
    for i, message in enumerate(anthropic_messages):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            raise BadRequestError(f"messages[{i}]: role must be 'user' or 'assistant'")
        messages.append(
            {"role": message["role"], "content": _text_from_content(message.get("content"), f"messages[{i}]")}
        )

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": body["max_tokens"],
    }
    for source_key, target_key in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(source_key) is not None:
            payload[target_key] = body[source_key]
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]
    return payload


def translate_response(openai_body: dict[str, Any]) -> dict[str, Any]:
    """OpenAI chat-completion body -> Anthropic message body."""
    choice = (openai_body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_body.get("usage") or {}
    return {
        "id": f"msg_{openai_body.get('id', 'unknown')}",
        "type": "message",
        "role": "assistant",
        "model": openai_body.get("model", ""),
        "content": [{"type": "text", "text": message.get("content") or ""}],
        "stop_reason": _STOP_REASON_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def error_body(exc: Exception) -> dict[str, Any]:
    """Any gateway exception -> Anthropic error envelope."""
    status = getattr(exc, "status_code", 500)
    code = getattr(exc, "code", "api_error")
    error_type = _OPENAI_STATUS_TO_ANTHROPIC_ERROR_TYPE.get(status, "api_error")
    return {
        "type": "error",
        "error": {"type": error_type, "message": str(exc), "code": code},
    }


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_events(
    openai_sse: AsyncIterator[str],
    *,
    model: str,
    prompt_text: str,
    max_tokens: int,
) -> AsyncIterator[str]:
    """Translate the gateway's OpenAI SSE stream into Anthropic events.

    input_tokens in message_start is the gateway's char-based estimate; the
    upstream-reported usage (when the provider sends it) lands in the final
    message_delta, mirroring how providers that stream usage behave.
    """
    message_id = f"msg_{model.replace(':', '')}"
    input_tokens = estimate_tokens(prompt_text)
    yield _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        },
    )

    block_open = False
    output_tokens = 0
    stop_reason: str | None = None
    usage_reported = False

    def open_block() -> str:
        return _sse_event(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        )

    async for line in openai_sse:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        if "error" in chunk:  # gateway-mapped error mid-stream
            error = chunk["error"]
            error_type = _GATEWAY_CODE_TO_ANTHROPIC_ERROR_TYPE.get(
                str(error.get("type") or error.get("code") or ""), "api_error"
            )
            yield _sse_event(
                "error",
                {
                    "type": "error",
                    "error": {"type": error_type, "message": str(error.get("message", "upstream error"))},
                },
            )
            return

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if not block_open:
                    yield open_block()
                    block_open = True
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            if choice.get("finish_reason"):
                stop_reason = _STOP_REASON_MAP.get(choice["finish_reason"], "end_turn")
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            output_tokens = usage.get("completion_tokens", 0)
            usage_reported = True

    if block_open:
        yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
    if not usage_reported:
        output_tokens = 0  # unknown: providers that omit streamed usage
    yield _sse_event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason or "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        },
    )
    yield _sse_event("message_stop", {"type": "message_stop"})


def prompt_text_of(openai_payload: dict[str, Any]) -> str:
    return "\n".join(
        str(m.get("content") or "") for m in openai_payload.get("messages", []) if isinstance(m, dict)
    )
