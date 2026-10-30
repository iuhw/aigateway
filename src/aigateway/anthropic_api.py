"""Anthropic Messages API compatibility surface.

Exposes the gateway to Anthropic-protocol clients (`POST /v1/messages`):
requests are translated to the OpenAI chat-completions payload the gateway
dispatches, and OpenAI responses (including SSE chunks) are translated back to
Anthropic format. This is the same protocol-conversion job Higress `ai-proxy`
does, scoped to text completions.

Supported: system (string/blocks), text content blocks, stop_sequences,
temperature/top_p/max_tokens, streaming (message_start / content_block_* /
message_delta / message_stop events), and tools — definitions, tool_choice,
tool_use / tool_result message blocks, and streamed tool-call arguments
(input_json_delta events). Not supported: images, citations — rejected with a
clear error rather than silently mangled.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from aigateway.errors import BadRequestError
from aigateway.tokenizers import TokenEstimator, estimate_default

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
    "pii_detected": "invalid_request_error",
    "prompt_injection_detected": "invalid_request_error",
    "webhook_review_blocked": "invalid_request_error",
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
                    "(text/tool_use/tool_result are supported by this gateway)"
                )
        return "".join(parts)
    raise BadRequestError(f"{where}: content must be a string or a list of blocks")


def _openai_tools(tools: Any) -> list[dict[str, Any]]:
    """Anthropic tool definitions -> OpenAI function-tool entries."""
    if not isinstance(tools, list):
        raise BadRequestError("'tools' must be a list")
    openai_tools: list[dict[str, Any]] = []
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict) or not tool.get("name"):
            raise BadRequestError(f"tools[{i}]: 'name' is required")
        schema = tool.get("input_schema")
        if schema is not None and not isinstance(schema, dict):
            raise BadRequestError(f"tools[{i}]: 'input_schema' must be an object")
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": str(tool["name"]),
                    "description": str(tool.get("description") or ""),
                    "parameters": schema or {"type": "object", "properties": {}},
                },
            }
        )
    return openai_tools


def _openai_tool_choice(tool_choice: Any) -> Any:
    """Anthropic tool_choice -> OpenAI tool_choice."""
    if not isinstance(tool_choice, dict):
        raise BadRequestError("'tool_choice' must be an object")
    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        if not tool_choice.get("name"):
            raise BadRequestError("tool_choice: 'name' is required when type is 'tool'")
        return {"type": "function", "function": {"name": str(tool_choice["name"])}}
    raise BadRequestError(f"tool_choice: unknown type {choice_type!r}")


def _translate_messages(anthropic_messages: Any) -> list[dict[str, Any]]:
    """Anthropic messages (with tool_use / tool_result blocks) -> OpenAI messages.

    Assistant tool_use blocks become `tool_calls`; user tool_result blocks
    become separate `role: "tool"` messages (emitted before any user text of
    the same turn, which is the order OpenAI upstreams expect).
    """
    openai_messages: list[dict[str, Any]] = []
    for i, message in enumerate(anthropic_messages):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            raise BadRequestError(f"messages[{i}]: role must be 'user' or 'assistant'")
        role = message["role"]
        content = message.get("content")
        blocks = content if isinstance(content, list) else None
        if blocks is None:
            openai_messages.append({"role": role, "content": _text_from_content(content, f"messages[{i}]")})
            continue

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                raise BadRequestError(f"messages[{i}]: content blocks must be objects")
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use" and role == "assistant":
                if not block.get("id") or not block.get("name"):
                    raise BadRequestError(f"messages[{i}]: tool_use requires 'id' and 'name'")
                tool_input = block.get("input")
                if tool_input is None:
                    tool_input = {}
                if not isinstance(tool_input, dict):
                    raise BadRequestError(f"messages[{i}]: tool_use 'input' must be an object")
                tool_calls.append(
                    {
                        "id": str(block["id"]),
                        "type": "function",
                        "function": {
                            "name": str(block["name"]),
                            "arguments": json.dumps(tool_input, ensure_ascii=False),
                        },
                    }
                )
            elif block_type == "tool_result" and role == "user":
                if not block.get("tool_use_id"):
                    raise BadRequestError(f"messages[{i}]: tool_result requires 'tool_use_id'")
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block["tool_use_id"]),
                        "content": _text_from_content(block.get("content"), f"messages[{i}] tool_result"),
                    }
                )
            else:
                raise BadRequestError(
                    f"messages[{i}]: unsupported content block {block_type!r} for role {role!r}"
                )

        text = "".join(text_parts)
        if role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": text if text else None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            openai_messages.append(entry)
        elif text or not blocks:  # user turn keeps its text (tool results already emitted)
            openai_messages.append({"role": "user", "content": text})
    return openai_messages


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

    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _text_from_content(system, "system")})

    anthropic_messages = body.get("messages")
    if not isinstance(anthropic_messages, list) or not anthropic_messages:
        raise BadRequestError("'messages' must be a non-empty list")
    messages.extend(_translate_messages(anthropic_messages))

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
    if body.get("tools"):
        payload["tools"] = _openai_tools(body["tools"])
    if body.get("tool_choice") is not None:
        payload["tool_choice"] = _openai_tool_choice(body["tool_choice"])
    return payload


def translate_response(openai_body: dict[str, Any]) -> dict[str, Any]:
    """OpenAI chat-completion body -> Anthropic message body (incl. tool_use)."""
    choice = (openai_body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_body.get("usage") or {}

    content_blocks: list[dict[str, Any]] = []
    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": function.get("arguments") or ""}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "input": arguments if isinstance(arguments, dict) else {"_raw": arguments},
            }
        )
    if not content_blocks:
        content_blocks = [{"type": "text", "text": ""}]

    return {
        "id": f"msg_{openai_body.get('id', 'unknown')}",
        "type": "message",
        "role": "assistant",
        "model": openai_body.get("model", ""),
        "content": content_blocks,
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
    estimator: TokenEstimator | None = None,
) -> AsyncIterator[str]:
    """Translate the gateway's OpenAI SSE stream into Anthropic events.

    Blocks are allocated lazily in arrival order: the first text delta opens a
    text block, each distinct OpenAI tool-call index opens a tool_use block.
    `content_block_stop` for every block is emitted after the stream ends
    (index-ordered), so a text/tool interleaving or an out-of-order tool-call
    fragment never reopens a closed block.

    input_tokens in message_start is the gateway's char-based estimate; the
    upstream-reported usage (when the provider sends it) lands in the final
    message_delta, mirroring how providers that stream usage behave.
    """
    message_id = f"msg_{model.replace(':', '')}"
    input_tokens = (estimator.estimate if estimator else estimate_default)(prompt_text)
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

    next_index = 0
    open_blocks: list[int] = []  # allocated block indices, in order
    text_block: int | None = None  # active text block (None: none or interrupted)
    tool_blocks: dict[int, int] = {}  # OpenAI tool-call index -> block index
    stop_reason: str | None = None
    output_tokens = 0
    usage_reported = False

    def _allocate() -> int:
        nonlocal next_index
        index = next_index
        next_index += 1
        open_blocks.append(index)
        return index

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
                if text_block is None:
                    text_block = _allocate()
                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": text_block,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                yield _sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": text_block,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            for tool_call in delta.get("tool_calls") or []:
                try:
                    call_index = int(tool_call.get("index") or 0)
                except (TypeError, ValueError):
                    continue
                function = tool_call.get("function") or {}
                if call_index not in tool_blocks:
                    block_index = _allocate()
                    tool_blocks[call_index] = block_index
                    yield _sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": block_index,
                            "content_block": {
                                "type": "tool_use",
                                "id": str(tool_call.get("id") or f"toolu_{block_index}"),
                                "name": str(function.get("name") or ""),
                                "input": {},
                            },
                        },
                    )
                arguments = function.get("arguments")
                if arguments:
                    yield _sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": tool_blocks[call_index],
                            "delta": {"type": "input_json_delta", "partial_json": str(arguments)},
                        },
                    )
            # A tool fragment interrupts the current text block: further text
            # opens a fresh block instead of appending to the old one.
            if delta.get("tool_calls"):
                text_block = None
            if choice.get("finish_reason"):
                stop_reason = _STOP_REASON_MAP.get(choice["finish_reason"], "end_turn")
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            output_tokens = usage.get("completion_tokens", 0)
            usage_reported = True

    for index in open_blocks:
        yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
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
