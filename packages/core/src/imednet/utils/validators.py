"""Validation and normalization utilities for API response data."""

from __future__ import annotations

import math
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

_drift_reported: set[str] = set()


def _effective_strict(strict: bool | None) -> bool:
    """Resolve explicit strict override or active strict-mode context."""
    if strict is not None:
        return strict
    from imednet.core.context import is_strict_mode

    return is_strict_mode()


def _report_drift(drift_key: str) -> None:
    """Emit a deduplicated type-only destructive drift warning."""
    import logging

    msg = f"Drift detected (destructive): type-changed field. {drift_key}"
    if drift_key not in _drift_reported:
        _drift_reported.add(drift_key)
        logging.getLogger("imednet.drift").warning(msg)


def is_missing_value(value: Any) -> bool:
    """Check if a value is None or an empty/whitespace-only string."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def is_boolean_token(value: str) -> bool:
    """Check if a string represents a boolean value (e.g., 'true', 'yes', '0')."""
    normalized = value.strip().lower()
    return normalized in _TRUE_LOWER or normalized in _FALSE_LOWER


def parse_datetime(
    v: str | int | float | datetime,
    *,
    strict: bool | None = None,
) -> datetime:
    """Parse an ISO datetime string, numeric timestamp, or return a sentinel value.

    In permissive mode, returns ``datetime(1969, 4, 20, 16, 20)`` when a timestamp
    field is empty. In strict mode, raises ``ValueError`` on empty or invalid values.

    Args:
        v: Date string, numeric timestamp (seconds since epoch), or datetime object.
           Numeric values are assumed to be UTC timestamps.
        strict: Optional explicit strict-mode override.
    """
    use_strict = _effective_strict(strict)
    if isinstance(v, datetime):
        return v
    if isinstance(v, bool):
        if use_strict:
            raise ValueError("Expected datetime, got bool")
        _report_drift("Expected datetime, got bool")
        return _SENTINEL_DATETIME
    if is_missing_value(v):
        if use_strict:
            raise ValueError(f"Expected non-empty timestamp, got {type(v).__name__}")
        return _SENTINEL_DATETIME
    if isinstance(v, str):
        return parse_iso_datetime(v.strip())
    if isinstance(v, (int, float)):
        if isinstance(v, float) and not math.isfinite(v):
            if use_strict:
                raise ValueError("Expected finite timestamp, got non-finite float")
            _report_drift("Expected datetime, got non-finite float")
            return _SENTINEL_DATETIME
        try:
            return datetime.fromtimestamp(v, tz=UTC)
        except (OSError, OverflowError, ValueError):
            try:
                return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=v)
            except (OverflowError, ValueError) as exc:
                if use_strict:
                    raise ValueError("Timestamp out of range") from exc
                return _SENTINEL_DATETIME
    if use_strict:
        raise ValueError(f"Expected datetime, got {type(v).__name__}")
    return v


def parse_bool(v: Any, strict: bool | None = None) -> bool:
    """Normalize boolean values from various representations.

    Accepts bool, str, int, and finite float values in permissive mode and returns
    ``False`` for unknown, unparsable, ``NaN``, or infinite inputs. Raises
    ``ValueError`` on ``NaN``, infinity, or invalid boolean inputs in strict mode.

    Example:
        >>> from imednet.utils.validators import parse_bool
        >>> parse_bool("yes")
        True
        >>> parse_bool("1.0")
        True
        >>> parse_bool(None)
        False
    """
    use_strict = _effective_strict(strict)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        # Gracefully handle empty string in permissive mode; reject in strict mode
        if not v or v.strip() == "":
            if use_strict:
                raise ValueError("Expected bool, got empty str")
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

        # Check numeric / special float strings ("1.0", "inf", "nan")
        if val and (val[0].isdigit() or val[0] in ("-", "+", ".", "n", "i")):
            try:
                num = float(val)
            except (ValueError, TypeError):
                pass
            else:
                if not math.isfinite(num):
                    if use_strict:
                        raise ValueError("Expected bool, got non-finite float string")
                    _report_drift("Expected bool, got non-finite float string")
                    return False
                if not use_strict:
                    return bool(num)

        if use_strict:
            raise ValueError("Expected bool, got invalid str")
        _report_drift("Expected bool, got str")
        return False

    if isinstance(v, float):
        if not math.isfinite(v):
            if use_strict:
                raise ValueError("Expected bool, got non-finite float")
            _report_drift("Expected bool, got non-finite float")
            return False
        if use_strict:
            raise ValueError("Expected bool, got float")
        return bool(v)

    if isinstance(v, int):
        if use_strict:
            raise ValueError("Expected bool, got int")
        return bool(v)

    if use_strict:
        raise ValueError(f"Expected bool, got {type(v).__name__}")
    if v is not None:
        _report_drift(f"Expected bool, got {type(v).__name__}")
    return False


def parse_int_or_default(v: Any, default: int = 0, strict: bool | None = None) -> int:
    """Normalize integer values, defaulting if None or empty string in permissive mode.

    Rejects ``bool`` inputs across all modes, and raises ``ValueError`` (or
    ``OverflowError`` on string infinity) when ``strict=True`` or strict mode is active.
    """
    use_strict = _effective_strict(strict)
    if isinstance(v, bool):
        if use_strict:
            raise ValueError("Expected int, got bool")
        _report_drift("Expected int, got bool")
        return default
    if is_missing_value(v):
        if use_strict:
            raise ValueError(f"Expected int, got {type(v).__name__}")
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if use_strict:
            raise ValueError("Float truncation is not allowed for int in strict mode")
        if not math.isfinite(v):
            _report_drift("Expected int, got non-finite float")
            return default
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            try:
                num = float(s)
            except ValueError:
                if use_strict:
                    raise ValueError("Expected int, got invalid str") from None
                _report_drift("Expected int, got str")
                return default
            if math.isinf(num):
                if use_strict:
                    return int(num)
                _report_drift("Expected int, got non-finite float")
                return default
            if math.isnan(num):
                if use_strict:
                    raise ValueError("Expected int, got NaN") from None
                _report_drift("Expected int, got NaN")
                return default
            if use_strict:
                raise ValueError("Float truncation is not allowed for int in strict mode") from None
            return int(num)
    if use_strict:
        raise ValueError(f"Expected int, got {type(v).__name__}")
    _report_drift(f"Expected int, got {type(v).__name__}")
    return default


def _guard_str_container(
    v: list[Any] | dict[Any, Any],
    *,
    strict: bool | None = None,
) -> None:
    """Emit a type-only drift warning for container values in string fields and raise in strict mode."""
    import logging

    msg = f"Drift detected (destructive): type-changed field. Expected str, got {type(v).__name__}"
    logging.getLogger("imednet.drift").warning(msg)
    if _effective_strict(strict):
        raise ValueError(msg)


def parse_str_or_default(v: Any, default: str = "", strict: bool | None = None) -> str:
    """Normalize string values, defaulting if None.

    Guards against list and dict container values by emitting a drift warning
    and raising ValueError when strict=True or IMEDNET_STRICT_MODE is enabled.
    """
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        _guard_str_container(v, strict=strict)
        return default
    return str(v)


def parse_list_or_default(
    v: Any,
    default_factory: Callable[[], list[T]] = list,
    *,
    strict: bool | None = None,
) -> list[T]:
    """Normalize list values, defaulting if None in permissive mode.

    Raises ``ValueError`` in strict mode when ``v`` is not a ``list``.
    """
    use_strict = _effective_strict(strict)
    if isinstance(v, list):
        return v
    if use_strict:
        raise ValueError(f"Expected list, got {type(v).__name__}")
    if is_missing_value(v):
        return default_factory()
    import logging

    logging.getLogger(__name__).warning(
        "Structural shift detected: API returned an object where a list was expected. Coercing by wrapping in a list."
    )
    return [v]


def parse_dict_or_default(
    v: Any,
    default_factory: Callable[[], dict[str, Any]] = dict,
    *,
    strict: bool | None = None,
) -> dict[str, Any]:
    """Normalize dictionary values, defaulting if None in permissive mode.

    Raises ``ValueError`` in strict mode when ``v`` is not a ``dict``.
    """
    use_strict = _effective_strict(strict)
    if isinstance(v, dict):
        return v
    if use_strict:
        raise ValueError(f"Expected dict, got {type(v).__name__}")
    if is_missing_value(v):
        return default_factory()
    if isinstance(v, list):
        if len(v) > 0 and isinstance(v[0], dict):
            import logging

            logging.getLogger(__name__).warning(
                "Structural shift detected: API returned a list where an object was expected. Coercing by extracting the first item."
            )
            return v[0]
        return default_factory()
    return default_factory()  # fallback if not a dict
