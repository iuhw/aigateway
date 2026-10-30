"""Built-in plugins, imported for side-effect registration."""

from __future__ import annotations

from aigateway.plugins.auth import AuthPlugin
from aigateway.plugins.keyword_filter import KeywordFilterPlugin
from aigateway.plugins.pii_redaction import PiiRedactionPlugin
from aigateway.plugins.prompt_guard import PromptGuardPlugin
from aigateway.plugins.rate_limit import RateLimitPlugin
from aigateway.plugins.request_log import RequestLogPlugin
from aigateway.plugins.response_cache import ResponseCachePlugin
from aigateway.plugins.semantic_cache import SemanticCachePlugin
from aigateway.plugins.webhook_review import WebhookReviewPlugin

__all__ = [
    "AuthPlugin",
    "KeywordFilterPlugin",
    "PiiRedactionPlugin",
    "PromptGuardPlugin",
    "RateLimitPlugin",
    "RequestLogPlugin",
    "ResponseCachePlugin",
    "SemanticCachePlugin",
    "WebhookReviewPlugin",
]
