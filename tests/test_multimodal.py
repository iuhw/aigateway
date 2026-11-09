"""v1.1: multimodal image input — entries, admission limits, cache fingerprint,
Anthropic block translation, and native-adapter image translation."""

from __future__ import annotations

import pytest

from aigateway.anthropic_api import _openai_image_part, translate_request
from aigateway.app import GatewayApp
from aigateway.core.bedrock_provider import to_converse_request
from aigateway.core.gemini_provider import to_gemini_request
from aigateway.errors import BadRequestError, PayloadTooLargeError
from aigateway.plugins.semantic_cache import partition_key
from aigateway.schemas import extract_images, image_data_bytes, image_fingerprint
from tests.conftest import base_config, write_config

pytestmark = pytest.mark.unit

B64 = "aGVsbG8="  # tiny stand-in payload


def image_part(uri: str) -> dict:
    return {"type": "image_url", "image_url": {"url": uri}}


DATA_URI = f"data:image/png;base64,{B64}"
HTTPS_URL = "https://example.com/cat.png"


# ---- schemas: extraction / size / fingerprint --------------------------------------


def test_extract_images_data_uri_and_url():
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            image_part(DATA_URI),
            image_part(HTTPS_URL),
        ],
    }
    images = extract_images(message)
    assert images[0] == {"mime_type": "image/png", "data": B64}
    assert images[1] == {"url": HTTPS_URL}
    assert extract_images({"role": "user", "content": "plain"}) == []


@pytest.mark.parametrize(
    "part",
    [
        {"type": "image_url"},  # missing image_url
        {"type": "image_url", "image_url": {}},  # missing url
        {"type": "image_url", "image_url": {"url": "ftp://x/y.png"}},  # bad scheme
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},  # empty data
        {"type": "image_url", "image_url": {"url": "data:text/plain;base64,QQ=="}},  # not an image
    ],
)
def test_extract_images_rejects_malformed(part):
    with pytest.raises(BadRequestError):
        extract_images({"role": "user", "content": [part]})


def test_image_data_bytes_and_fingerprint():
    images = extract_images({"role": "user", "content": [image_part(DATA_URI)]})
    assert image_data_bytes(images) == len(B64) * 3 // 4

    payload_a = {"messages": [{"role": "user", "content": ["same text"]}]}
    assert image_fingerprint(payload_a) == []  # no images -> no fingerprint
    payload_b = {"messages": [{"role": "user", "content": ["same text", image_part(DATA_URI)]}]}
    payload_c = {"messages": [{"role": "user", "content": ["same text", image_part(DATA_URI)]}]}
    assert image_fingerprint(payload_b) == image_fingerprint(payload_c)


def test_semantic_partition_separates_images():
    base_text = [{"role": "user", "content": ["same question"]}]
    with_a = {"messages": [*base_text, {"role": "user", "content": [image_part(DATA_URI)]}]}
    with_b = {
        "messages": [
            *base_text,
            {"role": "user", "content": [image_part("data:image/png;base64,Y2F0Cg==")]},
        ]
    }
    assert partition_key("m", with_a) != partition_key("m", with_b)
    assert partition_key("m", with_a) == partition_key("m", dict(with_a))


# ---- admission limits ----------------------------------------------------------------


async def test_max_image_bytes_rejected_413(tmp_path):
    config = base_config(server={"max_image_bytes": 2})
    app = GatewayApp(write_config(tmp_path, config))
    try:
        payload = {
            "model": "m1",
            "messages": [{"role": "user", "content": ["hi", image_part(DATA_URI)]}],
        }
        with pytest.raises(PayloadTooLargeError, match="max_image_bytes"):
            app._check_request_limits(payload)
    finally:
        await app.aclose()


async def test_text_parts_count_toward_message_chars(tmp_path):
    config = base_config(server={"max_message_chars": 10})
    app = GatewayApp(write_config(tmp_path, config))
    try:
        payload = {
            "model": "m1",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "x" * 20}]}
            ],
        }
        with pytest.raises(PayloadTooLargeError, match="messages too long"):
            app._check_request_limits(payload)
    finally:
        await app.aclose()


async def test_openai_entry_forwards_image_parts_verbatim(tmp_path):
    """Permissive pass-through: the OpenAI entry forwards image parts intact;
    the mock provider's echo shows the text part made it through scanning."""
    app = GatewayApp(write_config(tmp_path, base_config()))
    try:
        provider = app.state.providers["p1"]
        seen: list[dict] = []
        original = provider.chat

        async def spy(*, model, payload, timeout):
            seen.append(payload)
            return await original(model=model, payload=payload, timeout=timeout)

        provider.chat = spy
        body = await app.chat(
            {
                "model": "m1",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe"},
                            image_part(DATA_URI),
                        ],
                    }
                ],
            },
            {},
            "test",
        )
        assert body["choices"][0]["message"]["content"]  # mock served normally
        content = seen[0]["messages"][0]["content"]
        assert content[1] == image_part(DATA_URI)  # image part forwarded untouched
        assert content[0]["text"] == "describe"
    finally:
        await app.aclose()


