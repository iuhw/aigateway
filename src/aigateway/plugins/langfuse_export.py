"""Langfuse trace export plugin — blueprint M4 (`langfuse-export-plugin`).

Langfuse natively ingests OTLP: traces POSTed to ``{host}/api/public/otel/v1/traces``
with HTTP Basic auth (public_key/secret_key pair) show up as standard traces.
This plugin is a thin preset over `OtelExportPlugin` — same span tree, same
fire-and-forget guarantees, same ``[otel]`` extra.

Config: ``host`` (required, e.g. ``https://cloud.langfuse.com``), ``public_key`` /
``secret_key`` (required), plus the base plugin's ``sample_ratio`` /
``timeout_s`` / ``service_name`` / ``resource`` / ``headers``.
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar

from aigateway.plugins.otel_export import OtelExportPlugin, register_plugin

LANGFUSE_OTLP_PATH = "/api/public/otel/v1/traces"


@register_plugin
class LangfuseExportPlugin(OtelExportPlugin):
    """Traces requests to a Langfuse (cloud or self-hosted) OTLP endpoint."""

    name: ClassVar[str] = "langfuse-export-plugin"
    default_order: ClassVar[int] = 5

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = dict(config or {})
        host = str(cfg.get("host", "")).strip().rstrip("/")
        public_key = str(cfg.get("public_key", "")).strip()
        secret_key = str(cfg.get("secret_key", "")).strip()
        if "://" not in host:
            raise ValueError(
                "langfuse_export: 'host' must be an absolute URL, e.g. https://cloud.langfuse.com"
            )
        if not public_key or not secret_key:
            raise ValueError("langfuse_export: 'public_key' and 'secret_key' are required")
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        headers = {"authorization": f"Basic {token}"}
        headers.update({str(k): str(v) for k, v in (cfg.get("headers") or {}).items()})
        passthrough = {
            k: v
            for k, v in cfg.items()
            if k not in ("host", "public_key", "secret_key", "headers", "endpoint")
        }
        super().__init__({"endpoint": f"{host}{LANGFUSE_OTLP_PATH}", "headers": headers, **passthrough})
