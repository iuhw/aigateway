"""OpenAI-compatible request schemas.

Requests are validated by pydantic but kept permissive (`extra="allow"`) so that
provider-specific fields pass through the gateway untouched. Upstream response
bodies are handled as plain dicts (lossless pass-through) instead of being
round-tripped through models.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatCompletionRequest(BaseModel):
    """Minimal strict validation for the fields the gateway itself reasons about."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    @field_validator("messages")
    @classmethod
    def _validate_messages(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, message in enumerate(value):
            if "role" not in message:
                raise ValueError(f"messages[{i}]: 'role' is required")
            if message["role"] not in ("system", "user", "assistant", "tool", "developer", "function"):
                raise ValueError(f"messages[{i}]: unknown role {message['role']!r}")
        return value

    def upstream_payload(self, *, stream: bool) -> dict[str, Any]:
        """Serialize for the upstream provider.

        `model` and `stream` are excluded: the dispatcher re-injects them per
        deployment (the public model name may differ from the upstream one).
        Unset fields are dropped so upstream defaults apply.
        """
        payload = self.model_dump(exclude={"model", "stream"}, exclude_unset=True)
        payload["stream"] = stream
        return payload


class EmbeddingRequest(BaseModel):
    """/v1/embeddings request; `input` is a string, list of strings, or list
    of token-id arrays (per the OpenAI schema)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model: str
    input: str | list[str] | list[list[int]]
    encoding_format: str | None = None
    dimensions: int | None = None

    def upstream_payload(self) -> dict[str, Any]:
        """Serialize for the upstream provider; `model` is re-injected by the
        dispatcher per deployment, mirroring chat completions."""
        return self.model_dump(exclude={"model"}, exclude_unset=True)


def embedding_request_text(payload: dict[str, Any]) -> str:
    """Plain text of an embeddings request, for scanning and estimation."""
    value = payload.get("input")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, list):  # token-id array: text is unknown
                parts.append(" ".join(str(token) for token in item))
        return "\n".join(parts)
    return ""


def message_text(message: dict[str, Any]) -> str:
    """Best-effort plain-text extraction from a chat message for text scans."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal content parts
        parts = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"])
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def request_text(payload: dict[str, Any]) -> str:
    """All user-visible text of a chat request, joined for scanning."""
    return "\n".join(message_text(m) for m in payload.get("messages", []))


def response_text(body: dict[str, Any]) -> str:
    """Text content of a non-streaming chat completion body."""
    chunks: list[str] = []
    for choice in body.get("choices") or []:
        message = choice.get("message") or {}
        if isinstance(message.get("content"), str):
            chunks.append(message["content"])
    return "\n".join(chunks)


def set_response_text(body: dict[str, Any], text: str) -> None:
    for choice in body.get("choices") or []:
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"] = text


def chunk_text(chunk: dict[str, Any]) -> str:
    """Delta text of a streaming chat completion chunk."""
    parts: list[str] = []
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str):
            parts.append(delta["content"])
    return "".join(parts)


def set_chunk_text(chunk: dict[str, Any], text: str) -> None:
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta")
        if isinstance(delta, dict):
            delta["content"] = text


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) used when the upstream reports no usage."""
    return max(1, (len(text) + 3) // 4)
