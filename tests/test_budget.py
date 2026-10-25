"""Budget enforcement: per-key daily/monthly USD budgets with 429 rejection.

Covers the in-memory ledger (single process), the Redis shared ledger
(fakeredis TCP), the check-then-bill flow (request rejected BEFORE dispatch,
cost billed AFTER completion) and the fail-open behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from aigateway.api import create_app
from aigateway.plugins.auth import _key_hash, _window_keys
from tests.conftest import base_config, write_config


def make_client(tmp_path, config) -> httpx.AsyncClient:
    app = create_app(write_config(tmp_path, config), config_watcher=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def budget_config(**key_overrides) -> dict:
    key = {"key": "sk-b", "name": "budgeted", "budgets": {"daily_usd": 0.05}}
    key.update(key_overrides)
    return base_config(
        pricing={"m1": {"input_per_1k": 1.0, "output_per_1k": 2.0}},
        plugins={"auth-plugin": {"config": {"api_keys": [key]}}},
    )


AUTH = {"Authorization": "Bearer sk-b"}


def auth_payload() -> dict:
    return {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}


async def test_request_within_budget_passes_and_is_billed(tmp_path):
    async with make_client(tmp_path, budget_config()) as client:
        app = client._transport.app  # type: ignore[attr-defined]
        auth = next(
            p for p in app.state.gateway.state.plugins.plugins if type(p).__name__ == "AuthPlugin"
        )
        response = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert response.status_code == 200
        day_key, _, _, _ = _window_keys("budgeted", datetime.now(UTC))[0]
        spent = await auth._backend.get_spend(day_key)
        # mock usage: 12 prompt + 20 completion -> 0.012 + 0.04 = 0.052 USD
        assert abs(spent - 0.052) < 1e-9


async def test_daily_budget_exhaustion_rejects_429(tmp_path):
    config = budget_config()
    config["plugins"]["auth-plugin"]["config"]["api_keys"][0]["budgets"] = {"daily_usd": 0.05}
    async with make_client(tmp_path, config) as client:
        first = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert first.status_code == 200  # 0.052 spent... already over 0.05 budget
        second = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert second.status_code == 429
        error = second.json()["error"]
        assert error["code"] == "budget_exceeded"
        assert "daily" in error["message"]


async def test_monthly_budget_checked_and_billed(tmp_path):
    config = budget_config()
    config["plugins"]["auth-plugin"]["config"]["api_keys"][0]["budgets"] = {"monthly_usd": 0.1}
    async with make_client(tmp_path, config) as client:
        app = client._transport.app  # type: ignore[attr-defined]
        auth = next(
            p for p in app.state.gateway.state.plugins.plugins if type(p).__name__ == "AuthPlugin"
        )
        ok = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert ok.status_code == 200
        month_key = _window_keys("budgeted", datetime.now(UTC))[1][0]
        assert abs(await auth._backend.get_spend(month_key) - 0.052) < 1e-9
        # 2 x 0.052 = 0.104 > 0.1 monthly budget -> third request rejected
        second = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert second.status_code == 200
        denied = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert denied.status_code == 429
        assert "monthly" in denied.json()["error"]["message"]


async def test_key_without_budget_unlimited(tmp_path):
    config = budget_config(key="sk-free", name="free", budgets=None)
    headers = {"Authorization": "Bearer sk-free"}
    async with make_client(tmp_path, config) as client:
        for _ in range(4):
            response = await client.post("/v1/chat/completions", json=auth_payload(), headers=headers)
            assert response.status_code == 200


async def test_wrong_key_still_401(tmp_path):
    async with make_client(tmp_path, budget_config()) as client:
        response = await client.post(
            "/v1/chat/completions", json=auth_payload(), headers={"Authorization": "Bearer sk-wrong"}
        )
        assert response.status_code == 401


async def test_budget_keys_partitioned_by_key_name(tmp_path):
    config = budget_config()
    config["plugins"]["auth-plugin"]["config"]["api_keys"].append(
        {"key": "sk-c", "name": "other", "budgets": {"daily_usd": 10.0}}
    )
    async with make_client(tmp_path, config) as client:
        budgeted = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert budgeted.status_code == 200
        other = await client.post(
            "/v1/chat/completions", json=auth_payload(), headers={"Authorization": "Bearer sk-c"}
        )
        assert other.status_code == 200  # separate ledger
        rejected = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert rejected.status_code == 429


async def test_budget_not_billed_for_errors(tmp_path):
    config = budget_config(models=None)
    config["models"] = {"m1": {"deployments": [{"provider": "p1", "model": "x"}]}}
    async with make_client(tmp_path, config) as client:
        app = client._transport.app  # type: ignore[attr-defined]
        auth = next(
            p for p in app.state.gateway.state.plugins.plugins if type(p).__name__ == "AuthPlugin"
        )
        denied = await client.post(
            "/v1/chat/completions",
            json={"model": "missing", "messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH,
        )
        assert denied.status_code == 404
        day_key = _window_keys("budgeted", datetime.now(UTC))[0][0]
        assert await auth._backend.get_spend(day_key) == 0.0


# ---- shared ledger across instances (fakeredis TCP) -------------------------------


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


async def test_redis_ledger_shared_across_instances(tmp_path, redis_url):
    config = budget_config()
    config["shared_state"] = {"backend": "redis", "redis_url": redis_url}
    async with make_client(tmp_path, config) as first, make_client(tmp_path, config) as second:
        ok = await first.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert ok.status_code == 200
        # second instance sees the same ledger -> budget exhausted there too
        rejected = await second.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert rejected.status_code == 429


async def test_budget_fail_open_when_backend_down(tmp_path):
    config = budget_config()
    config["shared_state"] = {"backend": "redis", "redis_url": "redis://127.0.0.1:1/0"}
    config["plugins"]["auth-plugin"]["config"]["budget_fail_open"] = True
    async with make_client(tmp_path, config) as client:
        response = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert response.status_code == 200  # ledger unreachable -> allow


async def test_budget_fail_closed_when_backend_down(tmp_path):
    config = budget_config()
    config["shared_state"] = {"backend": "redis", "redis_url": "redis://127.0.0.1:1/0"}
    config["plugins"]["auth-plugin"]["config"]["budget_fail_open"] = False
    async with make_client(tmp_path, config) as client:
        response = await client.post("/v1/chat/completions", json=auth_payload(), headers=AUTH)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "budget_exceeded"


# ---- unit-level -----------------------------------------------------------------


def test_window_keys_shapes():
    now = datetime(2026, 10, 25, 23, 59, 59, tzinfo=UTC)
    keys = _window_keys("alice", now)
    assert keys[0][0] == "alice:2026-10-25"
    assert keys[1][0] == "alice:2026-10"
    assert keys[0][1] == "daily" and keys[1][1] == "monthly"
    assert keys[0][3] == "daily_usd" and keys[1][3] == "monthly_usd"


def test_key_hash_stable():
    assert _key_hash("abc") == _key_hash("abc")
    assert _key_hash("abc") != _key_hash("abd")
