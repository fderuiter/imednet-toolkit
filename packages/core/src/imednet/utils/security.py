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
        # Default clinical and CDISC PHI fields
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
            "brthdtc",
            "brthdat",
            "brthyr",
            "subjinit",
        }
        # Keys explicitly exempted from masking
        self._exempt_keys = {
            "subject_key",
            "study_key",
            "record_id",
            "usubjid",
            "subjid",
            "studyid",
        }
        self._sens_snapshot = frozenset(self._sensitive_keys)
        self._exempt_snapshot = frozenset(self._exempt_keys)
        self._collapsed_sensitive = {k.replace("_", "") for k in self._sensitive_keys}
        self._collapsed_exempt = {k.replace("_", "") for k in self._exempt_keys}
        self._key_cache: dict[str, bool] = {}

    def _sync_collapsed_sets(self) -> None:
        """Resynchronize collapsed key sets if underlying sets were mutated directly."""
        if self._sensitive_keys != self._sens_snapshot:
            self._sens_snapshot = frozenset(self._sensitive_keys)
            self._collapsed_sensitive = {k.replace("_", "") for k in self._sensitive_keys}
            self._key_cache.clear()
        if self._exempt_keys != self._exempt_snapshot:
            self._exempt_snapshot = frozenset(self._exempt_keys)
            self._collapsed_exempt = {k.replace("_", "") for k in self._exempt_keys}
            self._key_cache.clear()

    def add_sensitive_key(self, key: Any) -> None:
        """Add a key to the sensitive list if it is not exempted."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return
        collapsed = norm_key.replace("_", "")
        if norm_key not in self._exempt_keys and collapsed not in self._collapsed_exempt:
            self._sensitive_keys.add(norm_key)
            self._collapsed_sensitive.add(collapsed)
            self._sens_snapshot = frozenset(self._sensitive_keys)
            self._key_cache.clear()

    def remove_sensitive_key(self, key: Any) -> None:
        """Remove a key from the sensitive list."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return
        collapsed = norm_key.replace("_", "")
        for existing in list(self._sensitive_keys):
            if existing == norm_key or existing.replace("_", "") == collapsed:
                self._sensitive_keys.discard(existing)
        self._collapsed_sensitive.discard(collapsed)
        self._sens_snapshot = frozenset(self._sensitive_keys)
        self._key_cache.clear()

    def add_exempt_key(self, key: Any) -> None:
        """Add a key to the exemption list."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if norm_key:
            self._exempt_keys.add(norm_key)
            self._collapsed_exempt.add(norm_key.replace("_", ""))
            self._exempt_snapshot = frozenset(self._exempt_keys)
            self._key_cache.clear()

    def remove_exempt_key(self, key: Any) -> None:
        """Remove a key from the exemption list."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return
        collapsed = norm_key.replace("_", "")
        for existing in list(self._exempt_keys):
            if existing == norm_key or existing.replace("_", "") == collapsed:
                self._exempt_keys.discard(existing)
        self._collapsed_exempt.discard(collapsed)
        self._exempt_snapshot = frozenset(self._exempt_keys)
        self._key_cache.clear()

    def is_exempt(self, key: Any) -> bool:
        """Check if a key is explicitly exempted from PHI masking."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return False
        return norm_key in self._exempt_keys or norm_key.replace("_", "") in self._collapsed_exempt

    def is_sensitive(self, key: Any) -> bool:
        """Check if a key is considered sensitive and not exempted."""
        self._sync_collapsed_sets()
        if key.__class__ is str:
            cached = self._key_cache.get(key)
            if cached is not None:
                return cached
        norm_key = _normalize_key(key)
        if not norm_key:
            res = False
        else:
            collapsed = norm_key.replace("_", "")
            if norm_key in self._exempt_keys or collapsed in self._collapsed_exempt:
                res = False
            else:
                res = norm_key in self._sensitive_keys or collapsed in self._collapsed_sensitive
        if key.__class__ is str:
            if len(self._key_cache) >= 8192:
                self._key_cache.clear()
            self._key_cache[key] = res
        return res

    def snapshot(self) -> tuple[frozenset[str], frozenset[str]]:
        """Return an immutable snapshot of active sensitive and exempt key sets."""
        self._sync_collapsed_sets()
        return (self._sens_snapshot, self._exempt_snapshot)


global_sensitivity_registry = SensitivityRegistry()


_FORMULA_PREFIX_CHARS = frozenset("=+-@")
_CONTAINER_TYPES = (dict, list, tuple)


def _mask_clinical_phi_synced(
    value: Any,
    active_registry: SensitivityRegistry,
    cache_get: Any,
) -> Any:
    """Recursively mask sensitive keys after syncing ``active_registry`` once."""
    vc = value.__class__
    if vc is dict or isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if k.__class__ is str:
                is_sens = cache_get(k)
                if is_sens is None:
                    is_sens = active_registry.is_sensitive(k)
            else:
                is_sens = active_registry.is_sensitive(k)
            if is_sens:
                out[k] = "***MASKED***"
            elif isinstance(v, _CONTAINER_TYPES):
                out[k] = _mask_clinical_phi_synced(v, active_registry, cache_get)
            else:
                out[k] = v
        return out
    if vc is list or isinstance(value, list):
        return [
            _mask_clinical_phi_synced(v, active_registry, cache_get)
            if isinstance(v, _CONTAINER_TYPES)
            else v
            for v in value
        ]
    if vc is tuple or isinstance(value, tuple):
        return tuple(
            _mask_clinical_phi_synced(v, active_registry, cache_get)
            if isinstance(v, _CONTAINER_TYPES)
            else v
            for v in value
        )
    return value


def mask_clinical_phi(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Recursively mask sensitive keys in unstructured data."""
    if not isinstance(value, _CONTAINER_TYPES):
        return value
    active_registry = global_sensitivity_registry if registry is None else registry
    active_registry._sync_collapsed_sets()
    return _mask_clinical_phi_synced(value, active_registry, active_registry._key_cache.get)


def sanitize_csv_formula(value: Any) -> Any:
    """Sanitize a value to prevent CSV/Excel Formula Injection.

    Prefixes strings starting with =, +, -, or @ (even after whitespace) with a single quote.
    Lists and tuples are recursively sanitized.
    """
    if value.__class__ is str:
        if not value:
            return value
        ch0 = value[0]
        if ch0 in _FORMULA_PREFIX_CHARS:
            return f"'{value}"
        if not ch0.isspace():
            return value
        stripped = value.lstrip()
        if stripped and stripped[0] in _FORMULA_PREFIX_CHARS:
            return f"'{value}"
        return value
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
