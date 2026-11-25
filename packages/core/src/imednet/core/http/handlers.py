"""HTTP response handling and error mapping."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from imednet.constants import DEFAULT_MAX_RETRY_AFTER
from imednet.errors import DiagnosticContext, get_error_class
from imednet.utils.url import redact_url_query


def _clamp_retry_after(delay: float, max_retry_after: float | None) -> float | None:
    """Return a non-negative finite delay optionally capped at ``max_retry_after``."""
    if not math.isfinite(delay):
        return None
    bounded = max(delay, 0.0)
    if max_retry_after is not None:
        bounded = min(bounded, float(max_retry_after))
    return bounded


def parse_retry_after_seconds(
    response: httpx.Response,
    max_retry_after: float | None = None,
) -> float | None:
    """Parse the ``Retry-After`` header from an HTTP response into finite non-negative seconds."""
    if max_retry_after is not None and (
        isinstance(max_retry_after, bool)
        or not isinstance(max_retry_after, (int, float))
        or not math.isfinite(max_retry_after)
        or max_retry_after < 0
    ):
        raise ValueError("max_retry_after must be a finite non-negative number")

    value = response.headers.get("Retry-After")
    if not value or not value.strip():
        return None

    try:
        delay = float(value)
    except ValueError:
        pass
    else:
        return _clamp_retry_after(delay, max_retry_after)

    try:
        retry_time = parsedate_to_datetime(value)
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=UTC)
        delay = (retry_time - datetime.now(UTC)).total_seconds()
        return _clamp_retry_after(delay, max_retry_after)
    except (TypeError, ValueError, OverflowError):
        return None


def handle_response(
    response: httpx.Response,
    *,
    method: str | None = None,
    url: str | None = None,
    attempt_count: int | None = None,
    max_retry_after: float | None = DEFAULT_MAX_RETRY_AFTER,
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
        safe_url = redact_url_query(raw_url) if raw_url is not None else None
        retry_after = parse_retry_after_seconds(response, max_retry_after=max_retry_after)
        diagnostic_context = DiagnosticContext(
            method=resolved_method,
            url=safe_url,
            retry_after=retry_after,
            attempt_count=attempt_count,
        )

        exc_cls = get_error_class(status, body)
        raise exc_cls(body, status_code=status, diagnostic_context=diagnostic_context)
    return response
