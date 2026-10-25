"""FastAPI adapter: OpenAI-compatible HTTP surface over GatewayApp."""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from aigateway.app import GatewayApp
from aigateway.errors import (
    AuthenticationError,
    BadRequestError,
    GatewayError,
    NotFoundError,
    PayloadTooLargeError,
)
from aigateway.schemas import ChatCompletionRequest

#: Hard ceiling on how much of a request body is read, regardless of config.
ABSOLUTE_MAX_BODY_BYTES = 64 * 1024 * 1024

logger = logging.getLogger("aigateway.api")


def create_app(config_path: str, *, config_watcher: bool = True) -> FastAPI:
    gateway = GatewayApp(config_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if config_watcher:
            await gateway.start_config_watcher()
        yield
        await gateway.aclose()

    app = FastAPI(title="aigateway", version="0.1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan)
    app.state.gateway = gateway

    if not (gateway.state.config.section("server") or {}).get("admin_api_key"):
        logger.warning(
            "server.admin_api_key is not set: /metrics and /admin/* are UNPROTECTED (dev mode)"
        )

    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_openai_error())

    @app.exception_handler(ValidationError)
    async def _validation_error_handler(_request: Request, exc: ValidationError) -> JSONResponse:
        error = BadRequestError(f"Invalid request body: {exc.error_count()} error(s): {exc.errors()[:3]}")
        return JSONResponse(status_code=error.status_code, content=error.to_openai_error())

    def _require_admin(request: Request) -> None:
        """/metrics and /admin/* require server.admin_api_key when configured.

        Unset means dev mode (startup logs a warning); production must set it.
        """
        admin_key = (gateway.state.config.section("server") or {}).get("admin_api_key")
        if not admin_key:
            return
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
        if not token or not hmac.compare_digest(token, str(admin_key)):
            raise AuthenticationError("admin API key required", code="admin_auth_required")

    async def _read_body(request: Request) -> bytes:
        """Read the body with a hard size cap (header pre-check + streamed read)."""
        limits = gateway.state.config.section("server") or {}
        max_bytes = min(int(limits.get("max_body_bytes", 10 * 1024 * 1024)), ABSOLUTE_MAX_BODY_BYTES)
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > max_bytes:
            raise PayloadTooLargeError(f"request body exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_bytes:
                raise PayloadTooLargeError(f"request body exceeds {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _chat(request: Request) -> Response:
        raw = await _read_body(request)
        try:
            body: dict[str, Any] = json.loads(raw)
        except Exception as exc:
            raise BadRequestError(f"request body must be valid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise BadRequestError("request body must be a JSON object")
        # Validate strictly (raises ValidationError -> 400 handler) before processing.
        ChatCompletionRequest(**body)
        client_host = request.client.host if request.client else ""
        result = await gateway.chat(body, dict(request.headers), client_host)

        if isinstance(result, dict):
            return JSONResponse(status_code=200, content=result)

        async def sse_stream(inner: AsyncIterator[str]) -> AsyncIterator[str]:
            async for line in inner:
                yield line

        return StreamingResponse(
            sse_stream(result),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        return await _chat(request)

    @app.post("/chat/completions")
    async def chat_completions_alias(request: Request) -> Response:
        return await _chat(request)

    @app.post("/v1/messages")
    async def anthropic_messages(request: Request) -> Response:
        """Anthropic Messages API surface: translated to the internal OpenAI
        pipeline (same auth/plugins/routing), responses translated back."""
        from aigateway.anthropic_api import (
            error_body,
            prompt_text_of,
            stream_events,
            translate_request,
            translate_response,
        )

        raw = await _read_body(request)
        try:
            body = json.loads(raw)
        except Exception as exc:
            raise BadRequestError(f"request body must be valid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise BadRequestError("request body must be a JSON object")

        client_host = request.client.host if request.client else ""
        try:
            openai_body = translate_request(body)
            stream = bool(body.get("stream"))
            openai_body["stream"] = stream
            result = await gateway.chat(openai_body, dict(request.headers), client_host)
        except GatewayError as exc:
            return JSONResponse(status_code=exc.status_code, content=error_body(exc))

        if isinstance(result, dict):
            return JSONResponse(status_code=200, content=translate_response(result))

        prompt = prompt_text_of(openai_body)
        max_tokens = int(openai_body.get("max_tokens") or 0)

        async def anthropic_stream() -> AsyncIterator[str]:
            async for event in stream_events(
                result,
                model=str(openai_body["model"]),
                prompt_text=prompt,
                max_tokens=max_tokens,
                estimator=gateway.tokenizer,
            ):
                yield event

        return StreamingResponse(
            anthropic_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        """OpenAI-compatible embeddings; served through the same auth/plugin/
        routing/failover pipeline as chat completions (non-streaming only)."""
        raw = await _read_body(request)
        try:
            body: dict[str, Any] = json.loads(raw)
        except Exception as exc:
            raise BadRequestError(f"request body must be valid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise BadRequestError("request body must be a JSON object")
        client_host = request.client.host if request.client else ""
        result = await gateway.embeddings(body, dict(request.headers), client_host)
        return JSONResponse(status_code=200, content=result)

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "owned_by": "aigateway"} for name in gateway.model_names()
            ],
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        providers = gateway.state.providers
        if not providers:
            return JSONResponse(status_code=200, content={"status": "ready", "providers": {}})
        results = {name: await p.health() for name, p in providers.items()}
        status = "ready" if any(results.values()) else "degraded"
        return JSONResponse(
            status_code=200 if status == "ready" else 503,
            content={"status": status, "providers": results},
        )

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        _require_admin(request)
        return Response(content=generate_latest(gateway.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/admin/routes")
    async def admin_routes(request: Request) -> dict[str, Any]:
        _require_admin(request)
        return {"routes": gateway.model_info()}

    @app.get("/admin/plugins")
    async def admin_plugins(request: Request) -> dict[str, Any]:
        _require_admin(request)
        return {"plugins": gateway.plugin_info()}

    def _auth_plugin():
        from aigateway.plugins.auth import AuthPlugin

        for plugin in gateway.state.plugins.plugins:
            if isinstance(plugin, AuthPlugin):
                return plugin
        return None

    @app.get("/admin/keys")
    async def admin_list_keys(request: Request) -> dict[str, Any]:
        """All virtual keys (static config + dynamic overlay); key material is
        never returned, only a masked hash prefix."""
        _require_admin(request)
        plugin = _auth_plugin()
        if plugin is None:
            raise NotFoundError("auth plugin is not configured")
        return {"keys": await plugin.list_keys()}

    @app.post("/admin/keys", status_code=201)
    async def admin_create_key(request: Request) -> JSONResponse:
        """Create a dynamic virtual key (stored in the shared-state backend).

        The raw ``api_key`` is returned exactly once. Requires the shared_state
        backend to support dynamic keys (memory keeps them per-process, Redis
        shares and persists them)."""
        _require_admin(request)
        plugin = _auth_plugin()
        if plugin is None:
            raise NotFoundError("auth plugin is not configured")
        raw = await _read_body(request)
        try:
            spec: Any = json.loads(raw)
        except Exception as exc:
            raise BadRequestError(f"request body must be valid JSON: {exc}") from exc
        if not isinstance(spec, dict):
            raise BadRequestError("request body must be a JSON object")
        if not spec.get("name"):
            raise BadRequestError("dynamic keys require a 'name'")
        record, raw_key = await plugin.create_dynamic_key(spec)
        return JSONResponse(
            status_code=201,
            content={
                "key": record,
                "api_key": raw_key,
                "note": "store this api_key now; only its hash is kept",
            },
        )

    @app.delete("/admin/keys/{name}")
    async def admin_delete_key(name: str, request: Request) -> dict[str, Any]:
        """Delete a dynamic virtual key (config-defined keys return 409)."""
        _require_admin(request)
        plugin = _auth_plugin()
        if plugin is None:
            raise NotFoundError("auth plugin is not configured")
        await plugin.delete_dynamic_key(name)
        return {"deleted": name}

    @app.get("/admin/usage")
    async def admin_usage(request: Request, key: str | None = None) -> dict[str, Any]:
        """Per-key current-window budget spend and token-quota usage."""
        _require_admin(request)
        plugin = _auth_plugin()
        if plugin is None:
            raise NotFoundError("auth plugin is not configured")
        return await plugin.usage_snapshot(name_filter=key)

    return app
