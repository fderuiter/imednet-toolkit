"""Utility functions for building API filter strings.

This module provides functionality to construct filter query parameters
for iMednet API endpoints based on the reference documentation.
"""

import functools
import re
import weakref
from collections.abc import Sequence
from typing import Any, TypeVar

# Pre-compiled regex for performance to avoid re-compilation in loops
_UNSAFE_CHARS_REGEX = re.compile(r"[^A-Za-z0-9_.:-]")

ORDERED_FILTER_OPERATORS: tuple[str, ...] = ("<=", ">=", "!=", "==", "=~", "<", ">")
ALLOWED_OPERATORS = frozenset(ORDERED_FILTER_OPERATORS)
_TWO_CHAR_OPERATORS = frozenset(op for op in ORDERED_FILTER_OPERATORS if len(op) == 2)
_SINGLE_CHAR_OPERATORS = frozenset({*(op for op in ORDERED_FILTER_OPERATORS if len(op) == 1), "="})
_OPERATOR_KEY_CHARS = frozenset({"<", ">", "=", "!", "~"})

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


_MODEL_FILTER_KEYS_CACHE: weakref.WeakKeyDictionary[type[Any], frozenset[str]] = (
    weakref.WeakKeyDictionary()
)


def _get_model_filter_keys(model: type[Any]) -> frozenset[str]:
    """Return allowed snake_case and camelCase filter attribute names for a model."""
    cached = _MODEL_FILTER_KEYS_CACHE.get(model)
    if cached is not None:
        return cached

    model_fields = getattr(model, "model_fields", None)
    if not isinstance(model_fields, dict):
        return frozenset()

    allowed: set[str] = set()
    for field_name, field_info in model_fields.items():
        allowed.add(field_name)
        allowed.add(_snake_to_camel(field_name))
        alias = getattr(field_info, "alias", None)
        if isinstance(alias, str) and alias:
            allowed.add(alias)
        validation_alias = getattr(field_info, "validation_alias", None)
        if isinstance(validation_alias, str) and validation_alias:
            allowed.add(validation_alias)
    result = frozenset(allowed)
    _MODEL_FILTER_KEYS_CACHE[model] = result
    return result


def validate_filter_key(key: str, *, model: type[Any] | None = None) -> None:
    """Validate that a filter key is non-empty, operator-free, and optionally on ``model``.

    Args:
        key: The key string to validate.
        model: Optional Pydantic resource model class to validate attribute names against.

    Raises:
        ValueError: If key is empty, contains whitespace or operator characters, or is
            unknown on ``model``.
    """
    if not isinstance(key, str) or not key or not key.strip():
        raise ValueError("Filter key must be a non-empty string")
    if any(c.isspace() for c in key):
        raise ValueError(f"Filter key '{key}' cannot contain whitespace")
    if any(c in _OPERATOR_KEY_CHARS for c in key):
        raise ValueError(f"Filter key '{key}' cannot contain operator characters")
    if model is not None:
        valid_keys = _get_model_filter_keys(model)
        if valid_keys and key not in valid_keys:
            raise ValueError(
                f"Unknown filter attribute '{key}' for {model.__name__}. "
                f"Valid attributes: {', '.join(sorted(valid_keys))}"
            )


def _coerce_filter_scalar(raw_value: str) -> str | bool | int:
    """Coerce boolean and integer string tokens to native scalars."""
    lowered = raw_value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if raw_value.isdigit():
        return int(raw_value)
    return raw_value


