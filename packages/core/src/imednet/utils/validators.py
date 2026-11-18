"""Validation and normalization utilities for API response data."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from imednet.utils.dates import parse_iso_datetime  # Centralized date parsing

T = TypeVar("T")

# Pre-computed sets for boolean parsing optimization
_TRUE_LOWER = {"true", "1", "yes", "y", "t"}
_FALSE_LOWER = {"false", "0", "no", "n", "f"}
# Include common casing variants to avoid .strip().lower() allocation in hot paths
_TRUE_VARIANTS = _TRUE_LOWER | {"True", "TRUE"}
_FALSE_VARIANTS = _FALSE_LOWER | {"False", "FALSE"}


# Sentinel value for empty timestamps to avoid allocation overhead
_SENTINEL_DATETIME = datetime(1969, 4, 20, 16, 20)


def is_missing_value(value: Any) -> bool:
    """Check if a value is None or an empty/whitespace-only string."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def is_boolean_token(value: str) -> bool:
    """Check if a string represents a boolean value (e.g., 'true', 'yes', '0')."""
    normalized = value.strip().lower()
    return normalized in _TRUE_LOWER or normalized in _FALSE_LOWER


def parse_datetime(v: str | int | float | datetime) -> datetime:
    """Parse an ISO datetime string, numeric timestamp, or return a sentinel value.

    The SDK historically returns ``datetime(1969, 4, 20, 16, 20)`` when a
    timestamp field is empty. This helper mirrors that behaviour for backward
    compatibility.

    Args:
        v: Date string, numeric timestamp (seconds since epoch), or datetime object.
           Numeric values are assumed to be UTC timestamps.
    """
    if is_missing_value(v):
        return _SENTINEL_DATETIME
    if isinstance(v, str):
        return parse_iso_datetime(v.strip())
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=v)
    return v


_drift_reported: set[str] = set()


def parse_bool(v: Any) -> bool:
    """Normalize boolean values from various representations.

    Accepts bool, str, int, float and returns a bool.

    Defaults to False for unknown or unparsable types (e.g. None, [], object()).
    String representations like '1.0', 'inf', and 'nan' are treated as truthy
    via float fallback.

    Example:
        >>> from imednet.utils.validators import parse_bool
        >>> parse_bool("yes")
        True
        >>> parse_bool("1.0")
        True
        >>> parse_bool(None)
        False
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        # Gracefully handle empty string without logging drift (common API artifact)
        if not v or v.strip() == "":
            return False

        # Optimized path for common API responses to avoid string manipulation
        if v in _TRUE_VARIANTS:
            return True
        if v in _FALSE_VARIANTS:
            return False

        # Fallback for irregular casing or whitespace
        val = v.strip().lower()
        if val in _TRUE_LOWER:
            return True
        if val in _FALSE_LOWER:
            return False

        # Fallback: Try float conversion (for "1.0", "inf", "nan")
        # Optimization: Only attempt if it looks numeric or special float
        # to avoid try/except overhead on common strings like "apple"
        if val and (val[0].isdigit() or val[0] in ("-", "+", ".") or val[0] in ("n", "i")):
            try:
                return bool(float(val))
            except (ValueError, TypeError):
                pass

        import logging

        msg = f"Drift detected (destructive): type-changed field. Expected bool, got str (value: {v!r})"
        drift_key = "Expected bool, got str"
        if drift_key not in _drift_reported:
            _drift_reported.add(drift_key)
            logging.getLogger("imednet.drift").warning(msg)
    elif isinstance(v, (int, float)):
        return bool(v)
    elif v is not None:
        import logging

        msg = f"Drift detected (destructive): type-changed field. Expected bool, got {type(v).__name__} (value: {v!r})"
        drift_key = f"Expected bool, got {type(v).__name__}"
        if drift_key not in _drift_reported:
            _drift_reported.add(drift_key)
            logging.getLogger("imednet.drift").warning(msg)
    return False


def parse_int_or_default(v: Any, default: int = 0, strict: bool = False) -> int:
    """Normalize integer values, defaulting if None or empty string.

    If strict=True, raise ValueError on parse failure.
    """
    if is_missing_value(v):
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        # Fallback: Try float conversion (for "123.0", 5.0)
        try:
            return int(float(v))
        except (ValueError, TypeError, OverflowError):
            if strict:
                raise
            import logging

            msg = f"Drift detected (destructive): type-changed field. Expected int, got {type(v).__name__} (value: {v!r})"
            drift_key = f"Expected int, got {type(v).__name__}"
            if drift_key not in _drift_reported:
                _drift_reported.add(drift_key)
                logging.getLogger("imednet.drift").warning(msg)
            return default


def parse_str_or_default(v: Any, default: str = "", strict: bool = False) -> str:
    """Normalize string values, defaulting if None.

    Guards against list and dict container values by emitting a drift warning
    and raising ValueError when strict=True or IMEDNET_STRICT_MODE is enabled.
    """
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        import logging
        import os

        msg = (
            f"Drift detected (destructive): type-changed field. "
            f"Expected str, got {type(v).__name__} (value: {v!r})"
        )
        logging.getLogger("imednet.drift").warning(msg)
        if strict or parse_bool(os.getenv("IMEDNET_STRICT_MODE", "false")):
            raise ValueError(msg)
        return default
    return str(v)


def parse_list_or_default(v: Any, default_factory: Callable[[], list[T]] = list) -> list[T]:
    """Normalize list values, defaulting if None. Ensures result is a list."""
    if is_missing_value(v):
        return default_factory()
    if isinstance(v, list):
        return v
    import logging

    logging.getLogger(__name__).warning(
        "Structural shift detected: API returned an object where a list was expected. Coercing by wrapping in a list."
    )
    return [v]


def parse_dict_or_default(
    v: Any, default_factory: Callable[[], dict[str, Any]] = dict
) -> dict[str, Any]:
    """Normalize dictionary values, defaulting if None. Ensures result is a dict."""
    if is_missing_value(v):
        return default_factory()
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        if len(v) > 0 and isinstance(v[0], dict):
            import logging

            logging.getLogger(__name__).warning(
                "Structural shift detected: API returned a list where an object was expected. Coercing by extracting the first item."
            )
            return v[0]
        return default_factory()
    return default_factory()  # fallback if not a dict
