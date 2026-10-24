"""Keyword detection & filtering plugin (block or mask, request and/or response)."""

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


@register_plugin
class KeywordFilterPlugin(Plugin):
    """Scans request messages and/or response content for configured keywords.

    Modes:
      - ``block``: reject with HTTP 400 ``content_filter_rejected`` as soon as a
        keyword is found (in streaming responses this happens mid-stream; chunks
        already forwarded to the client cannot be recalled — same limitation as
        Higress/LiteLLM content-safety plugins).
      - ``mask``: replace every occurrence with ``mask_char`` repeated to the
        word length. Streaming masking uses a hold-back buffer so keywords
        spanning chunk boundaries are masked too.
    """

    default_order: ClassVar[int] = 120

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        cfg = self.config
        self.words: list[str] = [str(w) for w in cfg.get("words", [])]
        self.mode: str = cfg.get("mode", "block")
        if self.mode not in ("block", "mask"):
            raise ValueError(f"keyword_filter: unknown mode {self.mode!r}")
        self.mask_char: str = str(cfg.get("mask_char", "*"))[:1] or "*"
        self.case_sensitive: bool = bool(cfg.get("case_sensitive", False))
        self.check_request: bool = "request" in cfg.get("check", ["request", "response"])
        self.check_response: bool = "response" in cfg.get("check", ["request", "response"])
        self.block_message: str = str(
            cfg.get("block_message", "Your request or response was blocked by the keyword filter.")
        )
        self._compiled = [(self._fold(w), len(w)) for w in self.words if w]

    def _fold(self, text: str) -> str:
        return text if self.case_sensitive else text.lower()

    # -- detection -------------------------------------------------------------

    def find_word(self, text: str) -> str | None:
        if not self.words or not text:
            return None
        folded = self._fold(text)
        for word, _ in self._compiled:
            if word in folded:
                return word
        return None

    def _split_safe(self, buffer: str) -> tuple[str, str]:
        """Split a buffer into (emit, keep) for streaming mask mode.

        keep holds everything that could still grow into a keyword, and no
        keyword occurrence crosses the split point, so emitting `mask(emit)`
        can never leak a partial keyword.
        """
        max_kw = max((len(w) for w, _ in self._compiled), default=1)
        split = len(buffer) - (max_kw - 1)
        if split <= 0:
            return "", buffer
        folded = self._fold(buffer)
        for word, _ in self._compiled:
            start = 0
            while True:
                pos = folded.find(word, start)
                if pos == -1:
                    break
                end = pos + len(word)
                if pos < split < end:
                    split = pos
                start = pos + 1
        if split <= 0:
            return "", buffer
        return buffer[:split], buffer[split:]

    def mask_text(self, text: str) -> str:
        if not self.words or not text:
            return text
        result = text
        for word, _length in self._compiled:
            if self.case_sensitive:
                result = result.replace(word, self.mask_char * len(word))
            else:
                pattern = re.compile(re.escape(word), re.IGNORECASE)
                result = pattern.sub(self.mask_char * len(word), result)
        return result

    # -- hooks -----------------------------------------------------------------

    async def on_request(self, ctx: PluginContext) -> None:
        if not self.check_request:
            return
        text = request_text(ctx.payload)
        if self.mode == "block":
            hit = self.find_word(text)
            if hit:
                raise ContentFilterError(
                    self.block_message, code="content_filter_rejected"
                )
        else:
            self._mask_request_messages(ctx.payload)

    def _mask_request_messages(self, payload: dict[str, Any]) -> None:
        for message in payload.get("messages", []):
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
        if self.mode == "block":
            hit = self.find_word(text)
            if hit:
                raise ContentFilterError(
                    self.block_message, code="content_filter_rejected"
                )
        else:
            masked = self.mask_text(text)
            if masked != text:
                set_response_text(ctx.response, masked)

    async def on_stream_chunk(self, ctx: PluginContext, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if not self.check_response:
            return chunk
        state = ctx.state.setdefault(self.display_name(), {"buffer": "", "seen": ""})
        text = chunk_text(chunk)
        is_finish = any(c.get("finish_reason") for c in chunk.get("choices") or [])

        if self.mode == "block":
            # Sliding window: check everything, then keep only the tail that
            # could still complete a keyword arriving in a later chunk.
            max_kw = max((len(w) for w, _ in self._compiled), default=1)
            window = state["seen"] + text
            if self.find_word(window):
                raise ContentFilterError(
                    self.block_message, code="content_filter_rejected"
                )
            state["seen"] = window[-(max_kw - 1) :] if max_kw > 1 else ""
            return chunk

        # mask mode: accumulate, then emit only the provably-safe prefix.
        buffer: str = state["buffer"] + text
        if is_finish:
            if buffer:
                set_chunk_text(chunk, self.mask_text(buffer))
            state["buffer"] = ""
            return chunk
        emit, keep = self._split_safe(buffer)
        state["buffer"] = keep
        if emit:
            set_chunk_text(chunk, self.mask_text(emit))
        elif text:
            set_chunk_text(chunk, "")
        return chunk

    async def on_stream_end(self, ctx: PluginContext) -> None:
        if not self.check_response or self.mode != "mask":
            return
        state = ctx.state.get(self.display_name())
        if state and state.get("buffer"):
            # Stream ended without a finish_reason chunk: nothing we can emit
            # post-hoc; log the dropped tail length for diagnosability.
            from aigateway.plugins.base import logger as plugin_logger

            plugin_logger.warning(
                "keyword_filter: dropped %d held-back chars (stream ended without finish_reason)",
                len(state["buffer"]),
            )
