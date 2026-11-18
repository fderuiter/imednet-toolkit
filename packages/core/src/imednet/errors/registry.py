"""Registry for HTTP status code to error class mappings."""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Any

from .api import (
    ApiError,
    AuthenticationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UnauthorizedError,
)
from .validation import BadRequestError, ValidationError

# Use MappingProxyType to ensure immutability, avoiding global mutable state
STATUS_TO_ERROR: Mapping[int, type[ApiError]] = types.MappingProxyType(
    {
        400: BadRequestError,
        401: UnauthorizedError,
        403: ForbiddenError,
        404: NotFoundError,
        409: ConflictError,
        429: RateLimitError,
    }
)

CODE_TO_ERROR: Mapping[str, type[ApiError]] = types.MappingProxyType(
    {
        "1000": ValidationError,
        "9000": ServerError,
        "9001": AuthenticationError,
    }
)

METADATA_STATUS_TO_ERROR: Mapping[str, type[ApiError]] = types.MappingProxyType(
    {
        "VALIDATION_ERROR": ValidationError,
        "BAD_REQUEST": BadRequestError,
        "AUTHENTICATION_FAILED": AuthenticationError,
        "UNAUTHORIZED": AuthenticationError,
        "FORBIDDEN": ForbiddenError,
        "NOT_FOUND": NotFoundError,
        "CONFLICT": ConflictError,
        "TOO_MANY_REQUESTS": RateLimitError,
        "INTERNAL_SERVER_ERROR": ServerError,
    }
)


def _normalize_code(raw_code: Any) -> str | None:
    """Normalize an error code value to a stripped string if present."""
    if raw_code is None or isinstance(raw_code, bool):
        return None
    normalized = str(raw_code).strip()
    return normalized or None


def get_error_class(status_code: int, response_body: Any = None) -> type[ApiError]:
    """Get error class for status code and response payload.

    Inspects nested ``metadata.error.code`` and ``metadata.status`` first,
    then legacy top-level keys, and finally falls back to HTTP status mappings.
    Defaults to generic ``ApiError`` for unmapped client/server errors.
    """
    if isinstance(response_body, dict):
        metadata = response_body.get("metadata")
        if isinstance(metadata, dict):
            meta_error = metadata.get("error")
            if isinstance(meta_error, dict):
                nested_code = _normalize_code(meta_error.get("code"))
                if nested_code and nested_code in CODE_TO_ERROR:
                    return CODE_TO_ERROR[nested_code]

            meta_status = metadata.get("status")
            if isinstance(meta_status, str) and meta_status.strip():
                mapped_meta_cls = METADATA_STATUS_TO_ERROR.get(meta_status.strip().upper())
                if mapped_meta_cls is not None:
                    return mapped_meta_cls

        top_code = _normalize_code(response_body.get("code"))
        if top_code and top_code in CODE_TO_ERROR:
            return CODE_TO_ERROR[top_code]

        status = response_body.get("status")
        # Identify specific subclasses based on legacy top-level status
        if status_code == 400 and status == "validation_error":
            return ValidationError
        if status_code == 401 and status == "authentication_failed":
            return AuthenticationError

    error_cls = STATUS_TO_ERROR.get(status_code)
    if error_cls:
        return error_cls
    if 500 <= status_code < 600:
        return ServerError
    return ApiError
