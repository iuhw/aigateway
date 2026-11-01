"""OTLP trace export plugin — blueprint M4 (`otel-export-plugin`).

Exports one SERVER span per gateway request (plus one CLIENT sub-span for the
upstream dispatch window, and a TTFT event on streamed requests) to an OTLP/HTTP
collector. Two invariants drive the design:

  - **Fire-and-forget**: spans are handed to a ``BatchSpanProcessor`` (bounded
    background queue, export on a worker thread) — collector outages add zero
    request-path latency and never fail a request. Every OTel call in the hooks
    is additionally guarded; a tracing bug must never mask a completion.
  - **No content**: spans carry identity/metadata (model, key, tokens, cost,
    status, cache-hit, TTFT), never prompt or completion text.

Span tree::

    gateway.chat | gateway.embeddings    SERVER — full request lifetime
      gateway.upstream                   CLIENT — dispatch window (+attempts)
      (event) gateway.first_token        TTFT on streamed requests

Boundaries (documented in operations.md):
  - the sub-span aggregates attempts; the failover fan-out itself lives in
    dispatch logs (per-attempt spans would need dispatcher instrumentation,
    deliberately not done in M4 to keep core untouched);
  - a client disconnect mid-stream leaves the span unfinished — it is dropped
    by the SDK (never exported), not leaked beyond the span object;
  - a rejection raised by a response-side plugin ordered AFTER this one is
    visible in metrics/logs, but the already-ended span keeps its dispatch-era
    status (give the plugin a higher ``order`` to trade coverage for accuracy).

Requires the optional ``[otel]`` extra (opentelemetry-sdk + OTLP/HTTP exporter).
The module itself imports cleanly without it — plugin modules are imported for
side-effect registration at gateway startup.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from aigateway.errors import GatewayError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin

logger = logging.getLogger("aigateway.plugins.otel_export")

REQUEST_SPAN_CHAT = "gateway.chat"
REQUEST_SPAN_EMBEDDINGS = "gateway.embeddings"
UPSTREAM_SPAN_NAME = "gateway.upstream"
TTFT_EVENT_NAME = "gateway.first_token"


@register_plugin
class OtelExportPlugin(Plugin):
    """Traces requests to an OTLP/HTTP endpoint.

    Config: ``endpoint`` (required, e.g. ``http://otel-collector:4318/v1/traces``),
    ``headers`` (auth for SaaS backends), ``timeout_s`` (5), ``sample_ratio``
    (1.0), ``service_name`` (aigateway), ``resource`` (extra resource attrs).
    """

    name: ClassVar[str] = "otel-export-plugin"
    default_order: ClassVar[int] = 5  # open the span before every other hook

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        cfg = self.config
        self.endpoint = str(cfg.get("endpoint", "")).strip()
        if "://" not in self.endpoint:
            raise ValueError(
                "otel_export: 'endpoint' must be an absolute OTLP/HTTP URL, "
                "e.g. http://otel-collector:4318/v1/traces"
            )
        self.headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.timeout_s = float(cfg.get("timeout_s", 5.0))
        self.sample_ratio = min(1.0, max(0.0, float(cfg.get("sample_ratio", 1.0))))
        self.service_name = str(cfg.get("service_name", "aigateway"))
        self.resource_attrs = {str(k): str(v) for k, v in (cfg.get("resource") or {}).items()}

        try:  # availability probe; real imports stay lazy so hooks stay cheap
            import opentelemetry.exporter.otlp.proto.http.trace_exporter  # noqa: F401
            import opentelemetry.sdk.resources  # noqa: F401
            import opentelemetry.sdk.trace  # noqa: F401
            import opentelemetry.trace  # noqa: F401
        except ImportError as exc:
            raise ValueError(
                "otel_export: the otel-export-plugin needs the optional [otel] extra "
                "(pip install 'aigateway[otel]')"
            ) from exc

        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        from opentelemetry.trace import SpanKind, Status, StatusCode

        self._SpanKind = SpanKind
        self._Status = Status
        self._StatusCode = StatusCode
        self._set_span_in_context = otel_trace.set_span_in_context

        provider = TracerProvider(
            resource=Resource.create({"service.name": self.service_name, **self.resource_attrs}),
            # children (upstream sub-span) follow the root decision
            sampler=ParentBased(TraceIdRatioBased(self.sample_ratio)),
        )
        provider.add_span_processor(self._build_processor())
        self._provider = provider
        self._tracer = provider.get_tracer("aigateway.plugins.otel_export")

    # -- construction seams (tests swap these for in-memory exporters) ---------

    def _build_span_exporter(self):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=self.endpoint, headers=self.headers, timeout=self.timeout_s)

    def _build_processor(self):
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        return BatchSpanProcessor(self._build_span_exporter())

    # -- hooks -------------------------------------------------------------------

    async def on_request(self, ctx: PluginContext) -> None:
        try:
            span = self._tracer.start_span(
                self._span_name(ctx),
                kind=self._SpanKind.SERVER,
                attributes={
                    "gateway.request_id": ctx.request_id,
                    "gateway.model": ctx.model,
                    "gateway.stream": ctx.stream,
                    "gateway.client": ctx.client_host,
                },
            )
        except Exception:  # noqa: BLE001 - tracing must never break the request path
            logger.exception("otel_export: failed to start span for %s", ctx.request_id)
            return
        ctx.state[self.display_name()] = {"span": span, "ended": False, "ttft_emitted": False}

    async def on_stream_chunk(self, ctx: PluginContext, chunk: dict[str, Any]) -> dict[str, Any] | None:
        state = ctx.state.get(self.display_name())
        if state and not state["ttft_emitted"] and state.get("span") is not None:
            state["ttft_emitted"] = True
            try:
                state["span"].add_event(
                    TTFT_EVENT_NAME, {"gateway.ttft_s": round(ctx.first_token_latency_s or 0.0, 6)}
                )
            except Exception:  # noqa: BLE001
                logger.exception("otel_export: failed to record TTFT for %s", ctx.request_id)
        return chunk

    async def on_response(self, ctx: PluginContext) -> None:
        self._finish(ctx)

    async def on_stream_end(self, ctx: PluginContext) -> None:
        self._finish(ctx)

    async def on_error(self, ctx: PluginContext, error: GatewayError) -> None:
        self._finish(ctx, error=error)

    async def aclose(self) -> None:
        """Flush pending spans on shutdown (blocking briefly is fine here)."""
        try:
            self._provider.force_flush(timeout_millis=int(self.timeout_s * 1000))
            self._provider.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("otel_export: provider shutdown failed")

    # -- span finishing ------------------------------------------------------------

    def _finish(self, ctx: PluginContext, error: GatewayError | None = None) -> None:
        state = ctx.state.get(self.display_name())
        if not state or state["ended"]:
            return
        state["ended"] = True
        try:
            span = state["span"]
            span.set_attributes(self._final_attrs(ctx, error))
            self._export_upstream_span(ctx, state)
            if error is not None:
                span.set_status(self._Status(self._StatusCode.ERROR, str(error)))
                span.add_event(
                    "exception",
                    {"exception.type": type(error).__name__, "exception.message": str(error)},
                )
            span.end()
        except Exception:  # noqa: BLE001 - export plumbing must never mask a completion
            logger.exception("otel_export: failed to close spans for %s", ctx.request_id)

    def _export_upstream_span(self, ctx: PluginContext, state: dict[str, Any]) -> None:
        """Close the dispatch window as a CLIENT child span with explicit
        timestamps; absent for cache hits and pre-dispatch rejections."""
        started, ended = ctx.upstream_started_at, ctx.upstream_ended_at
        if started is None or ended is None:
            return
        child = self._tracer.start_span(
            UPSTREAM_SPAN_NAME,
            context=self._set_span_in_context(state["span"]),
            start_time=int(started * 1_000_000_000),
            kind=self._SpanKind.CLIENT,
            attributes=self._upstream_attrs(ctx, started, ended),
        )
        child.end(end_time=int(ended * 1_000_000_000))

    # -- attribute mapping -----------------------------------------------------------

    @staticmethod
    def _span_name(ctx: PluginContext) -> str:
        return REQUEST_SPAN_EMBEDDINGS if "input" in ctx.payload else REQUEST_SPAN_CHAT

    def _final_attrs(self, ctx: PluginContext, error: GatewayError | None) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "gateway.cache_hit": bool(ctx.cache_hit),
            "gateway.status_code": int(ctx.status_code or 0),
        }
        identity = ctx.identity if isinstance(ctx.identity, dict) else {}
        attrs["gateway.key"] = str(identity.get("name") or "anonymous")
        if ctx.attempts is not None:
            attrs["gateway.attempts"] = int(ctx.attempts)
        if ctx.prompt_tokens is not None:
            attrs["gateway.prompt_tokens"] = int(ctx.prompt_tokens or 0)
        if ctx.completion_tokens is not None:
            attrs["gateway.completion_tokens"] = int(ctx.completion_tokens or 0)
        if ctx.cost_usd is not None:
            attrs["gateway.cost_usd"] = round(ctx.cost_usd, 6)
        if ctx.first_token_latency_s is not None:
            attrs["gateway.ttft_s"] = round(ctx.first_token_latency_s, 6)
        if ctx.deployment is not None:
            attrs["gateway.provider"] = ctx.deployment.provider
            attrs["gateway.upstream_model"] = ctx.deployment.model
        failure = error or ctx.error
        if failure is not None:
            attrs["gateway.error_code"] = failure.code
        return attrs

    def _upstream_attrs(self, ctx: PluginContext, started: float, ended: float) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "gateway.attempts": int(ctx.attempts or 1),
            "gateway.upstream_latency_s": round(ended - started, 6),
        }
        if ctx.deployment is not None:
            attrs["gateway.provider"] = ctx.deployment.provider
            attrs["gateway.upstream_model"] = ctx.deployment.model
        return attrs
