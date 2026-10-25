"""Unit tests for model routing: strategies, cooldown, priority tiers."""

from __future__ import annotations

import time

import pytest

from aigateway.core.router import Deployment, ModelRoute, RoutePolicy, Router, RoutingStrategy


def make_router(strategy=RoutingStrategy.ROUND_ROBIN, **policy_kwargs) -> tuple[Router, ModelRoute]:
    route = ModelRoute(
        name="m",
        deployments=[
            Deployment(provider="p1", model="m-a"),
            Deployment(provider="p2", model="m-b"),
        ],
        policy=RoutePolicy(strategy=strategy, **policy_kwargs),
    )
    return Router({route.name: route}), route


def test_round_robin_cycles_all_deployments():
    router, route = make_router()
    picked = [router.pick(route).key for _ in range(4)]
    assert picked[0] != picked[1]
    assert set(picked) == {"p1/m-a", "p2/m-b"}


def test_weighted_respects_distribution():
    route = ModelRoute(
        name="m",
        deployments=[
            Deployment(provider="heavy", model="m", weight=9),
            Deployment(provider="light", model="m", weight=1),
        ],
        policy=RoutePolicy(strategy=RoutingStrategy.WEIGHTED),
    )
    router = Router({route.name: route})
    counts = {"heavy/m": 0, "light/m": 0}
    for _ in range(200):
        picked = router.pick(route)
        counts[picked.key] += 1
    assert counts["heavy/m"] > counts["light/m"] * 3


def test_least_latency_prefers_fastest():
    router, route = make_router(strategy=RoutingStrategy.LEAST_LATENCY)
    router.report_success(route.deployments[0], 0.5)
    router.report_success(route.deployments[1], 0.01)
    for _ in range(5):
        assert router.pick(route).key == "p2/m-b"


def test_cooldown_after_allowed_fails():
    router, route = make_router(allowed_fails=2, cooldown_s=60)
    router.report_failure(route.deployments[0], route)
    assert not route.deployments[0].is_cooling_down()
    router.report_failure(route.deployments[0], route)
    assert route.deployments[0].is_cooling_down()
    # Deployment 0 is excluded while cooling down.
    assert router.pick(route).key == "p2/m-b"


def test_cooldown_expires():
    router, route = make_router(allowed_fails=1, cooldown_s=0.05)
    router.report_failure(route.deployments[0], route)
    assert route.deployments[0].is_cooling_down()
    # Poll instead of a fixed sleep: time.sleep can return early under coarse
    # OS timer resolution, and the test must not depend on that precision.
    deadline = time.monotonic() + 2.0
    while route.deployments[0].is_cooling_down() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not route.deployments[0].is_cooling_down()
    assert router.pick(route) is not None


def test_success_resets_failure_counter():
    router, route = make_router(allowed_fails=2)
    router.report_failure(route.deployments[0], route)
    router.report_success(route.deployments[0], 0.1)
    assert route.deployments[0].consecutive_failures == 0


def test_priority_tiers():
    route = ModelRoute(
        name="m",
        deployments=[
            Deployment(provider="cheap", model="m", priority=0),
            Deployment(provider="expensive", model="m", priority=1),
        ],
    )
    router = Router({route.name: route})
    for _ in range(3):
        assert router.pick(route).key == "cheap/m"
    # Cooling down the cheap tier promotes the expensive one.
    route.deployments[0].cooldown_until = time.monotonic() + 60
    assert router.pick(route).key == "expensive/m"


def test_pick_returns_none_when_all_cooling():
    router, route = make_router(allowed_fails=1)
    for d in route.deployments:
        d.cooldown_until = time.monotonic() + 60
    assert router.pick(route) is None
    # excluded set also forces None
    assert router.pick(route, exclude={"p1/m-a", "p2/m-b"}) is None


def test_ema_latency_updated_on_success():
    router, route = make_router()
    router.report_success(route.deployments[0], 1.0)
    router.report_success(route.deployments[0], 0.0)
    assert 0 < route.deployments[0].ema_latency_s < 1.0


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        RoutePolicy(strategy=RoutingStrategy("bogus"))
