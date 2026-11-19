"""Security utilities."""

import functools
import re
from typing import Any

from imednet.errors import ClientError, PathTraversalValidationError

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")
_CAMEL_TO_SNAKE_RE1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_TO_SNAKE_RE2 = re.compile(r"([a-z\d])([A-Z])")
_DELIMITER_RE = re.compile(r"[^a-zA-Z0-9]+")


def _normalize_key_uncached(key: Any) -> str:
    """Normalize a field key to canonical lowercase snake_case without caching."""
    if key is None:
        return ""
    s = str(key).strip()
    if not s:
        return ""
    s = _CAMEL_TO_SNAKE_RE1.sub(r"\1_\2", s)
    s = _CAMEL_TO_SNAKE_RE2.sub(r"\1_\2", s)
    s = _DELIMITER_RE.sub("_", s)
    return s.strip("_").lower()


@functools.lru_cache(maxsize=4096)
def _normalize_key_cached(key: Any) -> str:
    """LRU-cached key normalization helper."""
    return _normalize_key_uncached(key)


def _normalize_key(key: Any) -> str:
    """Normalize a field key to canonical lowercase snake_case.

    Converts camelCase, PascalCase, uppercase acronyms, and delimited names
    (hyphens, whitespace, etc.) into lowercase snake_case. Non-string inputs
    are converted to strings first.
    """
    try:
        return _normalize_key_cached(key)
    except TypeError:
        return _normalize_key_uncached(key)


_normalize_key.cache_info = _normalize_key_cached.cache_info  # type: ignore[attr-defined]
_normalize_key.cache_clear = _normalize_key_cached.cache_clear  # type: ignore[attr-defined]


class SensitivityRegistry:
    """Registry for managing sensitive fields (PHI) that should be masked in logs."""

    def __init__(self) -> None:
        """Initialize the registry with default sensitive and exempt keys."""
        # Default clinical PHI fields
        self._sensitive_keys = {
            "patient_name",
            "patient_initials",
            "dob",
            "date_of_birth",
            "ssn",
            "phone",
            "email",
            "address",
            "birth_date",
        }
        # Keys explicitly exempted from masking
        self._exempt_keys = {"subject_key", "study_key", "record_id"}

    def add_sensitive_key(self, key: Any) -> None:
        """Add a key to the sensitive list if it is not exempted."""
        norm_key = _normalize_key(key)
        if norm_key and norm_key not in self._exempt_keys:
            self._sensitive_keys.add(norm_key)

    def remove_sensitive_key(self, key: Any) -> None:
        """Remove a key from the sensitive list."""
        self._sensitive_keys.discard(_normalize_key(key))

    def add_exempt_key(self, key: Any) -> None:
        """Add a key to the exemption list."""
        norm_key = _normalize_key(key)
        if norm_key:
            self._exempt_keys.add(norm_key)

    def remove_exempt_key(self, key: Any) -> None:
        """Remove a key from the exemption list."""
        self._exempt_keys.discard(_normalize_key(key))

    def is_exempt(self, key: Any) -> bool:
        """Check if a key is explicitly exempted from PHI masking."""
        norm_key = _normalize_key(key)
        return bool(norm_key and norm_key in self._exempt_keys)

    def is_sensitive(self, key: Any) -> bool:
        """Check if a key is considered sensitive and not exempted."""
        norm_key = _normalize_key(key)
        if not norm_key or norm_key in self._exempt_keys:
            return False
        return norm_key in self._sensitive_keys


global_sensitivity_registry = SensitivityRegistry()


def mask_clinical_phi(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Recursively mask sensitive keys in unstructured data."""
    active_registry = global_sensitivity_registry if registry is None else registry
    if isinstance(value, dict):
        return {
            k: (
                "***MASKED***"
                if active_registry.is_sensitive(k)
                else mask_clinical_phi(v, registry=active_registry)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_clinical_phi(v, registry=active_registry) for v in value]
    if isinstance(value, tuple):
        return tuple(mask_clinical_phi(v, registry=active_registry) for v in value)
    return value


def sanitize_csv_formula(value: Any) -> Any:
    """Sanitize a value to prevent CSV/Excel Formula Injection.

    Prefixes strings starting with =, +, -, or @ (even after whitespace) with a single quote.
    Lists and tuples are recursively sanitized.
    """
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    if isinstance(value, list):
        return [sanitize_csv_formula(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_csv_formula(v) for v in value)
    return value


def validate_header_value(value: str) -> None:
    """Validate that a header value does not contain newline characters.

    Args:
        value: The header value to validate.

    Raises:
        ClientError: If the value contains newline characters.
    """
    if "\n" in value or "\r" in value:
        raise ClientError(f"Header value must not contain newlines: {value!r}")


def validate_partition_key(key: str) -> None:
    """Reject partition keys that could escape or reshape the target directory."""
    if "\x00" in key:
        raise PathTraversalValidationError(f"Partition key must not contain null bytes: {key!r}")
    if key.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(key):
        raise PathTraversalValidationError(
            f"Partition key must not use absolute path modifiers: {key!r}"
        )
    if "../" in key or "..\\" in key or key == "..":
        raise PathTraversalValidationError(
            f"Partition key must not contain parent-directory traversal: {key!r}"
        )
    if "/" in key or "\\" in key:
        raise PathTraversalValidationError(
            f"Partition key must not contain path separators: {key!r}"
        )
