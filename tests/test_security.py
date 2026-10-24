"""Security surface tests: body/message limits, admin auth."""

from __future__ import annotations

import httpx

from aigateway.api import create_app
from tests.conftest import base_config, write_config


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
