"""HTTP response handling and error mapping."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from imednet.errors import DiagnosticContext, get_error_class
from imednet.utils.secrets import redact_sensitive_payload
from imednet.utils.url import redact_sensitive_text, redact_url_query


def parse_retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header from an HTTP response into non-negative seconds."""
    value = response.headers.get("Retry-After")
    if not value:
        return None

    try:
        delay = float(value)
        return max(delay, 0.0)
    except ValueError:
        pass

    try:
        retry_time = parsedate_to_datetime(value)
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=UTC)
        delay = (retry_time - datetime.now(UTC)).total_seconds()
        return max(delay, 0.0)
    except (TypeError, ValueError, OverflowError):
        return None


def handle_response(
    response: httpx.Response,
    *,
    method: str | None = None,
    url: str | None = None,
    attempt_count: int | None = None,
) -> httpx.Response:
    """Return the response or raise an appropriate ``ApiError`` with ``DiagnosticContext``."""
    if response.is_error:
        status = response.status_code
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = response.text

        try:
            req: httpx.Request | None = response.request
        except RuntimeError:
            req = None

        resolved_method = req.method if req is not None else method
        raw_url = str(req.url) if req is not None else url
        safe_url = (
            str(redact_sensitive_payload(redact_sensitive_text(redact_url_query(raw_url))))
            if raw_url is not None
            else None
        )
        retry_after = parse_retry_after_seconds(response)
        diagnostic_context = DiagnosticContext(
            method=resolved_method,
            url=safe_url,
            retry_after=retry_after,
            attempt_count=attempt_count,
        )

        exc_cls = get_error_class(status, body)
        raise exc_cls(body, status_code=status, diagnostic_context=diagnostic_context)
    return response
