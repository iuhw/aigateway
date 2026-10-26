"""Built-in plugins, imported for side-effect registration."""

from __future__ import annotations

from aigateway.plugins.auth import AuthPlugin
from aigateway.plugins.keyword_filter import KeywordFilterPlugin
from aigateway.plugins.rate_limit import RateLimitPlugin
from aigateway.plugins.request_log import RequestLogPlugin
from aigateway.plugins.response_cache import ResponseCachePlugin
from aigateway.plugins.semantic_cache import SemanticCachePlugin

__all__ = [
    "AuthPlugin",
    "KeywordFilterPlugin",
    "RateLimitPlugin",
    "RequestLogPlugin",
    "ResponseCachePlugin",
    "SemanticCachePlugin",
]
