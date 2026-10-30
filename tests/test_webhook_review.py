"""Webhook review plugin tests: verdict contract, fail-open/close, streaming post-delivery."""

from __future__ import annotations

import json

import httpx
import pytest

from aigateway.api import create_app
from aigateway.errors import ContentFilterError, PluginReject
from aigateway.observability.metrics import Metrics
from aigateway.plugins.base import PluginContext
from aigateway.plugins.webhook_review import WebhookReviewPlugin
from tests.conftest import base_config, write_config

URL = "http://review.internal/check"


def make_plugin(**config) -> WebhookReviewPlugin:
    cfg = {"url": URL, **config}
    plugin = WebhookReviewPlugin(cfg)
    metrics = Metrics()
    plugin.set_metrics(metrics)
    return plugin


def verdict_of(plugin: WebhookReviewPlugin, verdict: str) -> str | None:
    return plugin._metrics.registry.get_sample_value(
        "gateway_guardrail_flags_total", {"plugin": "webhook-review-plugin", "verdict": verdict}
    )


def _ctx(payload):
    return PluginContext(request_id="r1", model="m1", payload=payload, stream=False)


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=2.0)


# ---- config validation -------------------------------------------------------------------


def test_url_required_and_absolute():
    with pytest.raises(ValueError, match="'url' is required"):
        WebhookReviewPlugin({})
    with pytest.raises(ValueError, match="absolute"):
        WebhookReviewPlugin({"url": "review.internal/check"})


def test_on_unavailable_validated():
    with pytest.raises(ValueError, match="on_unavailable"):
        WebhookReviewPlugin({"url": URL, "on_unavailable": "panic"})


# ---- verdict contract ----------------------------------------------------------------------


async def test_allow_passes_and_counts():
    plugin = make_plugin()
    plugin._client = transport(lambda req: httpx.Response(200, json={"action": "allow"}))
    await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))
    assert verdict_of(plugin, "allowed") == 1


async def test_accepts_verdict_field_alias():
    plugin = make_plugin()
    plugin._client = transport(lambda req: httpx.Response(200, json={"verdict": "safe"}))
    await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))
    assert verdict_of(plugin, "allowed") == 1


async def test_block_with_reason_raises_review_error():
    plugin = make_plugin()
    plugin._client = transport(
        lambda req: httpx.Response(200, json={"action": "block", "reason": "violence"})
    )
    with pytest.raises(ContentFilterError) as excinfo:
        await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "bad"}]}))
    assert excinfo.value.code == "webhook_review_blocked"
    assert excinfo.value.status_code == 400
    assert "violence" in str(excinfo.value)
    assert verdict_of(plugin, "blocked") == 1


async def test_block_without_reason_uses_block_message():
    plugin = make_plugin(block_message="custom block text")
    plugin._client = transport(lambda req: httpx.Response(200, json={"action": "block"}))
    with pytest.raises(ContentFilterError) as excinfo:
        await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "bad"}]}))
    assert "custom block text" in str(excinfo.value)


# ---- unavailable outcomes & policy ----------------------------------------------------------


async def test_timeout_fail_open():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=req)

    plugin = make_plugin()
    plugin._client = transport(handler)
    await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))  # passes
    assert verdict_of(plugin, "timeout") == 1


async def test_timeout_fail_close_rejects_503():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=req)

    plugin = make_plugin(on_unavailable="fail-close")
    plugin._client = transport(handler)
    with pytest.raises(PluginReject) as excinfo:
        await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))
    assert excinfo.value.code == "webhook_review_unavailable"
    assert excinfo.value.status_code == 503


async def test_connection_error_fail_open():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    plugin = make_plugin()
    plugin._client = transport(handler)
    await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))
    assert verdict_of(plugin, "error") == 1


async def test_non_200_is_invalid_response():
    plugin = make_plugin()
    plugin._client = transport(lambda req: httpx.Response(500, text="boom"))
    await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))
    assert verdict_of(plugin, "invalid_response") == 1


async def test_malformed_json_is_invalid_response():
    plugin = make_plugin(on_unavailable="fail-close")
    plugin._client = transport(lambda req: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(PluginReject):
        await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))


async def test_unknown_action_is_invalid_response():
    plugin = make_plugin()
    plugin._client = transport(lambda req: httpx.Response(200, json={"action": "maybe"}))
    await plugin.on_request(_ctx({"messages": [{"role": "user", "content": "hi"}]}))
    assert verdict_of(plugin, "invalid_response") == 1


# ---- payload shape ---------------------------------------------------------------------------


async def test_payload_shape_and_truncation():
    seen: list[httpx.Request] = []
    plugin = make_plugin(max_text_chars=10, headers={"authorization": "Bearer t"})
    plugin._client = transport(
        lambda req: (seen.append(req), httpx.Response(200, json={"action": "allow"}))[1]
    )
    long_text = "x" * 100
    await plugin.on_request(_ctx({"messages": [{"role": "user", "content": long_text}]}))
    body = json.loads(seen[0].content)
    assert body["text"] == "x" * 10
    assert body["truncated"] is True
    assert body["stage"] == "request"
    assert body["model"] == "m1"
    assert seen[0].headers["authorization"] == "Bearer t"


async def test_empty_text_skips_webhook():
    calls: list[httpx.Request] = []
    plugin = make_plugin()
    plugin._client = transport(
        lambda req: (calls.append(req), httpx.Response(200, json={"action": "allow"}))[1]
    )
    await plugin.on_request(_ctx({"messages": []}))
    assert not calls


