"""Request monitoring and tracing."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, NoReturn

import httpx
from tenacity import RetryError

from imednet.core.operations.monitor_base import _BaseMonitor
from imednet.errors import RequestError
from imednet.utils.url import redact_url_query

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
        self.safe_url = redact_url_query(url)
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

    def on_retry_error(self, e: RetryError) -> NoReturn:
        """Record a request failure after all retries are exhausted.

        Args:
            e: The Tenacity RetryError that occurred.

        Raises:
            RequestError: Wraps the cause of the failure in a RequestError.
        """
        logger.error(
            "Request failed after retries",
            extra={"method": self.method, "url": self.safe_url},
        )
        cause = e.last_attempt.exception() if e.last_attempt else e
        raise RequestError("Network request failed after retries") from cause
