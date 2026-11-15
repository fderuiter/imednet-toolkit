"""Base monitor class providing dual synchronous and asynchronous context management."""

from __future__ import annotations

import time
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer
else:
    Tracer = Any

_T = TypeVar("_T", bound="_BaseMonitor")


class _BaseMonitor:
    """Base context manager for tracing, timing, and monitoring operations."""

    def __init__(
        self,
        tracer: Tracer | None,
        span_name: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the base monitor.

        Args:
            tracer: OpenTelemetry tracer to use for creating spans.
            span_name: Name of the span to be created.
            attributes: Optional key-value pairs to attach to the span.
        """
        self.tracer = tracer
        self.span_name = span_name
        self.attributes = attributes or {}
        self.start_time: float = 0.0
        self.span: Any = None
        self._cm: Any = None

    def _create_cm(self) -> Any:
        """Create a context manager for the span.

        Returns:
            Any: A context manager (either a span or a null context).
        """
        if self.tracer:
            return self.tracer.start_as_current_span(
                self.span_name,
                attributes=self.attributes,
            )
        return nullcontext()

    def __enter__(self: _T) -> _T:
        """Enter the synchronous monitoring context.

        Returns:
            _T: The monitor instance.
        """
        self._cm = self._create_cm()
        self.span = self._cm.__enter__()
        self.start_time = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the synchronous monitoring context.

        Args:
            exc_type: Exception type if an error occurred.
            exc_val: Exception value if an error occurred.
            exc_tb: Exception traceback if an error occurred.
        """
        if self._cm:
            self._cm.__exit__(exc_type, exc_val, exc_tb)

    async def __aenter__(self: _T) -> _T:
        """Enter the asynchronous monitoring context.

        Returns:
            _T: The monitor instance.
        """
        self._cm = self._create_cm()
        # Handle async context managers if the tracer supports them
        if hasattr(self._cm, "__aenter__"):
            self.span = await self._cm.__aenter__()
        else:
            self.span = self._cm.__enter__()
        self.start_time = time.monotonic()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the asynchronous monitoring context.

        Args:
            exc_type: Exception type if an error occurred.
            exc_val: Exception value if an error occurred.
            exc_tb: Exception traceback if an error occurred.
        """
        if self._cm:
            if hasattr(self._cm, "__aexit__"):
                await self._cm.__aexit__(exc_type, exc_val, exc_tb)
            else:
                self._cm.__exit__(exc_type, exc_val, exc_tb)
