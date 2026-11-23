"""URL manipulation and sanitization utilities."""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

from imednet.errors.validation import PathTraversalValidationError

__all__ = [
    "build_safe_path",
    "redact_sensitive_text",
    "redact_url_query",
    "sanitize_base_url",
    "validate_path_segment",
]

_FORBIDDEN_SEGMENT_CHARS = frozenset({"/", "\\", "?", "#"})
_FORBIDDEN_RAW_PATH_CHARS = frozenset({"\\", "?", "#"})
_URI_USERINFO_RE = re.compile(r"(://)[^@/\s\"']+@")
_TEXT_URL_OR_URI_RE = re.compile(
    r"""(?:[a-zA-Z][a-zA-Z0-9+.-]*://|(?i:file|sqlite(?:\+[a-zA-Z0-9_.-]+)?):(?!//))"""
    r"""[^\s"'>]*[^\s"'>.,;:!?\)\]]"""
)
_URI_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "api_key",
        "security_key",
        "token",
        "secret",
        "password",
        "passphrase",
        "key",
        "sig",
        "signature",
    }
)


def redact_sensitive_text(text: Any) -> str:
    """Scan and redact sensitive URIs, connection strings, and query parameters in text."""
    if not isinstance(text, str):
        try:
            text_str = str(text)
        except Exception:
            return "<unprintable object>"
    else:
        text_str = text

    # Redact userinfo in connection strings: e.g., mongodb://user:password@host or https://token@host  # pragma: allowlist secret
    text_str = _URI_USERINFO_RE.sub(r"\1***@", text_str)

    # Redact query parameters and userinfo in scheme:// URLs and authority-less file:/sqlite: URIs
    def replace_url(match: re.Match[str]) -> str:
        """Redact query parameters in the matched URL or URI string."""
        return redact_url_query(match.group(0))

    text_str = _TEXT_URL_OR_URI_RE.sub(replace_url, text_str)

    return str(text_str)


def sanitize_base_url(url: str) -> str:
    """Return base URL without trailing slashes or ``/api``, ``/api/v1``, or ``/api/v1/edc`` suffix."""
    cleaned = url.rstrip("/")
    return str(re.sub(r"/api(?:/v1(?:/edc)?)?$", "", cleaned)).rstrip("/")


def redact_url_query(url: str, sensitive_params: set[str] | None = None) -> str:
    """Return URL or URI with userinfo and sensitive query parameters redacted."""
    from imednet.utils.secrets import is_sensitive_key

    try:
        parsed = urlparse(url)
    except ValueError:
        return _URI_USERINFO_RE.sub(r"\1***@", url)

    has_userinfo = "@" in parsed.netloc
    if not has_userinfo and not parsed.query:
        return url

    new_netloc = parsed.netloc
    if has_userinfo:
        userinfo, _, hostport = parsed.netloc.rpartition("@")
        if userinfo:
            new_netloc = f"***@{hostport}"

    new_query = parsed.query
    redacted_any_query = False
    if parsed.query:
        query_params = parse_qsl(parsed.query, keep_blank_values=True)
        new_query_params = []
        for key, value in query_params:
            is_secret = (
                key in sensitive_params
                if sensitive_params is not None
                else (key.lower() in _URI_SENSITIVE_QUERY_KEYS or is_sensitive_key(key))
            )
            if is_secret:
                new_query_params.append((key, "***"))
                redacted_any_query = True
            else:
                new_query_params.append((key, value))
        if redacted_any_query:
            new_query = urlencode(new_query_params, safe="*")

    if not has_userinfo and not redacted_any_query:
        return url

    if not new_netloc and parsed.query:
        before_q, _, after_q = url.partition("?")
        _, hash_sep, fragment = after_q.partition("#")
        return f"{before_q}?{new_query}{hash_sep}{fragment}"

    return urlunparse(parsed._replace(netloc=new_netloc, query=new_query))


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
