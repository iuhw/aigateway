"""M1 tenant governance: key lifecycle (expires_at / concurrency / metadata),
per-window token quotas, the per-key cost metric and the /admin/keys +
/admin/usage API (including dynamic-key CRUD over the shared-state backend).

The mock provider reports prompt_tokens=12 / completion_tokens=20 per
request, so one billed request = 32 tokens and 0.052 USD at the pricing
used below.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aigateway.api import create_app
from aigateway.errors import BadRequestError
from tests.conftest import base_config, write_config

PAYLOAD = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}
AUTH = {"Authorization": "Bearer sk-k1"}


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def key_config(**overrides) -> dict:
    key = {"key": "sk-k1", "name": "k1"}
    key.update(overrides)
    return base_config(plugins={"auth-plugin": {"config": {"api_keys": [key]}}})


def auth_plugin_of(client: httpx.AsyncClient):
    app = client._transport.app  # type: ignore[attr-defined]
    return next(
        p for p in app.state.gateway.state.plugins.plugins if type(p).__name__ == "AuthPlugin"
    )


def admin_headers(config) -> dict[str, str]:
    token = (config.get("server") or {}).get("admin_api_key")
    return {"Authorization": f"Bearer {token}"} if token else {}


# ---- key lifecycle: expires_at -------------------------------------------------


async def test_expired_key_rejected_403(tmp_path):
    expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    async with make_client(tmp_path, key_config(expires_at=expired)) as client:
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "key_expired"


async def test_future_expiry_still_valid(tmp_path):
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    async with make_client(tmp_path, key_config(expires_at=future)) as client:
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code == 200


def test_invalid_expires_at_rejected_at_load(tmp_path):
    with pytest.raises(BadRequestError):
        create_app(write_config(tmp_path, key_config(expires_at="not-a-date")), config_watcher=False)


# ---- token quotas ---------------------------------------------------------------


async def test_daily_token_quota_exhaustion(tmp_path):
    async with make_client(tmp_path, key_config(token_quota={"daily": 32})) as client:
        first = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert first.status_code == 200  # 12 prompt + 20 completion = 32 used
        second = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert second.status_code == 429
        error = second.json()["error"]
        assert error["code"] == "token_quota_exceeded"
        assert "daily" in error["message"]


async def test_monthly_token_quota_checked_and_billed(tmp_path):
    async with make_client(tmp_path, key_config(token_quota={"monthly": 64})) as client:
        assert (await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)).status_code == 200
        assert (await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)).status_code == 200
        third = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert third.status_code == 429
        assert "monthly" in third.json()["error"]["message"]


async def test_token_quota_not_billed_on_cache_hit(tmp_path):
    # Quota billing follows budget semantics: cache hits consume no upstream
    # tokens and are not billed (the pre-request check still applies when the
    # window is already exhausted — same as USD budgets).
    config = key_config(token_quota={"daily": 100})
    config["plugins"]["response-cache-plugin"] = {"config": {"ttl_s": 300}}
    async with make_client(tmp_path, config) as client:
        assert (await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)).status_code == 200
        hit = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert hit.status_code == 200  # served from cache, quota untouched
        plugin = auth_plugin_of(client)
        now = datetime.now(UTC)
        day_key = f"tokquota:k1:{now.strftime('%Y-%m-%d')}"
        assert await plugin._backend.get_spend(day_key) == 32.0


async def test_streaming_bills_token_quota(tmp_path):
    async with make_client(tmp_path, key_config(token_quota={"daily": 32})) as client:
        async with client.stream(
            "POST", "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        ) as response:
            async for _line in response.aiter_lines():
                pass
        next_req = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert next_req.status_code == 429


async def test_key_without_quota_unlimited(tmp_path):
    async with make_client(tmp_path, key_config()) as client:
        for _ in range(3):
            response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
            assert response.status_code == 200


# ---- per-key concurrency ----------------------------------------------------------


async def test_concurrency_cap_rejects_and_releases(tmp_path):
    config = key_config(concurrency=1)
    config["providers"] = {"p1": {"type": "mock", "latency_s": 0.3}}
    async with make_client(tmp_path, config) as client:
        r1, r2 = await asyncio.gather(
            client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH),
            client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH),
        )
        assert sorted([r1.status_code, r2.status_code]) == [200, 429]
        rejected = r1 if r1.status_code == 429 else r2
        assert rejected.json()["error"]["code"] == "concurrency_exceeded"
        # released after completion -> the next sequential request passes
        after = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert after.status_code == 200


async def test_concurrency_released_after_stream(tmp_path):
    config = key_config(concurrency=1)
    async with make_client(tmp_path, config) as client:
        async with client.stream(
            "POST", "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        ) as response:
            async for _line in response.aiter_lines():
                pass
        after = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert after.status_code == 200


async def test_concurrency_released_on_error(tmp_path):
    config = key_config(concurrency=1)
    async with make_client(tmp_path, config) as client:
        bad = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "model": "missing"}, headers=AUTH
        )
        assert bad.status_code == 404
        after = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert after.status_code == 200


# ---- per-key cost metric ----------------------------------------------------------


async def test_per_key_cost_metric(tmp_path):
    config = key_config()
    config["pricing"] = {"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}}
    async with make_client(tmp_path, config) as client:
        await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        metrics = client._transport.app.state.gateway.metrics  # type: ignore[attr-defined]
        value = metrics.registry.get_sample_value(
            "gateway_key_cost_usd_total", {"key": "k1"}
        )
        assert value is not None and abs(value - 0.052) < 1e-9


async def test_per_key_cost_skips_anonymous(tmp_path):
    config = base_config(
        plugins={"auth-plugin": {"config": {"allow_anonymous": True}}},
        pricing={"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
    )
    async with make_client(tmp_path, config) as client:
        await client.post("/v1/chat/completions", json=PAYLOAD)
        metrics = client._transport.app.state.gateway.metrics  # type: ignore[attr-defined]
        assert metrics.registry.get_sample_value("gateway_key_cost_usd_total", {"key": "anonymous"}) is None


async def test_per_key_cost_label_cap_aggregates_other(tmp_path):
    config = base_config(
        server={"max_tracked_keys": 1},
        plugins={
            "auth-plugin": {
                "config": {
                    "api_keys": [
                        {"key": "sk-k1", "name": "k1"},
                        {"key": "sk-k2", "name": "k2"},
                    ]
                }
            }
        },
        pricing={"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
    )
    async with make_client(tmp_path, config) as client:
        await client.post("/v1/chat/completions", json=PAYLOAD, headers={"Authorization": "Bearer sk-k1"})
        await client.post("/v1/chat/completions", json=PAYLOAD, headers={"Authorization": "Bearer sk-k2"})
        metrics = client._transport.app.state.gateway.metrics  # type: ignore[attr-defined]
        assert metrics.registry.get_sample_value("gateway_key_cost_usd_total", {"key": "k1"}) is not None
        assert metrics.registry.get_sample_value("gateway_key_cost_usd_total", {"key": "k2"}) is None
        assert abs(
            metrics.registry.get_sample_value("gateway_key_cost_usd_total", {"key": "other"}) - 0.052
        ) < 1e-9


# ---- admin API: keys ----------------------------------------------------------------


async def test_admin_list_keys_masks_key_material(tmp_path):
    async with make_client(tmp_path, key_config()) as client:
        response = await client.get("/admin/keys")
        assert response.status_code == 200
        body = response.json()
        assert body["keys"][0]["name"] == "k1"
        assert body["keys"][0]["source"] == "config"
        assert "sk-k1" not in response.text
        assert body["keys"][0]["key_hash"].endswith("…")


async def test_admin_create_and_use_dynamic_key(tmp_path):
    async with make_client(tmp_path, key_config()) as client:
        created = await client.post(
            "/admin/keys",
            json={"name": "dyn1", "budgets": {"daily_usd": 5.0}, "metadata": {"team": "ops"}},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["api_key"].startswith("sk-dyn-")
        assert body["key"]["source"] == "dynamic"
        assert "sk-dyn-" not in json.dumps(body["key"])  # raw key only in api_key field
        headers = {"Authorization": f"Bearer {body['api_key']}"}
        used = await client.post("/v1/chat/completions", json=PAYLOAD, headers=headers)
        assert used.status_code == 200


async def test_admin_dynamic_key_honors_models_and_expiry(tmp_path):
    async with make_client(tmp_path, key_config()) as client:
        created = await client.post(
            "/admin/keys",
            json={"name": "limited", "models": ["m2"]},
        )
        headers = {"Authorization": f"Bearer {created.json()['api_key']}"}
        denied = await client.post("/v1/chat/completions", json=PAYLOAD, headers=headers)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "model_access_denied"
        expired = await client.post(
            "/admin/keys",
            json={"name": "old", "expires_at": "2020-01-01T00:00:00+00:00"},
        )
        old_headers = {"Authorization": f"Bearer {expired.json()['api_key']}"}
        gone = await client.post("/v1/chat/completions", json=PAYLOAD, headers=old_headers)
        assert gone.status_code == 403
        assert gone.json()["error"]["code"] == "key_expired"


async def test_admin_duplicate_name_rejected(tmp_path):
    async with make_client(tmp_path, key_config()) as client:
        dup_static = await client.post("/admin/keys", json={"name": "k1"})
        assert dup_static.status_code == 400
        await client.post("/admin/keys", json={"name": "dyn1"})
        dup_dynamic = await client.post("/admin/keys", json={"name": "dyn1"})
        assert dup_dynamic.status_code == 400


async def test_admin_delete_dynamic_key(tmp_path):
    config = key_config()
    config["plugins"]["auth-plugin"]["config"]["dynamic_key_cache_s"] = 0
    async with make_client(tmp_path, config) as client:
        created = await client.post("/admin/keys", json={"name": "dyn1"})
        api_key = created.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        assert (await client.post("/v1/chat/completions", json=PAYLOAD, headers=headers)).status_code == 200
        deleted = await client.delete("/admin/keys/dyn1")
        assert deleted.status_code == 200
        assert (await client.post("/v1/chat/completions", json=PAYLOAD, headers=headers)).status_code == 401
        missing = await client.delete("/admin/keys/dyn1")
        assert missing.status_code == 404
        static = await client.delete("/admin/keys/k1")
        assert static.status_code == 409
        assert static.json()["error"]["code"] == "key_not_deletable"


async def test_admin_endpoints_require_admin_key(tmp_path):
    config = key_config()
    config["server"] = {"admin_api_key": "admin-secret"}
    async with make_client(tmp_path, config) as client:
        for method, path, kwargs in (
            ("GET", "/admin/keys", {}),
            ("GET", "/admin/usage", {}),
            ("POST", "/admin/keys", {"json": {"name": "x"}}),
            ("DELETE", "/admin/keys/x", {}),
        ):
            response = await client.request(method, path, **kwargs)
            assert response.status_code == 401, path
        ok = await client.get("/admin/keys", headers={"Authorization": "Bearer admin-secret"})
        assert ok.status_code == 200


async def test_admin_keys_without_auth_plugin_404(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        response = await client.get("/admin/keys")
        assert response.status_code == 404


# ---- admin API: usage ----------------------------------------------------------------


async def test_admin_usage_reports_budget_and_quota(tmp_path):
    config = key_config(budgets={"daily_usd": 10.0}, token_quota={"daily": 1000})
    config["pricing"] = {"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}}
    async with make_client(tmp_path, config) as client:
        await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        usage = (await client.get("/admin/usage", params={"key": "k1"})).json()
        entry = usage["keys"][0]
        assert entry["name"] == "k1"
        assert entry["expired"] is False
        assert abs(entry["budgets"]["daily"]["spent_usd"] - 0.052) < 1e-9
        assert entry["budgets"]["daily"]["limit_usd"] == 10.0
        assert entry["token_quota"]["daily"]["used_tokens"] == 32.0
        assert entry["token_quota"]["daily"]["limit_tokens"] == 1000


async def test_admin_usage_key_without_limits(tmp_path):
    async with make_client(tmp_path, key_config()) as client:
        usage = (await client.get("/admin/usage")).json()
        assert usage["keys"][0]["limited"] is False
        assert "budgets" not in usage["keys"][0]
        assert "token_quota" not in usage["keys"][0]


# ---- shared backend (fakeredis TCP): cross-instance dynamic keys + quotas -------------


@pytest.fixture
async def redis_url():
    pytest.importorskip("fakeredis", reason="fakeredis not installed (dev extra)")
    import socket
    import threading

    from fakeredis import TcpFakeServer

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = TcpFakeServer(("127.0.0.1", port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"redis://127.0.0.1:{port}/0"
    server.shutdown()
    thread.join(timeout=5)


async def test_redis_dynamic_key_shared_across_instances(tmp_path, redis_url):
    config = key_config()
    config["shared_state"] = {"backend": "redis", "redis_url": redis_url}
    async with make_client(tmp_path, config) as first, make_client(tmp_path, config) as second:
        created = await first.post("/admin/keys", json={"name": "dyn1"})
        api_key = created.json()["api_key"]
        headers = {"Authorization": f"Bearer {api_key}"}
        used = await second.post("/v1/chat/completions", json=PAYLOAD, headers=headers)
        assert used.status_code == 200


async def test_redis_token_quota_shared_across_instances(tmp_path, redis_url):
    config = key_config(token_quota={"daily": 32})
    config["shared_state"] = {"backend": "redis", "redis_url": redis_url}
    async with make_client(tmp_path, config) as first, make_client(tmp_path, config) as second:
        assert (await first.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)).status_code == 200
        rejected = await second.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert rejected.status_code == 429
        assert rejected.json()["error"]["code"] == "token_quota_exceeded"
