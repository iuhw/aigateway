"""cache_control (Anthropic prompt-caching marker) fidelity.

Regression coverage for the v1.2 fix: markers were silently dropped when the
/v1/messages entry translated requests into the normalized OpenAI form, and
again when the native anthropic adapter translated back — the same defect
class as LiteLLM #26477. Markers must survive both hops attached to the right
block; marker-free messages keep their historical shapes.
"""

from __future__ import annotations

from typing import Any

import pytest

from aigateway.anthropic_api import prompt_text_of, translate_request
from aigateway.core.anthropic_provider import to_anthropic_request
from aigateway.core.gemini_provider import to_gemini_request
from aigateway.errors import BadRequestError
from aigateway.plugins.base import PluginContext
from aigateway.plugins.prompt_template import PromptTemplatePlugin

MARKER = {"type": "ephemeral"}


def anthropic_body(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "claude", "max_tokens": 10, "messages": messages}
    body.update(extra)
    return body


def user_message(text: str = "hi") -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


# -- entry translation (/v1/messages -> normalized OpenAI) -----------------------


def test_text_block_marker_forces_parts_form():
    payload = translate_request(
        anthropic_body(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "long doc"},
                        {"type": "text", "text": "question", "cache_control": MARKER},
                    ],
                }
            ]
        )
    )
    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "long doc"},
        {"type": "text", "text": "question", "cache_control": MARKER},
    ]


def test_marker_between_text_and_image_keeps_order():
    payload = translate_request(
        anthropic_body(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "before"},
                        {"type": "text", "text": "marked", "cache_control": MARKER},
                        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}},
                        {"type": "text", "text": "after"},
                    ],
                }
            ]
        )
    )
    parts = payload["messages"][-1]["content"]
    assert [p["type"] for p in parts] == ["text", "text", "image_url", "text"]
    assert parts[0]["text"] == "before"
    assert parts[1] == {"type": "text", "text": "marked", "cache_control": MARKER}
    assert parts[3]["text"] == "after"


def test_image_block_marker_attached():
    payload = translate_request(
        anthropic_body(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"},
                         "cache_control": MARKER},
                    ],
                }
            ]
        )
    )
    assert payload["messages"][-1]["content"][0]["cache_control"] == MARKER


def test_markerless_message_stays_string():
    payload = translate_request(
        anthropic_body(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "text", "text": "b"},
                    ],
                }
            ]
        )
    )
    assert payload["messages"][-1]["content"] == "ab"


def test_string_system_unchanged():
    payload = translate_request(anthropic_body(user_message(), system="plain"))
    assert payload["messages"][0] == {"role": "system", "content": "plain"}


def test_system_blocks_without_marker_stay_string():
    payload = translate_request(
        anthropic_body(user_message(), system=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    )
    assert payload["messages"][0]["content"] == "ab"


def test_system_blocks_marker_preserved():
    payload = translate_request(
        anthropic_body(
            user_message(),
            system=[
                {"type": "text", "text": "s1"},
                {"type": "text", "text": "s2", "cache_control": MARKER},
            ],
        )
    )
    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "s1"},
        {"type": "text", "text": "s2", "cache_control": MARKER},
    ]


@pytest.mark.parametrize("where", ["system", "message", "tool"])
def test_invalid_cache_control_rejected(where):
    if where == "system":
        body = anthropic_body(user_message(), system=[{"type": "text", "text": "s", "cache_control": "yes"}])
    elif where == "message":
        body = anthropic_body(
            [{"role": "user", "content": [{"type": "text", "text": "x", "cache_control": 5}]}]
        )
    else:
        body = anthropic_body(user_message(), tools=[{"name": "f", "input_schema": {}, "cache_control": []}])
    with pytest.raises(BadRequestError, match="cache_control"):
        translate_request(body)


def test_tool_definition_marker_carried():
    payload = translate_request(
        anthropic_body(user_message(), tools=[{"name": "f", "description": "", "input_schema": {},
                                   "cache_control": MARKER}])
    )
    assert payload["tools"][0]["cache_control"] == MARKER


def test_tool_use_and_result_markers_carried():
    payload = translate_request(
        anthropic_body(
            [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "f", "input": {},
                         "cache_control": MARKER},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok",
                         "cache_control": MARKER},
                    ],
                },
            ]
        )
    )
    assistant = payload["messages"][0]
    assert assistant["tool_calls"][0]["cache_control"] == MARKER
    tool_message = payload["messages"][1]
    assert tool_message["role"] == "tool"
    assert tool_message["cache_control"] == MARKER


def test_prompt_text_of_flattens_parts():
    payload = translate_request(
        anthropic_body(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part one"},
                        {"type": "text", "text": "part two", "cache_control": MARKER},
                    ],
                }
            ]
        )
    )
    assert prompt_text_of(payload) == "part onepart two"


# -- native anthropic adapter (normalized OpenAI -> Anthropic upstream) -----------


