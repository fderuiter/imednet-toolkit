"""Security utilities."""

import functools
import math
import re
from datetime import date, datetime
from typing import Any

from imednet.errors import ClientError, PathTraversalValidationError

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")
_CAMEL_TO_SNAKE_RE1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_TO_SNAKE_RE2 = re.compile(r"([a-z\d])([A-Z])")
_DELIMITER_RE = re.compile(r"[^a-zA-Z0-9]+")
_BIRTH_DATE_KEYS: frozenset[str] = frozenset(
    {"dob", "date_of_birth", "birth_date", "brthdtc", "brthdat", "brthyr"}
)
_BIRTH_DATE_COLLAPSED_KEYS: frozenset[str] = frozenset(k.replace("_", "") for k in _BIRTH_DATE_KEYS)
_ISO_YEAR_ONLY_RE = re.compile(r"^(\d{4})(?:-(?:0[1-9]|1[0-2]|UN|UNK|-).*)?$", re.IGNORECASE)
_SLASH_OR_DASH_MDY_RE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-](\d{4})(?:\D.*)?$")
_SLASH_OR_DASH_YMD_RE = re.compile(r"^(\d{4})[/-]\d{1,2}[/-]\d{1,2}(?:\D.*)?$")
_TEXT_MONTH_DMY_RE = re.compile(r"^\d{1,2}-[A-Za-z]{3,9}-(\d{4})$")


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

    def _sync_collapsed_sets(self) -> None:
        """Resynchronize collapsed key sets if underlying sets were mutated directly."""
        if self._sensitive_keys != self._sens_snapshot:
            self._sens_snapshot = frozenset(self._sensitive_keys)
            self._collapsed_sensitive = {k.replace("_", "") for k in self._sensitive_keys}
        if self._exempt_keys != self._exempt_snapshot:
            self._exempt_snapshot = frozenset(self._exempt_keys)
            self._collapsed_exempt = {k.replace("_", "") for k in self._exempt_keys}

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

    def add_exempt_key(self, key: Any) -> None:
        """Add a key to the exemption list."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if norm_key:
            self._exempt_keys.add(norm_key)
            self._collapsed_exempt.add(norm_key.replace("_", ""))
            self._exempt_snapshot = frozenset(self._exempt_keys)

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
        norm_key = _normalize_key(key)
        if not norm_key:
            return False
        collapsed = norm_key.replace("_", "")
        if norm_key in self._exempt_keys or collapsed in self._collapsed_exempt:
            return False
        return norm_key in self._sensitive_keys or collapsed in self._collapsed_sensitive


global_sensitivity_registry = SensitivityRegistry()


def is_birth_date_key(key: Any) -> bool:
    """Return True if *key* represents a clinical or CDISC birth-date field."""
    norm_key = _normalize_key(key)
    if not norm_key:
        return False
    return norm_key in _BIRTH_DATE_KEYS or norm_key.replace("_", "") in _BIRTH_DATE_COLLAPSED_KEYS


def redact_birth_date_year(value: Any) -> str | None:
    """Redact a birth-date value to a four-digit ISO 8601 year or None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return f"{value.year:04d}"
    if isinstance(value, date):
        return f"{value.year:04d}"
    if isinstance(value, int):
        return f"{value:04d}" if 1000 <= value <= 9999 else None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "***MASKED***":
            return None
        if len(stripped) == 10 and stripped[4] == "-" and stripped[7] == "-":
            try:
                return f"{date.fromisoformat(stripped).year:04d}"
            except ValueError:
                return None
        try:
            return f"{datetime.fromisoformat(stripped).year:04d}"
        except ValueError:
            pass
        for pattern in (
            _SLASH_OR_DASH_YMD_RE,
            _SLASH_OR_DASH_MDY_RE,
            _TEXT_MONTH_DMY_RE,
            _ISO_YEAR_ONLY_RE,
        ):
            match = pattern.match(stripped)
            if match:
                return f"{int(match.group(1)):04d}"
        return None
    return None


def mask_clinical_phi(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
    preserve_sdtm_brthdtc: bool = False,
    preserve_none: bool = False,
) -> Any:
    """Recursively mask sensitive keys in unstructured data."""
    active_registry = global_sensitivity_registry if registry is None else registry
    if isinstance(value, dict):
        masked: dict[Any, Any] = {}
        for k, v in value.items():
            if active_registry.is_sensitive(k):
                if preserve_sdtm_brthdtc and is_birth_date_key(k):
                    masked[k] = redact_birth_date_year(v)
                elif preserve_none and v is None:
                    masked[k] = None
                else:
                    masked[k] = "***MASKED***"
            else:
                masked[k] = mask_clinical_phi(
                    v,
                    registry=active_registry,
                    preserve_sdtm_brthdtc=preserve_sdtm_brthdtc,
                    preserve_none=preserve_none,
                )
        return masked
    if isinstance(value, list):
        return [
            mask_clinical_phi(
                v,
                registry=active_registry,
                preserve_sdtm_brthdtc=preserve_sdtm_brthdtc,
                preserve_none=preserve_none,
            )
            for v in value
        ]
    if isinstance(value, tuple):
        return tuple(
            mask_clinical_phi(
                v,
                registry=active_registry,
                preserve_sdtm_brthdtc=preserve_sdtm_brthdtc,
                preserve_none=preserve_none,
            )
            for v in value
        )
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
