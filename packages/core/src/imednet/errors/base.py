"""Base errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiagnosticContext:
    """Immutable HTTP and transport diagnostic metadata attached to SDK errors."""

    method: str | None = None
    url: str | None = None
    retry_after: float | None = None
    attempt_count: int | None = None
    cause_type: str | None = None

    def __post_init__(self) -> None:
        """Normalize HTTP method and sanitize any request URL."""
        if self.method is not None:
            normalized_method = str(self.method).strip().upper() or None
            object.__setattr__(self, "method", normalized_method)
        if self.url is not None:
            raw_url = str(self.url).strip()
            if raw_url:
                from imednet.utils.url import redact_sensitive_text, redact_url_query

                sanitized_url = redact_url_query(redact_sensitive_text(raw_url))
                object.__setattr__(self, "url", sanitized_url)
            else:
                object.__setattr__(self, "url", None)


class ImednetError(Exception):
    """Base exception for all iMednet SDK errors."""

    def __init__(
        self,
        message: str = "",
        status_code: int | None = None,
        code: int | None = None,
        status: Any = None,
        *,
        diagnostic_context: DiagnosticContext | None = None,
    ) -> None:
        """Initialize the error."""
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.status: Any = status
        self.diagnostic_context: DiagnosticContext = (
            diagnostic_context if diagnostic_context is not None else DiagnosticContext()
        )

    @property
    def method(self) -> str | None:
        """Return the HTTP method from ``diagnostic_context``, if present."""
        return self.diagnostic_context.method

    @property
    def url(self) -> str | None:
        """Return the sanitized request URL from ``diagnostic_context``, if present."""
        return self.diagnostic_context.url

    @property
    def retry_after(self) -> float | None:
        """Return the parsed ``Retry-After`` delay in seconds, if present."""
        return self.diagnostic_context.retry_after

    @property
    def attempt_count(self) -> int | None:
        """Return the number of attempts executed before failure, if present."""
        return self.diagnostic_context.attempt_count

    @property
    def cause_type(self) -> str | None:
        """Return the underlying exception class name, if present."""
        return self.diagnostic_context.cause_type
