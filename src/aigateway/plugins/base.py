"""Pluggable middleware system.

Every cross-cutting concern — auth, keyword filtering, rate limiting, caching,
logging, usage tracking — is a `Plugin` participating in a `PluginChain`.
Hook points (loosely modeled on Higress's wasm plugin chain and LiteLLM's
pre/post-call hooks):

    request ──► on_request ──► [route/dispatch] ──► on_response  (non-stream)
                                    │
                                    └─ stream ───► on_stream_start
                                                   on_stream_chunk*
                                                   on_stream_end

`on_error` runs for any request that finished with an error. A plugin aborts a
request by raising `PluginReject` (or any `GatewayError`), which the API layer
maps to an OpenAI-format error.

Third-party plugins load without touching gateway core: either register via the
`register_plugin` decorator, or reference an external module in config::

    plugins:
      my_filter:
        module: mypkg.mymodule
        class: MyFilter
        config: {...}
"""

from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from aigateway.errors import GatewayError, PluginReject

if TYPE_CHECKING:  # pragma: no cover - circular-import-free typing
    from aigateway.core.router import Deployment, ModelRoute

logger = logging.getLogger("aigateway.plugins")


@dataclass
class PluginContext:
    """Per-request state shared by all plugins."""

    request_id: str
    model: str
    payload: dict[str, Any]
    stream: bool
    headers: dict[str, str] = field(default_factory=dict)
    client_host: str = ""
    #: Set by the auth plugin; consumed by rate limiting / usage accounting.
    identity: Any = None
    #: Populated after routing; used by logging.
    route: ModelRoute | None = None
    deployment: Deployment | None = None
    #: Non-streaming response body (mutate in place in on_response).
    response: dict[str, Any] | None = None
    #: Post-execution metrics (filled by the API layer).
    status_code: int | None = None
    latency_s: float | None = None
    first_token_latency_s: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_reported: bool = False
    cache_hit: bool = False
    error: GatewayError | None = None
    #: Plugin-private scratch space, keyed by plugin name.
    state: dict[str, Any] = field(default_factory=dict)

    def reject(self, message: str, *, status_code: int = 400, code: str = "plugin_rejected") -> PluginReject:
        return PluginReject(message, code=code, status_code=status_code)


class Plugin(ABC):  # noqa: B024 - hook methods all have no-op defaults
    """Base class of all gateway plugins."""

    #: Unique plugin name (defaults to the class name in kebab-case).
    name: ClassVar[str] = ""
    #: Default chain position; lower runs first.
    default_order: ClassVar[int] = 100

    def __init__(self, config: dict[str, Any] | None = None):
        self.config: dict[str, Any] = config or {}
        self.enabled = True

    def display_name(self) -> str:
        # `_instance_name` is set by config loading to the plugin's config key,
        # so multiple instances of one plugin class stay distinguishable.
        return getattr(self, "_instance_name", None) or type(self).name or type(self).__name__

    async def on_request(self, ctx: PluginContext) -> None:
        """Called with the parsed request payload before routing."""

    async def on_response(self, ctx: PluginContext) -> None:
        """Called with the final non-streaming response body (mutable)."""

    async def on_stream_start(self, ctx: PluginContext) -> None:
        """Called once streaming begins."""

    async def on_stream_chunk(self, ctx: PluginContext, chunk: dict[str, Any]) -> dict[str, Any] | None:
        """Inspect/mutate each streamed chunk. Return None to drop the chunk."""
        return chunk

    async def on_stream_end(self, ctx: PluginContext) -> None:
        """Called after the last chunk was forwarded."""

    async def on_error(self, ctx: PluginContext, error: GatewayError) -> None:
        """Called when the request finishes with an error."""

    async def aclose(self) -> None:
        """Release plugin resources on shutdown/reload."""


_REGISTRY: dict[str, type[Plugin]] = {}


def register_plugin(cls: type[Plugin]) -> type[Plugin]:
    """Class decorator that makes a plugin discoverable by config name."""
    name = cls.name or _kebab(cls.__name__)
    if not issubclass(cls, Plugin):
        raise TypeError(f"{cls!r} is not a Plugin subclass")
    cls.name = name
    _REGISTRY[name] = cls
    return cls


def _kebab(text: str) -> str:
    return "".join(("-" + c.lower() if c.isupper() else c) for c in text).lstrip("-")


def lookup_plugin_class(name: str) -> type[Plugin] | None:
    return _REGISTRY.get(name)


def registered_plugins() -> list[str]:
    return sorted(_REGISTRY)


def load_external_plugin_class(module: str, class_name: str) -> type[Plugin]:
    """Import a third-party plugin class from an external module."""
    import importlib

    module_obj = importlib.import_module(module)
    cls = getattr(module_obj, class_name)
    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        raise TypeError(f"{module}.{class_name} is not a Plugin subclass")
    if not cls.name:
        cls.name = _kebab(class_name)
    return cls


class PluginChain:
    """Ordered set of enabled plugins; the unit of execution for hooks."""

    def __init__(self, plugins: list[Plugin]):
        self.plugins = sorted(plugins, key=lambda p: getattr(p, "_order", p.default_order))

    def __bool__(self) -> bool:
        return bool(self.plugins)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"name": p.display_name(), "enabled": p.enabled, "class": type(p).__name__}
            for p in self.plugins
        ]

    async def run_request(self, ctx: PluginContext) -> None:
        for plugin in self.plugins:
            if not plugin.enabled:
                continue
            try:
                await plugin.on_request(ctx)
            except GatewayError as exc:
                exc.plugin_name = plugin.display_name()
                raise

    async def run_response(self, ctx: PluginContext) -> None:
        for plugin in self.plugins:
            if not plugin.enabled:
                continue
            try:
                await plugin.on_response(ctx)
            except GatewayError as exc:
                exc.plugin_name = plugin.display_name()
                raise

    async def run_stream_start(self, ctx: PluginContext) -> None:
        for plugin in self.plugins:
            if plugin.enabled:
                await plugin.on_stream_start(ctx)

    async def run_stream_chunk(self, ctx: PluginContext, chunk: dict[str, Any]) -> dict[str, Any] | None:
        current: dict[str, Any] | None = chunk
        for plugin in self.plugins:
            if current is None:
                return None
            if not plugin.enabled:
                continue
            try:
                current = await plugin.on_stream_chunk(ctx, current)
            except GatewayError as exc:
                exc.plugin_name = plugin.display_name()
                raise
        return current

    async def run_stream_end(self, ctx: PluginContext) -> None:
        for plugin in self.plugins:
            if plugin.enabled:
                await plugin.on_stream_end(ctx)

    async def run_error(self, ctx: PluginContext, error: GatewayError) -> None:
        for plugin in self.plugins:
            if not plugin.enabled:
                continue
            try:
                await plugin.on_error(ctx, error)
            except Exception:  # noqa: BLE001 - error hooks must never mask the original error
                logger.exception("plugin %s on_error failed", plugin.display_name())

    async def aclose(self) -> None:
        for plugin in self.plugins:
            try:
                await plugin.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("plugin %s aclose failed", plugin.display_name())
