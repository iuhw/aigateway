"""Execution engine: retries, failover, cooldown reporting and fallback chains.

Failure model (mirrors LiteLLM semantics, see docs/research-*.md):

1. Transport errors (connect/timeout) and retryable HTTP statuses
   (408/409/429/5xx) -> retry the same deployment with exponential backoff,
   then fail over to the next healthy deployment (cooldown counted).
2. Client errors (other 4xx) -> request is considered invalid; surfaced to the
   caller with the upstream status/body unless `failover_on_client_error`.
3. Route exhausted -> next fallback model route; all exhausted -> 503.

For streams, failover only happens *before the first chunk* reaches the client.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aigateway.core.openai_provider import UpstreamStreamError
from aigateway.core.provider import Provider, UpstreamUnavailable
from aigateway.core.router import Deployment, ModelRoute, Router
from aigateway.errors import AllDeploymentsFailedError, GatewayError, UpstreamError

RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUSES


def _backoff(base_s: float, attempt: int) -> float:
    return min(base_s * (2**attempt), 8.0) + random.uniform(0, base_s * 0.1)


@dataclass
class ExecutionResult:
    """Non-streaming execution outcome."""

    body: dict[str, Any]
    route: ModelRoute
    deployment: Deployment
    provider_status: int
    latency_s: float
    attempts: int


@dataclass
class StreamOutcome:
    """Mutable metadata filled in while a stream is being consumed."""

    route: ModelRoute | None = None
    deployment: Deployment | None = None
    attempts: int = 0
    first_chunk_latency_s: float | None = None
    total_latency_s: float | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    error: GatewayError | None = None
    served_chunks: int = 0


class Dispatcher:
    def __init__(self, router: Router, providers: dict[str, Provider]):
        self.router = router
        self.providers = providers

    def _plan(self, route: ModelRoute) -> list[ModelRoute]:
        plan = [route]
        for name in route.fallbacks:
            fallback = self.router.route(name)
            if fallback is not None and fallback not in plan:
                plan.append(fallback)
        return plan

    async def execute(self, route: ModelRoute, payload: dict[str, Any]) -> ExecutionResult:
        """Run a non-streaming chat completion through retry/failover/fallback."""
        attempts_total = 0
        last_error: GatewayError | None = None

        for current_route in self._plan(route):
            exclude: set[str] = set()
            while True:
                deployment = self.router.pick(current_route, exclude=exclude)
                if deployment is None:
                    break  # route exhausted -> next fallback route
                exclude.add(deployment.key)
                provider = self.providers.get(deployment.provider)
                if provider is None:
                    last_error = UpstreamError(f"unknown provider {deployment.provider!r}")
                    continue

                per_deployment_attempts = current_route.policy.retries + 1
                for attempt in range(per_deployment_attempts):
                    attempts_total += 1
                    try:
                        result = await provider.chat(
                            model=deployment.model,
                            payload=payload,
                            timeout=current_route.policy.timeout_s,
                        )
                    except UpstreamUnavailable as exc:
                        last_error = exc
                        self.router.report_failure(deployment, current_route)
                        if attempt < per_deployment_attempts - 1:
                            await asyncio.sleep(_backoff(current_route.policy.retry_backoff_s, attempt))
                        continue

                    if result.status < 400:
                        self.router.report_success(deployment, result.latency_s)
                        return ExecutionResult(
                            body=result.body,
                            route=current_route,
                            deployment=deployment,
                            provider_status=result.status,
                            latency_s=result.latency_s,
                            attempts=attempts_total,
                        )

                    last_error = UpstreamError(
                        _upstream_error_message(result.body) or f"upstream {result.status}",
                        status_code=result.status if result.status >= 400 else 502,
                        code=_upstream_error_code(result.body),
                    )
                    retryable = is_retryable_status(
                        result.status
                    ) or current_route.policy.failover_on_client_error
                    if retryable:
                        self.router.report_failure(deployment, current_route)
                        if attempt < per_deployment_attempts - 1:
                            await asyncio.sleep(_backoff(current_route.policy.retry_backoff_s, attempt))
                        continue
                    # Fatal client error: do not burn further deployments.
                    self.router.report_failure(deployment, current_route)
                    raise last_error

        raise AllDeploymentsFailedError(
            f"all deployments failed for model {route.name!r}"
            + (f": {last_error}" if last_error else "")
        )

    async def stream(
        self, route: ModelRoute, payload: dict[str, Any], outcome: StreamOutcome
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a chat completion; failover happens only before the first chunk."""
        started = time.perf_counter()
        last_error: GatewayError | None = None

        for current_route in self._plan(route):
            exclude: set[str] = set()
            while True:
                deployment = self.router.pick(current_route, exclude=exclude)
                if deployment is None:
                    break
                exclude.add(deployment.key)
                provider = self.providers.get(deployment.provider)
                if provider is None:
                    last_error = UpstreamError(f"unknown provider {deployment.provider!r}")
                    continue

                per_deployment_attempts = current_route.policy.retries + 1
                for attempt in range(per_deployment_attempts):
                    self._outcome_attempts_bump(outcome)
                    got_first_chunk = False
                    chunk_iterator = provider.stream(
                        model=deployment.model,
                        payload=payload,
                        timeout=current_route.policy.timeout_s,
                    )
                    try:
                        async for chunk in chunk_iterator:
                            if not got_first_chunk:
                                got_first_chunk = True
                                outcome.route = current_route
                                outcome.deployment = deployment
                                outcome.first_chunk_latency_s = time.perf_counter() - started
                            if isinstance(chunk.get("usage"), dict):
                                outcome.usage = chunk["usage"]
                            for choice in chunk.get("choices") or []:
                                if choice.get("finish_reason"):
                                    outcome.finish_reason = choice["finish_reason"]
                            yield chunk
                            outcome.served_chunks += 1
                    except UpstreamStreamError as exc:
                        error = UpstreamError(
                            _error_body_message(exc.body_text) or f"upstream {exc.status_code}",
                            status_code=exc.status_code,
                        )
                        last_error = error
                        if got_first_chunk:
                            outcome.error = error
                            raise error from exc
                    except UpstreamUnavailable as exc:
                        last_error = exc
                        if got_first_chunk:
                            outcome.error = exc
                            raise
                    else:
                        if got_first_chunk:
                            outcome.total_latency_s = time.perf_counter() - started
                            self.router.report_success(
                                deployment, outcome.total_latency_s - (outcome.first_chunk_latency_s or 0.0)
                            )
                            return
                        # Provider produced no chunks at all: treat as failure.
                        last_error = UpstreamError(
                            f"provider {deployment.provider!r} produced an empty stream"
                        )

                    self.router.report_failure(deployment, current_route)
                    if attempt < per_deployment_attempts - 1:
                        await asyncio.sleep(_backoff(current_route.policy.retry_backoff_s, attempt))

                    fatal_client_error = (
                        isinstance(last_error, UpstreamError)
                        and last_error.status_code < 500
                        and last_error.status_code not in RETRYABLE_STATUSES
                        and not current_route.policy.failover_on_client_error
                    )
                    if fatal_client_error:
                        outcome.error = last_error
                        raise last_error

        outcome.error = AllDeploymentsFailedError(
            f"all deployments failed for streaming model {route.name!r}"
            + (f": {last_error}" if last_error else "")
        )
        raise outcome.error

    @staticmethod
    def _outcome_attempts_bump(outcome: StreamOutcome) -> None:
        outcome.attempts += 1


def _upstream_error_message(body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if error is not None:
        return str(error)
    return str(body.get("message") or "")


def _upstream_error_code(body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return "upstream_error"


def _error_body_message(text: str) -> str:
    try:
        import json

        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return _upstream_error_message(parsed)
    except (json.JSONDecodeError, ValueError):
        pass
    return ""
