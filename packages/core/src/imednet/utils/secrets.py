"""Secret redaction utilities."""

import base64
import json
import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passphrase",
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

_CAMEL_BOUNDARY_RE1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_RE2 = re.compile(r"([a-z\d])([A-Z])")
_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")

_EXACT_SENSITIVE_KEYS = frozenset(
    {
        *(k.lower().replace("-", "_") for k in SENSITIVE_KEYS),
        "passphrases",
        "passwd",
        "pwd",
        "tokens",
        "secrets",
        "credentials",
        "authkey",
        "accesskey",
        "securitykey",
        "privatekey",
        "secret_key",
        "secretkey",
        "csrf_key",
        "csrfkey",
        "clientsecret",
        "access_token",
        "accesstoken",
        "refresh_token",
        "refreshtoken",
        "id_token",
        "idtoken",
        "oidc_token",
        "oidctoken",
        "api_token",
        "apitoken",
        "auth_token",
        "authtoken",
        "bearer_token",
        "session_token",
        "csrf_token",
    }
)

_NON_SECRET_SUFFIX_TOKENS = frozenset(
    {
        "id",
        "ids",
        "uuid",
        "guid",
        "count",
        "total",
        "length",
        "type",
        "status",
        "flag",
        "enabled",
        "disabled",
        "required",
        "expiry",
        "expires",
        "at",
        "date",
        "time",
        "url",
        "uri",
    }
)

_SENSITIVE_ANYWHERE_TOKENS = frozenset(
    {
        "password",
        "passwords",
        "passphrase",
        "passphrases",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "authorization",
        "apikey",
        "securitykey",
        "authkey",
        "accesskey",
        "privatekey",
        "secretkey",
        "csrfkey",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "oidctoken",
        "apitoken",
        "authtoken",
    }
)

_SENSITIVE_KEY_MODIFIERS = frozenset(
    {
        "api",
        "security",
        "auth",
        "access",
        "private",
        "secret",
        "signing",
        "encryption",
        "csrf",
    }
)

_EXPLICIT_CREDENTIAL_KEY_EXPR = (
    r"x-imn-security-key|x-api-key|"
    r"(?:[a-z0-9]+[_-]+)*(?:api[_-]?key|security[_-]?key|auth[_-]?key|access[_-]?key|"
    r"private[_-]?key|secret[_-]?key|csrf[_-]?key|client[_-]?secret|"
    r"oidc[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?token|"
    r"auth[_-]?token|bearer[_-]?token|session[_-]?token|csrf[_-]?token)|"
    r"(?:[a-z0-9]+[_-]+)+(?:token|tokens|secret|secrets|password|passwords|passwd|passphrase|passphrases|credential|credentials)|"
    r"authorization"
)
_GENERIC_CREDENTIAL_KEY_EXPR = r"token|tokens|password|passwords|passwd|pwd|passphrase|passphrases|secret|secrets|credential|credentials"
_ALL_CREDENTIAL_KEY_EXPR = rf"{_EXPLICIT_CREDENTIAL_KEY_EXPR}|{_GENERIC_CREDENTIAL_KEY_EXPR}"

_QUOTED_SENSITIVE_PATTERN = re.compile(
    rf"(?i)(?<![a-z0-9_])([\"']?)(?<![a-z0-9_])({_ALL_CREDENTIAL_KEY_EXPR})(?![a-z0-9_])\1(\s*[:=]\s*)([\"'])([^\"'\r\n]*)\4"
)
_EQUALS_SENSITIVE_PATTERN = re.compile(
    rf"(?i)(?<![a-z0-9_])({_ALL_CREDENTIAL_KEY_EXPR})(?![a-z0-9_])(\s*=\s*)"
    r"(?:(?:bearer|basic)\s+)?([^\s,;&\"')\]}\x00]+)"
)
_HEADER_COLON_SENSITIVE_PATTERN = re.compile(
    rf"(?i)(?<![a-z0-9_])({_EXPLICIT_CREDENTIAL_KEY_EXPR})(?![a-z0-9_])(\s*:\s*)"
    r"(?:(?:bearer|basic)\s+)?([^\s,;&\"')\]}\x00]+)"
)
_STANDALONE_COLON_SENSITIVE_PATTERN = re.compile(
    rf"(?i)(?<![a-z0-9_])({_GENERIC_CREDENTIAL_KEY_EXPR})(?![a-z0-9_])(\s*:\s*)"
    r"([^\s,;&\"')\]}\x00]+)(?=\s*(?:[,;&)\]}\r\n\x00]|$))"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)([^\s,;&\"')\]}\x00]+)")
_STANDALONE_JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(eyJ[A-Za-z0-9_-]+)"
    r"(?:\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)(?![A-Za-z0-9_-])"
)


