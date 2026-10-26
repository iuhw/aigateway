"""PII redaction plugin — blueprint M3 (`pii-redaction-plugin`).

Detects personally identifiable information (built-in regex groups + custom
patterns) in requests and/or responses and either blocks the request or masks
the occurrences. Streaming masking reuses the keyword filter's hold-back
strategy, adapted for variable-length regexes: the emit boundary is pulled
back to the start of any match that crosses it, so no partially-buffered
match is ever emitted half-masked. Matches longer than `holdback_chars` that
are still incomplete at the buffer edge cannot be detected yet — the default
(128) covers every built-in group and the practical email length.

Built-in groups (CN-centric, deliberately conservative):
  - ``phone``:    CN mobile numbers (1[3-9] + 9 digits, digit-bounded)
  - ``email``:    common email shape
  - ``id_card``:  18-digit CN resident id (checksum char X/x allowed)
  - ``bank_card``: 13-19 digit runs (checked after phone/id so those win)

Actions: ``mask`` (replace every match with ``mask_char`` x match length, or a
custom ``replacement`` template) or ``block`` (HTTP 400 ``pii_detected``).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from aigateway.errors import ContentFilterError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin
from aigateway.schemas import (
    chunk_text,
    request_text,
    response_text,
    set_chunk_text,
    set_response_text,
)

BUILTIN_PATTERNS: dict[str, str] = {
    "phone": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",
    "id_card": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "bank_card": r"(?<!\d)\d{13,19}(?!\d)",
}


@register_plugin
class PiiRedactionPlugin(Plugin):
    """Masks or blocks PII in requests/responses, streaming-safe."""

    name: ClassVar[str] = "pii-redaction-plugin"
    default_order: ClassVar[int] = 110  # before keyword filter, after auth

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        cfg = self.config
        self.action: str = cfg.get("action", "mask")
        if self.action not in ("mask", "block"):
            raise ValueError(f"pii_redaction: unknown action {self.action!r}")
        self.mask_char: str = str(cfg.get("mask_char", "*"))[:1] or "*"
        self.replacement: str | None = cfg.get("replacement")  # re.sub template, wins over mask_char
        self.holdback_chars: int = max(16, int(cfg.get("holdback_chars", 128)))
        self.check_request: bool = "request" in cfg.get("check", ["request", "response"])
        self.check_response: bool = "response" in cfg.get("check", ["request", "response"])
        self.block_message: str = str(
            cfg.get("block_message", "Personal identifiable information detected and blocked.")
        )
        groups = list(cfg.get("groups", ["phone", "email", "id_card", "bank_card"]))
        unknown = [g for g in groups if g not in BUILTIN_PATTERNS]
        if unknown:
            raise ValueError(f"pii_redaction: unknown groups {unknown}; builtins: {sorted(BUILTIN_PATTERNS)}")
        # Fixed-length groups first so e.g. an 18-digit id is not half-eaten by
        # the bank-card pattern.
        ordered = [g for g in ("id_card", "phone", "bank_card", "email") if g in groups]
        self._compiled: list[re.Pattern[str]] = [re.compile(BUILTIN_PATTERNS[g]) for g in ordered]
        for pattern in cfg.get("custom_patterns", []):
            self._compiled.append(re.compile(str(pattern)))
        self.enabled = bool(self._compiled)

    # -- detection & masking -----------------------------------------------------

    def find(self, text: str) -> re.Match[str] | None:
        if not text:
            return None
        for pattern in self._compiled:
            match = pattern.search(text)
            if match:
                return match
        return None

    def mask_text(self, text: str) -> str:
        for pattern in self._compiled:
            if self.replacement is None:
                text = pattern.sub(lambda m, c=self.mask_char: c * len(m.group(0)), text)
            else:
                text = pattern.sub(lambda m, t=self.replacement: m.expand(t), text)
        return text

    # -- hooks -----------------------------------------------------------------

    async def on_request(self, ctx: PluginContext) -> None:
        if not self.check_request:
            return
        text = request_text(ctx.payload)
        if self.action == "block":
            if self.find(text):
                raise ContentFilterError(self.block_message, code="pii_detected")
        else:
            for message in ctx.payload.get("messages", []):
                content = message.get("content")
                if isinstance(content, str):
                    masked = self.mask_text(content)
                    if masked != content:
                        message["content"] = masked
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            part["text"] = self.mask_text(part["text"])

    async def on_response(self, ctx: PluginContext) -> None:
        if not self.check_response or ctx.response is None:
            return
        text = response_text(ctx.response)
        if self.action == "block":
            if self.find(text):
                raise ContentFilterError(self.block_message, code="pii_detected")
        else:
            masked = self.mask_text(text)
            if masked != text:
                set_response_text(ctx.response, masked)

    async def on_stream_chunk(self, ctx: PluginContext, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if not self.check_response:
            return chunk
        state = ctx.state.setdefault(self.display_name(), {"buffer": ""})
        text = chunk_text(chunk)
        is_finish = any(c.get("finish_reason") for c in chunk.get("choices") or [])

        if self.action == "block":
            # Full accumulation: a match may span any number of chunks.
            seen = state["buffer"] + text
            if self.find(seen):
                raise ContentFilterError(self.block_message, code="pii_detected")
            state["buffer"] = seen
            return chunk

        buffer = state["buffer"] + text
        if is_finish:
            if buffer:
                set_chunk_text(chunk, self.mask_text(buffer))
            state["buffer"] = ""
            return chunk
        # Emit only the provably-safe prefix: any match crossing the candidate
        # boundary pulls the boundary back to its start.
        split = max(0, len(buffer) - self.holdback_chars)
        for pattern in self._compiled:
            for match in pattern.finditer(buffer):
                if match.start() < split < match.end():
                    split = match.start()
        if split <= 0:
            state["buffer"] = buffer
            if text:
                set_chunk_text(chunk, "")  # keep chunk structure, emit no text yet
            return chunk
        emit, keep = buffer[:split], buffer[split:]
        state["buffer"] = keep
        set_chunk_text(chunk, self.mask_text(emit))
        return chunk

    async def on_stream_end(self, ctx: PluginContext) -> None:
        if not self.check_response or self.action != "mask":
            return
        state = ctx.state.get(self.display_name())
        if state and state.get("buffer"):
            from aigateway.plugins.base import logger as plugin_logger

            plugin_logger.warning(
                "pii_redaction: dropped %d held-back chars (stream ended without finish_reason)",
                len(state["buffer"]),
            )
