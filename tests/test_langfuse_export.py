"""M4/S3: langfuse-export-plugin — OTLP preset for Langfuse (Basic auth)."""

from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aigateway.app import GatewayApp
from aigateway.plugins.langfuse_export import LangfuseExportPlugin
from aigateway.plugins.otel_export import REQUEST_SPAN_CHAT, OtelExportPlugin
from tests.conftest import base_config, write_config

pytestmark = pytest.mark.unit


@pytest.fixture
def span_exporter(monkeypatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(OtelExportPlugin, "_build_processor", lambda self: SimpleSpanProcessor(exporter))
    return exporter


def make_langfuse_app(tmp_path, span_exporter, **plugin_cfg) -> GatewayApp:
    config = base_config(
        plugins={
            "langfuse-export-plugin": {
                "config": {
                    "host": "https://cloud.langfuse.com",
                    "public_key": "pk-lf-test",
                    "secret_key": "sk-lf-test",
                    **plugin_cfg,
                }
            }
        }
    )
    return GatewayApp(write_config(tmp_path, config))


def test_endpoint_and_basic_auth_header():
    plugin = LangfuseExportPlugin(
        {
            "host": "https://cloud.langfuse.com/",
            "public_key": "pk",
            "secret_key": "sk",
        }
    )
    assert plugin.endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    assert plugin.headers["authorization"].startswith("Basic ")
    assert plugin.timeout_s == 5.0


def test_self_hosted_host_supported():
    plugin = LangfuseExportPlugin({"host": "http://langfuse:3000", "public_key": "pk", "secret_key": "sk"})
    assert plugin.endpoint == "http://langfuse:3000/api/public/otel/v1/traces"


def test_missing_credentials_raise():
    with pytest.raises(ValueError, match="public_key"):
        LangfuseExportPlugin({"host": "https://cloud.langfuse.com", "public_key": "pk"})
    with pytest.raises(ValueError, match="absolute"):
        LangfuseExportPlugin({"host": "cloud.langfuse.com", "public_key": "pk", "secret_key": "sk"})


async def test_spans_reach_the_exporter_with_service_name(tmp_path, span_exporter):
    app = make_langfuse_app(tmp_path, span_exporter, service_name="aigateway-prod")
    try:
        await app.chat({"model": "m1", "messages": [{"role": "user", "content": "hi"}]}, {}, "t")
    finally:
        await app.aclose()
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1  # single-attempt success: parent span only
    parent = next(s for s in spans if s.name == REQUEST_SPAN_CHAT)
    assert parent.resource.attributes["service.name"] == "aigateway-prod"
