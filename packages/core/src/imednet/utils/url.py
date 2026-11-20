"""URL manipulation and sanitization utilities."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

__all__ = ["build_safe_path", "redact_sensitive_text", "redact_url_query", "sanitize_base_url"]
_DUMMY_BASE_URL = "http://dummy/"


def redact_sensitive_text(text: Any) -> str:
    """Scan and redact sensitive URIs, connection strings, and query parameters in text."""
    if not isinstance(text, str):
        try:
            text_str = str(text)
        except Exception:
            return "<unprintable object>"
    else:
        text_str = text

    # Redact passwords in connection strings: e.g., mongodb://user:password@host  # pragma: allowlist secret
    text_str = re.sub(r"(://[^:]+:)[^@]+(@)", r"\g<1>***\g<2>", text_str)

    # Redact query parameters in any URLs
    def replace_url(match: re.Match[str]) -> str:
        """Redact query parameters in the matched URL string."""
        return redact_url_query(match.group(0))

    text_str = re.sub(
        r'''[a-zA-Z][a-zA-Z0-9+.-]*://[^\s"'>]*[^\s"'>.,;:!?\)\]]''', replace_url, text_str
    )

    return str(text_str)


def sanitize_base_url(url: str) -> str:
    """Return base URL without trailing slashes or ``/api``, ``/api/v1``, or ``/api/v1/edc`` suffix."""
    cleaned = url.rstrip("/")
    return str(re.sub(r"/api(?:/v1(?:/edc)?)?$", "", cleaned)).rstrip("/")


def redact_url_query(url: str, sensitive_params: set[str] | None = None) -> str:
    """Return URL with sensitive query parameters redacted."""
    if sensitive_params is None:
        sensitive_params = {"api_key", "security_key", "token", "secret", "password"}

    parsed = urlparse(url)
    query_params = parse_qsl(parsed.query, keep_blank_values=True)

    new_query_params = []
    for key, value in query_params:
        if key in sensitive_params:
            new_query_params.append((key, "***"))
        else:
            new_query_params.append((key, value))

    new_query = urlencode(new_query_params, safe="*")
    return urlunparse(parsed._replace(query=new_query))


def build_safe_path(base_path: str, *segments: Any) -> str:
    """Build a normalized relative path using HTTPX URL joining.

    Args:
        base_path: Base path segment to start from.
        *segments: Additional path segments. Non-string values are stringified.

    Returns:
        A slash-normalized relative path with URL-encoded segments decoded
        (for example, ``"a%2Fb"`` becomes ``"a/b"``).
    """
    parts: list[str] = []
    for segment in (base_path, *segments):
        for part in str(segment).split("/"):
            if part:
                parts.append(part)

    if not parts:
        return ""

    normalized = "/".join(parts)
    return str(httpx.URL(_DUMMY_BASE_URL).join(normalized).path.strip("/"))
