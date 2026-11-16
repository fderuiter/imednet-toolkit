"""Utility functions for building API filter strings.

This module provides functionality to construct filter query parameters
for iMednet API endpoints based on the reference documentation.
"""

import functools
import re
from typing import Any, TypeVar

# Pre-compiled regex for performance to avoid re-compilation in loops
_UNSAFE_CHARS_REGEX = re.compile(r"[^A-Za-z0-9_.-]")

ALLOWED_OPERATORS = frozenset({"<", "<=", ">", ">=", "==", "!=", "=~"})

T = TypeVar("T")


@functools.lru_cache(maxsize=128)
def _snake_to_camel(text: str) -> str:
    """Convert a snake_case string to camelCase."""
    if "_" not in text:
        return text
    parts = text.split("_")
    first, rest = parts[0], parts[1:]
    return first + "".join(word.capitalize() for word in rest)


def _scan_filter_string(filter_str: str) -> tuple[bool, bool, bool, bool]:
    """Scan filter string returning (has_semi, has_comma, has_whitespace, in_quotes)."""
    in_quotes = False
    escape = False
    has_semicolon = False
    has_comma = False
    has_whitespace = False

    for char in filter_str:
        if escape:
            escape = False
            continue
        if char == "\\" and in_quotes:
            escape = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes:
            if char == ";":
                has_semicolon = True
            elif char == ",":
                has_comma = True
            elif char.isspace():
                has_whitespace = True

    return has_semicolon, has_comma, has_whitespace, in_quotes


def validate_filter_operator(operator: str) -> None:
    """Validate that an operator matches allowed tokens and contains no whitespace.

    Args:
        operator: The operator string to validate.

    Raises:
        ValueError: If operator contains whitespace or is not in ALLOWED_OPERATORS.
    """
    if not isinstance(operator, str) or not operator:
        raise ValueError("Filter operator must be a non-empty string")
    if any(c.isspace() for c in operator):
        raise ValueError(f"Filter operator '{operator}' cannot contain whitespace")
    if operator not in ALLOWED_OPERATORS:
        raise ValueError(
            f"Invalid filter operator '{operator}'. Allowed operators: {sorted(ALLOWED_OPERATORS)}"
        )


def validate_filter_key(key: str) -> None:
    """Validate that a filter key is non-empty and contains no whitespace.

    Args:
        key: The key string to validate.

    Raises:
        ValueError: If key is not a non-empty string or contains whitespace.
    """
    if not isinstance(key, str) or not key or not key.strip():
        raise ValueError("Filter key must be a non-empty string")
    if any(c.isspace() for c in key):
        raise ValueError(f"Filter key '{key}' cannot contain whitespace")


def validate_connector_exclusivity(filter_str: str) -> None:
    """Validate that a filter expression does not combine both ';' and ',' connectors.

    Connectors inside double quotation marks are ignored. The literal string
    'none' (case-insensitive) bypasses validation.

    Args:
        filter_str: The filter expression string to validate.

    Raises:
        ValueError: If both ';' and ',' connectors appear outside quotation marks,
            or if quotation marks are unclosed.
    """
    if not isinstance(filter_str, str):
        raise ValueError(f"Filter expression must be a string, got {type(filter_str).__name__}")

    stripped = filter_str.strip()
    if not stripped or stripped.lower() == "none":
        return

    has_semi, has_comma, _, in_quotes = _scan_filter_string(filter_str)
    if in_quotes:
        raise ValueError("Filter expression contains unclosed quotation marks")
    if has_semi and has_comma:
        raise ValueError("Filter expression cannot combine both ';' (AND) and ',' (OR) connectors")


def validate_filter_syntax(filter_str: str) -> None:
    """Validate a complete filter expression or recordDataFilter for syntax constraints.

    Verifies connector exclusivity and ensures no whitespace exists outside
    of double-quoted strings.

    Args:
        filter_str: The filter expression string to validate.

    Raises:
        ValueError: If connectors are combined, unclosed quotes exist, or
            whitespace appears outside quotation marks.
    """
    if not isinstance(filter_str, str):
        raise ValueError(f"Filter expression must be a string, got {type(filter_str).__name__}")

    stripped = filter_str.strip()
    if not stripped or stripped.lower() == "none":
        return

    has_semi, has_comma, has_whitespace, in_quotes = _scan_filter_string(filter_str)
    if in_quotes:
        raise ValueError("Filter expression contains unclosed quotation marks")
    if has_semi and has_comma:
        raise ValueError("Filter expression cannot combine both ';' (AND) and ',' (OR) connectors")
    if has_whitespace:
        raise ValueError(
            "Filter expression contains whitespace between attribute, operator, and value"
        )


validate_record_filter = validate_filter_syntax


def _format_filter_value(val: Any) -> str:
    """Format a single filter value, escaping if necessary."""
    if isinstance(val, (dict, set, list, tuple)):
        raise ValueError(f"Invalid filter value type: {type(val).__name__}")
    if isinstance(val, str):
        if _UNSAFE_CHARS_REGEX.search(val):
            # Escape backslashes first to prevent escape injection
            escaped = val.replace("\\", "\\\\").replace('"', r"\"")
            return f'"{escaped}"'
        return val
    return str(val)


def _build_filter_part(
    key: str, value: Any | tuple[str, Any] | list[Any], or_connector: str = ","
) -> str:
    """Build a single filter string part from a key and value."""
    validate_filter_key(key)
    camel_key = _snake_to_camel(key)
    if isinstance(value, tuple):
        if len(value) != 2:
            raise ValueError(
                f"Filter tuple for '{key}' must have exactly 2 elements (operator, value), got {len(value)}"
            )
        op, val = value
        validate_filter_operator(op)
        return f"{camel_key}{op}{_format_filter_value(val)}"
    if isinstance(value, list):
        subparts = [f"{camel_key}=={_format_filter_value(v)}" for v in value]
        return or_connector.join(subparts)
    return f"{camel_key}=={_format_filter_value(value)}"


def build_filter_string(
    filters: dict[str, Any | tuple[str, Any] | list[Any]],
    and_connector: str = ";",
    or_connector: str = ",",
) -> str:
    """Return a filter string constructed according to iMednet rules.

    Each key in ``filters`` is converted to camelCase. Raw values imply
    equality, tuples allow explicit operators, and lists generate multiple
    equality filters joined by ``or_connector``. Conditions are then joined by
    ``and_connector``.

    Example:
    >>> from imednet.utils.filters import build_filter_string
    >>> build_filter_string({'age': ('>', 30), 'status': 'active'})
    'age>30;status==active'
    >>> build_filter_string({'type': ['A', 'B']})
    'type==A,type==B'
    """
    parts = [_build_filter_part(key, value, or_connector) for key, value in filters.items()]
    result = and_connector.join(parts)
    validate_connector_exclusivity(result)
    return result


__all__ = [
    "ALLOWED_OPERATORS",
    "build_filter_string",
    "validate_connector_exclusivity",
    "validate_filter_key",
    "validate_filter_operator",
    "validate_filter_syntax",
    "validate_record_filter",
]
