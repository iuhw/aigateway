"""Webhook content-review plugin — blueprint M3 (`webhook-review-plugin`).

Sends request/response text to an external moderation service over HTTP and
enforces its verdict locally — the integration point for cloud content-safety
services (Aliyun/Green, Tencent CMS, AWS Comprehend, self-hosted Llama Guard, …).

Wire contract (request side)::

    POST <url>
    {"version": "v1", "request_id": ..., "model": ..., "stage": "request"|"response",
     "text": "...", "truncated": bool}
    → 200 {"action": "allow"}
    → 200 {"action": "block", "reason": "..."}

Anything else — non-200, timeout, connection error, malformed body, unknown
action — is *unavailable*, not *unsafe*: the ``on_unavailable`` policy decides.

  - ``fail-open`` (default): pass through, count the outcome in
    ``gateway_guardrail_flags_total{verdict="timeout"|"error"|"invalid_response"}``.
    Availability beats moderation for most deployments.
  - ``fail-close``: reject with HTTP 503 ``webhook_review_unavailable``.

Verdicts are counted in ``gateway_guardrail_flags_total`` with verdict=
``allowed`` | ``blocked`` | the unavailable outcomes above.

Boundaries (documented in operations.md):
  - the webhook call is on the request path: its latency gates every reviewed
    request — keep the endpoint fast, or scope ``check`` to the response side;
  - streaming responses can only be reviewed *after* delivery (chunks cannot
    be recalled): the full text is reviewed at stream end and a block verdict
    is recorded/logged, but content already sent cannot be pulled back.
"""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

import httpx

from aigateway.errors import ContentFilterError, PluginReject
from aigateway.plugins.base import Plugin, PluginContext, register_plugin
from aigateway.schemas import chunk_text, request_text, response_text

logger = logging.getLogger("aigateway.plugins.webhook_review")

ALLOWED_ACTIONS = frozenset({"allow", "allowed", "pass", "approve", "safe"})
BLOCKED_ACTIONS = frozenset({"block", "blocked", "reject", "deny", "unsafe"})

UNAVAILABLE_OUTCOMES = frozenset({"timeout", "error", "invalid_response"})


