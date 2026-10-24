"""Prometheus metrics (custom registry per app instance, safe for tests)."""

from __future__ import annotations

from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.utils import INF


@dataclass
class Metrics:
    registry: CollectorRegistry = field(default_factory=CollectorRegistry)

    def __post_init__(self) -> None:
        r = self.registry
        self.requests_total = Counter(
            "gateway_requests_total",
            "Chat completion requests processed",
            ("model", "provider", "status"),
            registry=r,
        )
        self.request_latency = Histogram(
            "gateway_request_latency_seconds",
            "End-to-end request latency",
            ("model", "provider"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, INF),
            registry=r,
        )
        self.stream_first_token = Histogram(
            "gateway_stream_first_token_seconds",
            "Time to first streamed token",
            ("model", "provider"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, INF),
            registry=r,
        )
        self.tokens_total = Counter(
            "gateway_tokens_total",
            "Tokens processed",
            ("model", "provider", "direction"),
            registry=r,
        )
        self.plugin_rejections_total = Counter(
            "gateway_plugin_rejections_total",
            "Requests rejected by plugins",
            ("plugin", "code"),
            registry=r,
        )
        self.rate_limit_rejections_total = Counter(
            "gateway_rate_limit_rejections_total",
            "Requests rejected by rate limiting",
            ("model",),
            registry=r,
        )
        self.cache_requests_total = Counter(
            "gateway_cache_requests_total",
            "Cache lookups",
            ("model", "result"),
            registry=r,
        )
        self.upstream_attempts_total = Counter(
            "gateway_upstream_attempts_total",
            "Upstream attempts by outcome",
            ("model", "provider", "outcome"),
            registry=r,
        )

    # -- convenience wrappers -------------------------------------------------

    def observe_request(self, model: str, provider: str, status: int, latency_s: float) -> None:
        self.requests_total.labels(model=model, provider=provider, status=str(status)).inc()
        self.request_latency.labels(model=model, provider=provider).observe(latency_s)

    def observe_first_token(self, model: str, provider: str, latency_s: float) -> None:
        self.stream_first_token.labels(model=model, provider=provider).observe(latency_s)

    def inc_tokens(self, model: str, provider: str, prompt: int, completion: int) -> None:
        if prompt:
            self.tokens_total.labels(model=model, provider=provider, direction="prompt").inc(prompt)
        if completion:
            self.tokens_total.labels(model=model, provider=provider, direction="completion").inc(completion)

    def inc_plugin_rejection(self, plugin: str, code: str) -> None:
        self.plugin_rejections_total.labels(plugin=plugin, code=code).inc()

    def inc_rate_limit(self, model: str) -> None:
        self.rate_limit_rejections_total.labels(model=model).inc()

    def inc_cache(self, model: str, hit: bool) -> None:
        self.cache_requests_total.labels(model=model, result="hit" if hit else "miss").inc()

    def inc_attempt(self, model: str, provider: str, outcome: str) -> None:
        self.upstream_attempts_total.labels(model=model, provider=provider, outcome=outcome).inc()
