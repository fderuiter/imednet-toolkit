"""API-level errors."""

from http import HTTPStatus
from typing import Any

from imednet.utils.secrets import redact_sensitive_payload as _redact_sensitive_value

from .base import DiagnosticContext, ImednetError


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


def _http_status_fallback(status_code: int | None) -> str:
    """Return a clean human-readable fallback message for empty HTTP error bodies."""
    if status_code is not None:
        try:
            phrase = HTTPStatus(status_code).phrase
            return f"HTTP {status_code} {phrase}"
        except ValueError:
            return f"HTTP {status_code}"
    return "API error"


def _normalize_error_entries(raw_error: Any) -> list[dict[str, Any]]:
    """Normalize dict or list error payloads into a list of error item dicts."""
    if isinstance(raw_error, dict):
        return [dict(raw_error)] if raw_error else []
    if isinstance(raw_error, list):
        entries: list[dict[str, Any]] = []
        for item in raw_error:
            if isinstance(item, dict) and item:
                entries.append(dict(item))
            elif item is not None:
                text = _non_empty_str(item)
                if text is not None:
                    entries.append({"description": text})
        return entries
    return []


def _summarize_error_entries(
    entries: list[dict[str, Any]],
) -> tuple[int | None, str | None, str | None, dict[str, Any] | None]:
    """Extract primary code, joined description, joined message, and primary field from entries."""
    code: int | None = None
    descriptions: list[str] = []
    messages: list[str] = []
    primary_field: dict[str, Any] | None = None

    for entry in entries:
        if code is None:
            code = _parse_int_code(entry.get("code"))
        desc = _non_empty_str(entry.get("description"))
        if desc is not None and desc not in descriptions:
            descriptions.append(desc)
        msg = desc or _non_empty_str(entry.get("message"))
        if msg is not None and msg not in messages:
            messages.append(msg)
        raw_field = entry.get("field")
        if primary_field is None and isinstance(raw_field, dict) and raw_field:
            primary_field = dict(raw_field)

    joined_desc = "; ".join(descriptions) if descriptions else None
    joined_msg = "; ".join(messages) if messages else None
    return code, joined_desc, joined_msg, primary_field


class ApiError(ImednetError):
    """Raised for generic API errors (non-2xx HTTP status codes).

    Attributes:
        status_code: HTTP status code returned by the API.
        response: Parsed JSON or raw text of the error response.
        code: Upstream API error code, if present.
        status: Upstream API status string, if present.
        description: Upstream API error description, if present.
        field: Offending request field details, if present.
        errors: Normalized list of structured error items, if present.
    """

    def __init__(
        self,
        response: dict[str, Any] | str | Any,
        status_code: int | None = None,
        *,
        diagnostic_context: DiagnosticContext | None = None,
    ) -> None:
        """Initialize an API error.

        Args:
            response: The error response from the API.
            status_code: Optional HTTP status code.
            diagnostic_context: Optional HTTP diagnostic context.
        """
        sanitized_response = _redact_sensitive_value(response)

        parsed_message: str | None = None
        description: str | None = None
        field: dict[str, Any] | None = None
        code: int | None = None
        status: str | None = None
        errors: list[dict[str, Any]] = []

        if isinstance(sanitized_response, dict):
            metadata = sanitized_response.get("metadata")
            if isinstance(metadata, dict):
                status = _non_empty_str(metadata.get("status"))
                meta_error = metadata.get("error")
                if isinstance(meta_error, (dict, list)):
                    errors = _normalize_error_entries(meta_error)
                    if errors:
                        code, description, parsed_message, field = _summarize_error_entries(errors)
                elif isinstance(meta_error, str) and meta_error.strip():
                    parsed_message = meta_error

            if not errors and isinstance(sanitized_response.get("errors"), list):
                errors = _normalize_error_entries(sanitized_response["errors"])
                if errors:
                    e_code, e_desc, e_msg, e_field = _summarize_error_entries(errors)
                    code = code if code is not None else e_code
                    description = description if description is not None else e_desc
                    parsed_message = parsed_message if parsed_message is not None else e_msg
                    field = field if field is not None else e_field

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
                    if isinstance(top_error, (dict, list)):
                        top_entries = _normalize_error_entries(top_error)
                        if not errors:
                            errors = top_entries
                        t_code, t_desc, t_msg, t_field = _summarize_error_entries(top_entries)
                        parsed_message = t_msg
                        if code is None:
                            code = t_code
                        if description is None:
                            description = t_desc
                        if field is None:
                            field = t_field
                    else:
                        parsed_message = str(top_error)

        has_diag = diagnostic_context is not None and bool(
            diagnostic_context.method or diagnostic_context.url
        )
        if parsed_message is not None:
            effective_message = parsed_message
        elif sanitized_response is None:
            effective_message = (
                _http_status_fallback(status_code)
                if (status_code is not None or has_diag)
                else "None"
            )
        elif (isinstance(sanitized_response, str) and not sanitized_response.strip()) or (
            isinstance(sanitized_response, (dict, list))
            and not sanitized_response
            and status_code is not None
        ):
            effective_message = _http_status_fallback(status_code)
        else:
            effective_message = str(sanitized_response)

        super().__init__(
            effective_message,
            status_code=status_code,
            code=code,
            status=status,
            diagnostic_context=diagnostic_context,
        )
        self.response = sanitized_response
        self.message = effective_message
        self.description = description
        self.field = field
        self.errors = errors
        self.variable: str | None = None
        if isinstance(field, dict):
            self.variable = _non_empty_str(field.get("variable")) or _non_empty_str(
                field.get("attribute")
            )

    @property
    def field_attribute(self) -> str | None:
        """Return the offending field or variable name from ``field``, if present."""
        if not isinstance(self.field, dict):
            return None
        return _non_empty_str(self.field.get("attribute")) or _non_empty_str(
            self.field.get("variable")
        )

    @property
    def field_value(self) -> Any:
        """Return the redacted offending field value from ``field``, if present."""
        if not isinstance(self.field, dict):
            return None
        return self.field.get("value")

    def __str__(self) -> str:
        """Return a string representation of the error, including status, response, and context."""
        base = super().__str__().strip()
        details: list[str] = []
        if self.status_code is not None:
            details.append(f"Status Code: {self.status_code}")
        if self.response and not (
            isinstance(self.response, str) and (not self.response.strip() or self.response == base)
        ):
            details.append(f"Response: {self.response}")

        field_dicts: list[dict[str, Any]] = [
            entry["field"]
            for entry in self.errors
            if isinstance(entry.get("field"), dict) and entry["field"]
        ]
        if not field_dicts and isinstance(self.field, dict) and self.field:
            field_dicts = [self.field]

        formatted_fields: list[str] = []
        for raw_field in field_dicts:
            redacted_field = _redact_sensitive_value(raw_field)
            attr = _non_empty_str(redacted_field.get("attribute")) or _non_empty_str(
                redacted_field.get("variable")
            )
            if attr is not None and "value" in redacted_field:
                formatted_fields.append(f"{attr}={redacted_field['value']!r}")
            elif attr is not None:
                formatted_fields.append(attr)
        if formatted_fields:
            details.append(f"Field: {'; '.join(formatted_fields)}")

        if self.method is not None:
            details.append(f"Method: {self.method}")
        if self.url is not None:
            details.append(f"URL: {self.url}")
        if self.retry_after is not None:
            details.append(f"Retry-After: {self.retry_after:g}s")

        if base and details:
            return f"{base} ({', '.join(details)})"
        if details:
            return f"({', '.join(details)})"
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


ImednetAPIError = ApiError
