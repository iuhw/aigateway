"""M4/S1: otel-export-plugin — span tree, TTFT event, fire-and-forget export."""

from __future__ import annotations

import asyncio
import sys

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from aigateway.app import GatewayApp
from aigateway.plugins.otel_export import (
    REQUEST_SPAN_CHAT,
    REQUEST_SPAN_EMBEDDINGS,
    TTFT_EVENT_NAME,
    UPSTREAM_SPAN_NAME,
    OtelExportPlugin,
)
from tests.conftest import base_config, write_config

pytestmark = pytest.mark.unit

ENDPOINT = "http://127.0.0.1:4318/v1/traces"


@pytest.fixture
def span_exporter(monkeypatch) -> InMemorySpanExporter:
    """Swap the OTLP batch processor for a synchronous in-memory one so tests
    can assert on finished spans right after the request completes."""
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(OtelExportPlugin, "_build_processor", lambda self: SimpleSpanProcessor(exporter))
    return exporter


def make_otel_app(tmp_path, plugins: dict | None = None) -> GatewayApp:
    plugin_cfgs = {"otel-export-plugin": {"config": {"endpoint": ENDPOINT}}}
    plugin_cfgs.update(plugins or {})
    return GatewayApp(write_config(tmp_path, base_config(plugins=plugin_cfgs)))


async def chat_once(app: GatewayApp, **overrides):
    body = {"model": "m1", "messages": [{"role": "user", "content": "hi"}], **overrides}
    return await app.chat(body, {}, "test")


def spans_by_name(exporter) -> dict:
    return {s.name: s for s in exporter.get_finished_spans()}


async def test_non_streaming_span_tree(tmp_path, span_exporter):
    app = make_otel_app(tmp_path)
    try:
        assert await chat_once(app) is not None
    finally:
        await app.aclose()
    spans = spans_by_name(span_exporter)
    # single-attempt success: one span, dispatch numbers as attributes
    assert REQUEST_SPAN_CHAT in spans and UPSTREAM_SPAN_NAME not in spans
    parent = spans[REQUEST_SPAN_CHAT]
    attrs = dict(parent.attributes)
    assert parent.kind == SpanKind.SERVER
    assert attrs["gateway.model"] == "m1"
    assert attrs["gateway.request_id"]
    assert attrs["gateway.status_code"] == 200
    assert attrs["gateway.attempts"] == 1
    assert attrs["gateway.cache_hit"] is False
    assert attrs["gateway.key"] == "anonymous"
    assert attrs["gateway.provider"] == "p1"
    assert attrs["gateway.upstream_latency_s"] >= 0


async def test_failover_emits_upstream_sub_span(tmp_path, span_exporter):
    config = base_config(
        providers={"dead": {"type": "mock", "fail_status": 500}, "p1": {"type": "mock"}},
        models={
            "m1": {
                "deployments": [
                    {"provider": "dead", "model": "x"},
                    {"provider": "p1", "model": "m1-up"},
                ],
                "allowed_fails": 999999,
            }
        },
        plugins={"otel-export-plugin": {"config": {"endpoint": ENDPOINT}}},
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        assert await chat_once(app) is not None
    finally:
        await app.aclose()
    spans = spans_by_name(span_exporter)
    parent, child = spans[REQUEST_SPAN_CHAT], spans[UPSTREAM_SPAN_NAME]
    attempts = dict(parent.attributes)["gateway.attempts"]
    assert attempts >= 2  # dead deployment retried, then failed over to p1
    assert child.kind == SpanKind.CLIENT
    assert child.parent.span_id == parent.get_span_context().span_id
    assert dict(child.attributes)["gateway.attempts"] == attempts


async def test_streaming_emits_ttft_event(tmp_path, span_exporter):
    app = make_otel_app(tmp_path)
    try:
        gen = await chat_once(app, stream=True)
        chunks = [c async for c in gen]
        assert chunks
    finally:
        await app.aclose()
    spans = spans_by_name(span_exporter)
    parent = spans[REQUEST_SPAN_CHAT]
    assert dict(parent.attributes)["gateway.stream"] is True
    ttft_events = [e for e in parent.events if e.name == TTFT_EVENT_NAME]
    assert len(ttft_events) == 1
    assert ttft_events[0].attributes["gateway.ttft_s"] >= 0


async def test_rejection_span_is_error_without_upstream(tmp_path, span_exporter):
    app = make_otel_app(tmp_path, plugins={"keyword-filter-plugin": {"config": {"words": ["forbidden"]}}})
    try:
        with pytest.raises(Exception):
            await chat_once(app, messages=[{"role": "user", "content": "forbidden content"}])
        for _ in range(5):
            await asyncio.sleep(0)  # on_error runs fire-and-forget
    finally:
        await app.aclose()
    spans = spans_by_name(span_exporter)
    parent = spans[REQUEST_SPAN_CHAT]
    assert parent.status.status_code is StatusCode.ERROR
    assert "gateway.error_code" in dict(parent.attributes)
    assert UPSTREAM_SPAN_NAME not in spans  # rejected before dispatch


async def test_export_failure_never_breaks_request(tmp_path, monkeypatch):
    class ExplodingExporter:
        def export(self, _spans):
            raise RuntimeError("collector down")

        def shutdown(self):
            return True

        def force_flush(self, timeout_millis=None):
            return True

    monkeypatch.setattr(
        OtelExportPlugin, "_build_processor", lambda self: SimpleSpanProcessor(ExplodingExporter())
    )
    app = make_otel_app(tmp_path)
    try:
        assert await chat_once(app) is not None
        assert await chat_once(app, stream=True) is not None
    finally:
        await app.aclose()  # shutdown must also survive the failing exporter


async def test_cache_hit_skips_upstream_span(tmp_path, span_exporter):
    app = make_otel_app(tmp_path, plugins={"response-cache-plugin": {"config": {"ttl_s": 60}}})
    try:
        await chat_once(app)
        span_exporter.clear()
        await chat_once(app)  # same body -> cache hit
    finally:
        await app.aclose()
    spans = spans_by_name(span_exporter)
    assert spans[REQUEST_SPAN_CHAT].attributes["gateway.cache_hit"] is True
    assert UPSTREAM_SPAN_NAME not in spans


async def test_embeddings_get_their_own_span_name(tmp_path, span_exporter):
    app = make_otel_app(tmp_path)
    try:
        assert await app.embeddings({"model": "m1", "input": "hello"}, {}, "test") is not None
    finally:
        await app.aclose()
    assert REQUEST_SPAN_EMBEDDINGS in spans_by_name(span_exporter)


def test_missing_otel_extra_raises_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace", None)
    with pytest.raises(ValueError, match=r"\[otel\] extra"):
        OtelExportPlugin({"endpoint": ENDPOINT})


def test_endpoint_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        OtelExportPlugin({"endpoint": "localhost:4318"})


async def test_sample_ratio_zero_exports_nothing(tmp_path, span_exporter):
    app = GatewayApp(
        write_config(
            tmp_path,
            base_config(
                plugins={"otel-export-plugin": {"config": {"endpoint": ENDPOINT, "sample_ratio": 0}}}
            ),
        )
    )
    try:
        assert await chat_once(app) is not None
    finally:
        await app.aclose()
    assert span_exporter.get_finished_spans() == ()
