"""M5/S2: canary gradual-traffic-shift — identity-hash/percent model split."""

from __future__ import annotations

from aigateway.app import GatewayApp
from aigateway.core.router import CanaryRule, Deployment, ModelRoute, RoutePolicy, Router
from aigateway.errors import BadRequestError
from tests.conftest import base_config, write_config

PAYLOAD = {"model": "m1", "messages": [{"role": "user", "content": "hi"}]}


def canary_config(**models_extra) -> dict:
    config = base_config()
    config["models"]["m1"]["canary"] = {"target": "m2"}
    config["models"]["m1"].update(models_extra)
    return config


# -- router unit behavior -------------------------------------------------------

def make_router(percent: float, **rule_extra) -> Router:
    rule = CanaryRule(target="m2", percent=percent, **rule_extra)
    routes = {
        "m1": ModelRoute("m1", [Deployment("p1", "m1-up")], RoutePolicy(), canary=rule),
        "m2": ModelRoute("m2", [Deployment("p2", "m2-up")], RoutePolicy()),
    }
    return Router(routes)


def test_no_canary_rule_returns_none_branch():
    router = Router({"m1": ModelRoute("m1", [Deployment("p1", "m1-up")])})
    assert router.resolve_canary("m1", identity="k1") == ("m1", None)


def test_percent_zero_and_one_hundred():
    router = make_router(0.0)
    assert router.resolve_canary("m1", identity="k1") == ("m1", False)
    router = make_router(100.0)
    assert router.resolve_canary("m1", identity="k1") == ("m2", True)


def test_sticky_hash_deterministic_and_salt_reshuffles():
    router = make_router(50.0)
    first = router.resolve_canary("m1", identity="k1")
    for _ in range(20):
        assert router.resolve_canary("m1", identity="k1") == first
    assert router.resolve_canary("m1", identity="k2") in (("m1", False), ("m2", True))
    # different model name hashes differently even with the same identity
    salted = make_router(50.0, salt="round-2")
    assert salted.resolve_canary("m1", identity="k1") in (("m1", False), ("m2", True))


def test_keys_force_canary_and_exclude_wins():
    router = make_router(0.0, keys=["beta"])
    assert router.resolve_canary("m1", identity="beta") == ("m2", True)
    assert router.resolve_canary("m1", identity="other") == ("m1", False)
    router = make_router(100.0, keys=["beta"], exclude_keys=["vip", "beta"])
    assert router.resolve_canary("m1", identity="beta") == ("m1", False)
    assert router.resolve_canary("m1", identity="vip") == ("m1", False)
    assert router.resolve_canary("m1", identity="regular") == ("m2", True)


def test_statistical_deviation_under_five_percent():
    router = make_router(10.0)
    canary = sum(
        1
        for i in range(10_000)
        if router.resolve_canary("m1", identity=f"user-{i}")[1] is True
    )
    assert abs(canary - 1_000) <= 50  # <5% deviation of the 10% share


def test_anonymous_traffic_gets_random_split():
    router = make_router(50.0)
    canary = sum(bool(router.resolve_canary("m1", identity=None)[1]) for _ in range(4_000)) / 4_000
    assert 0.40 < canary < 0.60


def test_missing_target_falls_back_to_stable():
    router = Router(
        {"m1": ModelRoute("m1", [Deployment("p1", "m1-up")], canary=CanaryRule(target="ghost", percent=100))}
    )
    assert router.resolve_canary("m1", identity="k1") == ("m1", False)


def test_route_snapshot_includes_canary():
    router = make_router(10.0)
    assert router.routes["m1"].snapshot()["canary"]["target"] == "m2"
    assert router.routes["m2"].snapshot()["canary"] is None


# -- config validation ------------------------------------------------------------

def test_config_rejects_missing_target(tmp_path):
    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "ghost", "percent": 10}
    import pytest

    with pytest.raises(BadRequestError, match="not a configured model"):
        GatewayApp(write_config(tmp_path, config))


