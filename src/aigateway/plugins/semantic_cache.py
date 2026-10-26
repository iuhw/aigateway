"""Semantic (similarity) response cache plugin — blueprint M2.

Serves a past response when the *meaning* of the incoming prompt matches a
previous one closely enough: the request text is embedded through the gateway's
own embeddings pipeline (the same routing/failover machinery `/v1/embeddings`
uses), then searched by cosine similarity within a per-request-shape partition.

Correctness defenses (blueprint §M2):
- **Partitioning**: the cache namespace is a fingerprint of model + sampling
  parameters + tools — different temperature/seed/tools/model can never match,
  only the prompt text is compared.
- **Conservative threshold** (default 0.95) and short independent TTL.
- **Opt-in**: absent from config = nonexistent; additionally, a missing
  `embed_model` self-disables the plugin at load.
- **Fail-open**: if embedding fails (routing error, timeout, no vectors), the
  request passes through untouched — the cache must never block traffic.

Streaming requests are never served from (or written to) the semantic cache;
exact-duplicate prompts are left to response-cache-plugin, which runs first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from aigateway.plugins.base import Plugin, PluginContext, register_plugin
from aigateway.schemas import message_text

logger = logging.getLogger("aigateway.plugins.semantic_cache")

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]] | None]]


def partition_key(model: str, payload: dict[str, Any]) -> str:
    """Fingerprint of everything EXCEPT the prompt text: same partition means
    the responses are directly interchangeable, only wording may differ."""
    normalized = {
        "model": model,
        "temperature": payload.get("temperature"),
        "top_p": payload.get("top_p"),
        "max_tokens": payload.get("max_tokens"),
        "seed": payload.get("seed"),
        "stop": payload.get("stop"),
        "tools": payload.get("tools"),
        "tool_choice": payload.get("tool_choice"),
        "response_format": payload.get("response_format"),
    }
    blob = json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def messages_text(payload: dict[str, Any]) -> str:
    """All chat message text (system+user+assistant), newline-joined."""
    parts = [message_text(message) for message in payload.get("messages") or []]
    return "\n".join(part for part in parts if part)


@register_plugin
class SemanticCachePlugin(Plugin):
    """Similarity-based response cache over the gateway's embeddings pipeline."""

    name: ClassVar[str] = "semantic-cache-plugin"
    default_order: ClassVar[int] = 31  # behind the exact-match cache

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.embed_model = str(self.config.get("embed_model") or "")
        self.threshold = float(self.config.get("similarity_threshold", 0.95))
        if not 0.0 < self.threshold <= 1.0:
            logger.warning("semantic-cache: invalid similarity_threshold %s; clamped to 0.95", self.threshold)
            self.threshold = 0.95
        self.ttl_s = int(self.config.get("ttl_s", 600))
        self.max_entries = int(self.config.get("max_entries", 256))
        #: async (texts) -> list of vectors | None; injected by GatewayApp.
        #: None => lookups always miss and fills are skipped (fail-open).
        self.embed_fn: EmbedFn | None = None
        self._backend: Any = None  # shared-state backend (optional)
        self._metrics: Any = None
        # namespace -> key -> (deadline, vector, payload); used when no shared backend
        self._store: dict[str, dict[str, tuple[float, list[float], dict[str, Any]]]] = {}
        if not self.embed_model:
            logger.warning(
                "semantic-cache-plugin disabled: 'embed_model' is required "
                "(route the plugin should use for embedding requests)"
            )
            self.enabled = False

    def set_shared(self, backend: Any, metrics: Any = None) -> None:
        """GatewayApp injection (existing set_shared convention)."""
        self._backend = backend
        if metrics is not None:
            self._metrics = metrics

    def set_embed_fn(self, fn: EmbedFn) -> None:
        """GatewayApp injection: embeddings through the routing pipeline."""
        self.embed_fn = fn

    # -- storage ---------------------------------------------------------------

    def _entry_count(self) -> int:
        return sum(len(entries) for entries in self._store.values())

    def _evict_if_needed(self) -> None:
        while self._entry_count() > self.max_entries:
            oldest_ns, oldest_key, oldest_deadline = None, None, None
            for namespace, entries in self._store.items():
                for key, (deadline, _vector, _payload) in entries.items():
                    if oldest_deadline is None or deadline < oldest_deadline:
                        oldest_ns, oldest_key, oldest_deadline = namespace, key, deadline
            if oldest_ns is None:
                return
            del self._store[oldest_ns][oldest_key]

    async def _search(
        self, namespace: str, vector: list[float]
    ) -> list[tuple[str, dict[str, Any], float]]:
        if self._backend is not None:
            try:
                return await self._backend.vector_search(namespace, vector, self.threshold)
            except Exception:  # noqa: BLE001 - backend outage must not break requests
                logger.exception("semantic cache backend search failed; bypassing")
                return []
        entries = self._store.get(namespace)
        if not entries:
            return []
        now = time.monotonic()
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for key, (deadline, entry_vector, payload) in list(entries.items()):
            if deadline <= now:
                del entries[key]
                continue
            from aigateway.state import cosine_similarity

            similarity = cosine_similarity(vector, entry_vector)
            if similarity >= self.threshold:
                scored.append((similarity, key, payload))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(key, payload, similarity) for similarity, key, payload in scored[:1]]

    async def _put(
        self, namespace: str, text: str, vector: list[float], body: dict[str, Any]
    ) -> None:
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if self._backend is not None:
            try:
                await self._backend.vector_put(namespace, key, vector, body, self.ttl_s)
            except Exception:  # noqa: BLE001
                logger.exception("semantic cache backend put failed; skipping fill")
            return
        self._store.setdefault(namespace, {})[key] = (time.monotonic() + self.ttl_s, vector, body)
        self._evict_if_needed()

    async def _safe_embed(self, texts: list[str]) -> list[list[float]] | None:
        if self.embed_fn is None:
            return None
        try:
            return await self.embed_fn(texts)
        except Exception:  # noqa: BLE001 - any embed failure is a passthrough
            logger.exception("semantic cache embed failed; bypassing")
            return None

    # -- hooks -----------------------------------------------------------------

    async def on_request(self, ctx: PluginContext) -> None:
        if not self.enabled or ctx.stream or self.embed_fn is None:
            return
        if ctx.payload.get("input") is not None:  # embeddings requests are out of scope
            return
        text = messages_text(ctx.payload)
        if not text.strip():
            return
        namespace = partition_key(ctx.model, ctx.payload)
        vectors = await self._safe_embed([text])
        if not vectors:
            return
        vector = vectors[0]
        state = ctx.state[self.display_name()] = {
            "namespace": namespace,
            "vector": vector,
            "text": text,
        }
        hits = await self._search(namespace, vector)
        if hits:
            _key, body, _similarity = hits[0]
            ctx.cache_hit = True
            ctx.response = body
            state["served"] = True
            if self._metrics is not None:
                self._metrics.inc_semantic_cache_hit(ctx.model)

    async def on_response(self, ctx: PluginContext) -> None:
        state = ctx.state.get(self.display_name())
        if not state or state.get("served") or ctx.response is None or ctx.cache_hit:
            return
        if ctx.status_code != 200:
            return
        vector = state.get("vector")
        if not vector:  # lookup-time embed failed; try once more on fill
            vectors = await self._safe_embed([state["text"]])
            if not vectors:
                return
            vector = vectors[0]
        await self._put(state["namespace"], state["text"], vector, ctx.response)
