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


_VALID_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
_CANDIDATE_OPERATORS: tuple[str, ...] = (*ORDERED_FILTER_OPERATORS, "=")


def _split_outside_quotes(expr: str, separator: str) -> list[str]:
    """Split a filter expression by ``separator`` only outside double quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    escape = False

    for ch in expr:
        if escape:
            current.append(ch)
            escape = False
            continue
        if ch == "\\" and in_quotes:
            current.append(ch)
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
            continue
        if not in_quotes and ch == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)

    parts.append("".join(current))
    return parts


def _has_unquoted_char(expr: str, target: str) -> bool:
    """Return True if ``target`` appears outside double quotes in ``expr``."""
    in_quotes = False
    escape = False
    for ch in expr:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_quotes:
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and ch == target:
            return True
    return False


def _find_operator(clause: str) -> tuple[int, str] | None:
    """Locate the first filter operator outside double quotes in a single clause."""
    in_quotes = False
    escape = False
    length = len(clause)

    for idx, ch in enumerate(clause):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_quotes:
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes:
            for op in _CANDIDATE_OPERATORS:
                op_len = len(op)
                if clause.startswith(op, idx) and idx + op_len <= length:
                    return idx, op
    return None


def _coerce_filter_scalar(raw_value: str, raw_arg: str = "") -> tuple[str | bool | int, bool]:
    """Coerce quoted, boolean, and integer string tokens to native scalars."""
    err_ctx = raw_arg or raw_value
    if len(raw_value) >= 2 and raw_value.startswith('"') and raw_value.endswith('"'):
        escape = False
        for idx, ch in enumerate(raw_value[1:], start=1):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and idx != len(raw_value) - 1:
                raise ValueError(
                    f"Invalid filter format: '{err_ctx}'. Unexpected quote inside value."
                )
        inner = raw_value[1:-1]
        unescaped = inner.replace(r"\"", '"').replace("\\\\", "\\")
        return unescaped, True

    if '"' in raw_value:
        raise ValueError(f"Invalid filter format: '{err_ctx}'. Malformed quoted value.")

    lowered = raw_value.lower()
    if lowered == "true":
        return True, False
    if lowered == "false":
        return False, False
    if raw_value.isdigit():
        return int(raw_value), False
    return raw_value, False


def _parse_single_clause(
    clause: str,
    raw_arg: str,
    *,
    model: type[Any] | None = None,
) -> tuple[str, str, str | bool | int, bool]:
    """Parse ``key<op>value`` into ``(key, op, parsed_value, was_quoted)``."""
    if not isinstance(clause, str) or not clause or not clause.strip():
        raise ValueError(
            f"Invalid filter format: '{raw_arg}'. "
            "Use 'key=value' with non-empty key and value (e.g., 'form_key=DEMO')."
        )

    found = _find_operator(clause)
    if found is None:
        raise ValueError(
            f"Invalid filter format: '{raw_arg}'. "
            "Use 'key=value' with non-empty key and value (e.g., 'form_key=DEMO')."
        )

    op_idx, matched_op = found
    raw_key = clause[:op_idx]
    raw_value = clause[op_idx + len(matched_op) :]
    if not raw_key or not raw_key.strip() or not raw_value or not raw_value.strip():
        raise ValueError(
            f"Invalid filter format: '{raw_arg}'. "
            "Key and value cannot be empty; use 'key=value' (e.g., 'form_key=DEMO')."
        )

    try:
        validate_filter_syntax(clause)
    except ValueError as exc:
        raise ValueError(f"Invalid filter format: '{raw_arg}'. {exc}") from exc

    if raw_value.startswith(("=", "<", ">", "~", "!")):
        raise ValueError(
            f"Invalid filter format: '{raw_arg}'. "
            f"Unsupported comparison operator; allowed operators: {list(ORDERED_FILTER_OPERATORS)}."
        )

    try:
        validate_filter_key(raw_key, model=model)
    except ValueError as exc:
        raise ValueError(f"Invalid filter format: '{raw_arg}'. {exc}") from exc

    if not _VALID_KEY_PATTERN.match(raw_key):
        raise ValueError(
            f"Invalid filter format: '{raw_arg}'. Invalid filter attribute '{raw_key}'."
        )

    if matched_op != "=":
        try:
            validate_filter_operator(matched_op)
        except ValueError as exc:
            raise ValueError(f"Invalid filter format: '{raw_arg}'. {exc}") from exc

    parsed_scalar, was_quoted = _coerce_filter_scalar(raw_value, raw_arg)
    return raw_key, matched_op, parsed_scalar, was_quoted


def _format_clause(key: str, op: str, val: str | bool | int, was_quoted: bool) -> str:
    """Format a parsed clause as a valid iMednet server filter segment."""
    api_op = "==" if op == "=" else op
    camel_key = _snake_to_camel(key)
    if was_quoted and isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', r"\"")
        return f'{camel_key}{api_op}"{escaped}"'
    return f"{camel_key}{api_op}{_format_filter_value(val)}"


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
    if isinstance(filter_str, str) and (
        _has_unquoted_char(filter_str, ";") or _has_unquoted_char(filter_str, ",")
    ):
        raise ValueError(
            f"Invalid filter format: '{filter_str}'. "
            "Expected a single 'key=value' filter expression."
        )
    raw_key, matched_op, parsed_scalar, _ = _parse_single_clause(
        filter_str, filter_str, model=model
    )
    if matched_op == "=":
        return raw_key, parsed_scalar
    return raw_key, (matched_op, parsed_scalar)


def parse_filter_expressions(
    expressions: Sequence[str],
    *,
    model: type[Any] | None = None,
) -> dict[str, Any]:
    """Parse multiple filter expressions into a filter dictionary.

    Supports compound ``;`` (AND) and ``,`` (OR) expressions, quote-aware
    values, and optional resource ``model`` attribute validation. Repeated AND
    conditions on the same key are preserved as a list of ``(operator, value)``
    tuples so :func:`build_filter_string` joins them with ``;``. Comma-separated
    OR expressions return ``{"filter": formatted_or_string}``.
    """
    for arg in expressions:
        if not isinstance(arg, str) or not arg or not arg.strip():
            raise ValueError(
                f"Invalid filter format: '{arg}'. "
                "Use 'key=value' with non-empty key and value (e.g., 'form_key=DEMO')."
            )
        try:
            validate_filter_syntax(arg)
        except ValueError as exc:
            raise ValueError(f"Invalid filter format: '{arg}'. {exc}") from exc

    combined_raw = ";".join(expressions)
    try:
        validate_connector_exclusivity(combined_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid filter format: '{combined_raw}'. {exc}") from exc

    if any(_has_unquoted_char(arg, ",") for arg in expressions):
        raw_arg = expressions[0]
        or_clauses = _split_outside_quotes(raw_arg, ",")
        parsed_or = [_parse_single_clause(clause, raw_arg, model=model) for clause in or_clauses]
        formatted = ",".join(_format_clause(*pc) for pc in parsed_or)
        try:
            validate_filter_syntax(formatted)
        except ValueError as exc:
            raise ValueError(f"Invalid filter format: '{raw_arg}'. {exc}") from exc
        return {"filter": formatted}

    filter_dict: dict[str, Any] = {}
    for expr in expressions:
        for clause in _split_outside_quotes(expr, ";"):
            key, op, val, _ = _parse_single_clause(clause, expr, model=model)
            parsed_val: Any = val if op == "=" else (op, val)
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