@register_plugin
class WebhookReviewPlugin(Plugin):
    """Reviews content via an external webhook; fail-open/fail-close configurable."""

    name: ClassVar[str] = "webhook-review-plugin"
    default_order: ClassVar[int] = 130  # after local filters — webhook sees only survivors

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        cfg = self.config
        self.url: str = str(cfg.get("url", "")).strip()
        if not self.url:
            raise ValueError("webhook_review: 'url' is required")
        if "://" not in self.url:
            raise ValueError(f"webhook_review: 'url' must be absolute, got {self.url!r}")
        self.timeout_s: float = float(cfg.get("timeout_s", 2.0))
        self.on_unavailable: str = str(cfg.get("on_unavailable", "fail-open"))
        if self.on_unavailable not in ("fail-open", "fail-close"):
            raise ValueError(f"webhook_review: unknown on_unavailable {self.on_unavailable!r}")
        self.check_request: bool = "request" in cfg.get("check", ["request"])
        self.check_response: bool = "response" in cfg.get("check", ["request"])
        self.block_message: str = str(
            cfg.get("block_message", "Content rejected by external review service.")
        )
        self.extra_headers: dict[str, str] = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.max_text_chars: int = max(1, int(cfg.get("max_text_chars", 20_000)))
        self._metrics: Any = None
        self._client: httpx.AsyncClient | None = None

    def set_metrics(self, metrics: Any) -> None:
        """GatewayApp injection (existing set_shared convention)."""
        self._metrics = metrics

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout_s)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._make_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- webhook ------------------------------------------------------------------

    async def _review(
        self, ctx: PluginContext, text: str, stage: str, *, already_truncated: bool = False
    ) -> tuple[str, str]:
        """POST the text; return (verdict, reason). verdict is an outcome as
        counted in gateway_guardrail_flags_total (allowed/blocked/timeout/error/
        invalid_response). Never raises."""
        if not text:
            return "allowed", ""
        truncated = already_truncated or len(text) > self.max_text_chars
        payload = {
            "version": "v1",
            "request_id": ctx.request_id,
            "model": ctx.model,
            "stage": stage,
            "text": text[: self.max_text_chars],
            "truncated": truncated,
        }
        try:
            response = await self._get_client().post(
                self.url, json=payload, headers={"content-type": "application/json", **self.extra_headers}
            )
        except httpx.TimeoutException:
            self._count("timeout")
            logger.warning(
                "webhook_review: timeout after %.1fs on %s (%s)", self.timeout_s, ctx.request_id, stage
            )
            return "timeout", ""
        except httpx.HTTPError as exc:
            self._count("error")
            logger.warning("webhook_review: request failed on %s (%s): %r", ctx.request_id, stage, exc)
            return "error", ""

        if response.status_code != 200:
            self._count("invalid_response")
            logger.warning(
                "webhook_review: non-200 status %d on %s (%s)", response.status_code, ctx.request_id, stage
            )
            return "invalid_response", ""
        try:
            body = response.json()
            action = str(body.get("action", body.get("verdict", ""))).lower()
        except (json.JSONDecodeError, AttributeError):
            action = ""
        if action in BLOCKED_ACTIONS:
            reason = str(body.get("reason", "")) if isinstance(body, dict) else ""
            self._count("blocked")
            return "blocked", reason
        if action in ALLOWED_ACTIONS:
            self._count("allowed")
            return "allowed", ""
        self._count("invalid_response")
        logger.warning("webhook_review: unparseable review response on %s (%s)", ctx.request_id, stage)
        return "invalid_response", ""

    def _count(self, verdict: str) -> None:
        if self._metrics is not None:
            self._metrics.inc_guardrail_flag(self.display_name(), verdict)

    def _enforce(self, ctx: PluginContext, verdict: str, reason: str, stage: str) -> None:
        if verdict == "blocked":
            raise ContentFilterError(
                reason or self.block_message, code="webhook_review_blocked"
            )
        if verdict in UNAVAILABLE_OUTCOMES:
            if self.on_unavailable == "fail-close":
                raise PluginReject(
                    f"Review service unavailable ({verdict}); failing closed.",
                    code="webhook_review_unavailable",
                    status_code=503,
                )
            logger.info(
                "webhook_review: fail-open (%s) on %s (%s)", verdict, ctx.request_id, stage
            )

    # -- hooks ----------------------------------------------------------------------

    async def on_request(self, ctx: PluginContext) -> None:
        if not self.check_request:
            return
        verdict, reason = await self._review(ctx, request_text(ctx.payload), "request")
        self._enforce(ctx, verdict, reason, "request")

    async def on_response(self, ctx: PluginContext) -> None:
        if not self.check_response or ctx.response is None:
            return
        verdict, reason = await self._review(ctx, response_text(ctx.response), "response")
        self._enforce(ctx, verdict, reason, "response")

    async def on_stream_chunk(self, ctx: PluginContext, chunk: dict[str, Any]) -> dict[str, Any] | None:
        if not self.check_response:
            return chunk
        state = ctx.state.setdefault(self.display_name(), {"text": "", "truncated_full": False})
        if not state["truncated_full"]:
            state["text"] += chunk_text(chunk)
            if len(state["text"]) > self.max_text_chars:
                state["text"] = state["text"][: self.max_text_chars]
                state["truncated_full"] = True
        return chunk

    async def on_stream_end(self, ctx: PluginContext) -> None:
        """Post-delivery review of a streamed response: the verdict is recorded
        (and a block is logged loudly) but chunks already sent cannot be recalled."""
        if not self.check_response:
            return
        state = ctx.state.get(self.display_name())
        text = state["text"] if state else ""
        verdict, reason = await self._review(
            ctx, text, "response", already_truncated=bool(state and state.get("truncated_full"))
        )
        if verdict == "blocked":
            logger.error(
                "webhook_review: streamed response %s was flagged AFTER delivery (reason: %s)",
                ctx.request_id, reason or "unspecified",
            )
        elif verdict in UNAVAILABLE_OUTCOMES:
            logger.info(
                "webhook_review: post-delivery review unavailable (%s) on %s", verdict, ctx.request_id
            )
