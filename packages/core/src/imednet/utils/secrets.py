"""Secret redaction utilities."""

import re
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "token",
        "secret",
        "credential",
        "api_key",
        "apikey",
        "auth_key",
        "access_key",
        "security_key",
        "private_key",
        "client_secret",
        "authorization",
        "x-api-key",
        "x-imn-security-key",
    }
)

_SENSITIVE_PATTERN_KEYS = (
    "x-imn-security-key",
    "x-api-key",
    "api[_-]?key",
    "security[_-]?key",
    "authorization",
    "token",
    "password",
    "secret",
)
_SENSITIVE_PATTERN = re.compile(
    rf"(?i)\b({'|'.join(_SENSITIVE_PATTERN_KEYS)})\b(\s*[:=]\s*)([\"']?)([^,;\r\n]*?)\3(?=,|;|$)"
)


def _replace_sensitive_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{match.group(3)}***{match.group(3)}"


def redact_sensitive_payload(data: Any) -> Any:
    """Return data with sensitive key values redacted."""
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        sensitive_field_attr = (
            "attribute" in data
            and "value" in data
            and isinstance(data.get("attribute"), str)
            and any(pattern in data["attribute"].lower() for pattern in SENSITIVE_KEYS)
        )
        for key, value in data.items():
            key_str = str(key)
            key_lower = key_str.lower()
            if (sensitive_field_attr and key_str == "value") or any(
                pattern in key_lower for pattern in SENSITIVE_KEYS
            ):
                redacted[key_str] = "***"
            else:
                redacted[key_str] = redact_sensitive_payload(value)
        return redacted

    if isinstance(data, list):
        return [redact_sensitive_payload(item) for item in data]

    if isinstance(data, tuple):
        return tuple(redact_sensitive_payload(item) for item in data)

    if isinstance(data, str):
        return _SENSITIVE_PATTERN.sub(_replace_sensitive_match, data)

    return data