def _tokenize_key(key: str) -> tuple[str, ...]:
    """Split a key or identifier into normalized lowercase word tokens."""
    stripped = key.strip()
    if not stripped:
        return ()
    separated = _CAMEL_BOUNDARY_RE1.sub(r"\1_\2", stripped)
    separated = _CAMEL_BOUNDARY_RE2.sub(r"\1_\2", separated)
    return tuple(part.lower() for part in _TOKEN_SPLIT_RE.split(separated) if part)


def is_sensitive_key(key: Any) -> bool:
    """Return True when a field, header, or variable name denotes a secret credential."""
    if not isinstance(key, str):
        key = str(key)
    tokens = _tokenize_key(key)
    if not tokens:
        return False

    joined = "_".join(tokens)
    if joined in _EXACT_SENSITIVE_KEYS:
        return True

    if tokens[-1] in _NON_SECRET_SUFFIX_TOKENS:
        return False

    if any(tok in _SENSITIVE_ANYWHERE_TOKENS for tok in tokens):
        return True

    if tokens[-1] in {"token", "tokens"} or tokens[-2:] in {
        ("token", "value"),
        ("token", "val"),
    }:
        return True

    return any(
        tokens[idx] in _SENSITIVE_KEY_MODIFIERS and tokens[idx + 1] in {"key", "keys"}
        for idx in range(len(tokens) - 1)
    )


def _replace_quoted_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{match.group(1)}{match.group(3)}{match.group(4)}***{match.group(4)}"


def _replace_unquoted_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}***"


def _replace_bearer_match(match: re.Match[str]) -> str:
    return f"{match.group(1)}***"


def _is_valid_jwt_header(header_segment: str) -> bool:
    """Return True when *header_segment* is a valid base64url JSON object starting with ``eyJ``."""
    if not header_segment.startswith("eyJ") or len(header_segment) % 4 == 1:
        return False
    padded = header_segment + ("=" * (-len(header_segment) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, RecursionError):
        return False
    return isinstance(parsed, dict) and bool(parsed)


def _replace_jwt_match(match: re.Match[str]) -> str:
    if _is_valid_jwt_header(match.group(1)):
        return "***"
    return match.group(0)


def _has_sensitive_field_target(data: Mapping[Any, Any]) -> bool:
    if "value" not in data:
        return False
    for target_key in (
        "attribute",
        "variable",
        "variable_name",
        "variableName",
        "name",
        "field",
        "field_name",
        "fieldName",
    ):
        raw_target = data.get(target_key)
        if isinstance(raw_target, str) and is_sensitive_key(raw_target):
            return True
    return False


def redact_sensitive_payload(data: Any) -> Any:
    """Return data with sensitive key values redacted."""
    if isinstance(data, Mapping):
        redacted: dict[str, Any] = {}
        sensitive_field_attr = _has_sensitive_field_target(data)
        for key, value in data.items():
            key_str = str(key)
            if (sensitive_field_attr and key_str == "value") or is_sensitive_key(key_str):
                redacted[key_str] = "***"
            else:
                redacted[key_str] = redact_sensitive_payload(value)
        return redacted

    if isinstance(data, list):
        return [redact_sensitive_payload(item) for item in data]

    if isinstance(data, tuple):
        return tuple(redact_sensitive_payload(item) for item in data)

    if isinstance(data, str):
        redacted_text = _QUOTED_SENSITIVE_PATTERN.sub(_replace_quoted_match, data)
        redacted_text = _EQUALS_SENSITIVE_PATTERN.sub(_replace_unquoted_match, redacted_text)
        redacted_text = _HEADER_COLON_SENSITIVE_PATTERN.sub(_replace_unquoted_match, redacted_text)
        redacted_text = _STANDALONE_COLON_SENSITIVE_PATTERN.sub(
            _replace_unquoted_match, redacted_text
        )
        redacted_text = _BEARER_PATTERN.sub(_replace_bearer_match, redacted_text)
        if "eyJ" in redacted_text:
            redacted_text = _STANDALONE_JWT_PATTERN.sub(_replace_jwt_match, redacted_text)
        return redacted_text

    return data
