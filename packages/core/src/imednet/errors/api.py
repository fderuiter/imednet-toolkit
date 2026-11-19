"""API-level errors."""

from typing import Any

from imednet.utils.secrets import redact_sensitive_payload as _redact_sensitive_value

from .base import ImednetError


def _parse_int_code(raw_code: Any) -> int | None:
    """Convert an error code to int when possible."""
    if raw_code is None or isinstance(raw_code, bool):
        return None
    try:
        return int(raw_code)
    except (ValueError, TypeError):
        return None


def _non_empty_str(value: Any) -> str | None:
    """Convert value to string if present and non-empty."""
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


class ApiError(ImednetError):
    """Raised for generic API errors (non-2xx HTTP status codes).

    Attributes:
        status_code: HTTP status code returned by the API.
        response: Parsed JSON or raw text of the error response.
        code: Upstream API error code, if present.
        status: Upstream API status string, if present.
        description: Upstream API error description, if present.
        field: Offending request field details, if present.
    """

    def __init__(
        self, response: dict[str, Any] | str | Any, status_code: int | None = None
    ) -> None:
        """Initialize an API error.

        Args:
            response: The error response from the API.
            status_code: Optional HTTP status code.
        """
        sanitized_response = _redact_sensitive_value(response)

        message_arg = str(sanitized_response)
        parsed_message: str | None = None
        description: str | None = None
        field: dict[str, Any] | None = None
        code: int | None = None
        status: str | None = None

        if isinstance(sanitized_response, dict):
            metadata = sanitized_response.get("metadata")
            if isinstance(metadata, dict):
                status = _non_empty_str(metadata.get("status"))
                meta_error = metadata.get("error")
                if isinstance(meta_error, dict):
                    code = _parse_int_code(meta_error.get("code"))
                    description = _non_empty_str(meta_error.get("description"))
                    parsed_message = description or _non_empty_str(meta_error.get("message"))
                    raw_field = meta_error.get("field")
                    if isinstance(raw_field, dict) and raw_field:
                        field = dict(raw_field)
                elif isinstance(meta_error, str) and meta_error.strip():
                    parsed_message = meta_error

            if code is None and "code" in sanitized_response:
                code = _parse_int_code(sanitized_response["code"])

            if status is None and "status" in sanitized_response:
                status = _non_empty_str(sanitized_response["status"])

            if description is None and "description" in sanitized_response:
                description = _non_empty_str(sanitized_response["description"])

            if field is None:
                top_field = sanitized_response.get("field")
                if isinstance(top_field, dict) and top_field:
                    field = dict(top_field)

            if parsed_message is None:
                if "message" in sanitized_response and sanitized_response["message"] is not None:
                    parsed_message = str(sanitized_response["message"])
                elif description is not None:
                    parsed_message = description
                elif "error" in sanitized_response and sanitized_response["error"] is not None:
                    top_error = sanitized_response["error"]
                    if isinstance(top_error, dict):
                        parsed_message = _non_empty_str(
                            top_error.get("description")
                        ) or _non_empty_str(top_error.get("message"))
                        if code is None:
                            code = _parse_int_code(top_error.get("code"))
                        if (
                            field is None
                            and isinstance(top_error.get("field"), dict)
                            and top_error["field"]
                        ):
                            field = dict(top_error["field"])
                    else:
                        parsed_message = str(top_error)

        effective_message = parsed_message if parsed_message is not None else message_arg
        super().__init__(effective_message, status_code=status_code, code=code, status=status)
        self.response = sanitized_response
        self.message = effective_message
        self.description = description
        self.field = field

    def __str__(self) -> str:
        """Return a string representation of the error, including status and response.

        Returns:
            str: The formatted error message.
        """
        base = super().__str__()
        details = []
        if self.status_code is not None:
            details.append(f"Status Code: {self.status_code}")
        if self.response:
            details.append(f"Response: {self.response}")
        if details:
            return f"{base} ({', '.join(details)})"
        return base


class AuthenticationError(ApiError):
    """Raised when authentication to the API fails (HTTP 401)."""


class AuthorizationError(ApiError):
    """Raised when access to the API is forbidden (HTTP 403)."""


class NotFoundError(ApiError):
    """Raised when a requested resource is not found (HTTP 404)."""


class RateLimitError(ApiError):
    """Raised when the API rate limit is exceeded (HTTP 429)."""


class ServerError(ApiError):
    """Raised when the API returns a server error (HTTP 5xx)."""


class UnauthorizedError(AuthenticationError):
    """Raised for HTTP 401 unauthorized errors."""


class ForbiddenError(AuthorizationError):
    """Raised for HTTP 403 forbidden errors."""


class ConflictError(ApiError):
    """Raised for HTTP 409 conflict errors."""
