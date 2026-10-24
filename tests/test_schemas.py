"""Tests for request schemas and text extraction helpers."""

from __future__ import annotations

import pytest

from aigateway.schemas import (
    ChatCompletionRequest,
    chunk_text,
    estimate_tokens,
    message_text,
    request_text,
    response_text,
    set_chunk_text,
    set_response_text,
)


def test_minimal_request_ok():
    request = ChatCompletionRequest(model="m", messages=[{"role": "user", "content": "hi"}])
    assert request.stream is False


def test_message_role_required():
    with pytest.raises(Exception):
        ChatCompletionRequest(model="m", messages=[{"content": "no role"}])


def test_unknown_role_rejected():
    with pytest.raises(Exception):
        ChatCompletionRequest(model="m", messages=[{"role": "wizard", "content": "x"}])


def test_empty_messages_rejected():
    with pytest.raises(Exception):
        ChatCompletionRequest(model="m", messages=[])


def test_extra_fields_pass_through():
    request = ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "hi"}], top_k=5, weird_field="x"
    )
    payload = request.upstream_payload(stream=True)
    assert payload["top_k"] == 5
    assert payload["weird_field"] == "x"
    assert "model" not in payload  # dispatcher re-injects per deployment
    assert payload["stream"] is True


def test_unset_fields_dropped():
    payload = ChatCompletionRequest(
        model="m", messages=[{"role": "user", "content": "hi"}]
    ).upstream_payload(stream=False)
    assert "temperature" not in payload
    assert "max_tokens" not in payload


def test_message_text_variants():
    assert message_text({"content": "abc"}) == "abc"
    assert message_text({"content": [{"type": "text", "text": "a"}, "b"]}) == "ab"
    assert message_text({"content": None}) == ""


def test_request_and_response_text():
    payload = {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]}
    assert request_text(payload) == "q\na"
    body = {"choices": [{"message": {"content": "out"}}]}
    assert response_text(body) == "out"


def test_set_response_text_and_chunks():
    body = {"choices": [{"message": {"content": "old"}}]}
    set_response_text(body, "new")
    assert response_text(body) == "new"
    chunk = {"choices": [{"delta": {"content": "old"}}]}
    set_chunk_text(chunk, "new")
    assert chunk_text(chunk) == "new"


def test_estimate_tokens_floor():
    assert estimate_tokens("") >= 1
    assert estimate_tokens("abcd" * 10) == 10
