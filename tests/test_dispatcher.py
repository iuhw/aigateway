"""Unit tests for the dispatcher: retry, failover, fallback chains, streaming."""

from __future__ import annotations

import pytest

from aigateway.core.dispatcher import Dispatcher
from aigateway.core.mock_provider import MockProvider
from aigateway.core.router import Deployment, ModelRoute, RoutePolicy, Router
from aigateway.errors import AllDeploymentsFailedError, UpstreamError


def build(providers: dict, routes: dict) -> Dispatcher:
    router = Router(routes)
    return Dispatcher(router, providers)


def payload() -> dict:
    return {"messages": [{"role": "user", "content": "hi"}], "stream": False}


async def test_retry_same_deployment_then_success():
    p1 = MockProvider("p1", fail_next_n=1, fail_status=500)
    route = ModelRoute(
        name="m", deployments=[Deployment("p1", "m")], policy=RoutePolicy(retries=2, retry_backoff_s=0.0)
    )
    dispatcher = build({"p1": p1}, {"m": route})
    result = await dispatcher.execute(route, payload())
    assert result.provider_status == 200
    assert result.attempts == 2
    assert p1.call_count == 2


async def test_failover_to_second_deployment():
    p1 = MockProvider("p1", fail_next_n=99)  # always 500
    p2 = MockProvider("p2")
    route = ModelRoute(
        name="m",
        deployments=[Deployment("p1", "m"), Deployment("p2", "m")],
        policy=RoutePolicy(retries=1, retry_backoff_s=0.0),
    )
    dispatcher = build({"p1": p1, "p2": p2}, {"m": route})
    result = await dispatcher.execute(route, payload())
    assert result.deployment.provider == "p2"
    # After enough failures p1 goes into cooldown.
    assert route.deployments[0].is_cooling_down()


async def test_transport_error_failover():
    p1 = MockProvider("p1", healthy=False)
    p2 = MockProvider("p2")
    route = ModelRoute(
        name="m", deployments=[Deployment("p1", "m"), Deployment("p2", "m")], policy=RoutePolicy(retries=0)
    )
    dispatcher = build({"p1": p1, "p2": p2}, {"m": route})
    result = await dispatcher.execute(route, payload())
    assert result.deployment.provider == "p2"


async def test_fatal_client_error_raises_immediately():
    p1 = MockProvider("p1", fail_status=400)
    p2 = MockProvider("p2")
    route = ModelRoute(
        name="m", deployments=[Deployment("p1", "m"), Deployment("p2", "m")], policy=RoutePolicy(retries=0)
    )
    dispatcher = build({"p1": p1, "p2": p2}, {"m": route})
    with pytest.raises(UpstreamError) as excinfo:
        await dispatcher.execute(route, payload())
    assert excinfo.value.status_code == 400
    assert p2.call_count == 0  # no failover on client error


async def test_failover_on_client_error_opt_in():
    p1 = MockProvider("p1", fail_status=400)
    p2 = MockProvider("p2")
    route = ModelRoute(
        name="m",
        deployments=[Deployment("p1", "m"), Deployment("p2", "m")],
        policy=RoutePolicy(retries=0, failover_on_client_error=True),
    )
    dispatcher = build({"p1": p1, "p2": p2}, {"m": route})
    result = await dispatcher.execute(route, payload())
    assert result.deployment.provider == "p2"


async def test_fallback_model_route_used():
    p1 = MockProvider("p1", fail_next_n=99)
    p2 = MockProvider("p2")
    primary = ModelRoute(
        name="primary",
        deployments=[Deployment("p1", "m")],
        policy=RoutePolicy(retries=0),
        fallbacks=["secondary"],
    )
    secondary = ModelRoute(name="secondary", deployments=[Deployment("p2", "m2")])
    dispatcher = build({"p1": p1, "p2": p2}, {"primary": primary, "secondary": secondary})
    result = await dispatcher.execute(primary, payload())
    assert result.route.name == "secondary"
    assert result.body["model"] == "m2"


