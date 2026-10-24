"""Structured request/response logging plugin with rotation."""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any, ClassVar

from aigateway.plugins.base import Plugin, PluginContext, register_plugin


def build_logger(path: str | None, max_bytes: int, backup_count: int) -> logging.Logger:
    logger = logging.getLogger("aigateway.request_log")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if path and not logger.handlers:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


@register_plugin
class RequestLogPlugin(Plugin):
    """Emits one JSON line per request (query + outcome) to file and/or stdout.

    Config: ``path`` (file path), ``log_prompt``/``log_response`` (bool, default
    False — payloads are truncated to ``max_body_chars`` when enabled).
    """

    default_order: ClassVar[int] = 90

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.log_prompt = bool(self.config.get("log_prompt", False))
        self.log_response = bool(self.config.get("log_response", False))
        self.max_body_chars = int(self.config.get("max_body_chars", 2000))
        self.logger = build_logger(
            self.config.get("path"),
            int(self.config.get("max_bytes", 10 * 1024 * 1024)),
            int(self.config.get("backup_count", 5)),
        )
        self.to_stdout = bool(self.config.get("stdout", False))
        self._t0 = time.perf_counter()

    def _truncate(self, text: str) -> str:
        if len(text) > self.max_body_chars:
            return text[: self.max_body_chars] + f"...[truncated {len(text) - self.max_body_chars} chars]"
        return text

    def _emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        if self.logger.handlers:
            self.logger.info(line)
        if self.to_stdout:
            print(line, flush=True)

    async def on_request(self, ctx: PluginContext) -> None:
        ctx.state[self.display_name()] = {"t0": time.perf_counter()}
        record: dict[str, Any] = {
            "event": "request",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": ctx.request_id,
            "model": ctx.model,
            "stream": ctx.stream,
            "client": ctx.client_host,
            "identity": getattr(ctx.identity, "get", lambda *_: None)("name")
            if isinstance(ctx.identity, dict)
            else None,
        }
        if self.log_prompt:
            record["payload"] = self._truncate(str(ctx.payload))
        self._emit(record)

    async def on_response(self, ctx: PluginContext) -> None:
        self._emit_outcome(ctx, "response")

    async def on_stream_end(self, ctx: PluginContext) -> None:
        self._emit_outcome(ctx, "stream_end")

    async def on_error(self, ctx: PluginContext, error: Any) -> None:
        self._emit_outcome(ctx, "error", error=str(error))

    def _emit_outcome(self, ctx: PluginContext, event: str, error: str | None = None) -> None:
        start = ctx.state.get(self.display_name(), {}).get("t0")
        latency = time.perf_counter() - start if start else None
        record: dict[str, Any] = {
            "event": event,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": ctx.request_id,
            "model": ctx.model,
            "status": ctx.status_code,
            "latency_s": round(latency, 6) if latency is not None else ctx.latency_s,
            "first_token_s": round(ctx.first_token_latency_s, 6) if ctx.first_token_latency_s else None,
            "prompt_tokens": ctx.prompt_tokens,
            "completion_tokens": ctx.completion_tokens,
            "cache_hit": ctx.cache_hit,
            "error": error,
        }
        if ctx.deployment is not None:
            record["provider"] = ctx.deployment.provider
            record["upstream_model"] = ctx.deployment.model
        if self.log_response and ctx.response is not None:
            record["response"] = self._truncate(str(ctx.response))
        self._emit(record)
