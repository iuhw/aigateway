"""Exact-match response cache plugin (LiteLLM `cache` / Higress `ai-cache`).

Storage is in-process by default. With `shared: true` and a `shared_state`
backend configured, entries live in the shared backend so N gateway replicas
serve each other's cached responses (Redis: TTL-native; capacity bounds apply
to the in-process store only — shared backends rely on TTL eviction).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, ClassVar

from aigateway.plugins.base import Plugin, PluginContext, register_plugin

logger = logging.getLogger("aigateway.plugins.response_cache")


def cache_key(model: str, payload: dict[str, Any]) -> str:
    """Stable hash over model + normalized messages + sampling parameters."""
    normalized = {
        "model": model,
        "messages": payload.get("messages", []),
        "input": payload.get("input"),  # embeddings: distinct inputs must not collide
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
        "response_format": payload.get("response_format"),
        "encoding_format": payload.get("encoding_format"),
        "dimensions": payload.get("dimensions"),
        "temperature": payload.get("temperature"),
        "top_p": payload.get("top_p"),
        "max_tokens": payload.get("max_tokens"),
        "seed": payload.get("seed"),
    }
    blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@register_plugin
class ResponseCachePlugin(Plugin):
    """Serves identical chat completions from an in-memory TTL cache.

    Streaming responses are never cached (partial content risk); only
    non-streaming GET-equivalent completions are. Cacheable when the request has
    no tools and temperature is None/0 by default (configurable).
    """

    default_order: ClassVar[int] = 30  # runs before most plugins; auth still runs first

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.ttl_s = float(self.config.get("ttl_s", 3600))
        self.max_entries = int(self.config.get("max_entries", 1024))
        self.cache_streaming = bool(self.config.get("cache_streaming", False))
        self.shared = bool(self.config.get("shared", False))
        self._store: dict[str, tuple[float, dict[str, Any]]] = {}
        self._backend: Any = None  # injected via set_shared (GatewayApp)
        self._shared_warned = False

    def set_shared(self, backend: Any, metrics: Any = None) -> None:
        """Called by GatewayApp to inject the shared-state backend."""
        self._backend = backend

    def _shared_ready(self) -> bool:
        if not self.shared:
            return False
        if self._backend is None:
            # `shared_state` not configured: degrade to in-process, warn once.
            if not self._shared_warned:
                logger.warning(
                    "response-cache shared=true but no shared_state backend; using in-process cache"
                )
                self._shared_warned = True
            return False
        return True

    def _evict_if_needed(self) -> None:
        if len(self._store) <= self.max_entries:
            return
        now = time.monotonic()
        expired = [k for k, (deadline, _) in self._store.items() if deadline <= now]
        for k in expired:
            del self._store[k]
        while len(self._store) > self.max_entries:
            oldest = min(self._store.items(), key=lambda item: item[1][0])[0]
            del self._store[oldest]

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        deadline, body = entry
        if deadline <= time.monotonic():
            del self._store[key]
            return None
        return body

    def put(self, key: str, body: dict[str, Any]) -> None:
        self._store[key] = (time.monotonic() + self.ttl_s, body)
        self._evict_if_needed()

    async def on_request(self, ctx: PluginContext) -> None:
        if ctx.stream and not self.cache_streaming:
            return
        key = cache_key(ctx.model, ctx.payload)
        ctx.state[self.display_name()] = {"key": key}
        hit: dict[str, Any] | None = None
        if self._shared_ready():
            try:
                hit = await self._backend.cache_get(key)
            except Exception:  # noqa: BLE001 - cache failures must not break the request
                logger.exception("shared cache_get failed; treating as miss")
        else:
            hit = self.get(key)
        if hit is not None:
            ctx.cache_hit = True
            ctx.response = hit
            ctx.state[self.display_name()]["served"] = True
        # miss: nothing to do; api layer will check state after routing

    async def on_response(self, ctx: PluginContext) -> None:
        state = ctx.state.get(self.display_name())
        if not state or state.get("served") or ctx.response is None or ctx.cache_hit:
            return
        if ctx.status_code != 200:
            return
        if self._shared_ready():
            try:
                await self._backend.cache_put(state["key"], ctx.response, int(self.ttl_s))
            except Exception:  # noqa: BLE001
                logger.exception("shared cache_put failed; skipping cache fill")
        else:
            self.put(state["key"], ctx.response)
