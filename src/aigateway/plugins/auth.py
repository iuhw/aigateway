"""API key authentication plugin (LiteLLM-style virtual keys)."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, ClassVar

from aigateway.errors import AuthenticationError, PermissionDeniedError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin


def _key_hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@register_plugin
class AuthPlugin(Plugin):
    """Validates `Authorization: Bearer <key>` against configured virtual keys.

    Config keys (under ``api_keys``): a list of
    ``{"key": "...", "name": "...", "models": ["model-a"], "rpm": 60}``.
    Keys may be given in plain text or as ``sha256:<hex>``. The resolved identity
    dict is stored on the context for downstream plugins (rate limiting, usage).
    """

    default_order: ClassVar[int] = 10

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.header: str = str(self.config.get("header", "Authorization"))
        self.allow_anonymous: bool = bool(self.config.get("allow_anonymous", False))
        self._by_hash: dict[str, dict[str, Any]] = {}
        for entry in self.config.get("api_keys", []):
            record = dict(entry)
            raw = str(record.get("key", ""))
            if raw.startswith("sha256:"):
                digest = raw[7:].lower()
            else:
                digest = _key_hash(raw)
            record["_hash"] = digest
            self._by_hash[digest] = record

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
                return {"name": "anonymous", "models": None, "rpm": None}
            raise AuthenticationError("Missing API key")
        digest = _key_hash(raw)
        record = self._by_hash.get(digest)
        if record is None or not hmac.compare_digest(digest, record["_hash"]):
            raise AuthenticationError("Invalid API key")
        return {
            "name": record.get("name", "key"),
            "models": record.get("models"),
            "rpm": record.get("rpm"),
        }

    async def on_request(self, ctx: PluginContext) -> None:
        identity = self.authenticate(ctx.headers)
        allowed = identity.get("models")
        if allowed and ctx.model not in allowed:
            raise PermissionDeniedError(
                f"key {identity.get('name')!r} is not allowed to access model {ctx.model!r}",
                code="model_access_denied",
            )
        ctx.identity = identity
