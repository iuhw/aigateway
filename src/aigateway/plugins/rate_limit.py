"""Rate limiting plugin: per-model global and per-identity request limiting."""

from __future__ import annotations

import time
from typing import Any, ClassVar

from aigateway.errors import RateLimitError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin


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
    """

    default_order: ClassVar[int] = 40

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.default_rpm: float | None = self.config.get("default_rpm")
        self.model_rpm: dict[str, float] = {
            k: float(v) for k, v in (self.config.get("model_rpm") or {}).items()
        }
        self.identity_rpm: float | None = self.config.get("identity_rpm")
        self._model_buckets: dict[str, TokenBucket] = {}
        self._identity_buckets: dict[str, TokenBucket] = {}

    def _bucket(self, store: dict[str, TokenBucket], key: str, rpm: float) -> TokenBucket:
        bucket = store.get(key)
        if bucket is None or bucket.rate != rpm / 60.0:
            bucket = TokenBucket(rpm)
            store[key] = bucket
        return bucket

    def _limits_for(self, ctx: PluginContext) -> list[tuple[str, TokenBucket]]:
        limits: list[tuple[str, TokenBucket]] = []
        rpm = self.model_rpm.get(ctx.model, self.default_rpm)
        if rpm:
            limits.append((f"model:{ctx.model}", self._bucket(self._model_buckets, ctx.model, float(rpm))))
        identity = ctx.identity or {}
        identity_rpm = self.identity_rpm or identity.get("rpm")
        if identity_rpm:
            name = str(identity.get("name") or ctx.client_host or "anonymous")
            limits.append(
                (f"identity:{name}", self._bucket(self._identity_buckets, name, float(identity_rpm)))
            )
        return limits

    async def on_request(self, ctx: PluginContext) -> None:
        for label, bucket in self._limits_for(ctx):
            if not bucket.try_acquire():
                from aigateway.plugins.base import logger as plugin_logger

                plugin_logger.info("rate limit exceeded for %s", label)
                raise RateLimitError(
                    f"Rate limit exceeded for {label}",
                    code="rate_limit_exceeded",
                )