def test_adapter_writes_back_text_and_system_markers():
    payload = {
        "model": "claude-up",
        "max_tokens": 10,
        "messages": [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": "s1"},
                    {"type": "text", "text": "s2", "cache_control": MARKER},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "doc"},
                    {"type": "text", "text": "question", "cache_control": MARKER},
                ],
            },
        ],
    }
    body = to_anthropic_request(payload)
    assert body["system"] == [
        {"type": "text", "text": "s1"},
        {"type": "text", "text": "s2", "cache_control": MARKER},
    ]
    assert body["messages"][-1]["content"] == [
        {"type": "text", "text": "doc"},
        {"type": "text", "text": "question", "cache_control": MARKER},
    ]


def test_adapter_writes_back_image_and_tool_markers():
    payload = {
        "model": "claude-up",
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://x/y.png"},
                        "cache_control": MARKER,
                    },
                ],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function",
                     "function": {"name": "f", "arguments": "{}"}, "cache_control": MARKER}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "t1",
                "content": "ok",
                "cache_control": MARKER,
            },
        ],
        "tools": [
            {"type": "function", "function": {"name": "f", "description": "", "parameters": {}},
             "cache_control": MARKER}
        ],
    }
    body = to_anthropic_request(payload)
    user_blocks = body["messages"][0]["content"]
    assert user_blocks[0]["type"] == "image"
    assert user_blocks[0]["cache_control"] == MARKER
    assistant_blocks = body["messages"][1]["content"]
    assert assistant_blocks[0]["type"] == "tool_use"
    assert assistant_blocks[0]["cache_control"] == MARKER
    tool_result = body["messages"][2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["cache_control"] == MARKER
    assert body["tools"][0]["cache_control"] == MARKER


def test_adapter_markerless_string_content_unchanged():
    payload = {
        "model": "claude-up",
        "max_tokens": 10,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
    }
    body = to_anthropic_request(payload)
    assert body["system"] == "sys"
    assert body["messages"][-1]["content"] == [{"type": "text", "text": "hello"}]


def test_adapter_ignores_non_dict_markers():
    payload = {
        "model": "claude-up",
        "max_tokens": 10,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "x", "cache_control": "yes"}]}
        ],
    }
    body = to_anthropic_request(payload)
    assert body["messages"][-1]["content"] == [{"type": "text", "text": "x"}]


def test_roundtrip_preserves_markers_end_to_end():
    body = anthropic_body(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "doc"},
                    {"type": "text", "text": "question", "cache_control": MARKER},
                ],
            }
        ],
        system=[{"type": "text", "text": "sys", "cache_control": MARKER}],
        tools=[{"name": "f", "description": "", "input_schema": {}, "cache_control": MARKER}],
    )
    upstream_body = to_anthropic_request(translate_request(body))
    assert upstream_body["system"] == [{"type": "text", "text": "sys", "cache_control": MARKER}]
    user_blocks = upstream_body["messages"][0]["content"]
    assert user_blocks[-1] == {"type": "text", "text": "question", "cache_control": MARKER}
    assert upstream_body["tools"][0]["cache_control"] == MARKER


# -- other native adapters: markers dropped, never crash --------------------------


def test_gemini_adapter_drops_markers_without_crash():
    payload = {
        "model": "gemini-up",
        "max_tokens": 10,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "x", "cache_control": MARKER}]}
        ],
    }
    body = to_gemini_request(payload)
    text = body["contents"][0]["parts"][0]["text"]
    assert text == "x"
    assert "cache_control" not in body["contents"][0]["parts"][0]


# -- prompt-template interaction ---------------------------------------------------


def _ctx(payload: dict[str, Any]) -> PluginContext:
    return PluginContext(request_id="r", model="m1", payload=payload, stream=False)


async def test_prompt_template_prepend_preserves_cache_markers():
    plugin = PromptTemplatePlugin({"templates": [{"system": "gateway rules", "mode": "prepend"}]})
    payload = {
        "model": "m1",
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "client", "cache_control": MARKER}],
            },
            {"role": "user", "content": "hi"},
        ],
    }
    await plugin.on_request(_ctx(payload))
    systems = [m for m in payload["messages"] if m["role"] == "system"]
    assert systems[0]["content"] == "gateway rules"
    assert systems[1]["content"] == [{"type": "text", "text": "client", "cache_control": MARKER}]


async def test_prompt_template_append_preserves_cache_markers():
    plugin = PromptTemplatePlugin({"templates": [{"system": "gateway rules", "mode": "append"}]})
    payload = {
        "model": "m1",
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "client", "cache_control": MARKER}],
            },
            {"role": "user", "content": "hi"},
        ],
    }
    await plugin.on_request(_ctx(payload))
    system = next(m for m in payload["messages"] if m["role"] == "system")
    assert system["content"] == [
        {"type": "text", "text": "client", "cache_control": MARKER},
        {"type": "text", "text": "gateway rules"},
    ]


async def test_prompt_template_replace_clears_client_system_by_design():
    plugin = PromptTemplatePlugin({"templates": [{"system": "gateway rules", "mode": "replace"}]})
    payload = {
        "model": "m1",
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "client", "cache_control": MARKER}],
            },
            {"role": "user", "content": "hi"},
        ],
    }
    await plugin.on_request(_ctx(payload))
    system = next(m for m in payload["messages"] if m["role"] == "system")
    assert system["content"] == "gateway rules"
