"""Tiered pricing: cache_read/cache_write rates + provider usage normalization."""

from __future__ import annotations

import httpx

from aigateway.api import create_app
from aigateway.core.anthropic_provider import from_anthropic_response
from aigateway.core.bedrock_provider import from_converse_response
from aigateway.core.gemini_provider import from_gemini_response
from aigateway.schemas import cache_tokens_of
from tests.conftest import base_config, write_config


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_cache_tokens_of_gateway_convention():
    assert cache_tokens_of({"cache_read_tokens": 10, "cache_write_tokens": 5}) == (10, 5)
    assert cache_tokens_of({}) == (0, 0)


def test_cache_tokens_of_openai_details():
    usage = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 30}}
    assert cache_tokens_of(usage) == (30, 0)


def test_anthropic_usage_normalization_with_cache():
    body = from_anthropic_response(
        {
            "id": "msg_1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10,
                "output_tokens": 25,
            },
        },
        model="claude",
    )
    assert body["usage"] == {
        "prompt_tokens": 150,  # 100 uncached + 40 read + 10 write
        "completion_tokens": 25,
        "total_tokens": 175,
        "cache_read_tokens": 40,
        "cache_write_tokens": 10,
    }


def test_anthropic_usage_without_cache_fields():
    body = from_anthropic_response(
        {
            "id": "msg_1",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        },
        model="claude",
    )
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


def test_bedrock_usage_normalization_with_cache():
    body = from_converse_response(
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 50,
                "cacheReadInputTokens": 20,
                "cacheWriteInputTokens": 8,
                "outputTokens": 9,
                "totalTokens": 87,
            },
        },
        model="m",
    )
    assert body["usage"]["prompt_tokens"] == 78
    assert body["usage"]["cache_read_tokens"] == 20
    assert body["usage"]["cache_write_tokens"] == 8


def test_gemini_usage_cache_read():
    body = from_gemini_response(
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 60,  # includes cached per Gemini convention
                "candidatesTokenCount": 7,
                "totalTokenCount": 67,
                "cachedContentTokenCount": 30,
            },
        },
        model="gemini",
    )
    assert body["usage"]["prompt_tokens"] == 60
    assert body["usage"]["cache_read_tokens"] == 30


async def test_tiered_pricing_computes_breakdown(tmp_path):
    """mock usage 12 prompt + 20 completion; inject cache split via provider body."""
    config = base_config(
        pricing={
            "m1": {
                "input_per_1k": 1.0,
                "output_per_1k": 2.0,
                "cache_read_per_1k": 0.1,
                "cache_write_per_1k": 1.25,
            }
        }
    )
    async with make_client(tmp_path, config) as client:
        app = client._transport.app  # type: ignore[attr-defined]
        gateway = app.state.gateway
        from aigateway.plugins.base import PluginContext

        ctx = PluginContext(request_id="t", model="m1", payload={}, stream=False)
        ctx.prompt_tokens = 100
        ctx.completion_tokens = 200
        ctx.cache_read_tokens = 40
        ctx.cache_write_tokens = 10
        ctx.usage_reported = True
        gateway._record_cost(ctx)
        # (100-40-10)/1000*1.0 + 200/1000*2.0 + 40/1000*0.1 + 10/1000*1.25
        # = 0.05 + 0.4 + 0.004 + 0.0125 = 0.4665
        assert abs(ctx.cost_usd - 0.4665) < 1e-9


async def test_tiered_rates_default_to_input_price(tmp_path):
    config = base_config(pricing={"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}})
    async with make_client(tmp_path, config) as client:
        gateway = client._transport.app.state.gateway  # type: ignore[attr-defined]
        from aigateway.plugins.base import PluginContext

        ctx = PluginContext(request_id="t", model="m1", payload={}, stream=False)
        ctx.prompt_tokens = 100
        ctx.completion_tokens = 0
        ctx.cache_read_tokens = 100  # all cached, no explicit cache rate
        ctx.usage_reported = True
        gateway._record_cost(ctx)
        # uncached 0; cache_read bills at input_per_1k fallback -> 0.1
        assert abs(ctx.cost_usd - 0.1) < 1e-9


async def test_usage_with_cache_details_recorded_end_to_end(tmp_path):
    """An OpenAI-style upstream that reports prompt_tokens_details.cached_tokens
    gets tiered billing through the normal chat pipeline (wire-level mock)."""
    import httpx as httpx_mod

    from aigateway.core.openai_provider import OpenAICompatibleProvider

    def handler(request: httpx_mod.Request) -> httpx_mod.Response:
        return httpx_mod.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hey"}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            },
        )

    config = base_config(
        pricing={
            "m1": {
                "input_per_1k": 1.0,
                "output_per_1k": 2.0,
                "cache_read_per_1k": 0.1,
            }
        }
    )
    path = write_config(tmp_path, config)
    app = create_app(path, config_watcher=False)
    provider = OpenAICompatibleProvider(
        "p1", base_url="http://upstream.test/v1", transport=httpx_mod.MockTransport(handler)
    )
    app.state.gateway.state.providers["p1"] = provider
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            "/v1/chat/completions", json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]}
        )
        assert response.status_code == 200
        gateway = app.state.gateway
        metrics = gateway.metrics
        # (100-80)/1000*1.0 + 50/1000*2.0 + 80/1000*0.1 = 0.02 + 0.1 + 0.008 = 0.128
        value = metrics.registry.get_sample_value("gateway_cost_usd_total", {"model": "m1"})
        assert value is not None and abs(value - 0.128) < 1e-9
    await app.state.gateway.aclose()
