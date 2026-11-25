"""URL manipulation and sanitization utilities."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any
from unittest.mock import NonCallableMock
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlsplit, urlunparse

from imednet.constants import DEFAULT_BASE_URL
from imednet.errors.validation import PathTraversalValidationError

if TYPE_CHECKING:
    from imednet.utils.security import SensitivityRegistry

__all__ = [
    "build_safe_path",
    "normalize_cache_origin",
    "redact_sensitive_text",
    "redact_url_query",
    "resolve_sdk_base_url",
    "sanitize_base_url",
    "validate_path_segment",
]

_FORBIDDEN_SEGMENT_CHARS = frozenset({"/", "\\", "?", "#"})
_FORBIDDEN_RAW_PATH_CHARS = frozenset({"\\", "?", "#"})
_URI_AUTH_PASSWORD_RE = re.compile(r"(://[^\s/:?#\"'<>@]*:)[^\s/?#\"'<>,;&)\]}]+(@)")
_URL_IN_TEXT_RE = re.compile(
    r"""(?:[a-zA-Z][a-zA-Z0-9+.-]*://[^\s"'<>\x00]*[^\s"'<>.,;:!?\)\]\}\x00]|(?<![A-Za-z0-9_:/.-])/[A-Za-z0-9_.~%@/-]*\?[^\s"'<>\x00]*[^\s"'<>.,;:!?\)\]\}\x00])"""
)


def redact_sensitive_text(
    text: Any,
    *,
    registry: SensitivityRegistry | None = None,
    _depth: int = 0,
) -> str:
    """Scan and redact sensitive URIs, connection strings, and query parameters in text."""
    if not isinstance(text, str):
        try:
            text_str = str(text)
        except Exception:
            return "<unprintable object>"
    else:
        text_str = text

    # Redact passwords in connection strings: e.g., mongodb://user:password@host  # pragma: allowlist secret
    text_str = _URI_AUTH_PASSWORD_RE.sub(r"\g<1>***\g<2>", text_str)

    # Redact query parameters and PHI filter clauses in scheme or relative URLs
    def replace_url(match: re.Match[str]) -> str:
        """Redact query parameters in the matched URL string."""
        return redact_url_query(match.group(0), registry=registry, _depth=_depth)

    text_str = _URL_IN_TEXT_RE.sub(replace_url, text_str)

    return str(text_str)


def sanitize_base_url(url: str) -> str:
    """Return base URL without trailing slashes or ``/api``, ``/api/v1``, or ``/api/v1/edc`` suffix."""
    cleaned = url.rstrip("/")
    return str(re.sub(r"/api(?:/v1(?:/edc)?)?$", "", cleaned)).rstrip("/")


def normalize_cache_origin(url: str | None) -> str:
    """Normalize an SDK base URL into a canonical cache partition key."""
    raw = url.strip() if isinstance(url, str) and url.strip() else DEFAULT_BASE_URL
    sanitized = sanitize_base_url(raw)
    if "://" not in sanitized and not sanitized.startswith("/"):
        sanitized = f"https://{sanitized}"
    parsed = urlsplit(sanitized)
    if not parsed.scheme or not parsed.hostname:
        return sanitized

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    formatted_host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = parsed.port
    except ValueError:
        port = None

    is_default_port = (
        port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    )
    netloc = formatted_host if is_default_port else f"{formatted_host}:{port}"
    normalized_path = sanitize_base_url(parsed.path) if parsed.path else ""
    if normalized_path and not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    return f"{scheme}://{netloc}{normalized_path}"


def _safe_optional_attr(obj: Any, attr: str) -> Any:
    """Read an attribute without spawning child mocks on NonCallableMock instances."""
    if obj is None:
        return None
    if isinstance(obj, NonCallableMock):
        obj_vars = vars(obj)
        mock_children = getattr(obj, "_mock_children", {})
        if attr not in obj_vars and attr not in mock_children:
            return None
    return getattr(obj, attr, None)