# ---- Anthropic entry: image block translation ---------------------------------------


def test_anthropic_base64_image_block_translated():
    body = translate_request(
        {
            "model": "m",
            "max_tokens": 10,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": B64,
                            },
                        },
                    ],
                }
            ],
        }
    )
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {"type": "image_url", "image_url": {"url": DATA_URI}}


def test_anthropic_url_image_source_passthrough():
    body = translate_request(
        {
            "model": "m",
            "max_tokens": 10,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image", "source": {"type": "url", "url": HTTPS_URL}}],
                }
            ],
        }
    )
    assert body["messages"][0]["content"] == [{"type": "image_url", "image_url": {"url": HTTPS_URL}}]


def test_anthropic_image_after_text_no_duplication():
    body = translate_request(
        {
            "model": "m",
            "max_tokens": 10,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "before "},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": B64},
                        },
                        {"type": "text", "text": "after"},
                    ],
                }
            ],
        }
    )
    content = body["messages"][0]["content"]
    assert [p["type"] for p in content] == ["text", "image_url", "text"]
    assert content[0]["text"] == "before " and content[2]["text"] == "after"


def test_anthropic_text_only_message_stays_string():
    body = translate_request(
        {
            "model": "m",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
    )
    assert body["messages"][0]["content"] == "hi"


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"type": "base64", "media_type": "text/plain", "data": B64}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
        {"type": "image", "source": {"type": "url", "url": "ftp://x/y"}},
        {"type": "image", "source": {"type": "bogus"}},
        {"type": "image"},
    ],
)
def test_anthropic_bad_image_sources_rejected(block):
    with pytest.raises(BadRequestError):
        translate_request(
            {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": [block]}]}
        )


def test_anthropic_assistant_image_rejected():
    with pytest.raises(BadRequestError, match="input-only"):
        translate_request(
            {
                "model": "m",
                "max_tokens": 10,
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "image", "source": {"type": "url", "url": HTTPS_URL}}],
                    }
                ],
            }
        )


def test_anthropic_tool_result_image_rejected():
    with pytest.raises(BadRequestError, match="images are user-message blocks only"):
        translate_request(
            {
                "model": "m",
                "max_tokens": 10,
                "tools": [{"name": "f", "input_schema": {}}],
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": [{"type": "image", "source": {"type": "url", "url": HTTPS_URL}}],
                            }
                        ],
                    },
                ],
            }
        )


def test_openai_image_part_helper():
    part = _openai_image_part({"type": "base64", "media_type": "image/jpeg", "data": B64}, "w")
    assert part["image_url"]["url"] == "data:image/jpeg;base64,aGVsbG8="


# ---- native adapters: image translation ---------------------------------------------


def test_gemini_request_inline_data():
    body = to_gemini_request(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "look"}, image_part(DATA_URI)]}
            ]
        }
    )
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "look"}
    assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": B64}}


def test_gemini_request_rejects_https_image():
    with pytest.raises(BadRequestError, match="data-URI"):
        to_gemini_request(
            {"messages": [{"role": "user", "content": [image_part(HTTPS_URL)]}]}
        )


def test_converse_request_image_block():
    body = to_converse_request(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "look"}, image_part(DATA_URI)]}
            ]
        }
    )
    blocks = body["messages"][0]["content"]
    assert blocks[0] == {"text": "look"}
    assert blocks[1] == {"image": {"format": "png", "source": {"bytes": B64}}}


def test_converse_request_image_format_guard():
    with pytest.raises(BadRequestError, match="png/jpeg/gif/webp"):
        to_converse_request(
            {
                "messages": [
                    {"role": "user", "content": [image_part("data:image/tiff;base64,QQ==")]}
                ]
            }
        )


def test_anthropic_provider_request_image_blocks():
    from aigateway.core.anthropic_provider import to_anthropic_request

    body = to_anthropic_request(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "look"}, image_part(DATA_URI)]},
                {"role": "user", "content": [image_part(HTTPS_URL)]},
            ]
        }
    )
    first = body["messages"][0]["content"]
    assert first[0] == {"type": "text", "text": "look"}
    assert first[1]["source"] == {"type": "base64", "media_type": "image/png", "data": B64}
    second = body["messages"][1]["content"]
    assert second[0]["source"] == {"type": "url", "url": HTTPS_URL}
