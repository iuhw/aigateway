"""FastAPI adapter: OpenAI-compatible HTTP surface over GatewayApp."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from aigateway.app import GatewayApp
from aigateway.errors import BadRequestError, GatewayError
from aigateway.schemas import ChatCompletionRequest


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

    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(_request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_openai_error())

    @app.exception_handler(ValidationError)
    async def _validation_error_handler(_request: Request, exc: ValidationError) -> JSONResponse:
        error = BadRequestError(f"Invalid request body: {exc.error_count()} error(s): {exc.errors()[:3]}")
        return JSONResponse(status_code=error.status_code, content=error.to_openai_error())

    async def _chat(request: Request) -> Response:
        try:
            body: dict[str, Any] = await request.json()
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
    async def metrics() -> Response:
        return Response(content=generate_latest(gateway.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/admin/routes")
    async def admin_routes() -> dict[str, Any]:
        return {"routes": gateway.model_info()}

    @app.get("/admin/plugins")
    async def admin_plugins() -> dict[str, Any]:
        return {"plugins": gateway.plugin_info()}

    return app