async def test_response_stage_reviewed():
    plugin = make_plugin(check=["response"])
    plugin._client = transport(lambda req: httpx.Response(200, json={"action": "block", "reason": "pii"}))
    ctx = _ctx({"messages": []})
    ctx.response = {"choices": [{"message": {"content": "leaked"}}]}
    with pytest.raises(ContentFilterError):
        await plugin.on_response(ctx)


# ---- streaming: post-delivery review ------------------------------------------------------------


async def test_stream_end_review_records_block_without_raising(caplog):
    plugin = make_plugin(check=["response"])
    plugin._client = transport(
        lambda req: httpx.Response(200, json={"action": "block", "reason": "late verdict"})
    )
    ctx = _ctx({"messages": []})
    ctx.stream = True
    for piece in ["hello ", "world"]:
        await plugin.on_stream_chunk(ctx, {"choices": [{"delta": {"content": piece}}]})
    await plugin.on_stream_end(ctx)  # must not raise
    assert verdict_of(plugin, "blocked") == 1


async def test_stream_accumulation_truncated_full():
    plugin = make_plugin(check=["response"], max_text_chars=5)
    seen: list[httpx.Request] = []
    plugin._client = transport(
        lambda req: (seen.append(req), httpx.Response(200, json={"action": "allow"}))[1]
    )
    ctx = _ctx({"messages": []})
    ctx.stream = True
    for piece in ["aaaa", "bbbb", "cccc"]:
        await plugin.on_stream_chunk(ctx, {"choices": [{"delta": {"content": piece}}]})
    await plugin.on_stream_end(ctx)
    # "aaaa" + "bbbb" overflows the 5-char cap mid-chunk; hard truncation wins.
    assert json.loads(seen[0].content)["text"] == "aaaab"
    assert json.loads(seen[0].content)["truncated"] is True


async def test_stream_end_webhook_failure_is_swallowed():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    plugin = make_plugin(check=["response"])
    plugin._client = transport(handler)
    ctx = _ctx({"messages": []})
    await plugin.on_stream_chunk(ctx, {"choices": [{"delta": {"content": "hi"}}]})
    await plugin.on_stream_end(ctx)  # no raise even when webhook is down
    assert verdict_of(plugin, "error") == 1


async def test_aclose_shuts_client():
    plugin = make_plugin()
    plugin._client = transport(lambda req: httpx.Response(200, json={"action": "allow"}))
    await plugin.aclose()
    assert plugin._client is None


# ---- E2E through the gateway ---------------------------------------------------------------------


def review_config(**cfg) -> dict:
    return base_config(plugins={"webhook-review-plugin": {"config": {"url": URL, **cfg}}})


async def test_e2e_block_returns_400(tmp_path, monkeypatch):
    monkeypatch.setattr(
        WebhookReviewPlugin,
        "_make_client",
        lambda self: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"action": "block", "reason": "nsfw"})
            )
        ),
    )
    app = create_app(write_config(tmp_path, review_config()), config_watcher=False)
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "webhook_review_blocked"
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()


async def test_e2e_fail_open_on_dead_endpoint(tmp_path):
    """Connection-refused endpoint, fail-open: request flows through untouched."""
    app = create_app(
        write_config(tmp_path, review_config(url="http://127.0.0.1:1/dead")), config_watcher=False
    )
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "m1", "messages": [{"role": "user", "content": "hello"}]},
            )
            assert response.status_code == 200
            gateway = app.state.gateway
            registry = gateway.metrics.registry
            # Windows firewalls often DROP to a closed port (timeout) instead of
            # refusing; both outcomes mean the fail-open path was exercised.
            outcomes = [
                registry.get_sample_value(
                    "gateway_guardrail_flags_total", {"plugin": "webhook-review-plugin", "verdict": v}
                )
                for v in ("error", "timeout")
            ]
            assert 1 in outcomes
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()


async def test_e2e_fail_close_returns_503(tmp_path):
    app = create_app(
        write_config(
            tmp_path, review_config(url="http://127.0.0.1:1/dead", on_unavailable="fail-close")
        ),
        config_watcher=False,
    )
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "m1", "messages": [{"role": "user", "content": "hello"}]},
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "webhook_review_unavailable"
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()


async def test_e2e_streaming_post_delivery_block_recorded(tmp_path, monkeypatch):
    def stage_handler(req: httpx.Request) -> httpx.Response:
        stage = json.loads(req.content)["stage"]
        action = "block" if stage == "response" else "allow"
        return httpx.Response(200, json={"action": action})

    monkeypatch.setattr(
        WebhookReviewPlugin,
        "_make_client",
        lambda self: httpx.AsyncClient(transport=httpx.MockTransport(stage_handler)),
    )
    config = review_config(check=["request", "response"])
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    try:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            assembled = ""
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "m1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as response:
                assert response.status_code == 200  # delivery unaffected by late verdict
                async for line in response.aiter_lines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        assembled += json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
            assert "mock reply" in assembled
            gateway = app.state.gateway
            assert gateway.metrics.registry.get_sample_value(
                "gateway_guardrail_flags_total", {"plugin": "webhook-review-plugin", "verdict": "blocked"}
            ) == 1
        finally:
            await client.aclose()
    finally:
        await app.state.gateway.aclose()
