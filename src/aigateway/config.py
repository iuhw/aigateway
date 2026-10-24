"""Gateway configuration (YAML) with typed accessors and hot-reload."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

from aigateway.core.openai_provider import OpenAICompatibleProvider
from aigateway.core.provider import Provider
from aigateway.core.router import Deployment, ModelRoute, RoutePolicy, Router, RoutingStrategy
from aigateway.errors import BadRequestError
from aigateway.plugins.base import (
    Plugin,
    PluginChain,
    load_external_plugin_class,
    lookup_plugin_class,
    registered_plugins,
)

logger = logging.getLogger("aigateway.config")

# Import built-in plugins for registration side effects.
import aigateway.plugins  # noqa: F401,E402


class GatewayConfig:
    """Parsed gateway configuration."""

    def __init__(self, data: dict[str, Any]):
        self.data = data

    # -- providers ------------------------------------------------------------

    def build_providers(self) -> dict[str, Provider]:
        providers: dict[str, Provider] = {}
        for name, spec in (self.data.get("providers") or {}).items():
            kind = spec.get("type", "openai")
            if kind == "openai":
                providers[name] = OpenAICompatibleProvider(
                    name,
                    base_url=spec.get("base_url", ""),
                    api_key=spec.get("api_key"),
                    max_connections=int(spec.get("max_connections", 100)),
                    extra_headers=spec.get("extra_headers"),
                    supports_stream_usage_option=bool(spec.get("supports_stream_usage_option", False)),
                )
            elif kind == "mock":
                from aigateway.core.mock_provider import MockProvider

                providers[name] = MockProvider(
                    name,
                    latency_s=float(spec.get("latency_s", 0.0)),
                    content_template=str(spec.get("content_template", "mock reply from {model}: {echo}")),
                    fail_status=spec.get("fail_status"),
                    fail_next_n=(
                        int(spec["fail_next_n"]) if spec.get("fail_next_n") is not None else None
                    ),
                    failure_rate=float(spec.get("failure_rate", 0.0)),
                    chunk_count=int(spec.get("chunk_count", 4)),
                    healthy=bool(spec.get("healthy", True)),
                )
            else:
                raise BadRequestError(f"unknown provider type {kind!r} for {name!r}")
        return providers

    # -- routing ----------------------------------------------------------------

    def build_router(self) -> Router:
        routes: dict[str, ModelRoute] = {}
        for model_name, spec in (self.data.get("models") or {}).items():
            deployments = [
                Deployment(
                    provider=str(d["provider"]),
                    model=str(d.get("model", model_name)),
                    weight=int(d.get("weight", 1)),
                    priority=int(d.get("priority", 0)),
                )
                for d in spec.get("deployments", [])
            ]
            if not deployments:
                raise BadRequestError(f"model {model_name!r} has no deployments")
            policy = RoutePolicy(
                timeout_s=float(spec.get("timeout_s", 60.0)),
                retries=int(spec.get("retries", 2)),
                retry_backoff_s=float(spec.get("retry_backoff_s", 0.5)),
                cooldown_s=float(spec.get("cooldown_s", 30.0)),
                allowed_fails=int(spec.get("allowed_fails", 2)),
                strategy=RoutingStrategy(spec.get("strategy", "round_robin")),
                failover_on_client_error=bool(spec.get("failover_on_client_error", False)),
            )
            routes[model_name] = ModelRoute(
                name=model_name,
                deployments=deployments,
                policy=policy,
                fallbacks=[str(f) for f in spec.get("fallbacks", [])],
            )
        return Router(routes)

    # -- plugins ------------------------------------------------------------------

    def build_plugin_chain(self) -> PluginChain:
        plugins: list[Plugin] = []
        for name, spec in (self.data.get("plugins") or {}).items():
            if isinstance(spec, dict) and spec.get("enabled") is False:
                continue
            spec = spec or {}
            config = spec.get("config", spec) if isinstance(spec, dict) else {}
            cls = None
            if isinstance(spec, dict) and spec.get("module"):
                cls = load_external_plugin_class(str(spec["module"]), str(spec.get("class", name)))
            else:
                cls = lookup_plugin_class(name)
            if cls is None:
                raise BadRequestError(
                    f"unknown plugin {name!r}; registered plugins: {registered_plugins()}"
                )
            order = spec.get("order") if isinstance(spec, dict) else None
            plugin = cls(config if isinstance(config, dict) else {})
            if order is not None:
                plugin._order = float(order)  # type: ignore[attr-defined]
            plugins.append(plugin)
        return PluginChain(plugins)

    # -- plain sections -----------------------------------------------------------

    def section(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)

    def snapshot(self) -> dict[str, Any]:
        return self.data


def load_config(path: str | Path) -> GatewayConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BadRequestError(f"config root must be a mapping, got {type(data).__name__}")
    return GatewayConfig(data)


class ConfigWatcher:
    """Rebuilds the full gateway state (providers, router, plugins) on file change.

    Providers are only rebuilt when the provider section changed; otherwise the
    existing connection pools are reused.
    """

    def __init__(self, path: str | Path, on_reload=None):
        self.path = Path(path)
        self.on_reload = on_reload
        self._lock = threading.Lock()
        self._mtime: float = 0.0

    def poll(self) -> GatewayConfig | None:
        """Return a fresh config when the file changed; None otherwise."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            if mtime == self._mtime:
                return None
            self._mtime = mtime
        try:
            config = load_config(self.path)
        except Exception:
            logger.exception("config reload failed; keeping previous config")
            return None
        logger.info("config reloaded from %s", self.path)
        if self.on_reload:
            try:
                self.on_reload(config)
            except Exception:
                logger.exception("config reload callback failed")
        return config

    def initialize(self) -> GatewayConfig:
        config = load_config(self.path)
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            pass
        return config
