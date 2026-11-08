"""OpenAI-compatible request schemas.

Requests are validated by pydantic but kept permissive (`extra="allow"`) so that
provider-specific fields pass through the gateway untouched. Upstream response
bodies are handled as plain dicts (lossless pass-through) instead of being
round-tripped through models.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aigateway.errors import BadRequestError


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


def embedding_inputs(payload: dict[str, Any]) -> list[str]:
    """Normalize an OpenAI embeddings `input` into plain texts for native APIs.

    str -> [str]; list of str -> as-is; token-id arrays are joined with spaces
    (the native upstreams are text-only; same convention as above).
    """
    value = payload.get("input")
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, list):
                texts.append(" ".join(str(token) for token in item))
            else:
                texts.append(str(item))
        return texts
    return [str(value)]


def embedding_list_body(
    *,
    model: str,
    vectors: list[list[float]],
    token_counts: list[int] | None = None,
    texts: list[str] | None = None,
) -> dict[str, Any]:
    """OpenAI `/v1/embeddings` response body from raw vectors.

    Usage: `token_counts` wins when the upstream reports real counts (Titan);
    otherwise estimated from the input `texts` with the gateway's heuristic.
    """
    from aigateway.tokenizers import estimate_default

    data = [{"object": "embedding", "index": i, "embedding": vector} for i, vector in enumerate(vectors)]
    if token_counts:
        total = sum(token_counts)
    elif texts:
        total = sum(estimate_default(text) for text in texts)
    else:
        total = 1
    return {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": max(1, total), "total_tokens": max(1, total)},
    }


def cache_tokens_of(usage: dict[str, Any]) -> tuple[int, int]:
    """(cache_read, cache_write) token counts from a usage dict.

    Accepts the gateway-normalized fields (`cache_read_tokens` /
    `cache_write_tokens`, emitted by the native adapters) and OpenAI's
    `prompt_tokens_details.cached_tokens`. Both are subsets of prompt_tokens.
    """
    read = usage.get("cache_read_tokens")
    if read is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            read = details.get("cached_tokens")
    write = usage.get("cache_write_tokens")
    return int(read or 0), int(write or 0)


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


# ---- multimodal (image input) helpers (v1.1) -------------------------------------

_DATA_URI_PREFIX = "data:"


def image_from_part(part: Any, where: str) -> dict[str, str]:
    """Normalized image reference from one OpenAI `image_url` content part.

    ``{"mime_type", "data"}`` for base64 data URIs, ``{"url"}`` for http(s)
    URLs. Raises BadRequestError on malformed parts — the single validation
    point shared by `extract_images` and the native adapters.
    """
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.strip():
        raise BadRequestError(
            f"{where}: image_url requires a 'url' string (data URI or https URL)"
        )
    url = url.strip()
    if url.startswith(_DATA_URI_PREFIX):
        header, _, data = url.partition(",")
        media_type = header[len(_DATA_URI_PREFIX) :].split(";", 1)[0].strip()
        if not data or not media_type.startswith("image/"):
            raise BadRequestError(
                f"{where}: image data URI must look like "
                "'data:image/png;base64,<data>' (got media type {media_type!r})"
            )
        return {"mime_type": media_type, "data": data}
    if url.startswith(("http://", "https://")):
        return {"url": url}
    raise BadRequestError(f"{where}: image url must be a data URI (base64) or an http(s) URL")


def extract_images(message: dict[str, Any]) -> list[dict[str, str]]:
    """Normalized image references from one message's content parts.

    Returns ``{"mime_type", "data"}`` items for base64 data URIs and
    ``{"url"}`` items for http(s) URLs, in message order. Raises
    BadRequestError on malformed image parts (one validation point for the
    OpenAI entry and all native adapters). Text-only/string messages return [].
    """
    content = message.get("content")
    if not isinstance(content, list):
        return []
    images: list[dict[str, str]] = []
    for i, part in enumerate(content):
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        images.append(image_from_part(part, f"content[{i}]"))
    return images


def image_data_bytes(images: list[dict[str, str]]) -> int:
    """Approximate decoded byte size of the data-URI images in a request."""
    total = 0
    for image in images:
        if "data" in image:
            # base64: 4 chars -> 3 bytes, minus padding
            total += len(image["data"]) * 3 // 4
    return total


def image_fingerprint(payload: dict[str, Any]) -> list[str]:
    """Stable per-request fingerprint of every image reference, for cache
    partitioning: requests that differ only in their images must never share
    a cached text answer. Data URIs are hashed whole; URLs by string."""
    import hashlib

    fingerprints: list[str] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        for image in extract_images(message):
            if "data" in image:
                digest = hashlib.sha256(
                    f"{image['mime_type']}\x1f{image['data']}".encode()
                ).hexdigest()
                fingerprints.append(f"sha256:{digest}")
            else:
                fingerprints.append(f"url:{image['url']}")
    return sorted(fingerprints)


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
    """Token estimate used when the upstream reports no usage.

    Delegates to the Unicode-aware heuristic in `tokenizers` (CJK-calibrated;
    the old flat ~4 chars/token model under-counted Chinese ~3.6x). Keeps the
    historical >=1 floor so call sites never record zero usage. The app itself
    uses its configurable estimator directly.
    """
    from aigateway.tokenizers import estimate_default

    return max(1, estimate_default(text))
