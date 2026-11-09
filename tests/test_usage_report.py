"""M4/S2: usage-report-plugin + /admin/usage aggregated report."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from aigateway.api import create_app
from aigateway.app import GatewayApp
from aigateway.plugins.usage_report import UsageReportPlugin, _Record
from tests.conftest import base_config, write_config

pytestmark = pytest.mark.integration

PAYLOAD = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def report_plugin_of(app: GatewayApp) -> UsageReportPlugin:
    return next(p for p in app.state.plugins.plugins if isinstance(p, UsageReportPlugin))


async def test_success_requests_are_recorded(tmp_path):
    app = GatewayApp(write_config(tmp_path, base_config(plugins={"usage-report-plugin": {"config": {}}})))
    try:
        await app.chat(dict(PAYLOAD), {}, "test")
    finally:
        await app.aclose()
    report = report_plugin_of(app).usage_report()
    assert report["totals"]["requests"] == 1
    # the mock provider reports prompt_tokens=12 / completion_tokens=20
    assert report["totals"]["prompt_tokens"] == 12
    assert report["totals"]["completion_tokens"] == 20
    assert report["by_model"]["m1"]["requests"] == 1
    assert report["by_key"]["anonymous"]["requests"] == 1
    assert len(report["buckets"]) == 1


async def test_streaming_recorded_once(tmp_path):
    app = GatewayApp(write_config(tmp_path, base_config(plugins={"usage-report-plugin": {"config": {}}})))
    try:
        gen = await app.chat(dict(PAYLOAD, stream=True), {}, "test")
        assert [c async for c in gen]
    finally:
        await app.aclose()
    assert report_plugin_of(app).usage_report()["totals"]["requests"] == 1


async def test_records_carry_key_identity(tmp_path):
    config = base_config(
        plugins={
            "auth-plugin": {"config": {"api_keys": [{"key": "sk-k1", "name": "k1"}]}},
            "usage-report-plugin": {"config": {}},
        }
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        await app.chat(dict(PAYLOAD), {"Authorization": "Bearer sk-k1"}, "test")
    finally:
        await app.aclose()
    report = report_plugin_of(app).usage_report()
    assert "anonymous" not in report["by_key"]
    assert report["by_key"]["k1"]["requests"] == 1


async def test_cache_hits_are_counted(tmp_path):
    config = base_config(
        plugins={
            "response-cache-plugin": {"config": {"ttl_s": 60}},
            "usage-report-plugin": {"config": {}},
        }
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        await app.chat(dict(PAYLOAD), {}, "test")
        await app.chat(dict(PAYLOAD), {}, "test")
    finally:
        await app.aclose()
    totals = report_plugin_of(app).usage_report()["totals"]
    assert totals["requests"] == 2
    assert totals["cache_hits"] == 1


async def test_rejections_are_recorded_as_errors(tmp_path):
    config = base_config(
        plugins={
            "keyword-filter-plugin": {"config": {"words": ["forbidden"]}},
            "usage-report-plugin": {"config": {}},
        }
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        blocked = dict(PAYLOAD)
        blocked["messages"] = [{"role": "user", "content": "forbidden content"}]
        try:
            await app.chat(blocked, {}, "test")
        except Exception:
            pass
        for _ in range(5):
            await asyncio.sleep(0)  # on_error runs fire-and-forget
    finally:
        await app.aclose()
    totals = report_plugin_of(app).usage_report()["totals"]
    assert totals["requests"] == 1
    assert totals["errors"] == 1


async def test_filters_and_windows(tmp_path):
    app = GatewayApp(write_config(tmp_path, base_config(plugins={"usage-report-plugin": {"config": {}}})))
    try:
        await app.chat(dict(PAYLOAD), {}, "test")
        await app.chat({"model": "m2", "messages": [{"role": "user", "content": "hi"}]}, {}, "test")
        plugin = report_plugin_of(app)
        assert plugin.usage_report(key="k1")["totals"]["requests"] == 0
        assert plugin.usage_report(model="m1")["totals"]["requests"] == 1
        assert list(plugin.usage_report(model="m2")["by_model"]) == ["m2"]
        day = plugin.usage_report(bucket="day")
        assert day["window"]["bucket"] == "day"
        assert day["totals"]["requests"] == 2
    finally:
        await app.aclose()


def test_memory_bounds_and_retention():
    plugin = UsageReportPlugin({"max_records": 3, "retention_hours": 1})
    now = time.time()
    for _ in range(10):
        plugin._records.append(
            _Record(
                ts=now,
                key="k",
                model="m1",
                provider="p",
                status=200,
                prompt_tokens=1,
                completion_tokens=1,
                cost_usd=0.0,
                cache_hit=False,
                error_code="",
            )
        )
    plugin._records.append(
        _Record(
            ts=now - 7200,
            key="old",
            model="m1",
            provider="p",
            status=200,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            cache_hit=False,
            error_code="",
        )
    )
    plugin._prune()
    assert len(plugin._records) == 3
    report = plugin.usage_report()
    assert report["recorded_records"] == 3
    assert report["totals"]["requests"] == 3  # the 2h-old record was pruned


async def test_admin_usage_endpoint_returns_report(tmp_path):
    config = base_config(plugins={"usage-report-plugin": {"config": {}}})
    client = make_client(tmp_path, config)
    try:
        await client.post("/v1/chat/completions", json=PAYLOAD)
        resp = await client.get("/admin/usage", params={"hours": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert body["report"]["totals"]["requests"] == 1
        resp = await client.get("/admin/usage", params={"bucket": "day"})
        assert resp.status_code == 200
        resp = await client.get("/admin/usage", params={"bucket": "week"})
        assert resp.status_code == 400
    finally:
        await client.aclose()


async def test_admin_usage_merges_budget_snapshot_and_report(tmp_path):
    config = base_config(
        plugins={
            "auth-plugin": {"config": {"api_keys": [{"key": "sk-k1", "name": "k1"}]}},
            "usage-report-plugin": {"config": {}},
        }
    )
    client = make_client(tmp_path, config)
    try:
        await client.post("/v1/chat/completions", json=PAYLOAD, headers={"Authorization": "Bearer sk-k1"})
        resp = await client.get("/admin/usage", params={"key": "k1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["keys"][0]["name"] == "k1"  # budget snapshot preserved
        assert body["report"]["by_key"]["k1"]["requests"] == 1
    finally:
        await client.aclose()
