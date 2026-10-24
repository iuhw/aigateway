"""Tests for the keyword detection/filtering plugin — block & mask modes."""

from __future__ import annotations

import pytest

from aigateway.errors import ContentFilterError
from aigateway.plugins.base import PluginContext
from aigateway.plugins.keyword_filter import KeywordFilterPlugin


def make_ctx(payload: dict, stream: bool = False) -> PluginContext:
    return PluginContext(request_id="r1", model="m1", payload=payload, stream=stream)


def chat_payload(text: str) -> dict:
    return {"messages": [{"role": "user", "content": text}]}


# ---- request side -----------------------------------------------------------


async def test_request_block_mode_rejects():
    plugin = KeywordFilterPlugin({"words": ["forbidden"], "mode": "block"})
    with pytest.raises(ContentFilterError):
        await plugin.on_request(make_ctx(chat_payload("this is forbidden content")))


async def test_request_block_mode_case_insensitive_by_default():
    plugin = KeywordFilterPlugin({"words": ["forbidden"], "mode": "block"})
    with pytest.raises(ContentFilterError):
        await plugin.on_request(make_ctx(chat_payload("totally FORBIDDEN")))


async def test_request_block_case_sensitive_passes():
    plugin = KeywordFilterPlugin({"words": ["forbidden"], "mode": "block", "case_sensitive": True})
    await plugin.on_request(make_ctx(chat_payload("totally FORBIDDEN")))  # no raise


async def test_request_clean_text_passes():
    plugin = KeywordFilterPlugin({"words": ["forbidden"], "mode": "block"})
    await plugin.on_request(make_ctx(chat_payload("perfectly fine")))


async def test_request_mask_mode_rewrites_messages():
    plugin = KeywordFilterPlugin({"words": ["secret"], "mode": "mask"})
    ctx = make_ctx(chat_payload("the secret code"))
    await plugin.on_request(ctx)
    assert ctx.payload["messages"][0]["content"] == "the ****** code"


async def test_check_request_disabled():
    plugin = KeywordFilterPlugin({"words": ["forbidden"], "mode": "block", "check": ["response"]})
    await plugin.on_request(make_ctx(chat_payload("forbidden")))  # no raise


async def test_multimodal_content_parts_scanned():
    plugin = KeywordFilterPlugin({"words": ["bad"], "mode": "block"})
    payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": "a bad part"}]}]}
    with pytest.raises(ContentFilterError):
        await plugin.on_request(make_ctx(payload))


# ---- response side (non-streaming) ------------------------------------------


async def test_response_block_mode_rejects():
    plugin = KeywordFilterPlugin({"words": ["boom"], "mode": "block", "check": ["response"]})
    ctx = make_ctx(chat_payload("hi"))
    ctx.response = {"choices": [{"message": {"role": "assistant", "content": "boom goes the model"}}]}
    with pytest.raises(ContentFilterError):
        await plugin.on_response(ctx)


async def test_response_mask_mode_masks():
    plugin = KeywordFilterPlugin({"words": ["boom"], "mode": "mask", "check": ["response"]})
    ctx = make_ctx(chat_payload("hi"))
    ctx.response = {"choices": [{"message": {"role": "assistant", "content": "big boom here"}}]}
    await plugin.on_response(ctx)
    assert ctx.response["choices"][0]["message"]["content"] == "big **** here"


# ---- streaming ---------------------------------------------------------------


async def test_stream_block_mode_rejects_mid_stream():
    plugin = KeywordFilterPlugin({"words": ["bomb"], "mode": "block", "check": ["response"]})
    ctx = make_ctx(chat_payload("hi"), stream=True)
    chunks = [
        {"choices": [{"delta": {"content": "hello "}}]},
        {"choices": [{"delta": {"content": "bomb"}}]},  # keyword spans this chunk
        {"choices": [{"delta": {"content": "!"}}]},
    ]
    with pytest.raises(ContentFilterError):
        for chunk in chunks:
            await plugin.on_stream_chunk(ctx, chunk)


async def test_stream_mask_mode_across_chunk_boundaries():
    plugin = KeywordFilterPlugin({"words": ["bomb"], "mode": "mask", "check": ["response"]})
    ctx = make_ctx(chat_payload("hi"), stream=True)
    texts = []
    # "bo" + "mb" split across chunks must still be masked.
    for chunk in [
        {"choices": [{"delta": {"content": "bo"}}]},
        {"choices": [{"delta": {"content": "mb!"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]:
        out = await plugin.on_stream_chunk(ctx, chunk)
        if out is not None:
            from aigateway.schemas import chunk_text

            texts.append(chunk_text(out))
    joined = "".join(texts)
    assert "bomb" not in joined
    assert joined.count("*") >= 4


async def test_stream_mask_short_chunks_held_back():
    plugin = KeywordFilterPlugin({"words": ["bomb"], "mode": "mask", "check": ["response"]})
    ctx = make_ctx(chat_payload("hi"), stream=True)
    from aigateway.schemas import chunk_text

    out1 = await plugin.on_stream_chunk(ctx, {"choices": [{"delta": {"content": "a"}}]})
    # 1-char chunk is entirely held back (holdback = len("bomb")-1 = 3)
    assert chunk_text(out1) == ""


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        KeywordFilterPlugin({"words": ["x"], "mode": "explode"})


def test_empty_words_never_blocks():
    plugin = KeywordFilterPlugin({"words": [], "mode": "block"})
    assert plugin.find_word("anything") is None
