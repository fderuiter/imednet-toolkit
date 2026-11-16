"""Utility functions for parsing and formatting ISO date/time strings."""

from __future__ import annotations

import re
import sys
from datetime import UTC, date, datetime
from typing import Any

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


def normalize_iso_date(value: Any) -> Any:
    """Normalize a date, datetime, or date-like string into a canonical ISO 8601 string.

    If the value is not a date or date-like string, it is returned unmodified.

    Args:
        value: Input value to normalize.

    Returns:
        ISO 8601 formatted date/datetime string, or original value if not a date.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
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
        return f"{match_dt.group(1)}T{match_dt.group(2)}"

    match_ymd = _SLASH_YMD_REGEX.match(s)
    if match_ymd:
        try:
            y, mo, d = int(match_ymd.group(1)), int(match_ymd.group(2)), int(match_ymd.group(3))
            return date(y, mo, d).isoformat()
        except ValueError:
            return value

    match_mdy = _SLASH_MDY_REGEX.match(s)
    if match_mdy:
        try:
            mo, d, y = int(match_mdy.group(1)), int(match_mdy.group(2)), int(match_mdy.group(3))
            return date(y, mo, d).isoformat()
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
            if 1900 <= y <= 2100:
                return date(y, mo, d).isoformat()
        except ValueError:
            return value

    return value
