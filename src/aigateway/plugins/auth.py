"""API key authentication plugin (LiteLLM-style virtual keys) with budgets,
token quotas and key lifecycle management.

Validates `Authorization: Bearer <key>` against configured virtual keys.
Keys may carry:

- **budgets** (``daily_usd`` / ``monthly_usd``): once a key's recorded spend
  in a UTC window reaches its budget, further requests are rejected with 429
  ``budget_exceeded``. Spend is recorded per completed request (from the cost
  computed by the app), so requests already in flight when the budget runs
  out may overshoot slightly — the standard check-then-record trade-off.
- **token quotas** (``token_quota: {daily: N, monthly: M}``): actual prompt +
  completion tokens are accumulated per UTC window and checked before each
  request (429 ``token_quota_exceeded``). Cache hits are not billed (they
  consume no upstream tokens). Same check-then-bill semantics as budgets.
- **lifecycle** (``expires_at`` ISO-8601): expired keys get 403
  ``key_expired``. ``concurrency`` caps in-flight requests per key per
  process (enforced by the app, see GatewayApp._acquire_concurrency).
  ``metadata`` is an opaque dict echoed in admin views.

Keys are defined statically in the plugin config; the admin API can
additionally create **dynamic keys** (``POST /admin/keys``), stored in the
shared-state backend and overlaid on config keys at auth time. Dynamic keys
are refreshed with a short TTL cache (``dynamic_key_cache_s``, default 3s).
With the memory backend they do not survive restarts; with Redis they are
shared by every replica and persist.

The ledger lives in the shared-state backend when one is configured (Redis:
all instances see and add to the same window) and in-process otherwise
(memory ledgers reset on restart/reload).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from aigateway.errors import (
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UpstreamError,
)
from aigateway.plugins.base import Plugin, PluginContext, register_plugin
from aigateway.state import InMemoryBackend, StateBackend, StateBackendError

#: Window TTLs: enough slack for late reconciliation, self-cleaning afterwards.
_DAY_TTL_S = 2 * 24 * 3600
_MONTH_TTL_S = 40 * 24 * 3600

#: Dynamic key names must be safe to embed in window ledger keys.
_KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


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


def _token_window_keys(name: str, now: datetime) -> list[tuple[str, str, int]]:
    """(window_key, label, ttl_s) for token-quota ledgers (separate namespace
    from the USD budget windows so counters never collide)."""
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    return [
        (f"tokquota:{name}:{day}", "daily", _DAY_TTL_S),
        (f"tokquota:{name}:{month}", "monthly", _MONTH_TTL_S),
    ]


def _parse_expires_at(value: Any) -> datetime | None:
    if value in (None, "", "never"):
        return None
    if isinstance(value, datetime):
        expires = value
    else:
        try:
            expires = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise BadRequestError(f"invalid expires_at {value!r}: expected an ISO-8601 timestamp") from exc
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires


def _normalize_budgets(value: Any) -> dict[str, float] | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise BadRequestError("budgets must be a mapping of daily_usd/monthly_usd")
    budgets: dict[str, float] = {}
    for field in ("daily_usd", "monthly_usd"):
        amount = value.get(field)
        if amount is None:
            continue
        try:
            budgets[field] = float(amount)
        except (TypeError, ValueError) as exc:
            raise BadRequestError(f"budgets.{field} must be a number, got {amount!r}") from exc
    return budgets or None


def _normalize_token_quota(value: Any) -> dict[str, float] | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise BadRequestError("token_quota must be a mapping of daily/monthly")
    quota: dict[str, float] = {}
    for field in ("daily", "monthly"):
        amount = value.get(field)
        if amount is None:
            continue
        try:
            quota[field] = float(amount)
        except (TypeError, ValueError) as exc:
            raise BadRequestError(f"token_quota.{field} must be a number, got {amount!r}") from exc
    return quota or None


def _to_int_or_400(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequestError(f"{field} must be an integer, got {value!r}") from exc


@register_plugin
class AuthPlugin(Plugin):
    """Validates `Authorization: Bearer <key>` against configured virtual keys.

    Config keys (under ``api_keys``): a list of
    ``{"key": "...", "name": "...", "models": ["model-a"], "rpm": 60,
    "budgets": {"daily_usd": 5, "monthly_usd": 100},
    "token_quota": {"daily": 1000000, "monthly": 20000000},
    "expires_at": "2026-12-31T23:59:59+00:00", "concurrency": 8,
    "metadata": {"team": "ops"}}``. Keys may be given in plain text or as
    ``sha256:<hex>``. The resolved identity dict is stored on the context for
    downstream plugins (rate limiting, usage accounting).

    Other options: ``allow_anonymous``, ``budget_fail_open`` (ledger read
    failures let requests through; default true — applies to budgets and
    token quotas), ``dynamic_key_cache_s`` (overlay cache TTL, default 3).
    """

    default_order: ClassVar[int] = 10

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.header: str = str(self.config.get("header", "Authorization"))
        self.allow_anonymous: bool = bool(self.config.get("allow_anonymous", False))
        self.fail_open: bool = bool(self.config.get("budget_fail_open", True))
        self.dynamic_cache_s: float = float(self.config.get("dynamic_key_cache_s", 3.0))
        self._by_hash: dict[str, dict[str, Any]] = {}
        self._config_names: set[str] = set()
        for entry in self.config.get("api_keys", []):
            record = self._normalize_static(entry)
            self._config_names.add(str(record.get("name") or "key"))
            self._by_hash[record["_hash"]] = record
        self._backend: StateBackend = InMemoryBackend()
        self._metrics = None
        self._dyn_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

    # -- key record normalization -------------------------------------------------

    def _normalize_static(self, entry: dict[str, Any]) -> dict[str, Any]:
        record = dict(entry)
        raw = str(record.get("key", ""))
        if raw.startswith("sha256:"):
            digest = raw[7:].lower()
        else:
            digest = _key_hash(raw)
        record["_hash"] = digest
        record["budgets"] = _normalize_budgets(record.get("budgets"))
        record["token_quota"] = _normalize_token_quota(record.get("token_quota"))
        record["_expires"] = _parse_expires_at(record.get("expires_at"))
        if record.get("concurrency") is not None:
            record["concurrency"] = _to_int_or_400(record["concurrency"], "concurrency")
        if record.get("metadata") is not None and not isinstance(record["metadata"], dict):
            raise BadRequestError("key metadata must be a mapping")
        return record

    def _normalize_dynamic(self, record: dict[str, Any]) -> dict[str, Any]:
        """Re-validate a stored dynamic record on load (defensive)."""
        record = dict(record)
        digest = str(record.get("key_hash", ""))
        if not digest:
            raise ValueError(f"dynamic key {record.get('name')!r} missing key_hash")
        record["_hash"] = digest
        record["budgets"] = _normalize_budgets(record.get("budgets"))
        record["token_quota"] = _normalize_token_quota(record.get("token_quota"))
        record["_expires"] = _parse_expires_at(record.get("expires_at"))
        return record

    def _identity_from(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": record.get("name", "key"),
            "models": record.get("models"),
            "rpm": record.get("rpm"),
            "budgets": record.get("budgets"),
            "token_quota": record.get("token_quota"),
            "expires_at": record.get("expires_at"),
            "concurrency": record.get("concurrency"),
            "metadata": record.get("metadata"),
        }

    def set_shared(self, backend: StateBackend, metrics: Any) -> None:
        """Called by GatewayApp to inject the shared ledger backend and metrics."""
        self._backend = backend
        self._metrics = metrics

    # -- authentication ---------------------------------------------------------------

    def _bearer(self, headers: dict[str, str]) -> str | None:
        wanted = self.header.lower()
        for name, value in headers.items():
            if name.lower() == wanted:
                raw = value.strip()
                if raw.lower().startswith("bearer "):
                    raw = raw[7:].strip()
                return raw or None
        return None

    def authenticate(self, headers: dict[str, str]) -> dict[str, Any]:
        """Authenticate against static config keys (sync; dynamic overlay is
        resolved asynchronously in on_request when this fails)."""
        raw = self._bearer(headers)
        if not raw:
            if self.allow_anonymous:
                return self._identity_from({"name": "anonymous"})
            raise AuthenticationError("Missing API key")
        digest = _key_hash(raw)
        record = self._by_hash.get(digest)
        if record is None or not hmac.compare_digest(digest, record["_hash"]):
            raise AuthenticationError("Invalid API key")
        return self._identity_from(record)

    async def _authenticate_dynamic(self, headers: dict[str, str]) -> dict[str, Any]:
        raw = self._bearer(headers)
        if not raw:
            raise AuthenticationError("Missing API key")
        digest = _key_hash(raw)
        record = (await self._dynamic_by_digest()).get(digest)
        if record is None:
            raise AuthenticationError("Invalid API key")
        return self._identity_from(record)

    async def _dynamic_by_digest(self) -> dict[str, dict[str, Any]]:
        """Dynamic key overlay, cached for ``dynamic_key_cache_s``.

        Backend failures are always fail-open (config keys keep working);
        every failure increments the state-backend error metric.
        """
        now = time.monotonic()
        if self._dyn_cache is not None and now - self._dyn_cache[0] < self.dynamic_cache_s:
            return self._dyn_cache[1]
        try:
            stored = await self._backend.get_dynamic_keys()
        except StateBackendError:
            self._record_backend_error()
            return self._dyn_cache[1] if self._dyn_cache is not None else {}
        by_digest: dict[str, dict[str, Any]] = {}
        for _name, record in stored.items():
            try:
                normalized = self._normalize_dynamic(record)
            except Exception:  # noqa: BLE001 - one bad record must not break auth
                continue
            by_digest[normalized["_hash"]] = normalized
        self._dyn_cache = (now, by_digest)
        return by_digest

    # -- enforcement ----------------------------------------------------------------

    async def on_request(self, ctx: PluginContext) -> None:
        try:
            identity = self.authenticate(ctx.headers)
        except AuthenticationError:
            identity = await self._authenticate_dynamic(ctx.headers)
        if identity.get("expires_at") is not None:
            expires = _parse_expires_at(identity["expires_at"])
            if expires is not None and datetime.now(UTC) >= expires:
                raise PermissionDeniedError(
                    f"key {identity.get('name')!r} expired at {expires.isoformat()}",
                    code="key_expired",
                )
        allowed = identity.get("models")
        if allowed and ctx.model not in allowed:
            raise PermissionDeniedError(
                f"key {identity.get('name')!r} is not allowed to access model {ctx.model!r}",
                code="model_access_denied",
            )
        await self._check_budgets(identity)
        await self._check_token_quota(identity)
        ctx.identity = identity

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

    async def _check_token_quota(self, identity: dict[str, Any]) -> None:
        quota = identity.get("token_quota") or {}
        if not quota:
            return
        name = str(identity.get("name") or "key")
        now = datetime.now(UTC)
        for window_key, label, _ttl in _token_window_keys(name, now):
            limit = quota.get(label)
            if limit is None:
                continue
            try:
                used = await self._backend.get_spend(window_key)
            except StateBackendError as exc:
                if self.fail_open:
                    self._record_backend_error()
                    return
                raise RateLimitError(
                    "token quota ledger unavailable", code="token_quota_exceeded"
                ) from exc
            if used >= limit:
                raise RateLimitError(
                    f"{label} token quota of {limit:.0f} exhausted for key {name!r} "
                    f"(used {used:.0f})",
                    code="token_quota_exceeded",
                )

    async def on_response(self, ctx: PluginContext) -> None:
        await self._bill_usage(ctx)

    async def on_stream_end(self, ctx: PluginContext) -> None:
        await self._bill_usage(ctx)

    async def _bill_usage(self, ctx: PluginContext) -> None:
        """Record this request's cost and token usage against the key's windows."""
        identity = ctx.identity or {}
        name = str(identity.get("name") or "key")
        now = datetime.now(UTC)
        # USD budgets: only billed when the request produced billable cost.
        if identity.get("budgets") and ctx.cost_usd:
            for window_key, _label, ttl, _field in _window_keys(name, now):
                await self._incr(window_key, float(ctx.cost_usd), ttl)
        # Token quotas: actual tokens, cache hits excluded (no upstream usage).
        total_tokens = (ctx.prompt_tokens or 0) + (ctx.completion_tokens or 0)
        if identity.get("token_quota") and total_tokens > 0 and not ctx.cache_hit:
            for window_key, _label, ttl in _token_window_keys(name, now):
                await self._incr(window_key, float(total_tokens), ttl)

    async def _incr(self, window_key: str, amount: float, ttl: int) -> None:
        try:
            await self._backend.incr_spend(window_key, amount, ttl)
        except StateBackendError:
            # Billing must never fail the response after it was produced.
            self._record_backend_error()

    def _record_backend_error(self) -> None:
        if self._metrics is not None:
            self._metrics.inc_state_backend_error("budget")

    # -- admin: dynamic keys ------------------------------------------------------------

    async def create_dynamic_key(self, spec: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Create a dynamic virtual key; returns (public_record, raw_key).

        The raw key is returned exactly once — only its SHA-256 hash is stored.
        """
        name = str(spec.get("name") or "").strip()
        if not _KEY_NAME_RE.match(name):
            raise BadRequestError(
                "dynamic key 'name' must match [A-Za-z0-9_.-]{1,64}"
            )
        if name in self._config_names:
            raise BadRequestError(f"key name {name!r} is already defined in the static config")
        stored = await self._backend.get_dynamic_keys()
        if name in stored:
            raise BadRequestError(f"dynamic key {name!r} already exists")
        raw = str(spec.get("key") or "")
        if not raw:
            raw = f"sk-dyn-{uuid.uuid4().hex}"
        digest = _key_hash(raw)
        if digest in self._by_hash:
            raise BadRequestError("this API key already exists in the static config")
        record: dict[str, Any] = {
            "name": name,
            "key_hash": digest,
            "models": spec.get("models"),
            "rpm": _to_int_or_400(spec.get("rpm"), "rpm"),
            "budgets": _normalize_budgets(spec.get("budgets")),
            "token_quota": _normalize_token_quota(spec.get("token_quota")),
            "expires_at": None,
            "concurrency": _to_int_or_400(spec.get("concurrency"), "concurrency"),
            "metadata": spec.get("metadata"),
            "source": "dynamic",
            "created_at": datetime.now(UTC).isoformat(),
        }
        if record["models"] is not None and not (
            isinstance(record["models"], list) and all(isinstance(m, str) for m in record["models"])
        ):
            raise BadRequestError("models must be a list of model names")
        if record["metadata"] is not None and not isinstance(record["metadata"], dict):
            raise BadRequestError("metadata must be a mapping")
        expires = _parse_expires_at(spec.get("expires_at"))
        if expires is not None:
            record["expires_at"] = expires.isoformat()
        await self._backend.set_dynamic_key(name, record)
        self._dyn_cache = None
        return self._public_view(self._normalize_dynamic(record)), raw

    async def delete_dynamic_key(self, name: str) -> None:
        if name in self._config_names:
            raise BadRequestError(
                f"key {name!r} is defined in the static config; remove it from the YAML instead",
                code="key_not_deletable",
                status_code=409,
            )
        stored = await self._backend.get_dynamic_keys()
        if name not in stored:
            raise NotFoundError(f"no dynamic key named {name!r}")
        await self._backend.delete_dynamic_key(name)
        self._dyn_cache = None

    # -- admin: views -----------------------------------------------------------------

    def _public_view(self, record: dict[str, Any]) -> dict[str, Any]:
        """Key record safe for admin API responses (never includes key material)."""
        view = {
            "name": record.get("name"),
            "source": record.get("source", "config"),
            "models": record.get("models"),
            "rpm": record.get("rpm"),
            "budgets": record.get("budgets"),
            "token_quota": record.get("token_quota"),
            "expires_at": record.get("expires_at"),
            "concurrency": record.get("concurrency"),
            "metadata": record.get("metadata"),
            "key_hash": f"{record['_hash'][:12]}…",
        }
        if record.get("created_at"):
            view["created_at"] = record["created_at"]
        return view

    async def list_keys(self) -> list[dict[str, Any]]:
        """All known keys (static config first, then dynamic overlay)."""
        views = [self._public_view(record) for record in self._by_hash.values()]
        try:
            stored = await self._backend.get_dynamic_keys()
        except StateBackendError:
            self._record_backend_error()
            return views
        views.extend(
            self._public_view(self._normalize_dynamic(record)) for record in stored.values()
        )
        return views

    async def usage_snapshot(self, name_filter: str | None = None) -> dict[str, Any]:
        """Per-key current-window usage from the shared ledger.

        Only windows the key actually limits (budgets / token_quota) are
        reported; keys without limits report ``limited: false``.
        """
        try:
            stored = await self._backend.get_dynamic_keys()
        except StateBackendError as exc:
            raise UpstreamError(f"shared state ledger unavailable: {exc}") from exc
        now = datetime.now(UTC)
        records = list(self._by_hash.values()) + [
            self._normalize_dynamic(record) for record in stored.values()
        ]
        keys_out: list[dict[str, Any]] = []
        for record in records:
            name = str(record.get("name") or "key")
            if name_filter is not None and name != name_filter:
                continue
            entry: dict[str, Any] = {
                "name": name,
                "source": record.get("source", "config"),
                "expired": bool(
                    record.get("_expires") is not None and now >= record["_expires"]
                ),
                "limited": bool(record.get("budgets") or record.get("token_quota")),
            }
            budgets = record.get("budgets") or {}
            if budgets:
                entry["budgets"] = {}
                for window_key, label, _ttl, field in _window_keys(name, now):
                    if field in budgets:
                        entry["budgets"][label] = {
                            "limit_usd": budgets[field],
                            "spent_usd": await self._backend.get_spend(window_key),
                        }
            quota = record.get("token_quota") or {}
            if quota:
                entry["token_quota"] = {}
                for window_key, label, _ttl in _token_window_keys(name, now):
                    if label in quota:
                        entry["token_quota"][label] = {
                            "limit_tokens": quota[label],
                            "used_tokens": await self._backend.get_spend(window_key),
                        }
            keys_out.append(entry)
        return {"keys": keys_out, "generated_at": now.isoformat()}
