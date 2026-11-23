"""Request monitoring and tracing."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
from tenacity import RetryError

from imednet.core.operations.monitor_base import _BaseMonitor
from imednet.errors import DiagnosticContext, RequestError
from imednet.utils.secrets import redact_sensitive_payload
from imednet.utils.url import redact_sensitive_text, redact_url_query

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer  # pragma: no cover
else:
    Tracer = Any

logger = logging.getLogger(__name__)


class RequestMonitor(_BaseMonitor):
    """Helper to handle request monitoring (tracing, timing, logging)."""

    def __init__(self, tracer: Tracer | None, method: str, url: str) -> None:
        """Initialize the request monitor.

        Args:
            tracer: Optional OpenTelemetry tracer for tracing requests.
            method: HTTP method of the request.
            url: URL of the request (will be redacted for logging).
        """
        self.method = method
        self.safe_url = str(redact_sensitive_payload(redact_sensitive_text(redact_url_query(url))))
        super().__init__(
            tracer=tracer,
            span_name="http_request",
            attributes={"endpoint": self.safe_url, "method": self.method},
        )

    def on_success(self, response: httpx.Response) -> None:
        """Record a successful request completion.

        Args:
            response: The successful HTTPX response.
        """
        latency = time.monotonic() - self.start_time
        logger.info(
            "http_request",
            extra={
                "method": self.method,
                "url": self.safe_url,
                "status_code": response.status_code,
                "latency": latency,
            },
        )
        if self.span:
            self.span.set_attribute("status_code", response.status_code)

    def _resolve_error_context(
        self,
        cause: BaseException | None,
        attempt_count: int | None,
    ) -> DiagnosticContext:
        """Build a sanitized DiagnosticContext for a transport failure."""
        resolved_method = self.method
        resolved_url = self.safe_url
        if isinstance(cause, httpx.RequestError):
            try:
                req: httpx.Request | None = cause.request
            except RuntimeError:
                req = None
            if req is not None:
                resolved_method = req.method or resolved_method
                resolved_url = str(
                    redact_sensitive_payload(redact_sensitive_text(redact_url_query(str(req.url))))
                )

        valid_attempts = (
            attempt_count
            if isinstance(attempt_count, int) and not isinstance(attempt_count, bool)
            else None
        )
        cause_type = type(cause).__name__ if cause is not None else None
        return DiagnosticContext(
            method=resolved_method,
            url=resolved_url,
            attempt_count=valid_attempts,
            cause_type=cause_type,
        )

    def on_retry_error(
        self,
        e: RetryError,
        *,
        attempt_count: int | None = None,
    ) -> NoReturn:
        """Record a request failure after all retries are exhausted.

        Args:
            e: The Tenacity RetryError that occurred.
            attempt_count: Optional count of attempts executed before exhaustion.

        Raises:
            RequestError: Wraps the cause of the failure in a RequestError.
        """
        cause = e.last_attempt.exception() if e.last_attempt else e
        if attempt_count is None and e.last_attempt is not None:
            raw_attempt = getattr(e.last_attempt, "attempt_number", None)
            if isinstance(raw_attempt, int) and not isinstance(raw_attempt, bool):
                attempt_count = raw_attempt
        ctx = self._resolve_error_context(cause, attempt_count)
        logger.error(
            "Request failed after retries",
            extra={"method": ctx.method, "url": ctx.url},
        )
        raise RequestError(
            "Network request failed after retries", diagnostic_context=ctx
        ) from cause

    def on_request_error(
        self,
        e: httpx.RequestError,
        *,
        attempt_count: int | None = None,
    ) -> NoReturn:
        """Record an unretried HTTPX transport failure and wrap in RequestError."""
        ctx = self._resolve_error_context(e, attempt_count)
        logger.error(
            "Request failed",
            extra={"method": ctx.method, "url": ctx.url},
        )
        raise RequestError("Network request failed", diagnostic_context=ctx) from e
