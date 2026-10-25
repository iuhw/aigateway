"""AWS Signature Version 4 request signing (pure stdlib, no boto dependency).

Implements the algorithm from the AWS "Signature Version 4 signing process"
reference: canonical request -> string-to-sign -> derived signing key -> HMAC
signature. Scoped to what the Bedrock runtime needs (HTTPS POST, no query
string, optional session token).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def amz_date_now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def sigv4_headers(
    *,
    method: str,
    url: str,
    body: bytes,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str,
    session_token: str | None = None,
    amz_date: str | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, str]:
    """Signed headers for one request.

    `url` must carry scheme and host (query strings are not signed — Bedrock
    paths have none). `now`/`amz_date` are injectable for deterministic tests.
    """
    parsed = url.split("://", 1)
    rest = parsed[1] if len(parsed) == 2 else url
    host, _, path = rest.partition("/")
    path = "/" + path if path else "/"

    moment = now or _dt.datetime.now(_dt.UTC)
    timestamp = amz_date or moment.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = timestamp[:8]

    headers: dict[str, str] = {"host": host, "x-amz-date": timestamp}
    if session_token:
        headers["x-amz-security-token"] = session_token

    signed_headers = ";".join(headers)
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in headers)
    canonical_request = "\n".join(
        [method.upper(), path, "", canonical_headers, signed_headers, _sha256_hex(body)]
    )

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", timestamp, scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )

    key_date = _hmac_sha256(f"AWS4{secret_access_key}".encode(), date_stamp)
    key_region = _hmac_sha256(key_date, region)
    key_service = _hmac_sha256(key_region, service)
    key_signing = _hmac_sha256(key_service, "aws4_request")
    signature = hmac.new(key_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    result = {"x-amz-date": timestamp, "Authorization": authorization}
    if session_token:
        result["x-amz-security-token"] = session_token
    return result
