"""Monitoring utilities for iMednet operations, supporting tracing and logging."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, NoReturn

from imednet.core.operations.monitor_base import _BaseMonitor

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
else:
    Tracer = Any

logger = logging.getLogger(__name__)


class OperationMonitor(_BaseMonitor):
    """Helper to handle generic operation monitoring (tracing, timing, logging)."""

    def __init__(self, tracer: Tracer | None, operation_name: str, **attributes: Any) -> None:
        """Initialize the operation monitor.

        Args:
            tracer: OpenTelemetry tracer to use for creating spans.
            operation_name: Name of the operation being monitored.
            **attributes: Key-value pairs to attach to the operation span/log.
        """
        self.operation_name = operation_name
        super().__init__(tracer=tracer, span_name=operation_name, attributes=attributes)

    def on_success(self, **extra_attributes: Any) -> None:
        """Record a successful operation completion.

        Args:
            **extra_attributes: Additional attributes to log or attach to the span.
        """
        latency = time.monotonic() - self.start_time
        logger.info(
            f"{self.operation_name} succeeded",
            extra={**self.attributes, **extra_attributes, "latency": latency},
        )
        if self.span:
            for k, v in extra_attributes.items():
                self.span.set_attribute(k, v)
            self.span.set_attribute("status", "success")

    def on_retry_error(self, cause: Exception, retries: int) -> NoReturn:
        """Record a failure after all retries are exhausted.

        Args:
            cause: The final exception that caused the failure.
            retries: Number of retries attempted.

        Raises:
            Exception: Re-raises the cause exception.
        """
        logger.error(
            f"{self.operation_name} failed after retries",
            extra={**self.attributes, "retries": retries},
        )
        if self.span:
            self.span.set_attribute("status", "failed")
            self.span.set_attribute("retries", retries)
        raise cause

    def on_failure(self, cause: Exception) -> None:
        """Record an operation failure.

        Args:
            cause: The exception that caused the failure.
        """
        if self.span:
            self.span.set_attribute("status", "failed")
