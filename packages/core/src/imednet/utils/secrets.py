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


_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)([^\s,;\"']+)")


def _replace_sensitive_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{match.group(3)}***{match.group(3)}"


def _replace_bearer_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}***"


def _has_sensitive_field_target(data: dict[str, Any]) -> bool:
    if "value" not in data:
        return False
    for target_key in ("attribute", "variable", "name", "field"):
        raw_target = data.get(target_key)
        if isinstance(raw_target, str):
            normalized = raw_target.lower().replace("-", "_")
            if any(pattern.replace("-", "_") in normalized for pattern in SENSITIVE_KEYS):
                return True
    return False


def redact_sensitive_payload(data: Any) -> Any:
    """Return data with sensitive key values redacted."""
    if isinstance(data, dict):
        redacted: dict[str, Any] = {}
        sensitive_field_attr = _has_sensitive_field_target(data)
        for key, value in data.items():
            key_str = str(key)
            key_lower = key_str.lower()
            key_norm = key_lower.replace("-", "_")
            if (sensitive_field_attr and key_str == "value") or any(
                pattern.replace("-", "_") in key_norm for pattern in SENSITIVE_KEYS
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
        redacted_text = _SENSITIVE_PATTERN.sub(_replace_sensitive_match, data)
        return _BEARER_PATTERN.sub(_replace_bearer_match, redacted_text)

    return data
