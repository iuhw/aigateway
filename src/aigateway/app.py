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
        self.state = self._build_state()

    # -- lifecycle ---------------------------------------------------------------

    def _build_state(self) -> GatewayState:
        config = ConfigWatcher(self.config_path).initialize()
        return self._materialize(config)

    def _materialize(self, config: GatewayConfig) -> GatewayState:
        return GatewayState(
            config=config,
            providers=config.build_providers(),
            router=config.build_router(),
            plugins=config.build_plugin_chain(),
        )

    async def start_config_watcher(self, interval_s: float = 5.0) -> None:
        if self._watch_task is not None:
            return
        self._watcher = ConfigWatcher(self.config_path)
        async def _loop() -> None:
            while True:
                await asyncio.sleep(interval_s)
                try:
                    await self.reload_if_changed()
                except Exception:  # noqa: BLE001
                    logger.exception("config watcher iteration failed")
        self._watch_task = asyncio.create_task(_loop())
        logger.info("config watcher started (interval %.1fs)", interval_s)

    async def reload_if_changed(self) -> bool:
        """Hot-reload config; rebuilds router/plugins, providers only if changed."""
        assert self._watcher is not None
        fresh = self._watcher.poll()
        if fresh is None:
            return False
        old = self.state
        try:
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
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        for provider in self.state.providers.values():
            await provider.aclose()
        await self.state.plugins.aclose()

    # -- model metadata ------------------------------------------------------------

    def model_names(self) -> list[str]:
        return self.state.router.model_names()

    def model_info(self) -> dict[str, Any]:
        return {name: route.snapshot() for name, route in self.state.router.routes.items()}

    def plugin_info(self) -> list[dict[str, Any]]:
        return self.state.plugins.describe()

    # -- chat completions -----------------------------------------------------------

    async def chat(
        self, body: dict[str, Any], headers: dict[str, str], client_host: str
    ) -> dict[str, Any] | AsyncIterator[str]:
        """Process a chat completion; returns the response body or an SSE generator."""
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
