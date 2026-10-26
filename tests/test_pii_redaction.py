"""PII redaction plugin tests: builtin groups, mask/block, streaming safety."""

from __future__ import annotations

import httpx
import pytest

from aigateway.api import create_app
from aigateway.app import GatewayApp
from aigateway.errors import ContentFilterError
from aigateway.plugins.base import PluginContext
from aigateway.plugins.pii_redaction import PiiRedactionPlugin
from aigateway.schemas import chunk_text
from tests.conftest import base_config, write_config

PHONE = "13812345678"
EMAIL = "alice@example.com"
ID_CARD = "11010519491231002X"  # valid GB11643 checksum


def make_plugin(**config) -> PiiRedactionPlugin:
    return PiiRedactionPlugin({"groups": ["phone", "email", "id_card", "bank_card"], **config})


# ---- detection ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [f"call me at {PHONE} today", f"mail {EMAIL} now", f"id {ID_CARD} here", "card 6222020200112233445 ok"],
)
def test_builtin_groups_detected(text):
    assert make_plugin().find(text)


def test_clean_text_not_detected():
    assert make_plugin().find("just a normal sentence with 123 and hello") is None


def test_group_selection_limits_scope():
    plugin = PiiRedactionPlugin({"groups": ["email"]})
    assert plugin.find(f"mail {EMAIL}")
    assert not plugin.find(f"call {PHONE}")
    with pytest.raises(ValueError, match="unknown groups"):
        PiiRedactionPlugin({"groups": ["passport"]})


def test_no_patterns_disables_plugin():
    assert PiiRedactionPlugin({"groups": []}).enabled is False


def test_id_card_wins_over_bank_card():
    plugin = make_plugin()
    masked = plugin.mask_text(f"id {ID_CARD} card 6222020200112233445")
    assert ID_CARD not in masked and "6222020200112233445" not in masked


def test_custom_pattern():
    plugin = PiiRedactionPlugin({"groups": [], "custom_patterns": [r"project-\d{4}"]})
    assert plugin.mask_text("see project-1234 docs") == "see ************ docs"


# ---- mask / block hooks ----------------------------------------------------------------


def _ctx(payload):
    return PluginContext(request_id="r", model="m1", payload=payload, stream=False)


async def test_mask_request_messages_string_and_parts():
    plugin = make_plugin()
    payload = {
        "messages": [
            {"role": "user", "content": f"call {PHONE}"},
            {"role": "user", "content": [{"type": "text", "text": f"mail {EMAIL}"}]},
        ]
    }
    await plugin.on_request(_ctx(payload))
    assert payload["messages"][0]["content"] == "call ***********"
    assert payload["messages"][1]["content"][0]["text"] == "mail *****************"


async def test_block_request_raises_pii_error():
    plugin = make_plugin(action="block")
    with pytest.raises(ContentFilterError) as excinfo:
        await plugin.on_request(_ctx({"messages": [{"role": "user", "content": f"id {ID_CARD}"}]}))
    assert excinfo.value.code == "pii_detected"


async def test_block_response_raises():
    plugin = make_plugin(action="block", check=["response"])
    ctx = _ctx({"messages": []})
    ctx.response = {"choices": [{"message": {"content": "his card is 6222020200112233445"}}]}
    with pytest.raises(ContentFilterError):
        await plugin.on_response(ctx)


async def test_mask_response_body():
    plugin = make_plugin(check=["response"])
    ctx = _ctx({"messages": []})
    ctx.response = {"choices": [{"message": {"content": f"reach me at {EMAIL}"}}]}
    await plugin.on_response(ctx)
    assert EMAIL not in ctx.response["choices"][0]["message"]["content"]
    assert "***************" in ctx.response["choices"][0]["message"]["content"]


# ---- streaming safety ---------------------------------------------------------------------


async def test_stream_mask_across_chunk_boundaries():
    """PII split across arbitrary chunk boundaries must never leak."""
    plugin = make_plugin(check=["response"])
    full_text = f"call {PHONE} or mail {EMAIL} or id {ID_CARD} end"
    state_ctx = _ctx({"messages": []})

    # feed one char at a time, collect emitted text
    emitted = []
    for i, char in enumerate(full_text):
        chunk = {"choices": [{"delta": {"content": char}}]}
        is_last = i == len(full_text) - 1
        if is_last:
            chunk["choices"][0]["finish_reason"] = "stop"
        out = await plugin.on_stream_chunk(state_ctx, chunk)
        if is_last:
            emitted.append(chunk_text(out))
        else:
            emitted.append(chunk_text(out))
    assembled = "".join(emitted)
    assert PHONE not in assembled
    assert EMAIL not in assembled
    assert ID_CARD not in assembled
    assert "call " in assembled and " end" in assembled  # surrounding text preserved


async def test_stream_block_accumulates_across_chunks():
    plugin = make_plugin(action="block", check=["response"])
    ctx = _ctx({"messages": []})
    pieces = ["call ", "138", "1234", "5678"]  # phone split over 4 chunks
    with pytest.raises(ContentFilterError):
        for piece in pieces:
            await plugin.on_stream_chunk(ctx, {"choices": [{"delta": {"content": piece}}]})


# ---- E2E through the gateway ---------------------------------------------------------------


def pii_config(**plugins) -> dict:
    return base_config(
        plugins={
            "pii-redaction-plugin": {
                "config": {
                    "groups": ["phone", "email", "id_card"],
                    "action": "mask",
                    "check": ["request", "response"],
                }
            },
            **plugins,
        }
    )


async def test_e2e_mask_roundtrip_through_mock_echo(tmp_path):
    """Mock provider echoes the (masked) request back, so one call proves
    both request masking and response masking."""
    app = GatewayApp(write_config(tmp_path, pii_config()))
    try:
        body = {"model": "m1", "messages": [{"role": "user", "content": f"call {PHONE} please"}]}
        response = await app.chat(body, {}, "test")
        content = response["choices"][0]["message"]["content"]
        assert PHONE not in content
        assert "***********" in content
    finally:
        await app.aclose()


async def test_e2e_block_returns_400(tmp_path):
    config = pii_config()
    config["plugins"]["pii-redaction-plugin"]["config"]["action"] = "block"
    path = write_config(tmp_path, config)
    app = create_app(path, config_watcher=False)
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "m1", "messages": [{"role": "user", "content": f"mail {EMAIL}"}]},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "pii_detected"
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()


async def test_e2e_streaming_mask_no_leak(tmp_path):
    app = create_app(write_config(tmp_path, pii_config()), config_watcher=False)
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            assembled = ""
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": f"call {PHONE} now"}],
                    "stream": True,
                },
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        import json as _json

                        chunk = _json.loads(line[5:])
                        assembled += chunk["choices"][0]["delta"].get("content") or ""
            assert PHONE not in assembled
            assert "call " in assembled and " now" in assembled
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()