def resolve_sdk_base_url(sdk: Any) -> str:
    """Resolve and normalize the configured EDC origin URL for an SDK or facade instance."""
    client = _safe_optional_attr(sdk, "_client")
    async_client = _safe_optional_attr(sdk, "_async_client")
    candidates = (
        _safe_optional_attr(sdk, "base_url"),
        _safe_optional_attr(sdk, "_base_url"),
        _safe_optional_attr(_safe_optional_attr(sdk, "_ctx"), "base_url"),
        _safe_optional_attr(client, "base_url"),
        _safe_optional_attr(client, "_base_url"),
        _safe_optional_attr(async_client, "base_url"),
        _safe_optional_attr(async_client, "_base_url"),
        _safe_optional_attr(_safe_optional_attr(sdk, "config"), "base_url"),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return normalize_cache_origin(candidate)
        if (
            candidate is not None
            and not isinstance(candidate, NonCallableMock)
            and hasattr(candidate, "scheme")
            and hasattr(candidate, "host")
        ):
            rendered = str(candidate).strip()
            if rendered:
                return normalize_cache_origin(rendered)
    return normalize_cache_origin(DEFAULT_BASE_URL)


def redact_url_query(
    url: str,
    sensitive_params: set[str] | None = None,
    *,
    registry: SensitivityRegistry | None = None,
    _depth: int = 0,
) -> str:
    """Return URL with sensitive credential and clinical PHI query parameters redacted."""
    from imednet.utils.secrets import is_sensitive_key
    from imednet.utils.security import global_sensitivity_registry, sanitize_diagnostic_text

    default_params = {"api_key", "security_key", "token", "secret", "password", "passphrase"}
    active_registry = global_sensitivity_registry if registry is None else registry

    parsed = urlparse(url)
    if not parsed.query:
        return urlunparse(parsed)

    query_params = parse_qsl(parsed.query, keep_blank_values=True)

    new_query_params: list[tuple[str, str]] = []
    for key, value in query_params:
        is_secret = (
            key in sensitive_params
            if sensitive_params is not None
            else (key.lower() in default_params or is_sensitive_key(key))
        )
        if is_secret:
            new_query_params.append((key, "***"))
        elif sensitive_params is None and active_registry.is_sensitive(key):
            new_query_params.append((key, "***MASKED***"))
        elif value and _depth < 4:
            sanitized_val = sanitize_diagnostic_text(
                value,
                registry=active_registry,
                _depth=_depth + 1,
            )
            if "%" in value:
                unquoted_val = unquote(value)
                if unquoted_val != value:
                    sanitized_unquoted = sanitize_diagnostic_text(
                        unquoted_val,
                        registry=active_registry,
                        _depth=_depth + 1,
                    )
                    if sanitized_unquoted != unquoted_val:
                        sanitized_val = sanitized_unquoted
            new_query_params.append((key, sanitized_val))
        else:
            new_query_params.append((key, value))

    new_query = urlencode(new_query_params, safe="*")
    return urlunparse(parsed._replace(query=new_query))


def _has_control_chars(value: str) -> bool:
    """Return True if ``value`` contains ASCII or Unicode control characters."""
    return any(
        ord(ch) < 0x20 or ord(ch) == 0x7F or unicodedata.category(ch) == "Cc" for ch in value
    )


def validate_path_segment(segment: Any, *, name: str = "segment") -> str:
    r"""Validate and percent-encode a single dynamic URL path segment.

    Args:
        segment: Single dynamic identifier (e.g., ``study_key``, ``batch_id``, ``item_id``).
        name: Parameter label included in error diagnostics.

    Returns:
        Strictly percent-encoded single segment preserving ``@`` unencoded.

    Raises:
        PathTraversalValidationError: If the segment is empty, boolean, contains control
            characters or ``/``, ``\\``, ``?``, ``#``, or resolves to ``.`` or ``..``.
    """
    if segment is None or isinstance(segment, bool):
        raise PathTraversalValidationError(f"Invalid {name}: segment must not be empty.")

    raw = segment if isinstance(segment, str) else str(segment)
    if not raw or not raw.strip():
        raise PathTraversalValidationError(f"Invalid {name}: segment must not be empty.")

    decoded = unquote(raw)
    if not decoded or not decoded.strip():
        raise PathTraversalValidationError(f"Invalid {name}: segment must not be empty.")

    if _has_control_chars(raw) or _has_control_chars(decoded):
        raise PathTraversalValidationError(
            f"Invalid {name}: control characters are not allowed in URL path segments."
        )

    if any(ch in _FORBIDDEN_SEGMENT_CHARS for ch in raw) or any(
        ch in _FORBIDDEN_SEGMENT_CHARS for ch in decoded
    ):
        raise PathTraversalValidationError(
            f"Invalid {name}: path, query, or fragment delimiters are not allowed."
        )

    if raw in {".", ".."} or decoded in {".", ".."}:
        raise PathTraversalValidationError(
            f"Invalid {name}: relative dot-segments are not allowed."
        )

    return quote(decoded, safe="@")


def build_safe_path(base_path: str, *segments: Any) -> str:
    r"""Build a normalized relative URL path with strict segment validation and encoding.

    Args:
        base_path: Static base path prefix to start from.
        *segments: Additional path segments. Non-string values are stringified.

    Returns:
        A slash-normalized relative path with each segment validated and percent-encoded
        (preserving ``@`` unencoded).

    Raises:
        PathTraversalValidationError: If any segment contains ``?``, ``#``, ``\\``,
            control characters, decoded ``/``, or raw/decoded ``.`` or ``..`` parts.
    """
    encoded_parts: list[str] = []
    for segment in (base_path, *segments):
        if segment is None or isinstance(segment, bool):
            raise PathTraversalValidationError("Invalid path segment: segment must not be empty.")
        raw_segment = segment if isinstance(segment, str) else str(segment)
        if _has_control_chars(raw_segment) or any(
            ch in _FORBIDDEN_RAW_PATH_CHARS for ch in raw_segment
        ):
            raise PathTraversalValidationError(
                "Invalid path segment: unsafe URL delimiter or control character."
            )
        for part in raw_segment.split("/"):
            if part:
                encoded_parts.append(validate_path_segment(part))

    return "/".join(encoded_parts)
