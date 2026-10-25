"""API key authentication plugin (LiteLLM-style virtual keys) with budgets.

Validates `Authorization: Bearer <key>` against configured virtual keys.
Keys may also carry **budgets** (``daily_usd`` / ``monthly_usd``): once a
key's recorded spend in a UTC window reaches its budget, further requests
are rejected with 429 ``budget_exceeded``. Spend is recorded per completed
request (from the cost computed by the app), so requests already in flight
when the budget runs out may overshoot slightly — the standard
check-then-record trade-off shared with LiteLLM's budget feature.

The ledger lives in the shared-state backend when one is configured (Redis:
all instances see and add to the same window) and in-process otherwise
(memory ledgers reset on restart/reload).
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any, ClassVar

from aigateway.errors import AuthenticationError, PermissionDeniedError, RateLimitError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin
from aigateway.state import InMemoryBackend, StateBackend, StateBackendError

#: Window TTLs: enough slack for late reconciliation, self-cleaning afterwards.
_DAY_TTL_S = 2 * 24 * 3600
_MONTH_TTL_S = 40 * 24 * 3600


def _key_hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _window_keys(name: str, now: datetime) -> list[tuple[str, str, int, float]]:
    """(window_key, label, ttl_s, budget_field) for the active UTC windows."""
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    return [
        (f"{name}:{day}", "daily", _DAY_TTL_S, "daily_usd"),
        (f"{name}:{month}", "monthly", _MONTH_TTL_S, "monthly_usd"),
    ]


@register_plugin
class AuthPlugin(Plugin):
    """Validates `Authorization: Bearer <key>` against configured virtual keys.

    Config keys (under ``api_keys``): a list of
    ``{"key": "...", "name": "...", "models": ["model-a"], "rpm": 60,
    "budgets": {"daily_usd": 5, "monthly_usd": 100}}``. Keys may be given in
    plain text or as ``sha256:<hex>``. The resolved identity dict is stored on
    the context for downstream plugins (rate limiting, usage accounting).
    """

    default_order: ClassVar[int] = 10

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.header: str = str(self.config.get("header", "Authorization"))
        self.allow_anonymous: bool = bool(self.config.get("allow_anonymous", False))
        self.fail_open: bool = bool(self.config.get("budget_fail_open", True))
        self._by_hash: dict[str, dict[str, Any]] = {}
        for entry in self.config.get("api_keys", []):
            record = dict(entry)
            raw = str(record.get("key", ""))
            if raw.startswith("sha256:"):
                digest = raw[7:].lower()
            else:
                digest = _key_hash(raw)
            record["_hash"] = digest
            budgets = record.get("budgets") or {}
            if isinstance(budgets, dict) and budgets:
                record["budgets"] = {
                    str(k): float(v) for k, v in budgets.items() if v is not None
                }
            else:
                record["budgets"] = None
            self._by_hash[digest] = record
        self._backend: StateBackend = InMemoryBackend()
        self._metrics = None

    def set_shared(self, backend: StateBackend, metrics: Any) -> None:
        """Called by GatewayApp to inject the shared ledger backend and metrics."""
        self._backend = backend
        self._metrics = metrics

    def authenticate(self, headers: dict[str, str]) -> dict[str, Any]:
        raw = None
        wanted = self.header.lower()
        for name, value in headers.items():
            if name.lower() == wanted:
                raw = value.strip()
                break
        if raw and raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            if self.allow_anonymous:
                return {"name": "anonymous", "models": None, "rpm": None, "budgets": None}
            raise AuthenticationError("Missing API key")
        digest = _key_hash(raw)
        record = self._by_hash.get(digest)
        if record is None or not hmac.compare_digest(digest, record["_hash"]):
            raise AuthenticationError("Invalid API key")
        return {
            "name": record.get("name", "key"),
            "models": record.get("models"),
            "rpm": record.get("rpm"),
            "budgets": record.get("budgets"),
        }

    async def _check_budgets(self, identity: dict[str, Any]) -> None:
        budgets = identity.get("budgets") or {}
        if not budgets:
            return
        name = str(identity.get("name") or "key")
        now = datetime.now(UTC)
        for window_key, label, _ttl, field in _window_keys(name, now):
            budget = budgets.get(field)
            if budget is None:
                continue
            try:
                spent = await self._backend.get_spend(window_key)
            except StateBackendError as exc:
                if self.fail_open:
                    self._record_backend_error()
                    return  # availability over strict budgets
                raise RateLimitError(
                    "budget ledger unavailable", code="budget_exceeded"
                ) from exc
            if spent >= budget:
                raise RateLimitError(
                    f"{label} budget of ${budget:.2f} exhausted for key {name!r} "
                    f"(spent ${spent:.2f})",
                    code="budget_exceeded",
                )

    async def on_request(self, ctx: PluginContext) -> None:
        identity = self.authenticate(ctx.headers)
        allowed = identity.get("models")
        if allowed and ctx.model not in allowed:
            raise PermissionDeniedError(
                f"key {identity.get('name')!r} is not allowed to access model {ctx.model!r}",
                code="model_access_denied",
            )
        await self._check_budgets(identity)
        ctx.identity = identity

    async def on_response(self, ctx: PluginContext) -> None:
        await self._bill_budget(ctx)

    async def on_stream_end(self, ctx: PluginContext) -> None:
        await self._bill_budget(ctx)

    async def _bill_budget(self, ctx: PluginContext) -> None:
        """Record this request's cost against the key's active windows."""
        identity = ctx.identity or {}
        if not identity.get("budgets") or not ctx.cost_usd:
            return
        name = str(identity.get("name") or "key")
        now = datetime.now(UTC)
        for window_key, _label, ttl, _field in _window_keys(name, now):
            try:
                await self._backend.incr_spend(window_key, float(ctx.cost_usd), ttl)
            except StateBackendError:
                # Billing must never fail the response after it was produced.
                self._record_backend_error()

    def _record_backend_error(self) -> None:
        if self._metrics is not None:
            self._metrics.inc_state_backend_error("budget")