async def test_all_failed_raises_503():
    p1 = MockProvider("p1", fail_next_n=99)
    route = ModelRoute(name="m", deployments=[Deployment("p1", "m")], policy=RoutePolicy(retries=0))
    dispatcher = build({"p1": p1}, {"m": route})
    with pytest.raises(AllDeploymentsFailedError) as excinfo:
        await dispatcher.execute(route, payload())
    assert excinfo.value.status_code == 503


async def test_stream_success_collects_chunks():
    p1 = MockProvider("p1", chunk_count=3)
    route = ModelRoute(name="m", deployments=[Deployment("p1", "m")], policy=RoutePolicy(retries=0))
    dispatcher = build({"p1": p1}, {"m": route})
    from aigateway.core.dispatcher import StreamOutcome

    outcome = StreamOutcome()
    chunks = [chunk async for chunk in dispatcher.stream(route, payload(), outcome)]
    assert len(chunks) >= 4  # content chunks + finish
    assert outcome.finish_reason == "stop"
    assert outcome.usage["prompt_tokens"] == 12
    assert outcome.first_chunk_latency_s is not None


async def test_stream_failover_before_first_chunk():
    p1 = MockProvider("p1", fail_next_n=99, fail_status=503)
    p2 = MockProvider("p2", chunk_count=2)
    route = ModelRoute(
        name="m", deployments=[Deployment("p1", "m"), Deployment("p2", "m")], policy=RoutePolicy(retries=0)
    )
    dispatcher = build({"p1": p1, "p2": p2}, {"m": route})
    from aigateway.core.dispatcher import StreamOutcome

    outcome = StreamOutcome()
    chunks = [chunk async for chunk in dispatcher.stream(route, payload(), outcome)]
    assert outcome.deployment.provider == "p2"
    assert outcome.finish_reason == "stop"
    assert all("error" not in c for c in chunks)


async def test_stream_retry_then_succeed():
    p1 = MockProvider("p1", fail_next_n=1, fail_status=500, chunk_count=2)
    route = ModelRoute(
        name="m", deployments=[Deployment("p1", "m")], policy=RoutePolicy(retries=1, retry_backoff_s=0.0)
    )
    dispatcher = build({"p1": p1}, {"m": route})
    from aigateway.core.dispatcher import StreamOutcome

    outcome = StreamOutcome()
    _chunks = [chunk async for chunk in dispatcher.stream(route, payload(), outcome)]
    assert outcome.attempts == 2
    assert outcome.finish_reason == "stop"


async def test_stream_client_error_no_failover():
    p1 = MockProvider("p1", fail_status=400)
    p2 = MockProvider("p2")
    route = ModelRoute(
        name="m", deployments=[Deployment("p1", "m"), Deployment("p2", "m")], policy=RoutePolicy(retries=0)
    )
    dispatcher = build({"p1": p1, "p2": p2}, {"m": route})
    from aigateway.core.dispatcher import StreamOutcome

    outcome = StreamOutcome()
    with pytest.raises(UpstreamError):
        _ = [chunk async for chunk in dispatcher.stream(route, payload(), outcome)]
    assert p2.call_count == 0


async def test_stream_after_first_chunk_error_propagates():
    """Once streaming started, an error cannot be retried transparently."""
    from aigateway.core.provider import UpstreamUnavailable

    class MidStreamFailProvider(MockProvider):
        def stream(self, *, model, payload, timeout):
            async def gen():
                yield {"choices": [{"delta": {"content": "partial"}}]}
                raise UpstreamUnavailable("mid-stream failure")

            return gen()

    provider = MidStreamFailProvider("p1")
    route = ModelRoute(
        name="m", deployments=[Deployment("p1", "m")], policy=RoutePolicy(retries=2, retry_backoff_s=0.0)
    )
    dispatcher = build({"p1": provider}, {"m": route})
    from aigateway.core.dispatcher import StreamOutcome

    outcome = StreamOutcome()
    chunks = []
    with pytest.raises(UpstreamUnavailable):
        async for chunk in dispatcher.stream(route, payload(), outcome):
            chunks.append(chunk)
    assert len(chunks) == 1  # first chunk reached the client before the failure
    assert outcome.error is not None
