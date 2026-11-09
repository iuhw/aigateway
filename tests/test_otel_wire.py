"""M4 integration: the REAL OTLP/HTTP export path (no mocks).

Runs the plugin with its shipped BatchSpanProcessor + OTLPSpanExporter against
a local stdlib HTTP collector stub, and asserts the collector receives a
decodable ExportTraceServiceRequest containing the request span. The full
dockerized collector check is the milestone validation step; this proves the
wire path (exporter -> HTTP -> protobuf) end to end in-process.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from aigateway.app import GatewayApp
from tests.conftest import base_config, write_config

pytestmark = pytest.mark.unit


def make_collector(received: list) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            received.append(ExportTraceServiceRequest.FromString(body))
            self.send_response(200)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args) -> None:  # silence request logging
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def test_real_otlp_wire_export(tmp_path):
    received: list = []
    server = make_collector(received)
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/traces"
    config = base_config(
        plugins={"otel-export-plugin": {"config": {"endpoint": endpoint, "timeout_s": 5.0}}}
    )
    app = GatewayApp(write_config(tmp_path, config))
    try:
        await app.chat({"model": "m1", "messages": [{"role": "user", "content": "hi"}]}, {}, "t")
        # aclose() force-flushes the batch processor over the wire
        await app.aclose()
    finally:
        server.shutdown()
        server.server_close()

    assert received, "collector received no OTLP export"
    export = received[0]
    span = export.resource_spans[0].scope_spans[0].spans[0]
    assert span.name == "gateway.chat"
    attrs = {
        a.key: a.value.string_value
        for a in span.attributes
        if a.value.WhichOneof("value") == "string_value"
    }
    assert attrs["gateway.model"] == "m1"
    assert attrs["gateway.key"] == "anonymous"
