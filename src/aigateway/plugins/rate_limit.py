"""Rate limiting plugin: per-model global and per-identity request limiting.

Backends:
- ``memory`` (default): exact within one process; behind N workers each worker
  enforces its own bucket (limits effectively multiplied by N).
- ``redis``: shared atomic token bucket across all instances (production).
  Requires the optional ``redis`` dependency and the gateway's shared_state
  backend (injected via :meth:`set_backend` by the app on startup).

Redis outage policy: with ``fail_open: true`` (default) requests pass through
when the backend is unreachable — availability beats strict limiting; every
failure increments ``gateway_state_backend_errors_total``. Set false to fail
closed (429).
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

from aigateway.errors import RateLimitError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin
from aigateway.state import InMemoryBackend, StateBackend, StateBackendError


class TokenBucket:
    """Classic token bucket; capacity defaults to one minute's worth of requests
    (i.e. a burst up to the rpm limit is allowed, matching gateway semantics)."""

    def __init__(self, rate_per_minute: float, capacity: float | None = None):
        self.rate = rate_per_minute / 60.0
        self.capacity = capacity if capacity is not None else max(1.0, float(rate_per_minute))
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def try_acquire(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

    def retry_after_s(self, amount: float = 1.0) -> float:
        deficit = amount - self.tokens
        return max(0.0, deficit / self.rate) if self.rate > 0 else 60.0


@register_plugin
class RateLimitPlugin(Plugin):
    """Token-bucket rate limiting.

    Config:
      - ``model_rpm``: {model_name: requests_per_minute} global per model
      - ``default_rpm``: fallback for unlisted models
      - ``identity_rpm``: per API-key limit (overrides the key's own rpm)
      - ``capacity``: bucket burst size override (default: the rpm itself)
      - ``backend``: ``memory`` (default) or ``redis`` (shared; injected)
      - ``fail_open``: allow requests when the shared backend is down (default true)
    """

    default_order: ClassVar[int] = 40

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.default_rpm: float | None = self.config.get("default_rpm")
        self.model_rpm: dict[str, float] = {
            k: float(v) for k, v in (self.config.get("model_rpm") or {}).items()
        }
        self.identity_rpm: float | None = self.config.get("identity_rpm")
        self.capacity: float | None = (
            float(self.config["capacity"]) if self.config.get("capacity") is not None else None
        )
        self.fail_open: bool = bool(self.config.get("fail_open", True))
        self._backend: StateBackend = InMemoryBackend()
        self._metrics = None

    def set_shared(self, backend: StateBackend, metrics: Any) -> None:
        """Called by GatewayApp to inject the shared backend and metrics sink."""
        self._backend = backend
        self._metrics = metrics

    def _limits_for(self, ctx: PluginContext) -> list[tuple[str, float]]:
        limits: list[tuple[str, float]] = []
        rpm = self.model_rpm.get(ctx.model, self.default_rpm)
        if rpm:
            limits.append((f"model:{ctx.model}", float(rpm)))
        identity = ctx.identity or {}
        identity_rpm = self.identity_rpm or identity.get("rpm")
        if identity_rpm:
            name = str(identity.get("name") or ctx.client_host or "anonymous")
            limits.append((f"identity:{name}", float(identity_rpm)))
        return limits

    async def on_request(self, ctx: PluginContext) -> None:
        from aigateway.plugins.base import logger as plugin_logger

        for scope, rpm in self._limits_for(ctx):
            try:
                allowed, retry_after = await self._backend.acquire_bucket(
                    scope, rpm, capacity=self.capacity or rpm
                )
            except StateBackendError as exc:
                plugin_logger.warning("rate-limit backend unavailable (%s): %s", scope, exc)
                self._record_backend_error()
                if self.fail_open:
                    continue
                raise RateLimitError(
                    "Rate limit backend unavailable",
                    code="rate_limit_exceeded",
                ) from exc
            if not allowed:
                plugin_logger.info("rate limit exceeded for %s", scope)
                raise RateLimitError(
                    f"Rate limit exceeded for {scope}",
                    code="rate_limit_exceeded",
                )

    def _record_backend_error(self) -> None:
        if self._metrics is not None:
            self._metrics.inc_state_backend_error("rate_limit")
