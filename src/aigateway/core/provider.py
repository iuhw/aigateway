"""Provider abstraction.

A provider speaks the normalized OpenAI wire protocol: `chat()` performs a
non-streaming completion, `stream()` is an async generator of parsed SSE chunk
dicts. Routing, retries and failover are *not* provider concerns — they live in
`router.py` / `dispatcher.py`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from aigateway.errors import GatewayError


@dataclass
class ProviderResult:
    """Result of a non-streaming upstream call, including error statuses."""

    status: int
    body: dict[str, Any]
    latency_s: float

    @property
    def ok(self) -> bool:
        return self.status < 400


class UpstreamUnavailable(GatewayError):
    """Transport-level failure (connect error, timeout). Retryable by design."""

    status_code = 503
    code = "upstream_unavailable"


class UpstreamStreamError(Exception):
    """Non-2xx status on a streaming request (raised before any chunk is yielded)."""

    def __init__(self, status_code: int, body_text: str):
        self.status_code = status_code
        self.body_text = body_text
        super().__init__(f"upstream stream error {status_code}: {body_text}")


class Provider(ABC):
    """Base class for all upstream providers."""

    #: Inject `stream_options={"include_usage": true}` when streaming. Only
    #: enable for upstreams that support it; otherwise usage falls back to
    #: heuristic estimation.
    supports_stream_usage_option: bool = False

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def chat(self, *, model: str, payload: dict[str, Any], timeout: float) -> ProviderResult:
        """Perform a non-streaming chat completion (any HTTP status is returned, not raised)."""

    @abstractmethod
    def stream(
        self, *, model: str, payload: dict[str, Any], timeout: float
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed SSE chunk dicts. `data: [DONE]` is not yielded."""

    @abstractmethod
    async def health(self) -> bool:
        """Cheap liveness probe used by /readyz."""

    async def aclose(self) -> None:
        """Release underlying resources."""


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """Parse one SSE `data:` line into a chunk dict; [DONE]/other lines -> None."""
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None
    return chunk if isinstance(chunk, dict) else None
