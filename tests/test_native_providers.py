"""Wire-level tests for the native upstream adapters (Anthropic / Gemini / Bedrock).

Each provider is driven through httpx.MockTransport: real request/response
semantics without a network. Asserts the outbound translation (path, auth
headers, body shape) and the inbound translation back to OpenAI format,
including SSE streams and tool calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from aigateway.core.anthropic_provider import (
    AnthropicProvider,
    from_anthropic_response,
    to_anthropic_request,
)
from aigateway.core.bedrock_provider import BedrockProvider, to_converse_request
from aigateway.core.gemini_provider import GeminiProvider, from_gemini_response, to_gemini_request
from aigateway.core.sigv4 import sigv4_headers
from tests.conftest import base_config, write_config

OPENAI_PAYLOAD = {
    "messages": [{"role": "user", "content": "hi"}],
    "stream": False,
    "temperature": 0.2,
    "max_tokens": 100,
}


def sse(events: list[dict]) -> bytes:
    lines = []
    for event in events:
        lines.append(f"event: {event.get('type', 'message')}")
        lines.append(f"data: {json.dumps(event)}")
        lines.append("")
    return "\n".join(lines).encode()


# ---- SigV4 ------------------------------------------------------------------------


def test_sigv4_aws_reference_vector():
    # AWS docs "Signature Version 4 signing process" GET example; expected
    # signature cross-validated byte-for-byte against botocore's SigV4Auth
    # with the same fixed timestamp, region and service.
    headers = sigv4_headers(
        method="GET",
        url="https://example.amazonaws.com/",
        body=b"",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
        service="service",
        amz_date="20150830T123600Z",
    )
    assert headers["x-amz-date"] == "20150830T123600Z"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_sigv4_deterministic_and_payload_sensitive():
    kwargs = dict(
        method="POST",
        url="https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse",
        body=b"{}",
        access_key_id="AKIA1",
        secret_access_key="secret",
        region="us-east-1",
        service="bedrock",
        amz_date="20260101T000000Z",
    )
    first = sigv4_headers(**kwargs)
    assert first == sigv4_headers(**kwargs)  # deterministic
    other = sigv4_headers(**{**kwargs, "body": b'{"x":1}'})
    assert first["Authorization"].split("Signature=")[1] != other["Authorization"].split("Signature=")[1]
    assert "us-east-1/bedrock/aws4_request" in first["Authorization"]


def test_sigv4_includes_session_token():
    headers = sigv4_headers(
        method="POST",
        url="https://h.example.com/path",
        body=b"",
        access_key_id="a",
        secret_access_key="s",
        region="r",
        service="svc",
        amz_date="20260101T000000Z",
        session_token="tok",
    )
    assert headers["x-amz-security-token"] == "tok"
    assert "x-amz-security-token" in headers["Authorization"].split("SignedHeaders=")[1]


# ---- Anthropic upstream -------------------------------------------------------------


def test_to_anthropic_request_full_shape():
    payload = {
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "wx", "arguments": '{"c": "SF"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "foggy"},
            {"role": "tool", "tool_call_id": "call_2", "content": "12C"},
        ],
        "max_tokens": 64,
        "stop": ["END"],
        "tools": [
            {
                "type": "function",
                "function": {"name": "wx", "description": "w", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": "required",
        "temperature": 0.1,
    }
    body = to_anthropic_request(payload)
    assert body["system"] == "be terse"
    assert body["max_tokens"] == 64
    assert body["stop_sequences"] == ["END"]
    assert body["tool_choice"] == {"type": "any"}
    assert body["tools"] == [{"name": "wx", "description": "w", "input_schema": {"type": "object"}}]
    assert body["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "weather?"}]}
    assistant = body["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "text", "text": "checking"}
    assert assistant["content"][1] == {"type": "tool_use", "id": "call_1", "name": "wx", "input": {"c": "SF"}}
    # consecutive tool results merge into ONE user turn
    assert body["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "foggy"},
            {"type": "tool_result", "tool_use_id": "call_2", "content": "12C"},
        ],
    }


def test_to_anthropic_request_defaults_max_tokens():
    body = to_anthropic_request({"messages": [{"role": "user", "content": "x"}]})
    assert body["max_tokens"] == 4096


def test_from_anthropic_response_tool_use():
    body = from_anthropic_response(
        {
            "id": "msg_1",
            "stop_reason": "tool_use",
            "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "toolu_1", "name": "wx", "input": {"c": "SF"}},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        model="claude",
    )
    choice = body["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "calling"
    assert choice["message"]["tool_calls"] == [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "wx", "arguments": '{"c": "SF"}'},
        }
    ]
    assert body["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def make_anthropic(handler) -> AnthropicProvider:
    return AnthropicProvider(
        "anthropic",
        base_url="http://anthropic.test",
        api_key="sk-ant",
        transport=httpx.MockTransport(handler),
    )


async def test_anthropic_chat_wire():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("x-api-key")
        captured["version"] = request.headers.get("anthropic-version")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "msg_9",
                "type": "message",
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "bonjour"}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    provider = make_anthropic(handler)
    result = await provider.chat(model="claude-3", payload=OPENAI_PAYLOAD, timeout=5)
    assert captured["path"] == "/v1/messages"
    assert captured["auth"] == "sk-ant" and captured["version"] == "2023-06-01"
    assert captured["body"]["model"] == "claude-3" and captured["body"]["stream"] is False
    assert result.ok
    assert result.body["choices"][0]["message"]["content"] == "bonjour"
    assert result.body["choices"][0]["finish_reason"] == "stop"
    assert result.body["model"] == "claude-3"


async def test_anthropic_chat_error_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}
        )

    provider = make_anthropic(handler)
    result = await provider.chat(model="c", payload=OPENAI_PAYLOAD, timeout=5)
    assert result.status == 429
    assert result.body["error"]["message"] == "slow"


async def test_anthropic_stream_wire_translates_tool_use():
    events = [
        {"type": "message_start", "message": {"id": "msg_s", "usage": {"input_tokens": 7}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "sure"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_9", "name": "wx", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"c"'},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": ': "SF"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 6}},
        {"type": "message_stop"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(events), headers={"content-type": "text/event-stream"})

    provider = make_anthropic(handler)
    chunks = [
        chunk
        async for chunk in provider.stream(
            model="claude-3", payload={**OPENAI_PAYLOAD, "stream": True}, timeout=5
        )
    ]

    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    texts = [c["choices"][0]["delta"]["content"] for c in chunks if c["choices"][0]["delta"].get("content")]
    assert "".join(texts) == "sure"
    tool_starts = [
        c
        for c in chunks
        if c["choices"][0]["delta"].get("tool_calls") and c["choices"][0]["delta"]["tool_calls"][0].get("id")
    ]
    assert tool_starts[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "wx"
    arg_frags = [
        c["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
        and c["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments")
    ]
    assert "".join(arg_frags) == '{"c": "SF"}'
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"] == {"prompt_tokens": 7, "completion_tokens": 6, "total_tokens": 13}


# ---- Gemini upstream ----------------------------------------------------------------


def test_to_gemini_request_full_shape():
    payload = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "fn", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
        ],
        "max_tokens": 99,
        "temperature": 0.3,
        "tools": [
            {
                "type": "function",
                "function": {"name": "fn", "description": "d", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": "required",
    }
    body = to_gemini_request(payload)
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["contents"][0] == {"role": "user", "parts": [{"text": "go"}]}
    assert body["contents"][1]["role"] == "model"
    assert body["contents"][1]["parts"][0] == {"functionCall": {"name": "fn", "args": {}}}
    # functionResponse carries the function NAME resolved from the tool_call_id
    assert body["contents"][2]["parts"][0]["functionResponse"]["name"] == "fn"
    assert body["contents"][2]["parts"][0]["functionResponse"]["response"] == {"content": "done"}
    assert body["generationConfig"] == {"temperature": 0.3, "maxOutputTokens": 99}
    assert body["tools"] == [
        {"functionDeclarations": [{"name": "fn", "description": "d", "parameters": {"type": "object"}}]}
    ]
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"


def test_from_gemini_response_function_call():
    body = from_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "sure"},
                            {"functionCall": {"name": "fn", "args": {"x": 1}}},
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7},
        },
        model="gemini",
    )
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "sure"
    assert choice["message"]["tool_calls"][0]["function"] == {"name": "fn", "arguments": '{"x": 1}'}
    assert body["usage"]["total_tokens"] == 7


def make_gemini(handler) -> GeminiProvider:
    return GeminiProvider(
        "gemini",
        base_url="http://gemini.test",
        api_key="g-key",
        transport=httpx.MockTransport(handler),
    )


async def test_gemini_chat_wire():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["key"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": "salut"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3},
            },
        )

    provider = make_gemini(handler)
    result = await provider.chat(model="gemini-2.0", payload=OPENAI_PAYLOAD, timeout=5)
    assert captured["path"] == "/v1beta/models/gemini-2.0:generateContent"
    assert captured["key"] == "g-key"
    assert result.ok and result.body["choices"][0]["message"]["content"] == "salut"
    assert result.body["usage"]["prompt_tokens"] == 1


async def test_gemini_stream_wire():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":streamGenerateContent")
        chunks = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "he"}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "y"}]}, "finishReason": "STOP"}]},
            {
                "candidates": [{"content": {}}],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 3,
                },
            },
        ]
        body = "\n".join(f"data: {json.dumps(c)}" for c in chunks).encode()
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    provider = make_gemini(handler)
    chunks = [
        c
        async for c in provider.stream(
            model="gemini-2.0", payload={**OPENAI_PAYLOAD, "stream": True}, timeout=5
        )
    ]
    texts = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert texts == "hey"
    finish_chunks = [c for c in chunks if c["choices"][0]["finish_reason"]]
    assert finish_chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] == 3  # standalone usage chunk (include_usage style)


# ---- Bedrock upstream ----------------------------------------------------------------


def test_to_converse_request_full_shape():
    payload = {
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "fn", "arguments": '{"a":1}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ],
        "temperature": 0.1,
        "stop": ["\n\n"],
        "tools": [
            {
                "type": "function",
                "function": {"name": "fn", "description": "d", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "fn"}},
    }
    body = to_converse_request(payload)
    assert body["system"] == [{"text": "sys"}]
    assert body["messages"][0] == {"role": "user", "content": [{"text": "go"}]}
    assert body["messages"][1]["content"][0] == {
        "toolUse": {"toolUseId": "c1", "name": "fn", "input": {"a": 1}}
    }
    assert body["messages"][2]["content"][0]["toolResult"]["toolUseId"] == "c1"
    assert body["inferenceConfig"] == {"maxTokens": 4096, "temperature": 0.1}
    assert body["additionalModelRequestFields"] == {"stop_sequences": ["\n\n"]}
    tool_spec = body["toolConfig"]["tools"][0]["toolSpec"]
    assert tool_spec["name"] == "fn" and tool_spec["inputSchema"] == {"json": {"type": "object"}}
    assert body["toolConfig"]["toolChoice"] == {"tool": {"name": "fn"}}


def make_bedrock(handler) -> BedrockProvider:
    return BedrockProvider(
        "bedrock",
        region="us-east-1",
        access_key_id="AKIATEST",
        secret_access_key="secret",
        transport=httpx.MockTransport(handler),
    )


async def test_bedrock_chat_wire_with_sigv4():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["amz_date"] = request.headers.get("x-amz-date")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": {"message": {"role": "assistant", "content": [{"text": "ola"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
            },
        )

    provider = make_bedrock(handler)
    result = await provider.chat(model="anthropic.claude-v2", payload=OPENAI_PAYLOAD, timeout=5)
    assert captured["path"] == "/model/anthropic.claude-v2/converse"
    auth = captured["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIATEST/")
    assert "/us-east-1/bedrock/aws4_request" in auth
    assert "SignedHeaders=host;x-amz-date" in auth
    assert captured["amz_date"] and captured["amz_date"].endswith("Z")
    assert result.ok and result.body["choices"][0]["message"]["content"] == "ola"
    assert result.body["usage"] == {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}


async def test_bedrock_stream_wire():
    events = [
        {"start": {"toolUse": {"toolUseId": "tu1", "name": "fn"}}, "contentBlockIndex": 0},
        {"delta": {"toolUse": {"input": '{"x"'}}, "contentBlockIndex": 0},
        {"delta": {"toolUse": {"input": ": 1}"}}, "contentBlockIndex": 0},
        {"delta": {"text": "hi"}, "contentBlockIndex": 1},
        {"messageDelta": {"delta": {"stopReason": "tool_use"}, "usage": {"outputTokens": 4}}},
        {"metadata": {"usage": {"inputTokens": 6, "outputTokens": 4, "totalTokens": 10}}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/converse-stream")
        return httpx.Response(200, content=sse(events), headers={"content-type": "text/event-stream"})

    provider = make_bedrock(handler)
    chunks = [c async for c in provider.stream(model="m", payload=OPENAI_PAYLOAD, timeout=5)]
    tool_start = next(c for c in chunks if c["choices"][0]["delta"].get("tool_calls"))
    assert tool_start["choices"][0]["delta"]["tool_calls"][0]["id"] == "tu1"
    args = "".join(
        c["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        for c in chunks
        if c["choices"][0]["delta"].get("tool_calls")
        and c["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments")
    )
    assert args == '{"x": 1}'
    texts = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert texts == "hi"
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"] == {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10}


# ---- config wiring -------------------------------------------------------------------


def test_config_builds_native_providers(tmp_path):
    from aigateway.config import GatewayConfig

    config = GatewayConfig(
        {
            "providers": {
                "a": {"type": "anthropic", "api_key": "k"},
                "g": {"type": "gemini", "api_key": "k"},
                "b": {
                    "type": "bedrock",
                    "region": "eu-west-1",
                    "access_key_id": "k",
                    "secret_access_key": "s",
                },
            },
            "models": {},
            "plugins": {},
        }
    )
    providers = config.build_providers()
    assert type(providers["a"]).__name__ == "AnthropicProvider"
    assert type(providers["g"]).__name__ == "GeminiProvider"
    assert type(providers["b"]).__name__ == "BedrockProvider"


def test_config_bedrock_requires_region():
    from aigateway.config import GatewayConfig
    from aigateway.errors import BadRequestError

    config = GatewayConfig({"providers": {"b": {"type": "bedrock"}}, "models": {}, "plugins": {}})
    with pytest.raises(BadRequestError, match="region"):
        config.build_providers()


def test_sigv4_amz_date_now_shape():
    from aigateway.core.sigv4 import amz_date_now

    stamp = amz_date_now()
    assert len(stamp) == 16 and stamp.endswith("Z")
    datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)  # parses


# ---- embeddings (native adapters) -----------------------------------------------------


async def test_gemini_embed_single_wire():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embedding": {"values": [0.1, 0.2, 0.3]}})

    provider = make_gemini(handler)
    result = await provider.embed(model="text-embedding-004", payload={"input": "hello"}, timeout=5)
    assert captured["path"] == "/v1beta/models/text-embedding-004:embedContent"
    assert captured["body"] == {"content": {"parts": [{"text": "hello"}]}}
    assert result.ok
    assert result.body["object"] == "list"
    assert result.body["data"] == [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}]
    assert result.body["model"] == "text-embedding-004"
    assert result.body["usage"]["prompt_tokens"] >= 1


async def test_gemini_embed_batch_wire():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"embeddings": [{"values": [1.0]}, {"values": [2.0]}, {"values": [3.0]}]}
        )

    provider = make_gemini(handler)
    result = await provider.embed(
        model="text-embedding-004", payload={"input": ["alpha", "beta", "gamma"]}, timeout=5
    )
    assert captured["path"] == "/v1beta/models/text-embedding-004:batchEmbedContents"
    requests = captured["body"]["requests"]
    assert [r["model"] for r in requests] == ["models/text-embedding-004"] * 3
    assert [r["content"]["parts"][0]["text"] for r in requests] == ["alpha", "beta", "gamma"]
    assert [d["index"] for d in result.body["data"]] == [0, 1, 2]
    assert [d["embedding"] for d in result.body["data"]] == [[1.0], [2.0], [3.0]]


async def test_gemini_embed_error_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad model", "status": "INVALID_ARGUMENT"}})

    provider = make_gemini(handler)
    result = await provider.embed(model="nope", payload={"input": "x"}, timeout=5)
    assert result.status == 400 and not result.ok
    assert result.body["error"]["message"] == "bad model"  # raw upstream error, not mangled


async def test_bedrock_embed_titan_wire():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embedding": [0.5, -0.5], "inputTextTokenCount": 4})

    provider = make_bedrock(handler)
    result = await provider.embed(
        model="amazon.titan-embed-text-v2:0", payload={"input": "embed me"}, timeout=5
    )
    assert captured["path"] == "/model/amazon.titan-embed-text-v2:0/invoke"
    assert captured["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIATEST/")
    assert "/us-east-1/bedrock/aws4_request" in captured["authorization"]
    assert captured["body"] == {"inputText": "embed me"}
    assert result.ok
    assert result.body["data"][0]["embedding"] == [0.5, -0.5]
    assert result.body["usage"]["prompt_tokens"] == 4  # real count from Titan


async def test_bedrock_embed_titan_batch_invokes_per_text():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"embedding": [1.0], "inputTextTokenCount": 2})

    provider = make_bedrock(handler)
    result = await provider.embed(
        model="amazon.titan-embed-text-v2:0", payload={"input": ["a", "bb", "ccc"]}, timeout=5
    )
    assert [b["inputText"] for b in bodies] == ["a", "bb", "ccc"]  # one invoke per text
    assert len(result.body["data"]) == 3
    assert result.body["usage"]["prompt_tokens"] == 6


async def test_bedrock_embed_cohere_wire():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[0.1], [0.2]], "id": "e1"})

    provider = make_bedrock(handler)
    result = await provider.embed(
        model="cohere.embed-english-v3", payload={"input": ["one", "two"]}, timeout=5
    )
    assert captured["path"] == "/model/cohere.embed-english-v3/invoke"
    assert captured["body"]["texts"] == ["one", "two"]
    assert captured["body"]["input_type"] == "search_document"
    assert result.ok
    assert [d["embedding"] for d in result.body["data"]] == [[0.1], [0.2]]


def test_embedding_inputs_normalization():
    from aigateway.schemas import embedding_inputs

    assert embedding_inputs({"input": "solo"}) == ["solo"]
    assert embedding_inputs({"input": ["a", "b"]}) == ["a", "b"]
    assert embedding_inputs({"input": [[1, 2], [3]]}) == ["1 2", "3"]  # token arrays joined
    assert embedding_inputs({}) == []


async def test_embeddings_endpoint_with_native_provider(tmp_path):
    """E2E: /v1/embeddings serves through a native adapter swapped into the app."""
    from aigateway.api import create_app

    config = base_config(models={"m1": {"deployments": [{"provider": "g", "model": "text-embedding-004"}]}})
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    try:
        provider = make_gemini(
            lambda request: httpx.Response(200, json={"embedding": {"values": [0.9, 0.8]}})
        )
        app.state.gateway.state.providers["g"] = provider
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            response = await client.post("/v1/embeddings", json={"model": "m1", "input": "hi"})
            assert response.status_code == 200
            body = response.json()
            assert body["data"][0]["embedding"] == [0.9, 0.8]
            assert body["object"] == "list"
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()
