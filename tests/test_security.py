"""Security surface tests: body/message limits, admin auth."""

from __future__ import annotations

import httpx
import pytest

from aigateway.api import create_app
from aigateway.errors import AuthenticationError
from tests.conftest import base_config, write_config

pytestmark = pytest.mark.integration


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_oversized_body_rejected_413(tmp_path):
    config = base_config(server={"max_body_bytes": 1000})
    async with make_client(tmp_path, config) as client:
        big = "x" * 2000
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": big}]},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"


async def test_lying_content_length_still_capped(tmp_path):
    """A forged Content-Length doesn't bypass the cap: the stream is capped too."""
    config = base_config(server={"max_body_bytes": 100})
    async with make_client(tmp_path, config) as client:
        # httpx sets the true content-length; also test via raw content explicitly
        body = b'{"model":"m1","messages":[{"role":"user","content":"' + b"y" * 500 + b'"}]}'
        response = await client.post(
            "/v1/chat/completions",
            content=body,
            headers={"Content-Type": "application/json", "Content-Length": "10"},
        )
        assert response.status_code == 413


async def test_too_many_messages_rejected(tmp_path):
    config = base_config(server={"max_messages": 2})
    async with make_client(tmp_path, config) as client:
        messages = [{"role": "user", "content": f"m{i}"} for i in range(5)]
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": messages},
        )
        assert response.status_code == 413
        assert "too many messages" in response.json()["error"]["message"]


async def test_oversized_message_chars_rejected(tmp_path):
    config = base_config(server={"max_message_chars": 50})
    async with make_client(tmp_path, config) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "z" * 200}]},
        )
        assert response.status_code == 413


async def test_message_limits_allow_normal_traffic(tmp_path):
    config = base_config(server={"max_messages": 2, "max_message_chars": 50})
    async with make_client(tmp_path, config) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "fine"}]},
        )
        assert response.status_code == 200


async def test_admin_endpoints_require_key_when_configured(tmp_path):
    config = base_config(server={"admin_api_key": "admin-secret"})
    async with make_client(tmp_path, config) as client:
        for path in ("/metrics", "/admin/routes", "/admin/plugins"):
            assert (await client.get(path)).status_code == 401, path
            assert (
                await client.get(path, headers={"Authorization": "Bearer wrong"})
            ).status_code == 401
            assert (
                await client.get(path, headers={"Authorization": "Bearer admin-secret"})
            ).status_code == 200
        # health endpoints stay open for orchestrator probes
        assert (await client.get("/healthz")).status_code == 200


async def test_admin_endpoints_open_in_dev_mode(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        assert (await client.get("/metrics")).status_code == 200
        assert (await client.get("/admin/routes")).status_code == 200


# ---- GA security self-audit (v1.0) ---------------------------------------------


async def test_models_listing_requires_key_with_auth_plugin(tmp_path):
    """/v1/models goes through the auth plugin when configured: unauthenticated
    model enumeration is closed (OpenAI-compatible clients send the key anyway)."""
    config = base_config(
        plugins={"auth-plugin": {"config": {"api_keys": [{"key": "sk-k1", "name": "k1"}]}}}
    )
    async with make_client(tmp_path, config) as client:
        assert (await client.get("/v1/models")).status_code == 401
        assert (await client.get("/v1/models", headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await client.get("/v1/models", headers={"Authorization": "Bearer sk-k1"})
        assert ok.status_code == 200
        assert [m["id"] for m in ok.json()["data"]] == ["m1", "m2"]


async def test_models_listing_open_without_auth_plugin_or_anonymous(tmp_path):
    async with make_client(tmp_path, base_config()) as client:
        assert (await client.get("/v1/models")).status_code == 200
    config = base_config(plugins={"auth-plugin": {"config": {"allow_anonymous": True}}})
    async with make_client(tmp_path, config) as client:
        assert (await client.get("/v1/models")).status_code == 200


async def test_models_listing_accepts_dynamic_key(tmp_path):
    config = base_config(
        plugins={
            "auth-plugin": {"config": {"api_keys": [{"key": "sk-admin", "name": "admin"}]}},
        },
        server={"admin_api_key": "admin-secret"},
    )
    async with make_client(tmp_path, config) as client:
        created = await client.post(
            "/admin/keys",
            headers={"Authorization": "Bearer admin-secret"},
            json={"name": "dyn-1"},
        )
        assert created.status_code == 201
        raw = created.json()["api_key"]
        ok = await client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
        assert ok.status_code == 200


async def test_static_key_material_not_kept_in_memory(tmp_path):
    """GA key-storage audit: plaintext static keys are dropped from memory
    after digesting — record dumps/tracebacks can never leak them."""
    config = base_config(
        plugins={"auth-plugin": {"config": {"api_keys": [{"key": "sk-secret-plain", "name": "k1"}]}}}
    )
    from aigateway.app import GatewayApp

    app = GatewayApp(write_config(tmp_path, config))
    try:
        auth = next(p for p in app.state.plugins.plugins if p.display_name() == "auth-plugin")
        assert len(auth._by_hash) == 1
        for record in auth._by_hash.values():
            assert "key" not in record
        # authentication still works after the strip
        assert auth.authenticate({"Authorization": "Bearer sk-secret-plain"})["name"] == "k1"
        with pytest.raises(AuthenticationError):
            auth.authenticate({"Authorization": "Bearer wrong"})
    finally:
        await app.aclose()


async def test_request_log_payloads_off_by_default(tmp_path):
    """GA log-masking audit: payload logging is opt-in; identity name (not key
    material) is the only client-attributable field in records."""
    from aigateway.app import GatewayApp

    config = base_config(plugins={"request-log-plugin": {"config": {}}})
    app = GatewayApp(write_config(tmp_path, config))
    try:
        log_plugin = next(p for p in app.state.plugins.plugins if p.display_name() == "request-log-plugin")
        assert log_plugin.log_prompt is False
        assert log_plugin.log_response is False
    finally:
        await app.aclose()
