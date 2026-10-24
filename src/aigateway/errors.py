"""Central error types mapped to OpenAI-style API errors."""

from __future__ import annotations


class GatewayError(Exception):
    """Base error carrying an HTTP status and an OpenAI error code."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_openai_error(self) -> dict:
        return {
            "error": {
                "message": str(self),
                "type": self.code,
                "code": self.code,
            }
        }


class BadRequestError(GatewayError):
    status_code = 400
    code = "invalid_request_error"


class AuthenticationError(GatewayError):
    status_code = 401
    code = "authentication_error"


class PermissionDeniedError(GatewayError):
    status_code = 403
    code = "permission_denied"


class ContentFilterError(GatewayError):
    """Raised when a safety plugin (e.g. keyword filter) rejects the content."""

    status_code = 400
    code = "content_filter_rejected"


class RateLimitError(GatewayError):
    status_code = 429
    code = "rate_limit_exceeded"


class NotFoundError(GatewayError):
    status_code = 404
    code = "not_found_error"


class UpstreamError(GatewayError):
    """The upstream provider returned an unrecoverable error."""

    status_code = 502
    code = "upstream_error"


class AllDeploymentsFailedError(GatewayError):
    status_code = 503
    code = "no_healthy_deployment"


class PluginReject(GatewayError):
    """Raised by a plugin to abort the request with a specific error."""
