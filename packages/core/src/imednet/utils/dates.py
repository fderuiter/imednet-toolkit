"""Utility functions for parsing and formatting ISO date/time strings."""

from __future__ import annotations

import re
import sys
from datetime import UTC, date, datetime
from typing import Any, Literal

# Python 3.11+ supports Z and any precision of fractional seconds in fromisoformat
_IS_PY311_OR_GREATER = sys.version_info >= (3, 11)

# Pre-compile regex for older python versions
_ISO8601_FRAC_REGEX = re.compile(r"\.(\d+)(?=[+-]\d{2}:\d{2}|$)")

_MONTHS: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_ISO_DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_REGEX = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)$"
)
_SLASH_YMD_REGEX = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_SLASH_MDY_REGEX = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_CDISC_DMY_REGEX = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_COMPACT_DATE_REGEX = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def parse_iso_datetime(date_str: str) -> datetime:
    """Parse an ISO 8601 date/time string into a datetime object.

    Handles timestamps ending with 'Z' as UTC.

    Args:
        date_str: ISO 8601 formatted date/time string.

    Returns:
        A timezone-aware datetime object.

    Raises:
        ValueError: If the input string is not a valid ISO format.
    """
    if _IS_PY311_OR_GREATER:
        # Python 3.11+ handles Z suffix and arbitrary precision natively
        return datetime.fromisoformat(date_str)

    if date_str.endswith("Z"):
        date_str = date_str[:-1] + "+00:00"

    match = _ISO8601_FRAC_REGEX.search(date_str)
    if match:
        frac = match.group(1)
        if 1 <= len(frac) <= 5:
            date_str = date_str.replace("." + frac, "." + frac.ljust(6, "0"))
        elif len(frac) > 6:
            date_str = date_str.replace("." + frac, "." + frac[:6])

    return datetime.fromisoformat(date_str)


def format_iso_datetime(dt: datetime) -> str:
    """Format a datetime object into an ISO 8601 string ending with 'Z'.

    Args:
        dt: datetime object (naive or timezone-aware).

    Returns:
        A string representing the datetime in ISO 8601 format.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_iso_date(value: Any, *, date_order: Literal["MDY", "DMY"] | None = None) -> Any:
    """Normalize a date, datetime, or date-like string into a canonical ISO 8601 string.

    Use only for explicitly selected date fields. Invalid, ambiguous and partial
    strings are returned unchanged; no components are imputed. Slash dates with
    two possible interpretations require date_order. Timezone offsets and source
    fractional precision are preserved.

    Args:
        value: Input value to normalize.
        date_order: Explicit ordering for ambiguous slash dates.

    Returns:
        ISO 8601 formatted date/datetime string, or original value if not a date.
    """
    if date_order not in (None, "MDY", "DMY"):
        raise ValueError("date_order must be MDY, DMY or None")
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if not isinstance(value, str):
        return value

    s = value.strip()
    if not s:
        return value

    if _ISO_DATE_REGEX.match(s):
        try:
            date.fromisoformat(s)
            return s
        except ValueError:
            return value

    match_dt = _ISO_DATETIME_REGEX.match(s)
    if match_dt:
        candidate = f"{match_dt.group(1)}T{match_dt.group(2)}"
        try:
            datetime.fromisoformat(candidate)
        except ValueError:
            return value
        offset = re.search(r"[+-](\d{2}):?(\d{2})$", candidate)
        if offset and (int(offset.group(1)) > 23 or int(offset.group(2)) > 59):
            return value
        return candidate

    match_ymd = _SLASH_YMD_REGEX.match(s)
    if match_ymd:
        try:
            y, mo, d = int(match_ymd.group(1)), int(match_ymd.group(2)), int(match_ymd.group(3))
            return date(y, mo, d).isoformat()
        except ValueError:
            return value

    match_mdy = _SLASH_MDY_REGEX.fullmatch(s)
    if match_mdy:
        first, second, year = map(int, match_mdy.groups())
        if date_order is None and first <= 12 and second <= 12 and first != second:
            return value
        month, day = (
            (second, first)
            if date_order == "DMY" or (date_order is None and first > 12)
            else (first, second)
        )
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return value

    match_cdisc = _CDISC_DMY_REGEX.match(s)
    if match_cdisc:
        mon_str = match_cdisc.group(2).upper()
        if mon_str in _MONTHS:
            try:
                d, mo, y = int(match_cdisc.group(1)), _MONTHS[mon_str], int(match_cdisc.group(3))
                return date(y, mo, d).isoformat()
            except ValueError:
                return value

    match_compact = _COMPACT_DATE_REGEX.match(s)
    if match_compact:
        try:
            y, mo, d = (
                int(match_compact.group(1)),
                int(match_compact.group(2)),
                int(match_compact.group(3)),
            )
            return date(y, mo, d).isoformat()
        except ValueError:
            return value

    return value


def normalize_utc_timestamp_bound(value: str, *, is_end: bool = False) -> str:
    """Normalize a date or timestamp string to an ISO 8601 UTC timestamp bound.

    Date-only inputs expand to ``T00:00:00Z`` when ``is_end`` is ``False`` and
    ``T23:59:59Z`` when ``is_end`` is ``True``. Full timestamps are converted
    to UTC ending with ``Z``.

    Args:
        value: Date or ISO 8601 datetime string.
        is_end: Whether to use end-of-day time for date-only inputs.

    Returns:
        Canonical ISO 8601 UTC timestamp string ending with ``Z``.

    Raises:
        ValueError: If ``value`` is empty or cannot be parsed as a valid date or datetime.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Date filter bound must be a non-empty ISO 8601 date or timestamp string")

    stripped = value.strip()
    normalized = normalize_iso_date(stripped)
    candidate = normalized if isinstance(normalized, str) else stripped

    if _ISO_DATE_REGEX.fullmatch(candidate):
        date.fromisoformat(candidate)
        suffix = "T23:59:59Z" if is_end else "T00:00:00Z"
        return f"{candidate}{suffix}"

    dt = parse_iso_datetime(candidate)
    return format_iso_datetime(dt)
