"""Cost tracking: pricing config -> gateway_cost_usd_total metric."""

from __future__ import annotations

import httpx

from aigateway.api import create_app
from tests.conftest import base_config, write_config


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_priced_model_accumulates_cost(tmp_path):
    config = base_config(pricing={"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}})
    async with make_client(tmp_path, config) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        app = client._transport.app  # type: ignore[attr-defined]
        metrics = app.state.gateway.metrics
        # mock provider usage: prompt 12, completion 20
        # cost = 12/1000*1.0 + 20/1000*2.0 = 0.012 + 0.04 = 0.052
        value = metrics.registry.get_sample_value("gateway_cost_usd_total", {"model": "m1"})
        assert value is not None and abs(value - 0.052) < 1e-9


async def test_unpriced_model_costs_nothing(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
        )
        metrics = client._transport.app.state.gateway.metrics  # type: ignore[attr-defined]
        assert metrics.registry.get_sample_value("gateway_cost_usd_total", {"model": "m1"}) is None


async def test_cache_hit_is_free(tmp_path):
    config = base_config(
        pricing={"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
        plugins={"response-cache-plugin": {"config": {"ttl_s": 300}}},
    )
    async with make_client(tmp_path, config) as client:
        payload = {"model": "m1", "messages": [{"role": "user", "content": "cache me"}]}
        await client.post("/v1/chat/completions", json=payload)
        await client.post("/v1/chat/completions", json=payload)  # cache hit
        metrics = client._transport.app.state.gateway.metrics  # type: ignore[attr-defined]
        value = metrics.registry.get_sample_value("gateway_cost_usd_total", {"model": "m1"})
        assert value is not None and abs(value - 0.052) < 1e-9  # only the first call


async def test_streaming_cost_accumulates(tmp_path):
    config = base_config(pricing={"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}})
    async with make_client(tmp_path, config) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as response:
            async for _line in response.aiter_lines():
                pass
        metrics = client._transport.app.state.gateway.metrics  # type: ignore[attr-defined]
        value = metrics.registry.get_sample_value("gateway_cost_usd_total", {"model": "m1"})
        assert value is not None and abs(value - 0.052) < 1e-9