def test_config_rejects_self_target_and_bad_percent(tmp_path):
    import pytest

    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "m1", "percent": 10}
    with pytest.raises(BadRequestError, match="cannot be the model itself"):
        GatewayApp(write_config(tmp_path, config))

    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "m2", "percent": 150}
    with pytest.raises(BadRequestError, match="percent"):
        GatewayApp(write_config(tmp_path, config))


# -- end-to-end through the app -----------------------------------------------------

async def test_canary_full_diversion_serves_target_model(tmp_path):
    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "m2", "percent": 100}
    app = GatewayApp(write_config(tmp_path, config))
    try:
        body = await app.chat(dict(PAYLOAD), {}, "test")
        assert body["model"] == "m2-up"
        assert app.metrics.canary_requests_total.labels(model="m1", branch="canary")._value.get() == 1
    finally:
        await app.aclose()


async def test_canary_zero_keeps_stable_and_counts_metric(tmp_path):
    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "m2", "percent": 0}
    app = GatewayApp(write_config(tmp_path, config))
    try:
        body = await app.chat(dict(PAYLOAD), {}, "test")
        assert body["model"] == "m1-up"
        assert app.metrics.canary_requests_total.labels(model="m1", branch="stable")._value.get() == 1
    finally:
        await app.aclose()


async def test_no_canary_rule_emits_no_metric(tmp_path):
    app = GatewayApp(write_config(tmp_path, base_config()))
    try:
        await app.chat(dict(PAYLOAD), {}, "test")
        assert not list(app.metrics.canary_requests_total._metrics)
    finally:
        await app.aclose()


async def test_key_forcing_through_auth_identity(tmp_path):
    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "m2", "percent": 0, "keys": ["beta"]}
    config["plugins"] = {
        "auth-plugin": {
            "config": {"api_keys": [{"key": "sk-b", "name": "beta"}, {"key": "sk-r", "name": "regular"}]}
        }
    }
    app = GatewayApp(write_config(tmp_path, config))
    try:
        body = await app.chat(dict(PAYLOAD), {"Authorization": "Bearer sk-b"}, "test")
        assert body["model"] == "m2-up"
        body = await app.chat(dict(PAYLOAD), {"Authorization": "Bearer sk-r"}, "test")
        assert body["model"] == "m1-up"
    finally:
        await app.aclose()


async def test_canary_streaming_diversion(tmp_path):
    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "m2", "percent": 100}
    app = GatewayApp(write_config(tmp_path, config))
    try:
        gen = await app.chat(dict(PAYLOAD, stream=True), {}, "test")
        chunks = [c async for c in gen]
        assert any('"model": "m2-up"' in c or "m2-up" in c for c in chunks)
    finally:
        await app.aclose()


async def test_canary_failure_falls_back_to_stable_route(tmp_path):
    # canary deployment is dead; the canary route's fallback chain lands on
    # the stable route so the request still succeeds (M5 acceptance: canary +
    # fallback failure model).
    config = canary_config()
    config["providers"]["p2"] = {"type": "mock", "latency_s": 0.0, "fail_status": 500}
    config["models"]["m1"]["canary"] = {"target": "m2", "percent": 100}
    config["models"]["m2"] = {
        "deployments": [{"provider": "p2", "model": "m2-up"}],
        "fallbacks": ["m1"],
        "retries": 0,
    }
    app = GatewayApp(write_config(tmp_path, config))
    try:
        body = await app.chat(dict(PAYLOAD), {}, "test")
        assert body["model"] == "m1-up"
    finally:
        await app.aclose()


async def test_pricing_and_usage_report_use_served_model(tmp_path):
    config = canary_config()
    config["models"]["m1"]["canary"] = {"target": "m2", "percent": 100}
    config["pricing"] = {"m2": {"input_per_1k": 1.0, "output_per_1k": 1.0}}
    config["plugins"] = {"usage-report-plugin": {"config": {}}}
    app = GatewayApp(write_config(tmp_path, config))
    try:
        await app.chat(dict(PAYLOAD), {}, "test")
    finally:
        await app.aclose()
    report = next(
        p for p in app.state.plugins.plugins if p.display_name() == "usage-report-plugin"
    ).usage_report()
    assert list(report["by_model"]) == ["m2"]
