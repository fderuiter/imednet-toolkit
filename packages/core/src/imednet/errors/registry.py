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


def _extract_error_codes(raw_error: Any) -> list[str]:
    """Extract normalized error code strings from a dict or list error container."""
    codes: list[str] = []
    if isinstance(raw_error, dict):
        code = _normalize_code(raw_error.get("code"))
        if code:
            codes.append(code)
    elif isinstance(raw_error, list):
        for item in raw_error:
            if isinstance(item, dict):
                code = _normalize_code(item.get("code"))
                if code:
                    codes.append(code)
    return codes


def _resolve_code_error(code: str, *, is_forbidden: bool) -> type[ApiError] | None:
    """Resolve an upstream error code to an ApiError subclass."""
    if code == "9001":
        return ForbiddenError if is_forbidden else AuthenticationError
    return CODE_TO_ERROR.get(code)


def _preserve_http_status_subclass(
    status_code: int,
    exc_cls: type[ApiError],
) -> type[ApiError]:
    """Preserve HTTP 400 and 401 status-specific subclasses during payload dispatch."""
    if status_code == 400 and exc_cls is ValidationError:
        return BadRequestError
    if status_code == 401 and exc_cls is AuthenticationError:
        return UnauthorizedError
    return exc_cls


def get_error_class(status_code: int, response_body: Any = None) -> type[ApiError]:
    """Get error class for status code and response payload.

    Inspects nested ``metadata.error`` (dict or list) and ``metadata.status`` first,
    preserving ``BadRequestError`` on HTTP 400, ``UnauthorizedError`` on HTTP 401, and
    ``ForbiddenError`` on HTTP 403 or ``FORBIDDEN`` responses with code ``9001``,
    then legacy top-level keys, and finally falls back to HTTP status mappings.
    Defaults to generic ``ApiError`` for unmapped client/server errors.
    """
    if isinstance(response_body, dict):
        metadata = response_body.get("metadata")
        meta_status_upper = ""
        if isinstance(metadata, dict):
            raw_meta_status = metadata.get("status")
            if isinstance(raw_meta_status, str) and raw_meta_status.strip():
                meta_status_upper = raw_meta_status.strip().upper()

        raw_top_status = response_body.get("status")
        top_status_upper = (
            raw_top_status.strip().upper()
            if isinstance(raw_top_status, str) and raw_top_status.strip()
            else ""
        )
        is_forbidden = (
            status_code == 403
            or meta_status_upper == "FORBIDDEN"
            or top_status_upper == "FORBIDDEN"
        )

        if isinstance(metadata, dict):
            for nested_code in _extract_error_codes(metadata.get("error")):
                mapped_code_cls = _resolve_code_error(nested_code, is_forbidden=is_forbidden)
                if mapped_code_cls is not None:
                    return _preserve_http_status_subclass(status_code, mapped_code_cls)

            if meta_status_upper:
                mapped_meta_cls = METADATA_STATUS_TO_ERROR.get(meta_status_upper)
                if mapped_meta_cls is not None:
                    return _preserve_http_status_subclass(status_code, mapped_meta_cls)

        top_code = _normalize_code(response_body.get("code"))
        if top_code:
            mapped_top_cls = _resolve_code_error(top_code, is_forbidden=is_forbidden)
            if mapped_top_cls is not None:
                return _preserve_http_status_subclass(status_code, mapped_top_cls)

        for fallback_key in ("error", "errors"):
            for item_code in _extract_error_codes(response_body.get(fallback_key)):
                mapped_item_cls = _resolve_code_error(item_code, is_forbidden=is_forbidden)
                if mapped_item_cls is not None:
                    return _preserve_http_status_subclass(status_code, mapped_item_cls)

        # Identify specific subclasses based on legacy top-level status
        if status_code == 400 and top_status_upper in {"VALIDATION_ERROR", "BAD_REQUEST"}:
            return BadRequestError
        if status_code == 401 and top_status_upper in {"AUTHENTICATION_FAILED", "UNAUTHORIZED"}:
            return UnauthorizedError

    error_cls = STATUS_TO_ERROR.get(status_code)
    if error_cls:
        return error_cls
    if 500 <= status_code < 600:
        return ServerError
    return ApiError
