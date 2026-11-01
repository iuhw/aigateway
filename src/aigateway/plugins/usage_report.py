"""Usage report plugin — blueprint M4 (`usage-report-plugin`).

Keeps a bounded in-memory ring of per-request outcome records and aggregates
them by key / model / time bucket; served as the ``report`` section of
``/admin/usage`` alongside the auth plugin's budget snapshot.

Boundaries (documented in operations.md):
  - **Per-process**: records live in this replica's memory — no shared state,
    and the request-path cost is one deque append. Cluster-wide and long-term
    views come from the Prometheus/OTLP exporters + Grafana instead.
  - **Metadata only**: key, model, tokens, cost, status — never content.
  - Memory is double-bounded: ``max_records`` drops the oldest records and
    ``retention_hours`` prunes whole expired windows.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, ClassVar

from aigateway.errors import GatewayError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin

BUCKETS = ("hour", "day")
_MAX_HOURS = 24 * 30  # one month of lookback at most


@dataclass(slots=True)
class _Record:
    ts: float
    key: str
    model: str
    provider: str
    status: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cache_hit: bool
    error_code: str


@register_plugin
class UsageReportPlugin(Plugin):
    """Aggregates per-request outcomes for /admin/usage reports.

    Config: ``max_records`` (50000), ``retention_hours`` (24).
    """

    name: ClassVar[str] = "usage-report-plugin"
    default_order: ClassVar[int] = 200  # record last, after every other hook settled

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.max_records = max(1, int(self.config.get("max_records", 50_000)))
        self.retention_s = max(1, int(self.config.get("retention_hours", 24))) * 3600
        self._records: deque[_Record] = deque(maxlen=self.max_records)

    # -- recording ------------------------------------------------------------------

    async def on_response(self, ctx: PluginContext) -> None:
        self._record(ctx)

    async def on_stream_end(self, ctx: PluginContext) -> None:
        self._record(ctx)

    async def on_error(self, ctx: PluginContext, error: GatewayError) -> None:
        self._record(ctx, error=error)

    def _record(self, ctx: PluginContext, error: GatewayError | None = None) -> None:
        state = ctx.state.setdefault(self.display_name(), {})
        if state.get("recorded"):
            return
        state["recorded"] = True
        identity = ctx.identity if isinstance(ctx.identity, dict) else {}
        failure = error or ctx.error
        deployment = ctx.deployment
        self._records.append(
            _Record(
                ts=time.time(),
                key=str(identity.get("name") or "anonymous"),
                model=ctx.model,
                provider=deployment.provider if deployment else "",
                status=int(ctx.status_code or (failure.status_code if failure else 0)),
                prompt_tokens=int(ctx.prompt_tokens or 0),
                completion_tokens=int(ctx.completion_tokens or 0),
                cost_usd=float(ctx.cost_usd or 0.0),
                cache_hit=bool(ctx.cache_hit),
                error_code=failure.code if failure else "",
            )
        )
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.retention_s
        records = self._records
        while records and records[0].ts < cutoff:
            records.popleft()

    # -- aggregation ------------------------------------------------------------------

    def usage_report(
        self,
        *,
        key: str | None = None,
        model: str | None = None,
        hours: int = 24,
        bucket: str = "hour",
    ) -> dict[str, Any]:
        """Aggregate records over the last ``hours`` (clamped 1..720), optionally
        filtered by key/model, grouped into hour/day buckets."""
        bucket_s = 3600 if bucket == "hour" else 86400
        since = time.time() - max(1, min(int(hours), _MAX_HOURS)) * 3600
        totals = _blank()
        by_key: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        by_bucket: dict[int, dict[str, Any]] = {}
        counted = 0
        for r in self._records:
            if r.ts < since:
                continue
            if key is not None and r.key != key:
                continue
            if model is not None and r.model != model:
                continue
            counted += 1
            _add(totals, r)
            _add(by_key.setdefault(r.key, _blank()), r)
            _add(by_model.setdefault(r.model, _blank()), r)
            _add(by_bucket.setdefault(int(r.ts // bucket_s * bucket_s), _blank()), r)
        return {
            "window": {
                "since_epoch": since,
                "bucket": bucket,
                "retention_hours": self.retention_s // 3600,
            },
            "recorded_records": len(self._records),
            "matched_requests": counted,
            "totals": totals,
            "by_key": dict(sorted(by_key.items())),
            "by_model": dict(sorted(by_model.items())),
            "buckets": [{"ts": ts, **vals} for ts, vals in sorted(by_bucket.items())],
        }


def _blank() -> dict[str, Any]:
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "cache_hits": 0,
        "errors": 0,
    }


def _add(acc: dict[str, Any], r: _Record) -> None:
    acc["requests"] += 1
    acc["prompt_tokens"] += r.prompt_tokens
    acc["completion_tokens"] += r.completion_tokens
    acc["cost_usd"] = round(acc["cost_usd"] + r.cost_usd, 6)
    acc["cache_hits"] += 1 if r.cache_hit else 0
    acc["errors"] += 1 if r.error_code else 0