def parse_filter_expression(
    filter_str: str,
    *,
    model: type[Any] | None = None,
) -> tuple[str, Any]:
    """Parse a single filter expression string into ``(key, value_or_operator_tuple)``.

    Matches two-character comparison operators (``<=``, ``>=``, ``!=``, ``==``,
    ``=~``) and single-character inequalities (``<``, ``>``) before single ``=``
    equality shorthand per the iMedNet Common API specification.

    Args:
        filter_str: Raw filter expression string.
        model: Optional Pydantic model class used to validate the parsed key.

    Returns:
        Tuple of ``(key, parsed_value)`` where ``parsed_value`` is ``(operator, scalar)``
        for explicit operators in :data:`ALLOWED_OPERATORS` or ``scalar`` for ``=`` shorthand.

    Raises:
        ValueError: If syntax, operator, or key validation fails.
    """
    if not isinstance(filter_str, str) or not filter_str or not filter_str.strip():
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            "Use 'key=value' with non-empty key and value (e.g., 'form_key=DEMO')."
        )

    matched_op: str | None = None
    op_idx = -1
    for idx in range(len(filter_str)):
        two_char = filter_str[idx : idx + 2]
        if len(two_char) == 2 and two_char in _TWO_CHAR_OPERATORS:
            matched_op = two_char
            op_idx = idx
            break
        one_char = filter_str[idx]
        if one_char in _SINGLE_CHAR_OPERATORS:
            matched_op = one_char
            op_idx = idx
            break

    if matched_op is None:
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            "Use 'key=value' with non-empty key and value (e.g., 'form_key=DEMO')."
        )

    raw_key = filter_str[:op_idx].strip()
    raw_value = filter_str[op_idx + len(matched_op) :].strip()
    if not raw_key or not raw_value:
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            "Key and value cannot be empty; use 'key=value' (e.g., 'form_key=DEMO')."
        )

    if (matched_op == "==" and raw_value.startswith("=")) or (
        matched_op == "<" and raw_value.startswith(">")
    ):
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            f"Unsupported comparison operator; allowed operators: {list(ORDERED_FILTER_OPERATORS)}."
        )

    validate_filter_key(raw_key, model=model)
    parsed_scalar = _coerce_filter_scalar(raw_value)
    if matched_op == "=":
        return raw_key, parsed_scalar
    return raw_key, (matched_op, parsed_scalar)


def parse_filter_expressions(
    expressions: Sequence[str],
    *,
    model: type[Any] | None = None,
) -> dict[str, Any]:
    """Parse multiple filter expressions into a filter dictionary.

    Repeated conditions on the same key are preserved as a list of
    ``(operator, value)`` tuples so :func:`build_filter_string` joins them with
    the semicolon ``AND`` connector.
    """
    filter_dict: dict[str, Any] = {}
    for expr in expressions:
        key, parsed_val = parse_filter_expression(expr, model=model)
        if key not in filter_dict:
            filter_dict[key] = parsed_val
            continue

        prev = filter_dict[key]
        if isinstance(prev, list):
            combined = list(prev)
        elif isinstance(prev, tuple):
            combined = [prev]
        else:
            combined = [("==", prev)]

        combined.append(parsed_val if isinstance(parsed_val, tuple) else ("==", parsed_val))
        filter_dict[key] = combined
    return filter_dict


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
    key: str,
    value: Any | tuple[str, Any] | list[Any],
    and_connector: str = ";",
    or_connector: str = ",",
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
        if value and any(isinstance(item, tuple) for item in value):
            if not all(isinstance(item, tuple) for item in value):
                raise ValueError(
                    f"Filter list for '{key}' cannot mix operator tuples and scalar values"
                )
            tuple_parts: list[str] = []
            for item in value:
                if len(item) != 2:
                    raise ValueError(
                        f"Filter tuple for '{key}' must have exactly 2 elements (operator, value), got {len(item)}"
                    )
                op, val = item
                validate_filter_operator(op)
                tuple_parts.append(f"{camel_key}{op}{_format_filter_value(val)}")
            return and_connector.join(tuple_parts)
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
    equality, tuples allow explicit operators, lists of ``(operator, value)``
    tuples on one key join with ``and_connector``, and lists of scalars
    generate multiple equality filters joined by ``or_connector``. Conditions
    across keys are joined by ``and_connector``.

    Example:
    >>> from imednet.utils.filters import build_filter_string
    >>> build_filter_string({'age': ('>', 30), 'status': 'active'})
    'age>30;status==active'
    >>> build_filter_string({'type': ['A', 'B']})
    'type==A,type==B'
    """
    parts = [
        _build_filter_part(key, value, and_connector=and_connector, or_connector=or_connector)
        for key, value in filters.items()
    ]
    non_empty_parts = [part for part in parts if part]
    result = and_connector.join(non_empty_parts)
    validate_connector_exclusivity(result)
    return result


__all__ = [
    "ALLOWED_OPERATORS",
    "ORDERED_FILTER_OPERATORS",
    "build_filter_string",
    "parse_filter_expression",
    "parse_filter_expressions",
    "validate_connector_exclusivity",
    "validate_filter_key",
    "validate_filter_operator",
    "validate_filter_syntax",
    "validate_record_filter",
]
