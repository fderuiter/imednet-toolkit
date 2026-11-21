"""Network errors."""

from .base import ImednetError


class RequestError(ImednetError):
    """Raised when an HTTP transport request fails."""

    def __str__(self) -> str:
        """Format transport error message with sanitized request context when available."""
        base = super().__str__().strip()
        details: list[str] = []
        if self.method is not None:
            details.append(f"Method: {self.method}")
        if self.url is not None:
            details.append(f"URL: {self.url}")
        if self.attempt_count is not None:
            details.append(f"Attempts: {self.attempt_count}")
        if self.cause_type is not None:
            details.append(f"Cause: {self.cause_type}")
        if base and details:
            return f"{base} ({', '.join(details)})"
        if details:
            return f"Network request failed ({', '.join(details)})"
        return base


class CircuitBreakerError(ImednetError):
    """Raised when the circuit breaker is open and a request is blocked."""
