"""Gateway application state and the request processing pipeline."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aigateway.config import ConfigWatcher, GatewayConfig
from aigateway.core.dispatcher import Dispatcher
from aigateway.core.provider import Provider
from aigateway.core.router import Router
from aigateway.errors import GatewayError, NotFoundError, UpstreamError
from aigateway.observability.metrics import Metrics
from aigateway.plugins.base import PluginChain, PluginContext
from aigateway.schemas import (
    ChatCompletionRequest,
    chunk_text,
    estimate_tokens,
    request_text,
    response_text,
)

logger = logging.getLogger("aigateway.app")


@dataclass
class GatewayState:
    """Immutable-by-convention snapshot swapped on config reload."""

    config: GatewayConfig
    providers: dict[str, Provider]
    router: Router
    plugins: PluginChain


class GatewayApp:
    """Owns runtime state; the FastAPI layer is a thin adapter over this class."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.metrics = Metrics()
        self._watcher: ConfigWatcher | None = None
        self._watch_task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._shared_backend: Any = None
        self._watcher = ConfigWatcher(self.config_path)
        self.state = self._build_state()

    # -- lifecycle ---------------------------------------------------------------

    def _build_state(self) -> GatewayState:
        assert self._watcher is not None
        config = self._watcher.initialize()
        if self._shared_backend is None:
            self._shared_backend = self._build_shared_backend(config)
        return self._materialize(config)

    def _build_shared_backend(self, config: GatewayConfig) -> Any:
        from aigateway.state import build_backend

        try:
            return build_backend(config.section("shared_state"))
        except Exception:
            logger.exception("shared_state backend init failed; continuing without shared state")
            return None

    def _materialize(self, config: GatewayConfig) -> GatewayState:
        from aigateway.plugins.rate_limit import RateLimitPlugin

        state = GatewayState(
            config=config,
            providers=config.build_providers(),
            router=config.build_router(),
            plugins=config.build_plugin_chain(),
        )
        if self._shared_backend is not None:
            for plugin in state.plugins.plugins:
                if isinstance(plugin, RateLimitPlugin):
                    plugin.set_shared(self._shared_backend, self.metrics)
        return state

    async def start_config_watcher(self, interval_s: float = 5.0) -> None:
        if self._watch_task is not None:
            return
        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.reload_if_changed()
                except Exception:  # noqa: BLE001
                    logger.exception("config watcher iteration failed")
        self._watch_task = asyncio.create_task(_loop())
        logger.info("config watcher started (interval %.1fs)", interval_s)
        self._start_cooldown_syncer()

    def _start_cooldown_syncer(self, interval_s: float = 2.0) -> None:
        """Share deployment cooldowns across instances (Redis backend only)."""
        from aigateway.state import RedisBackend

        if self._sync_task is not None or not isinstance(self._shared_backend, RedisBackend):
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self._cooldown_sync_once()
                except Exception:  # noqa: BLE001
                    logger.exception("cooldown sync iteration failed")

        self._sync_task = asyncio.create_task(_loop())
        logger.info("cooldown syncer started (interval %.1fs)", interval_s)

    async def _cooldown_sync_once(self) -> None:
        backend = self._shared_backend
        if backend is None:
            return
        # Model names may contain ':', so composite keys use an explicit
        # separator Redis keys never contain.
        sep = "\x1f"
        for model, deployment_key, remaining_s in self.state.router.export_cooldowns():
            await backend.set_cooldown(f"{model}{sep}{deployment_key}", remaining_s)
        for name, remaining_s in (await backend.fetch_cooldowns("")).items():
            model, _, deployment_key = name.partition(sep)
            self.state.router.impose_cooldown(model, deployment_key, remaining_s)

    async def reload_if_changed(self) -> bool:
        """Hot-reload config; rebuilds router/plugins, providers only if changed."""
        fresh = self._watcher.poll() if self._watcher is not None else None
        if fresh is None:
            return False
        old = self.state
        try:
            if (old.config.section("shared_state") or {}) != (fresh.section("shared_state") or {}):
                old_backend = self._shared_backend
                self._shared_backend = None  # force rebuild
                self._shared_backend = self._build_shared_backend(fresh)
                if old_backend is not None:
                    await old_backend.aclose()
            new_state = self._materialize(fresh)
        except Exception:
            logger.exception("config reload materialization failed; keeping previous state")
            return False
        if (old.config.section("providers") or {}) != (fresh.section("providers") or {}):
            for provider in old.providers.values():
                await provider.aclose()
        self.state = new_state
        await old.plugins.aclose()
        return True

    async def aclose(self) -> None:
        for task in (self._watch_task, self._sync_task):
            if task is not None:
                task.cancel()
        self._watch_task = None
        self._sync_task = None
        for provider in self.state.providers.values():
            await provider.aclose()
        await self.state.plugins.aclose()
        if self._shared_backend is not None:
            await self._shared_backend.aclose()
            self._shared_backend = None

    # -- model metadata ------------------------------------------------------------

    def model_names(self) -> list[str]:
        return self.state.router.model_names()

    def model_info(self) -> dict[str, Any]:
        return {name: route.snapshot() for name, route in self.state.router.routes.items()}

    def plugin_info(self) -> list[dict[str, Any]]:
        return self.state.plugins.describe()

    # -- chat completions -----------------------------------------------------------

    def _server_limits(self) -> dict[str, Any]:
        return self.state.config.section("server") or {}

    def _check_request_limits(self, body: dict[str, Any]) -> None:
        """Admission control independent of plugins (fail before any work)."""
        limits = self._server_limits()
        messages = body.get("messages")
        if isinstance(messages, list):
            max_messages = int(limits.get("max_messages", 128))
            if len(messages) > max_messages:
                from aigateway.errors import PayloadTooLargeError

                raise PayloadTooLargeError(
                    f"too many messages: {len(messages)} > {max_messages}"
                )
            max_chars = int(limits.get("max_message_chars", 200_000))
            total_chars = sum(
                len(str(m.get("content") or "")) for m in messages if isinstance(m, dict)
            )
            if total_chars > max_chars:
                from aigateway.errors import PayloadTooLargeError

                raise PayloadTooLargeError(
                    f"messages too long: {total_chars} chars > {max_chars}"
                )

    async def chat(
        self, body: dict[str, Any], headers: dict[str, str], client_host: str
    ) -> dict[str, Any] | AsyncIterator[str]:
        """Process a chat completion; returns the response body or an SSE generator."""
        self._check_request_limits(body)
        request = ChatCompletionRequest(**body)
        payload = request.upstream_payload(stream=request.stream)
        ctx = PluginContext(
            request_id=uuid.uuid4().hex,
            model=request.model,
            payload=payload,
            stream=request.stream,
            headers=dict(headers),
            client_host=client_host,
        )
        started = time.perf_counter()

        try:
            route = self.state.router.route(request.model)
            if route is None:
                raise NotFoundError(f"model {request.model!r} is not configured")

            await self.state.plugins.run_request(ctx)

            # Response cache hit: serve without dispatching (non-streaming only).
            if ctx.cache_hit and ctx.response is not None:
                ctx.status_code = 200
                ctx.latency_s = time.perf_counter() - started
                self._record_usage_from_body(ctx, ctx.response)
                self._metrics_finish(ctx)
                await self.state.plugins.run_response(ctx)
                return ctx.response  # type: ignore[return-value]

            if request.stream:
                return self._chat_streaming(ctx, route, started)
            return await self._chat_non_streaming(ctx, route, started)
        except GatewayError as exc:
            self._finish_error(ctx, exc, started)
            raise

    # -- non-streaming -----------------------------------------------------------

    async def _chat_non_streaming(self, ctx: PluginContext, route, started: float) -> dict[str, Any]:
        result = await self.state_dispatcher().execute(route, ctx.payload)
        ctx.route = result.route
        ctx.deployment = result.deployment
        ctx.latency_s = time.perf_counter() - started
        body = result.body
        self._record_usage_from_body(ctx, body)
        ctx.response = body
        ctx.status_code = 200

        try:
            await self.state.plugins.run_response(ctx)
        except GatewayError:
            # A response-side plugin rejected the completion: surface the filter
            # error instead of the model output.
            ctx.response = None
            raise

        self._metrics_finish(ctx)
        return body

    # -- streaming ------------------------------------------------------------------

    async def _chat_streaming(self, ctx: PluginContext, route, started: float) -> AsyncIterator[str]:
        from aigateway.core.dispatcher import StreamOutcome

        outcome = StreamOutcome()
        state = self.state
        try:
            await state.plugins.run_stream_start(ctx)
        except GatewayError as exc:
            self._finish_error(ctx, exc, started)
            yield _sse({"error": exc.to_openai_error()["error"]})
            yield "data: [DONE]\n\n"
            return

        error: GatewayError | None = None
        text_parts: list[str] = []
        try:
            async for chunk in self.state_dispatcher().stream(route, ctx.payload, outcome):
                ctx.first_token_latency_s = ctx.first_token_latency_s or outcome.first_chunk_latency_s
                filtered = await state.plugins.run_stream_chunk(ctx, chunk)
                if filtered is None:
                    continue
                text_parts.append(chunk_text(filtered))
                yield _sse(filtered)
            # Settle usage before end hooks so plugins (e.g. token-limit
            # reconciliation) see the final numbers.
            if outcome.usage and not ctx.usage_reported:
                ctx.prompt_tokens = outcome.usage.get("prompt_tokens")
                ctx.completion_tokens = outcome.usage.get("completion_tokens")
                ctx.usage_reported = True
            await state.plugins.run_stream_end(ctx)
        except GatewayError as exc:
            error = exc
            ctx.error = exc
            yield _sse({"error": exc.to_openai_error()["error"]})
            yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception to an SSE client
            logger.exception("stream failed unexpectedly")
            error = UpstreamError(f"internal stream error: {exc}")
            ctx.error = error
            yield _sse({"error": error.to_openai_error()["error"]})
            yield "data: [DONE]\n\n"
        finally:
            ctx.latency_s = time.perf_counter() - started
            if outcome.route is not None:
                ctx.route = outcome.route
                ctx.deployment = outcome.deployment
            if outcome.usage and not ctx.usage_reported:
                ctx.prompt_tokens = outcome.usage.get("prompt_tokens")
                ctx.completion_tokens = outcome.usage.get("completion_tokens")
                ctx.usage_reported = True
            if not ctx.usage_reported:
                ctx.prompt_tokens = ctx.prompt_tokens or estimate_tokens(request_text(ctx.payload))
                ctx.completion_tokens = ctx.completion_tokens or estimate_tokens("".join(text_parts))
            if error is not None:
                self._finish_error(ctx, error, started)
            else:
                ctx.status_code = 200
                self._metrics_finish(ctx)

    # -- helpers ---------------------------------------------------------------------

    def state_dispatcher(self) -> Dispatcher:
        return Dispatcher(self.state.router, self.state.providers)

    def _record_usage_from_body(self, ctx: PluginContext, body: dict[str, Any]) -> None:
        usage = body.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            ctx.prompt_tokens = usage.get("prompt_tokens")
            ctx.completion_tokens = usage.get("completion_tokens")
            ctx.usage_reported = True
        else:
            ctx.prompt_tokens = estimate_tokens(request_text(ctx.payload))
            ctx.completion_tokens = estimate_tokens(response_text(body))

    def _pricing_for(self, model: str) -> dict[str, float] | None:
        pricing = self.state.config.section("pricing") or {}
        entry = pricing.get(model)
        return entry if isinstance(entry, dict) else None

    def _record_cost(self, ctx: PluginContext) -> None:
        """Accumulate upstream spend; cache hits cost nothing. Silent no-op for
        unpriced models."""
        if ctx.cache_hit or not ctx.usage_reported:
            return
        pricing = self._pricing_for(ctx.model)
        if not pricing:
            return
        try:
            usd = (
                (ctx.prompt_tokens or 0) / 1000 * float(pricing.get("input_per_1k", 0))
                + (ctx.completion_tokens or 0) / 1000 * float(pricing.get("output_per_1k", 0))
            )
        except (TypeError, ValueError):
            return
        self.metrics.inc_cost(ctx.model, usd)

    def _metrics_finish(self, ctx: PluginContext) -> None:
        provider = ctx.deployment.provider if ctx.deployment else "none"
        status = ctx.status_code or 200
        self.metrics.observe_request(ctx.model, provider, status, ctx.latency_s or 0.0)
        if ctx.stream and ctx.first_token_latency_s:
            self.metrics.observe_first_token(ctx.model, provider, ctx.first_token_latency_s)
        if not ctx.usage_reported:
            from aigateway.schemas import estimate_tokens as _est

            ctx.prompt_tokens = ctx.prompt_tokens or _est(request_text(ctx.payload))
        self.metrics.inc_tokens(ctx.model, provider, ctx.prompt_tokens or 0, ctx.completion_tokens or 0)
        self._record_cost(ctx)
        if ctx.cache_hit:
            self.metrics.inc_cache(ctx.model, True)
        else:
            self.metrics.inc_cache(ctx.model, False)

    def _finish_error(self, ctx: PluginContext, exc: GatewayError, started: float) -> None:
        ctx.error = exc
        ctx.status_code = exc.status_code
        ctx.latency_s = time.perf_counter() - started
        plugin_name = getattr(exc, "plugin_name", None)
        if plugin_name:
            self.metrics.inc_plugin_rejection(plugin_name, exc.code)
        if exc.code == "rate_limit_exceeded":
            self.metrics.inc_rate_limit(ctx.model)
        provider = ctx.deployment.provider if ctx.deployment else "none"
        self.metrics.observe_request(ctx.model, provider, exc.status_code, ctx.latency_s)
        # Fire-and-forget error hooks.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.state.plugins.run_error(ctx, exc))
        except RuntimeError:
            pass


def _sse(chunk: dict[str, Any]) -> str:
    import json

    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
