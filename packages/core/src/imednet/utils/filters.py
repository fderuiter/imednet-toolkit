"""Utility functions for building API filter strings.

This module provides functionality to construct filter query parameters
for iMednet API endpoints based on the reference documentation.
"""

import functools
import re
from datetime import date, datetime
from typing import Any, TypeVar

from imednet.utils.dates import format_iso_datetime

# Pre-compiled regexes for performance to avoid re-compilation in loops
_UNSAFE_CHARS_REGEX = re.compile(r"[^A-Za-z0-9_.-]")
_ISO_8601_TIMESTAMP_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)

ALLOWED_OPERATORS = frozenset({"<", "<=", ">", ">=", "==", "!="})
RECORD_DATA_OPERATORS = frozenset({"<", "<=", ">", ">=", "==", "!=", "=~"})
_OPERATOR_CHARS = frozenset("<>=!~")

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


def _split_filter_clauses(filter_str: str) -> tuple[list[str], str | None]:
    """Split a filter expression on ';' or ',' outside quotation marks."""
    clauses: list[str] = []
    current: list[str] = []
    in_quotes = False
    escape = False
    connector: str | None = None

    for char in filter_str:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\" and in_quotes:
            current.append(char)
            escape = True
            continue
        if char == '"':
            current.append(char)
            in_quotes = not in_quotes
            continue
        if not in_quotes and char in (";", ","):
            connector = char
            clauses.append("".join(current))
            current = []
            continue
        current.append(char)

    clauses.append("".join(current))
    return clauses, connector


def _parse_clause_operator(clause: str) -> tuple[str, str, str] | None:
    """Parse (attribute, operator, raw_value) from a single filter clause."""
    quote_idx = clause.find('"')
    prefix = clause if quote_idx == -1 else clause[:quote_idx]

    op_start = -1
    for idx, char in enumerate(prefix):
        if char in _OPERATOR_CHARS:
            op_start = idx
            break

    if op_start == -1:
        return None

    op_end = op_start
    while op_end < len(prefix) and prefix[op_end] in _OPERATOR_CHARS:
        op_end += 1

    attr = clause[:op_start]
    op = clause[op_start:op_end]
    val = clause[op_end:]
    if not attr or not val:
        raise ValueError(f"Malformed filter clause '{clause}': missing attribute or value")
    return attr, op, val


def extract_filter_attributes(filter_str: str) -> list[str]:
    """Extract attribute names from a raw filter expression."""
    stripped = filter_str.strip()
    if not stripped or stripped.lower() == "none":
        return []
    clauses, _ = _split_filter_clauses(stripped)
    attributes: list[str] = []
    for clause in clauses:
        if not clause:
            continue
        parsed = _parse_clause_operator(clause)
        attributes.append(parsed[0] if parsed is not None else clause)
    return attributes


def validate_filter_operator(operator: str, *, allow_regex: bool = False) -> None:
    """Validate that an operator matches allowed tokens and contains no whitespace.

    Args:
        operator: The operator string to validate.
        allow_regex: Whether to allow the ``=~`` operator (for ``recordDataFilter``).

    Raises:
        ValueError: If operator contains whitespace or is not in the allowed set.
    """
    if not isinstance(operator, str) or not operator:
        raise ValueError("Filter operator must be a non-empty string")
    if any(c.isspace() for c in operator):
        raise ValueError(f"Filter operator '{operator}' cannot contain whitespace")
    allowed = RECORD_DATA_OPERATORS if allow_regex else ALLOWED_OPERATORS
    if operator not in allowed:
        raise ValueError(
            f"Invalid filter operator '{operator}'. Allowed operators: {sorted(allowed)}"
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


def _validate_filter_expression(filter_str: str, *, allow_regex: bool) -> None:
    """Validate a filter or recordDataFilter expression."""
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

    clauses, connector = _split_filter_clauses(stripped)
    allowed = RECORD_DATA_OPERATORS if allow_regex else ALLOWED_OPERATORS
    seen_and_attrs: set[str] = set()

    for clause in clauses:
        if not clause:
            raise ValueError("Filter expression contains an empty clause")
        parsed = _parse_clause_operator(clause)
        if parsed is None:
            continue
        attr, op, _ = parsed
        if op not in allowed:
            raise ValueError(
                f"Invalid filter operator '{op}'. Allowed operators: {sorted(allowed)}"
            )
        if connector == ";":
            if attr in seen_and_attrs:
                raise ValueError(f"Duplicate filter attribute '{attr}' in AND clause")
            seen_and_attrs.add(attr)


def validate_filter_syntax(filter_str: str) -> None:
    """Validate a standard ``filter`` expression for syntax, operators, and uniqueness.

    Verifies connector exclusivity, ensures no whitespace exists outside of
    double-quoted strings, restricts operators to Table 2 (excluding ``=~``),
    and rejects duplicate attributes in ``;`` (AND) clauses.

    Args:
        filter_str: The filter expression string to validate.

    Raises:
        ValueError: If syntax, operator, or attribute uniqueness rules are violated.
    """
    _validate_filter_expression(filter_str, allow_regex=False)


def validate_record_filter(filter_str: str) -> None:
    """Validate a ``recordDataFilter`` expression for syntax, operators, and uniqueness.

    Allows ``=~`` per Common API Table 7 while enforcing connector exclusivity,
    quote/whitespace rules, and unique attributes in ``;`` (AND) clauses.

    Args:
        filter_str: The recordDataFilter expression string to validate.

    Raises:
        ValueError: If syntax, operator, or attribute uniqueness rules are violated.
    """
    _validate_filter_expression(filter_str, allow_regex=True)


def _format_filter_value(val: Any) -> str:
    """Format a single filter value, escaping if necessary."""
    if val is None:
        raise ValueError("Filter value cannot be None")
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (dict, set, list, tuple)):
        raise ValueError(f"Invalid filter value type: {type(val).__name__}")
    if isinstance(val, datetime):
        return format_iso_datetime(val)
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, str):
        if _ISO_8601_TIMESTAMP_REGEX.match(val):
            return val
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
    seen_keys: set[str] = set()
    check_duplicates = and_connector.strip() in (";", "and")
    parts: list[str] = []
    for key, value in filters.items():
        validate_filter_key(key)
        camel_key = _snake_to_camel(key)
        if check_duplicates:
            if camel_key in seen_keys:
                raise ValueError(f"Duplicate filter attribute '{camel_key}' in AND clause")
            seen_keys.add(camel_key)
        parts.append(_build_filter_part(key, value, or_connector))
    result = and_connector.join(parts)
    validate_connector_exclusivity(result)
    return result


__all__ = [
    "ALLOWED_OPERATORS",
    "RECORD_DATA_OPERATORS",
    "build_filter_string",
    "extract_filter_attributes",
    "validate_connector_exclusivity",
    "validate_filter_key",
    "validate_filter_operator",
    "validate_filter_syntax",
    "validate_record_filter",
]
