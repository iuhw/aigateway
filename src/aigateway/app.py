"""Gateway application state and the request processing pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from aigateway.config import ConfigWatcher, GatewayConfig
from aigateway.core.dispatcher import RETRYABLE_STATUSES, Dispatcher
from aigateway.core.provider import Provider, UpstreamUnavailable
from aigateway.core.router import Router
from aigateway.errors import (
    AllDeploymentsFailedError,
    BadRequestError,
    GatewayError,
    NotFoundError,
    RateLimitError,
    UpstreamError,
)
from aigateway.observability.metrics import Metrics
from aigateway.plugins.base import PluginChain, PluginContext
from aigateway.schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    cache_tokens_of,
    chunk_text,
    embedding_request_text,
    extract_images,
    image_data_bytes,
    message_text,
    request_text,
    response_text,
)
from aigateway.tokenizers import TokenEstimator, build_tokenizer

logger = logging.getLogger("aigateway.app")

_PASSTHROUGH_RELAY_HEADERS = ("content-type", "content-disposition", "x-request-id")


@dataclass
class PassthroughResult:
    """Raw upstream response relayed by the /v1/upstream tunnel (v1.2).

    Either `body` (buffered bytes) or `stream` (live byte iterator the caller
    must fully consume) is set; `headers` is the relay-safe header subset."""

    status: int
    headers: dict[str, str]
    body: bytes | None = None
    stream: AsyncIterator[bytes] | None = None


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
        #: In-flight request counters per virtual key (per-process; behind N
        #: replicas the configured per-key concurrency limit applies per replica).
        self._key_conc: dict[str, int] = {}
        self._watcher = ConfigWatcher(self.config_path)
        self.tokenizer: TokenEstimator = build_tokenizer(None)  # replaced below once config loads
        self.state = self._build_state()
        self._apply_server_runtime_config()

    def _apply_server_runtime_config(self) -> None:
        """Runtime knobs derived from the `server` section (tokenizer + metrics)."""
        self.tokenizer = build_tokenizer(self.state.config.section("server"))
        for plugin in self.state.plugins.plugins:
            plugin.estimator = self.tokenizer
        limits = self.state.config.section("server") or {}
        self.metrics.key_label_limit = int(limits.get("max_tracked_keys", 100))

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
        from aigateway.plugins.semantic_cache import SemanticCachePlugin

        state = GatewayState(
            config=config,
            providers=config.build_providers(),
            router=config.build_router(),
            plugins=config.build_plugin_chain(),
        )
        for plugin in state.plugins.plugins:
            plugin.estimator = self.tokenizer
        if self._shared_backend is not None:
            for plugin in state.plugins.plugins:
                if isinstance(plugin, RateLimitPlugin):
                    plugin.set_shared(self._shared_backend, self.metrics)
                elif hasattr(plugin, "set_shared"):
                    # Other stateful plugins (e.g. auth budget ledger) share
                    # the same backend the rate limiter uses.
                    plugin.set_shared(self._shared_backend, self.metrics)
        for plugin in state.plugins.plugins:
            if hasattr(plugin, "set_metrics"):
                plugin.set_metrics(self.metrics)
        for plugin in state.plugins.plugins:
            if isinstance(plugin, SemanticCachePlugin):
                plugin.set_embed_fn(self._plugin_embed_fn(plugin.embed_model))
        return state

    def _plugin_embed_fn(self, embed_model: str):
        """Embed texts for plugins through the routing/failover machinery that
        /v1/embeddings uses (dispatcher-level: no plugin chain, no recursion).
        Returns None on any failure — callers must treat that as a bypass."""

        async def _embed(texts: list[str]) -> list[list[float]] | None:
            if not embed_model:
                return None
            try:
                route = self.state.router.route(embed_model)
                if route is None:
                    logger.warning("semantic cache embed_model %r has no route; bypassing", embed_model)
                    return None
                result = await self.state_dispatcher().execute_embedding(route, {"input": texts})
                if result.provider_status >= 400:
                    return None
                data = result.body.get("data")
                vectors = [d.get("embedding") for d in data or [] if isinstance(d, dict)]
                if len(vectors) != len(texts) or any(not isinstance(v, list) for v in vectors):
                    return None
                return vectors
            except Exception:  # noqa: BLE001 - embed failure is never request-fatal
                logger.exception("plugin embed via %r failed; bypassing", embed_model)
                return None

        return _embed

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

    # -- per-key concurrency ------------------------------------------------------

    def _acquire_concurrency(self, ctx: PluginContext) -> None:
        """Enforce the key's `concurrency` cap on in-flight requests.

        Process-local by design (per-replica semantics). The release flag on
        ctx.state makes acquire/release idempotent pairs even across error and
        client-disconnect paths.
        """
        identity = ctx.identity if isinstance(ctx.identity, dict) else {}
        limit = identity.get("concurrency")
        if not limit:
            return
        name = str(identity.get("name") or "anonymous")
        if self._key_conc.get(name, 0) >= int(limit):
            raise RateLimitError(
                f"key {name!r} concurrency limit of {limit} in-flight requests exceeded",
                code="concurrency_exceeded",
            )
        self._key_conc[name] = self._key_conc.get(name, 0) + 1
        ctx.state["_conc_held"] = name

    def _release_concurrency(self, ctx: PluginContext) -> None:
        name = ctx.state.get("_conc_held")
        if name is None:
            return
        ctx.state["_conc_held"] = None
        self._key_conc[name] = max(0, self._key_conc.get(name, 0) - 1)

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
            if (old.config.section("server") or {}) != (fresh.section("server") or {}):
                self._apply_server_runtime_config()
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
            max_image_bytes = int(limits.get("max_image_bytes", 5 * 1024 * 1024))
            total_chars = 0
            for i, message in enumerate(messages):
                if not isinstance(message, dict):
                    continue
                total_chars += len(message_text(message))
                images = extract_images(message)
                if images:
                    from aigateway.errors import PayloadTooLargeError

                    size = image_data_bytes(images)
                    if size > max_image_bytes:
                        raise PayloadTooLargeError(
                            f"messages[{i}]: image payload {size} bytes > "
                            f"max_image_bytes {max_image_bytes}"
                        )
            if total_chars > max_chars:
                from aigateway.errors import PayloadTooLargeError

                raise PayloadTooLargeError(
                    f"messages too long: {total_chars} chars > {max_chars}"
                )

    def _check_embedding_limits(self, body: dict[str, Any]) -> None:
        """Admission control for embeddings (mirrors _check_request_limits)."""
        from aigateway.errors import PayloadTooLargeError

        limits = self._server_limits()
        inputs = body.get("input")
        if isinstance(inputs, list):
            max_inputs = int(limits.get("max_messages", 128))
            if len(inputs) > max_inputs:
                raise PayloadTooLargeError(f"too many embedding inputs: {len(inputs)} > {max_inputs}")
        total_chars = len(embedding_request_text(body))
        max_chars = int(limits.get("max_message_chars", 200_000))
        if total_chars > max_chars:
            raise PayloadTooLargeError(f"embedding input too long: {total_chars} chars > {max_chars}")

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
            self._acquire_concurrency(ctx)

            # Canary split (M5): resolved after the plugin chain so the
            # auth-provided identity can drive sticky hashing. On diversion
            # ctx.model becomes the served model — metrics, pricing, usage
            # reports and logs record what actually answered. Rate limiting
            # and model allowlists above deliberately used the requested name.
            identity = ctx.identity if isinstance(ctx.identity, dict) else {}
            effective, branch = self.state.router.resolve_canary(
                request.model, identity=identity.get("name")
            )
            if branch is not None:
                self.metrics.inc_canary(request.model, "canary" if branch else "stable")
                if branch:
                    ctx.requested_model = request.model
                    ctx.model = effective
                    route = self.state.router.route(effective)

            # Response cache hit: serve without dispatching (non-streaming only).
            if ctx.cache_hit and ctx.response is not None:
                ctx.status_code = 200
                ctx.latency_s = time.perf_counter() - started
                self._record_usage_from_body(ctx, ctx.response)
                self._metrics_finish(ctx)
                await self.state.plugins.run_response(ctx)
                self._release_concurrency(ctx)
                return ctx.response  # type: ignore[return-value]

            if request.stream:
                return self._chat_streaming(ctx, route, started)
            try:
                return await self._chat_non_streaming(ctx, route, started)
            finally:
                self._release_concurrency(ctx)
        except GatewayError as exc:
            self._finish_error(ctx, exc, started)
            raise

    # -- embeddings ------------------------------------------------------------------

    async def embeddings(
        self, body: dict[str, Any], headers: dict[str, str], client_host: str
    ) -> dict[str, Any]:
        """Process an embeddings request through the same plugin/routing pipeline."""
        self._check_embedding_limits(body)
        request = EmbeddingRequest(**body)
        payload = request.upstream_payload()
        ctx = PluginContext(
            request_id=uuid.uuid4().hex,
            model=request.model,
            payload=payload,
            stream=False,
            headers=dict(headers),
            client_host=client_host,
        )
        started = time.perf_counter()

        try:
            route = self.state.router.route(request.model)
            if route is None:
                raise NotFoundError(f"model {request.model!r} is not configured")

            await self.state.plugins.run_request(ctx)
            self._acquire_concurrency(ctx)

            if ctx.cache_hit and ctx.response is not None:
                ctx.status_code = 200
                ctx.latency_s = time.perf_counter() - started
                self._record_usage_from_body(ctx, ctx.response)
                self._metrics_finish(ctx)
                await self.state.plugins.run_response(ctx)
                self._release_concurrency(ctx)
                return ctx.response  # type: ignore[return-value]

            ctx.upstream_started_at = time.perf_counter()
            try:
                result = await self.state_dispatcher().execute_embedding(route, payload)
            finally:
                ctx.upstream_ended_at = time.perf_counter()
            ctx.attempts = result.attempts
            ctx.route = result.route
            ctx.deployment = result.deployment
            ctx.latency_s = time.perf_counter() - started
            body_out = result.body

            usage = body_out.get("usage")
            if isinstance(usage, dict) and usage.get("total_tokens") is not None:
                ctx.prompt_tokens = usage.get("prompt_tokens")
                ctx.completion_tokens = usage.get("completion_tokens") or 0
                ctx.cache_read_tokens, ctx.cache_write_tokens = cache_tokens_of(usage)
                ctx.usage_reported = True
            else:
                ctx.prompt_tokens = self.tokenizer.estimate(embedding_request_text(payload))
                ctx.completion_tokens = 0
            ctx.response = body_out
            ctx.status_code = 200
            self._record_cost(ctx)

            try:
                await self.state.plugins.run_response(ctx)
            except GatewayError:
                ctx.response = None
                raise

            self._metrics_finish(ctx)
            return body_out
        except GatewayError as exc:
            self._finish_error(ctx, exc, started)
            raise
        finally:
            self._release_concurrency(ctx)

    # -- passthrough tunnel ----------------------------------------------------------

    def _passthrough_enabled(self) -> bool:
        section = (self._server_limits() or {}).get("passthrough")
        return isinstance(section, dict) and bool(section.get("enabled"))

    async def passthrough(
        self,
        *,
        model_name: str,
        subpath: str,
        query: str = "",
        method: str = "POST",
        body_bytes: bytes | None = None,
        body_dict: Any = None,
        headers: dict[str, str] | None = None,
        client_host: str = "",
    ) -> PassthroughResult:
        """Relay a raw request to the model's upstream (v1.2 escape hatch).

        Tunnel semantics: the upstream path is appended verbatim to the
        deployment's base_url and its status/body stream back unmodified. The
        gateway still provides identity governance (virtual-key auth, model
        allowlist, budgets, rate limiting, concurrency, request logs, metrics)
        but skips schema validation, caches, canary and stream content hooks.
        JSON bodies get their `model` field rewritten to the upstream model so
        virtual model names keep working. Transport failures raise
        UpstreamUnavailable (502 envelope) — upstream 4xx/5xx relay as-is.
        """
        started = time.perf_counter()
        ctx = PluginContext(
            request_id=uuid.uuid4().hex,
            model=model_name,
            payload=body_dict if isinstance(body_dict, dict) else {},
            stream=False,
            headers=dict(headers or {}),
            client_host=client_host,
        )
        ctx.passthrough = True
        streaming = False

        try:
            if not self._passthrough_enabled():
                raise NotFoundError("passthrough is not enabled on this gateway")
            route = self.state.router.route(model_name)
            if route is None:
                raise NotFoundError(f"model {model_name!r} is not configured")

            await self.state.plugins.run_request(ctx)
            self._acquire_concurrency(ctx)

            deployment = self.state.router.pick(route, exclude=set())
            if deployment is None:
                raise AllDeploymentsFailedError(f"no healthy deployment for model {model_name!r}")
            provider = self.state.providers.get(deployment.provider)
            if provider is None or not hasattr(provider, "request_raw"):
                raise BadRequestError(
                    f"provider {deployment.provider!r} of model {model_name!r} "
                    "does not support passthrough (openai/anthropic upstreams only)"
                )

            body_out = body_bytes
            if isinstance(body_dict, dict) and isinstance(body_dict.get("model"), str):
                body_out = json.dumps({**body_dict, "model": deployment.model}, ensure_ascii=False).encode()

            ctx.route = route
            ctx.deployment = deployment
            ctx.attempts = 1
            ctx.upstream_started_at = time.perf_counter()
            try:
                response = await provider.request_raw(
                    method=method,
                    path=subpath,
                    query=query,
                    body=body_out,
                    timeout=route.policy.timeout_s,
                )
            except UpstreamUnavailable:
                self.state.router.report_failure(deployment, route)
                raise
            latency = time.perf_counter() - started

            relay_headers = {
                name: value
                for name, value in response.headers.items()
                if name.lower() in _PASSTHROUGH_RELAY_HEADERS
            }
            content_type = response.headers.get("content-type", "")

            if "text/event-stream" in content_type:
                # the relay generator owns concurrency release for this path —
                # it is still in flight when this method returns
                streaming = True
                return PassthroughResult(
                    status=response.status_code,
                    headers=relay_headers,
                    stream=self._passthrough_stream(ctx, response, route, started),
                )

            try:
                data = await response.aread()
            finally:
                await response.aclose()
            ctx.upstream_ended_at = time.perf_counter()
            ctx.latency_s = time.perf_counter() - started
            ctx.status_code = response.status_code
            if response.status_code < 400:
                self.state.router.report_success(deployment, latency)
            elif response.status_code in RETRYABLE_STATUSES:
                # only upstream-side failures cool the deployment down; client
                # errors on tunnel paths are the caller's, not the route's
                self.state.router.report_failure(deployment, route)

            if response.status_code < 400:
                self._passthrough_settle(ctx, data)
                try:
                    await self.state.plugins.run_response(ctx)
                except GatewayError:
                    ctx.response = None
                    raise
            self.metrics.inc_passthrough(model_name)
            self._passthrough_metrics(ctx)
            return PassthroughResult(
                status=response.status_code, headers=relay_headers, body=data
            )
        except GatewayError as exc:
            self._finish_error(ctx, exc, started)
            raise
        finally:
            if not streaming:
                self._release_concurrency(ctx)

    def _passthrough_settle(self, ctx: PluginContext, data: bytes) -> None:
        """Usage/cost settlement for a buffered passthrough response: usage is
        recorded only when the upstream body reports it (bodies the gateway
        cannot parse contribute nothing)."""
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(parsed, dict):
            return
        ctx.response = parsed
        usage = parsed.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens") is not None:
            ctx.prompt_tokens = usage.get("prompt_tokens")
            ctx.completion_tokens = usage.get("completion_tokens") or 0
            ctx.cache_read_tokens, ctx.cache_write_tokens = cache_tokens_of(usage)
            ctx.usage_reported = True
        self._record_cost(ctx)

    def _passthrough_metrics(self, ctx: PluginContext) -> None:
        """Metrics finish for the tunnel: request/latency/tokens only — cache
        hit-rate series are not applicable to passthrough traffic."""
        provider = ctx.deployment.provider if ctx.deployment else "none"
        status = ctx.status_code or 200
        self.metrics.observe_request(ctx.model, provider, status, ctx.latency_s or 0.0)
        if ctx.usage_reported:
            self.metrics.inc_tokens(ctx.model, provider, ctx.prompt_tokens or 0, ctx.completion_tokens or 0)

    async def _passthrough_stream(
        self, ctx: PluginContext, response: httpx.Response, route, started: float
    ) -> AsyncIterator[bytes]:
        """Relay an upstream SSE/byte stream verbatim; cleanup mirrors
        _chat_streaming (concurrency release, metrics, error hooks)."""
        error: GatewayError | None = None
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            error = UpstreamError(f"passthrough stream failed: {exc}")
            ctx.error = error
        except GatewayError as exc:  # noqa: BLE001 - never leak a raw exception to the client
            error = exc
            ctx.error = error
        finally:
            await response.aclose()
            self._release_concurrency(ctx)
            ctx.upstream_ended_at = time.perf_counter()
            ctx.latency_s = time.perf_counter() - started
            if error is not None:
                if route is not None and ctx.deployment is not None:
                    self.state.router.report_failure(ctx.deployment, route)
                self._finish_error(ctx, error, started)
            else:
                ctx.status_code = response.status_code
                if ctx.deployment is not None:
                    self.state.router.report_success(
                        ctx.deployment, (ctx.upstream_ended_at or 0) - (ctx.upstream_started_at or 0)
                    )
                self.metrics.inc_passthrough(ctx.model)
                self._passthrough_metrics(ctx)
                try:
                    await self.state.plugins.run_stream_end(ctx)
                except GatewayError:
                    # post-delivery rejection: chunks are already gone; log loudly
                    logger.error(
                        "passthrough stream %s rejected by a plugin after delivery",
                        ctx.request_id,
                    )

    # -- non-streaming -----------------------------------------------------------

    async def _chat_non_streaming(self, ctx: PluginContext, route, started: float) -> dict[str, Any]:
        ctx.upstream_started_at = time.perf_counter()
        try:
            result = await self.state_dispatcher().execute(route, ctx.payload)
        finally:
            ctx.upstream_ended_at = time.perf_counter()
        ctx.attempts = result.attempts
        ctx.route = result.route
        ctx.deployment = result.deployment
        ctx.latency_s = time.perf_counter() - started
        body = result.body
        self._record_usage_from_body(ctx, body)
        ctx.response = body
        ctx.status_code = 200
        self._record_cost(ctx)  # before response hooks: plugins bill budgets from ctx.cost_usd

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
            ctx.upstream_started_at = time.perf_counter()
            async for chunk in self.state_dispatcher().stream(route, ctx.payload, outcome):
                ctx.first_token_latency_s = ctx.first_token_latency_s or outcome.first_chunk_latency_s
                filtered = await state.plugins.run_stream_chunk(ctx, chunk)
                if filtered is None:
                    continue
                text_parts.append(chunk_text(filtered))
                yield _sse(filtered)
            ctx.upstream_ended_at = time.perf_counter()
            # Settle usage and cost before end hooks so plugins (token-limit
            # reconciliation, budget billing) see the final numbers.
            if outcome.usage and not ctx.usage_reported:
                ctx.prompt_tokens = outcome.usage.get("prompt_tokens")
                ctx.completion_tokens = outcome.usage.get("completion_tokens")
                ctx.cache_read_tokens, ctx.cache_write_tokens = cache_tokens_of(outcome.usage)
                ctx.usage_reported = True
            self._record_cost(ctx)
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
            self._release_concurrency(ctx)
            ctx.latency_s = time.perf_counter() - started
            if ctx.upstream_started_at is not None and ctx.upstream_ended_at is None:
                ctx.upstream_ended_at = time.perf_counter()
            ctx.attempts = outcome.attempts
            if outcome.route is not None:
                ctx.route = outcome.route
                ctx.deployment = outcome.deployment
            if outcome.usage and not ctx.usage_reported:
                ctx.prompt_tokens = outcome.usage.get("prompt_tokens")
                ctx.completion_tokens = outcome.usage.get("completion_tokens")
                ctx.cache_read_tokens, ctx.cache_write_tokens = cache_tokens_of(outcome.usage)
                ctx.usage_reported = True
            if not ctx.usage_reported:
                ctx.prompt_tokens = ctx.prompt_tokens or self.tokenizer.estimate(request_text(ctx.payload))
                ctx.completion_tokens = ctx.completion_tokens or self.tokenizer.estimate("".join(text_parts))
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
            ctx.cache_read_tokens, ctx.cache_write_tokens = cache_tokens_of(usage)
            ctx.usage_reported = True
        else:
            ctx.prompt_tokens = self.tokenizer.estimate(request_text(ctx.payload))
            ctx.completion_tokens = self.tokenizer.estimate(response_text(body))

    def _pricing_for(self, model: str) -> dict[str, float] | None:
        pricing = self.state.config.section("pricing") or {}
        entry = pricing.get(model)
        return entry if isinstance(entry, dict) else None

    def _record_cost(self, ctx: PluginContext) -> None:
        """Compute and record spend; sets ctx.cost_usd for budget accounting.

        Tiered pricing: `cache_read_per_1k` / `cache_write_per_1k` price the
        cached portions of the prompt; the remainder bills at `input_per_1k`.
        Cache hits and unpriced models cost nothing (silent no-op)."""
        if ctx.cache_hit or not ctx.usage_reported:
            ctx.cost_usd = 0.0
            return
        pricing = self._pricing_for(ctx.model)
        if not pricing:
            ctx.cost_usd = 0.0
            return
        cache_read = ctx.cache_read_tokens or 0
        cache_write = ctx.cache_write_tokens or 0
        uncached_input = max(0, (ctx.prompt_tokens or 0) - cache_read - cache_write)
        input_rate = float(pricing.get("input_per_1k", 0))
        try:
            usd = (
                uncached_input / 1000 * input_rate
                + (ctx.completion_tokens or 0) / 1000 * float(pricing.get("output_per_1k", 0))
                # missing cache rates fall back to the input price (conservative)
                + cache_read / 1000 * float(pricing.get("cache_read_per_1k", input_rate))
                + cache_write / 1000 * float(pricing.get("cache_write_per_1k", input_rate))
            )
        except (TypeError, ValueError):
            ctx.cost_usd = 0.0
            return
        ctx.cost_usd = usd
        self.metrics.inc_cost(ctx.model, usd)
        identity = ctx.identity if isinstance(ctx.identity, dict) else {}
        key_name = str(identity.get("name") or "")
        if key_name and key_name != "anonymous":
            self.metrics.inc_key_cost(key_name, usd)

    def _metrics_finish(self, ctx: PluginContext) -> None:
        provider = ctx.deployment.provider if ctx.deployment else "none"
        status = ctx.status_code or 200
        self.metrics.observe_request(ctx.model, provider, status, ctx.latency_s or 0.0)
        if ctx.stream and ctx.first_token_latency_s:
            self.metrics.observe_first_token(ctx.model, provider, ctx.first_token_latency_s)
        if not ctx.usage_reported:
            ctx.prompt_tokens = ctx.prompt_tokens or self.tokenizer.estimate(request_text(ctx.payload))
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
        if exc.code in ("rate_limit_exceeded", "concurrency_exceeded"):
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
