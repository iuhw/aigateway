"""Prompt template plugin — blueprint M5 (`prompt-template-plugin`).

Injects a gateway-owned system prompt per model / virtual-key identity, with
variable substitution. This plugin is also the reference implementation of the
gateway's supported **request-rewrite extension point**: any plugin may mutate
``ctx.payload`` (e.g. ``messages``) inside ``on_request`` before routing.

Config::

    prompt-template-plugin:
      config:
        templates:
          - models: ["glm4:*"]          # optional fnmatch on requested model
            keys: ["team-a"]            # optional exact virtual-key names
            metadata: {tier: premium}   # optional identity-metadata subset match
            system: "You are the {product} assistant for {team}."
            mode: prepend               # prepend | append | replace
            variables: {product: ACME}  # static variables
            header_variables: [x-team]  # request headers exposed as variables

Variables, in increasing precedence: built-ins (``model``, ``key``), static
``variables``, identity ``metadata`` fields, request ``header_variables``.
Rendering is single-pass: unknown placeholders stay verbatim (so JSON/YAML
examples inside templates survive), and substituted values are never
re-scanned — a variable containing ``{other}`` is emitted literally.

Modes (applied in config order; every matching rule applies):
  - ``prepend`` (default): new system message inserted at position 0, ahead of
    any client system message (gateway prompt takes precedence).
  - ``append``: text appended to the first existing system message (a new one
    is created at position 0 when none exists).
  - ``replace``: client system messages are removed, gateway text wins.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from aigateway.plugins.base import Plugin, PluginContext, register_plugin

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_MODES = ("prepend", "append", "replace")


@dataclass
class TemplateRule:
    system: str
    mode: str = "prepend"
    models: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    header_variables: list[str] = field(default_factory=list)

    def matches(self, model: str, identity: dict[str, Any] | None) -> bool:
        if self.models and not any(fnmatch.fnmatchcase(model, pat) for pat in self.models):
            return False
        if self.keys or self.metadata:
            if not isinstance(identity, dict):
                return False
            if self.keys and str(identity.get("name") or "") not in self.keys:
                return False
            meta = identity.get("metadata")
            if self.metadata:
                if not isinstance(meta, dict):
                    return False
                if any(meta.get(k) != v for k, v in self.metadata.items()):
                    return False
        return True


def render(template: str, variables: dict[str, Any]) -> str:
    """Single-pass ``{name}`` substitution; unknown names stay verbatim."""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            value = variables[name]
            return "" if value is None else str(value)
        return match.group(0)

    return _PLACEHOLDER.sub(_sub, template)


def _header_variable_name(header: str) -> str:
    return header.strip().lower().replace("-", "_")


@register_plugin
class PromptTemplatePlugin(Plugin):
    """Injects system-prompt templates by model/identity (M5)."""

    name: ClassVar[str] = "prompt-template-plugin"
    default_order: ClassVar[int] = 40  # before guardrails: they scan the final prompt

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.rules: list[TemplateRule] = []
        for raw in self.config.get("templates") or []:
            if not isinstance(raw, dict) or not str(raw.get("system") or "").strip():
                raise ValueError("prompt-template-plugin: each template needs a 'system' text")
            mode = str(raw.get("mode", "prepend"))
            if mode not in _MODES:
                raise ValueError(f"prompt-template-plugin: unknown mode {mode!r}")
            self.rules.append(
                TemplateRule(
                    system=str(raw["system"]),
                    mode=mode,
                    models=[str(m) for m in raw.get("models") or []],
                    keys=[str(k) for k in raw.get("keys") or []],
                    metadata=dict(raw.get("metadata") or {}),
                    variables=dict(raw.get("variables") or {}),
                    header_variables=[str(h) for h in raw.get("header_variables") or []],
                )
            )

    async def on_request(self, ctx: PluginContext) -> None:
        if not self.rules:
            return
        messages = ctx.payload.get("messages")
        if not isinstance(messages, list):
            return
        identity = ctx.identity if isinstance(ctx.identity, dict) else None
        for rule in self.rules:
            if not rule.matches(ctx.model, identity):
                continue
            text = render(rule.system, self._variables_for(rule, ctx, identity))
            messages = _apply(messages, text, rule.mode)
            ctx.payload["messages"] = messages

    def _variables_for(
        self, rule: TemplateRule, ctx: PluginContext, identity: dict[str, Any] | None
    ) -> dict[str, Any]:
        # Precedence: built-ins < static variables < identity metadata < headers.
        variables: dict[str, Any] = {"model": ctx.model, "key": "anonymous"}
        if isinstance(identity, dict):
            variables["key"] = str(identity.get("name") or "anonymous")
        variables.update(rule.variables)
        if isinstance(identity, dict) and isinstance(identity.get("metadata"), dict):
            variables.update(identity["metadata"])
        if rule.header_variables:
            lower = {k.lower(): v for k, v in (ctx.headers or {}).items()}
            for header in rule.header_variables:
                value = lower.get(header.lower())
                if value is not None:
                    variables[_header_variable_name(header)] = value
        return variables


def _apply(messages: list[Any], text: str, mode: str) -> list[Any]:
    if mode == "replace":
        kept = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
        return [{"role": "system", "content": text}, *kept]
    if mode == "append":
        out = list(messages)
        for i, message in enumerate(out):
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content = message.get("content")
            if isinstance(content, str):
                out[i] = {**message, "content": f"{content}\n\n{text}" if content else text}
            elif isinstance(content, list):
                out[i] = {**message, "content": [*content, {"type": "text", "text": text}]}
            else:  # unknown content shape: do not corrupt it, append as new part
                out.append({"role": "system", "content": text})
            return out
        return [{"role": "system", "content": text}, *out]
    # prepend (default)
    return [{"role": "system", "content": text}, *messages]
