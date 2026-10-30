"""Heuristic prompt-injection guard plugin — blueprint M3 (`prompt-guard-plugin`).

Scores the request text against configurable rule groups (built-in bilingual
EN/CN patterns + custom regexes). Every matching pattern adds its group's
weight; when the total reaches ``threshold`` the request counts as an
injection attempt. Two actions:

  - ``flag`` (default): log, count ``gateway_guardrail_flags_total{verdict="flagged"}``,
    annotate ``ctx.state`` — the request proceeds. Observe-first default so the
    false-positive rate can be measured on real traffic before switching to
    ``block`` (``gateway_plugin_rejections_total`` covers the block verdict).
  - ``block``: reject with HTTP 400 ``prompt_injection_detected``.

Score calibration keeps false positives conservative: the unambiguous groups
(``override``, ``delimiters``) weigh 1.0 — a single hit trips the default
threshold — while the context-dependent groups (``role_override``, ``evasion``)
weigh 0.5 and need corroboration. Layering with an external review service is
the webhook-review-plugin's job: chain both plugins, prompt-guard as the cheap
local first line.

Rule groups (built-in, CN-centric where it matters):
  - ``override``:      "ignore previous instructions" / 忽略(之前|系统)指令 …
  - ``role_override``: "you are now a", "do anything now", 进入(开发者|越狱)模式 …
  - ``delimiters``:    fake chat-template/system markers — ``<|im_start|>``,
                       ``[system]``, ``### system``, ``SYSTEM:`` …
  - ``evasion``:       encode-output asks, "don't tell anyone", 别告诉任何人 …

Boundary: the ``response`` check applies to non-streaming responses only —
streamed responses are not scanned (a mid-stream block cannot recall delivered
chunks; use request-side guard + webhook review for stream safety).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from aigateway.errors import ContentFilterError
from aigateway.plugins.base import Plugin, PluginContext, register_plugin
from aigateway.schemas import request_text, response_text

logger = logging.getLogger("aigateway.plugins.prompt_guard")

# (group, pattern). Case sensitivity per pattern via scoped (?i:…) flags —
# literal template tokens like <|im_start|> stay case-sensitive while prose
# patterns fold case.
BUILTIN_RULES: list[tuple[str, str]] = [
    # ---- override: explicit instruction-override attempts --------------------
    ("override", r"(?i:\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
                 r"(?:instructions?|prompts?|rules?|messages?|directives?|directions?"
                 r"|constraints?|commands?|guidelines?))"),
    ("override", r"(?i:\bignore\s+(?:the\s+|your\s+)?system\s+(?:prompt|message|instructions?))"),
    ("override", r"(?i:\bdisregard\s+(?:the\s+|all\s+|your\s+)?(?:above|previous|prior"
                 r"|earlier|system|initial)\s+"
                 r"(?:instructions?|prompts?|rules?|messages?|directives?|constraints?))"),
    ("override", r"(?i:\b(?:forget|drop)\s+(?:all\s+|your\s+)?(?:previous|prior|above)\s+"
                 r"(?:instructions?|prompts?|rules?|commands?))"),
    ("override", r"(?i:\boverride\s+(?:your\s+)?(?:system\s+prompt|instructions?"
                 r"|safety\s+(?:guidelines?|rules?)))"),
    ("override", r"(?:忽略|无视|忘记|抛开)(?:之前|以上|上面|先前|前面|上述|系统)"
                 r"(?:里|中)?的?(?:所有|全部|任何)?(?:系统)?"
                 r"(?:指令|提示词?|设定|规则|约束|要求)"),
    ("override", r"(?:不要|别|无需)(?:再)?(?:遵守|遵循|理会|听|执行)(?:之前|以上|上面|先前|系统|任何)(?:的)?"
                 r"(?:指令|提示|设定|规则|约束)"),
    # ---- role_override: identity/role manipulation (context-dependent) -------
    ("role_override", r"(?i:\byou\s+are\s+now\s+(?:a|an|the)\b)"),
    ("role_override", r"(?i:\bfrom\s+now\s+on\b[^\n]{0,40}?\byou\s+(?:are|will\s+be|must|can|may|should)\b)"),
    ("role_override", r"(?i:\b(?:pretend|act)\s+(?:(?:that\s+)?you\s+(?:are|were)"
                      r"|to\s+be|as\s+if\s+you\s+(?:are|were))\b)"),
    ("role_override", r"(?i:\bact\s+as\s+(?:an?\s+|the\s+)?(?:dan\b|unrestricted\s+(?:ai|model)"
                      r"|uncensored|jailbreak))"),
    ("role_override", r"(?i:\bdo\s+anything\s+now\b)"),
    ("role_override", r"(?i:\bstay\s+in\s+character\b[^\n]{0,40}?\b(?:no\s+matter|even\s+if|regardless)\b)"),
    ("role_override", r"进入(?:开发者|上帝|无限|无限制|越狱|调试)模式"),
    ("role_override", r"你现在(?:是|变成|扮演)(?:一?个)?(?:无(?:限制|约束|禁忌)"
                      r"|不受(?:任何)?限制|越狱|邪恶|没有道德)"),
    ("role_override", r"你(?:将|要)?扮演(?:一?个)?(?:无(?:限制|禁忌)|不受限|邪恶|黑(?:客|帽))"),
    # ---- delimiters: fake chat-template / system markers ---------------------
    ("delimiters", r"<\|?(?:im_start|im_end|endoftext|system|user|assistant)\|?>"),
    ("delimiters", r"</?\s*(?i:system)\s*>"),
    ("delimiters", r"\[\s*(?i:system)\s*\]"),
    ("delimiters", r"【\s*(?:系统提示|系统指令|系统消息|system)\s*】"),
    ("delimiters", r"\bSYSTEM\s*:"),
    ("delimiters", r"(?im:^###\s*(?:system|instructions?)\s*$)"),
    # ---- evasion: output-encoding / secrecy asks -----------------------------
    ("evasion", r"(?i:\b(?:rot13|atbash|base64)\b)"),
    ("evasion", r"(?i:\b(?:encode|encrypt|write|respond|answer|reply|translate|convert)\b[^\n]{0,60}?"
                r"\b(?:in|into|with|using)\s+(?:base64|rot13|binary|hex(?:adecimal)?|morse|reversed?|pig\s+latin)\b)"),
    ("evasion", r"(?i:\bdo(?:\s+)?n[o']?t\s+(?:tell|reveal|mention|inform)\s+"
                r"(?:anyone|anybody|the\s+(?:user|admin|operator|developer)|him|her|them)\b)"),
    ("evasion", r"(?i:\bkeep\s+(?:this|it|our)\s+(?:conversation|secret|between\s+us)\b)"),
    ("evasion", r"(?:请|要)?(?:用|以|把.{0,10}转成)(?:base64|rot13|十六进制|二进制|摩斯|倒序)"
                r"(?:编码|加密|输出|回复|表示)"),
    ("evasion", r"(?:这件事|这些内容|我们的?对话|以上内容)(?:请)?(?:要)?"
                r"(?:保密|别(?:说|告诉|外传)|不要告诉(?:任何人|别人))"),
]

DEFAULT_WEIGHTS: dict[str, float] = {
    "override": 1.0,
    "delimiters": 1.0,
    "role_override": 0.5,
    "evasion": 0.5,
}


@dataclass
class GuardVerdict:
    """Result of scanning one text: total score plus per-pattern evidence.

    `detected` is decided by the plugin against its configured threshold;
    the verdict itself only carries the score and evidence."""

    score: float = 0.0
    matches: list[dict[str, Any]] = field(default_factory=list)


@register_plugin
class PromptGuardPlugin(Plugin):
    """Detects prompt-injection attempts in requests (and non-stream responses)."""

    name: ClassVar[str] = "prompt-guard-plugin"
    default_order: ClassVar[int] = 115  # after pii-redaction, before keyword filter

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        cfg = self.config
        self.action: str = cfg.get("action", "flag")
        if self.action not in ("flag", "block"):
            raise ValueError(f"prompt_guard: unknown action {self.action!r}")
        self.threshold: float = float(cfg.get("threshold", 1.0))
        self.block_message: str = str(
            cfg.get("block_message", "Prompt injection attempt detected and blocked.")
        )
        self.check_request: bool = "request" in cfg.get("check", ["request"])
        self.check_response: bool = "response" in cfg.get("check", ["request"])

        groups = list(cfg.get("groups", list(DEFAULT_WEIGHTS)))
        unknown = [g for g in groups if g not in DEFAULT_WEIGHTS]
        if unknown:
            raise ValueError(
                f"prompt_guard: unknown groups {unknown}; builtins: {sorted(DEFAULT_WEIGHTS)}"
            )
        weights_cfg = dict(cfg.get("weights") or {})
        weights = {g: float(weights_cfg.get(g, DEFAULT_WEIGHTS[g])) for g in groups}

        self._rules: list[tuple[str, float, re.Pattern[str]]] = [
            (group, weights[group], re.compile(pattern))
            for group, pattern in BUILTIN_RULES
            if group in weights
        ]
        for entry in cfg.get("custom_patterns", []):
            if isinstance(entry, dict):
                pattern, weight = str(entry["pattern"]), float(entry.get("weight", 1.0))
            else:
                pattern, weight = str(entry), 1.0
            self._rules.append(("custom", weight, re.compile(pattern)))
        self.enabled = bool(self._rules)
        self._metrics: Any = None

    def set_metrics(self, metrics: Any) -> None:
        """GatewayApp injection (existing set_shared convention)."""
        self._metrics = metrics

    # -- detection ----------------------------------------------------------------

    def scan(self, text: str) -> GuardVerdict:
        verdict = GuardVerdict()
        if not text:
            return verdict
        for group, weight, pattern in self._rules:
            match = pattern.search(text)
            if match:
                verdict.score += weight
                verdict.matches.append(
                    {"group": group, "weight": weight, "matched": match.group(0)[:80]}
                )
        return verdict

    # -- hooks ----------------------------------------------------------------------

    def _handle_verdict(self, ctx: PluginContext, verdict: GuardVerdict, stage: str) -> None:
        if verdict.score < self.threshold:
            logger.debug(
                "prompt_guard: %s below threshold on %s (score %.2f < %.2f)",
                stage, ctx.request_id, verdict.score, self.threshold,
            )
            return
        if self._metrics is not None:
            verdict_label = "flagged" if self.action == "flag" else "blocked"
            self._metrics.inc_guardrail_flag(self.display_name(), verdict_label)
        state = ctx.state.setdefault(
            self.display_name(), {"score": verdict.score, "groups": [], "matches": []}
        )
        state["score"] += verdict.score
        state["groups"].extend({m["group"] for m in verdict.matches})
        state["matches"].extend(verdict.matches)
        logger.warning(
            "prompt_guard: %s flagged on %s (score %.2f >= %.2f, groups=%s)",
            stage, ctx.request_id, verdict.score, self.threshold, state["groups"],
        )
        if self.action == "block":
            raise ContentFilterError(self.block_message, code="prompt_injection_detected")

    async def on_request(self, ctx: PluginContext) -> None:
        if self.check_request:
            self._handle_verdict(ctx, self.scan(request_text(ctx.payload)), "request")

    async def on_response(self, ctx: PluginContext) -> None:
        if self.check_response and ctx.response is not None:
            self._handle_verdict(ctx, self.scan(response_text(ctx.response)), "response")

    async def on_stream_start(self, ctx: PluginContext) -> None:
        if self.check_response:
            logger.debug("prompt_guard: response check skipped for streamed response %s", ctx.request_id)
