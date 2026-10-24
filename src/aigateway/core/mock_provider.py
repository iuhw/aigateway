"""Deterministic in-process provider for tests and benchmarks.

Lets the test suite exercise routing, failover, cooldown, rate limiting and
streaming without any network dependency, and lets benchmarks measure the pure
gateway overhead against a near-zero-latency upstream.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from typing import Any

from aigateway.core.provider import Provider, ProviderResult, UpstreamStreamError
from aigateway.schemas import message_text


class MockProvider(Provider):
    """Serves deterministic OpenAI-format completions with injectable failures."""

    supports_stream_usage_option = True

    def __init__(
        self,
        name: str,
        *,
        latency_s: float = 0.0,
        content_template: str = "mock reply from {model}: {echo}",
        fail_status: int | None = None,
        fail_next_n: int | None = None,
        failure_rate: float = 0.0,
        chunk_count: int = 4,
        prompt_tokens: int = 12,
        completion_tokens: int = 20,
        healthy: bool = True,
    ):
        super().__init__(name)
        self.latency_s = latency_s
        self.content_template = content_template
        self.fail_status = fail_status
        self.fail_next_n = fail_next_n
        self.failure_rate = failure_rate
        self.chunk_count = max(1, chunk_count)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.healthy = healthy
        self.call_count = 0
        self._ids = itertools.count(1)

    def _maybe_fail(self) -> int | None:
        self.call_count += 1
        status = self.fail_status or 500
        if self.fail_next_n is not None and self.fail_next_n > 0:
            self.fail_next_n -= 1
            return status
        if self.failure_rate > 0:
            return status if self.call_count % max(1, round(1 / self.failure_rate)) == 0 else None
        if self.fail_status is not None and self.fail_next_n is None:
            return status  # persistent failure mode
        return None

    def _content(self, model: str, payload: dict[str, Any]) -> str:
        echo = ""
        for message in reversed(payload.get("messages", [])):
            if message.get("role") == "user":
                echo = message_text(message)
                break
        return self.content_template.format(model=model, echo=echo)

    async def chat(self, *, model: str, payload: dict[str, Any], timeout: float) -> ProviderResult:
        started = time.perf_counter()
        if not self.healthy:
            return ProviderResult(
                status=503,
                body={"error": {"message": f"provider {self.name} is unhealthy", "type": "server_error"}},
                latency_s=0.0,
            )
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if (status := self._maybe_fail()) is not None:
            return ProviderResult(
                status=status,
                body={"error": {"message": f"mock failure from {self.name}", "type": "server_error"}},
                latency_s=time.perf_counter() - started,
            )
        content = self._content(model, payload)
        body = {
            "id": f"chatcmpl-mock-{next(self._ids)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            },
        }
        return ProviderResult(status=200, body=body, latency_s=time.perf_counter() - started)

    async def stream(
        self, *, model: str, payload: dict[str, Any], timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.healthy:
            raise UpstreamStreamError(503, f'{{"error": {{"message": "provider {self.name} is unhealthy"}}}}')
        if (status := self._maybe_fail()) is not None:
            import json

            raise UpstreamStreamError(
                status, json.dumps({"error": {"message": f"mock failure from {self.name}", "code": status}})
            )
        completion_id = f"chatcmpl-mock-{next(self._ids)}"
        created = int(time.time())
        content = self._content(model, payload)
        per_chunk = max(1, (len(content) + self.chunk_count - 1) // self.chunk_count)
        for i in range(0, len(content), per_chunk):
            if self.latency_s:
                await asyncio.sleep(self.latency_s)
            yield {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"content": content[i : i + per_chunk]}, "finish_reason": None}
                ],
            }
        yield {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            },
        }

    async def health(self) -> bool:
        return self.healthy
