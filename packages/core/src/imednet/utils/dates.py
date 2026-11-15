"""Utility functions for parsing and formatting ISO date/time strings."""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime

# Python 3.11+ supports Z and any precision of fractional seconds in fromisoformat
_IS_PY311_OR_GREATER = sys.version_info >= (3, 11)

# Pre-compile regex for older python versions
# Breakdown of _ISO8601_FRAC_REGEX:
# \.               - Matches the literal dot preceding fractional seconds
# (\d+)            - Captures one or more digits (the fractional seconds)
# (?=              - Positive lookahead to ensure the fraction is followed by:
#   [+-]\d{2}:\d{2} - A timezone offset (e.g., +00:00, -05:00)
#   |               - OR
#   $               - The end of the string (e.g., implicitly UTC if 'Z' was stripped)
# )
_ISO8601_FRAC_REGEX = re.compile(r"\.(\d+)(?=[+-]\d{2}:\d{2}|$)")


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
