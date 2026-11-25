"""Validation errors."""

from typing import Any

from .api import ApiError
from .base import DiagnosticContext
from .client import ClientError


class ValidationError(ClientError, ApiError):
    """Raised when client-side validation fails or a request is malformed."""

    def __init__(
        self,
        response: dict[str, Any] | str | Any = "",
        status_code: int | None = None,
        *,
        validation_report: Any | None = None,
        diagnostic_context: DiagnosticContext | None = None,
    ) -> None:
        """Initialize validation error, preserving upstream status when present."""
        ApiError.__init__(
            self,
            response,
            status_code=status_code,
            diagnostic_context=diagnostic_context,
        )
        if not self.status:
            self.status = "validation_error"
        self.validation_report = validation_report


class BadRequestError(ValidationError):
    """Raised for HTTP 400 bad requests."""

    def __init__(
        self,
        response: dict[str, Any] | str | Any = "",
        status_code: int | None = 400,
        *,
        validation_report: Any | None = None,
        diagnostic_context: DiagnosticContext | None = None,
    ) -> None:
        """Initialize HTTP 400 bad request error."""
        super().__init__(
            response,
            status_code=status_code,
            validation_report=validation_report,
            diagnostic_context=diagnostic_context,
        )


class UnknownVariableTypeError(ValidationError):
    """Raised when an unrecognized variable type is encountered."""


class ConfigurationError(ClientError, ValueError):
    """Raised when required SDK/client configuration is missing."""


class PathTraversalValidationError(ClientError):
    """Raised when a partition key contains unsafe path content."""


class EvaluationError(ClientError):
    """Raised when expression evaluation fails."""


class RestrictedEvaluationError(EvaluationError):
    """Raised when an expression attempts forbidden operations or syntax."""
