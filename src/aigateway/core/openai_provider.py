"""OpenAI-compatible HTTP provider.

Covers any upstream exposing the OpenAI chat-completions protocol: OpenAI
itself, Ollama (`/v1`), vLLM, DeepSeek, Groq, Together, LM Studio, ... This is
the same "fast path" LiteLLM uses for `openai/`-prefixed models: requests are
forwarded nearly verbatim with only the model name remapped.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from aigateway.core.provider import (
    Provider,
    ProviderResult,
    UpstreamStreamError,
    UpstreamUnavailable,
    parse_sse_line,
)

DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def _timeout(timeout_s: float) -> httpx.Timeout:
    return httpx.Timeout(connect=5.0, read=timeout_s, write=30.0, pool=timeout_s)


class OpenAICompatibleProvider(Provider):
    """Forwards chat completions to an OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        api_key: str | None = None,
        max_connections: int = 100,
        extra_headers: dict[str, str] | None = None,
        supports_stream_usage_option: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(name)
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "headers": headers,
            "timeout": _timeout(120.0),
        }
        if transport is not None:
            # Test/fault-injection hook: caller-supplied httpx transport.
            client_kwargs["transport"] = transport
        else:
            client_kwargs["limits"] = (
                DEFAULT_LIMITS
                if max_connections == 100
                else httpx.Limits(
                    max_connections=max_connections, max_keepalive_connections=min(20, max_connections)
                )
            )
        self._client = httpx.AsyncClient(**client_kwargs)
        self.supports_stream_usage_option = supports_stream_usage_option

    async def chat(self, *, model: str, payload: dict[str, Any], timeout: float) -> ProviderResult:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/chat/completions",
                json={**payload, "model": model, "stream": False},
                timeout=_timeout(timeout),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc
        latency = time.perf_counter() - started
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": response.text[:512], "type": "upstream_invalid_body"}}
        if not isinstance(body, dict):
            body = {"error": {"message": str(body)[:512], "type": "upstream_invalid_body"}}
        return ProviderResult(status=response.status_code, body=body, latency_s=latency)

    async def stream(
        self, *, model: str, payload: dict[str, Any], timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            async with self._client.stream(
                "POST",
                "/chat/completions",
                json={**payload, "model": model, "stream": True},
                timeout=_timeout(timeout),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:512]
                    raise UpstreamStreamError(response.status_code, body)
                async for line in response.aiter_lines():
                    chunk = parse_sse_line(line)
                    if chunk is not None:
                        yield chunk
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise UpstreamUnavailable(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get("/models", timeout=_timeout(5.0))
            return response.status_code < 500
        except (httpx.TimeoutException, httpx.TransportError):
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
