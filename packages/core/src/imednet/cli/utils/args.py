"""CLI argument parsing utilities."""

from __future__ import annotations

import argparse
import itertools
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Any

from imednet.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from imednet.utils.filters import (
    _format_filter_value,
    _snake_to_camel,
    validate_connector_exclusivity,
    validate_filter_key,
    validate_filter_operator,
    validate_filter_syntax,
    validate_record_filter,
)

from .output import print_error

STUDY_KEY_ARG = "The key identifying the study."
DEFAULT_INTERACTIVE_LIMIT = DEFAULT_PAGE_SIZE

_CANDIDATE_OPERATORS: tuple[str, ...] = ("<=", ">=", "==", "!=", "=~", "<", ">", "=")
_VALID_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


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


def _parse_scalar_value(raw_val: str, raw_arg: str) -> tuple[str | bool | int, bool]:
    """Parse a filter value token, returning ``(value, was_quoted)``."""
    if len(raw_val) >= 2 and raw_val.startswith('"') and raw_val.endswith('"'):
        # Verify the opening quote closes only at the final character
        escape = False
        for idx, ch in enumerate(raw_val[1:], start=1):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and idx != len(raw_val) - 1:
                print_error(
                    f"Error: Invalid filter format: '{raw_arg}'. Unexpected quote inside value."
                )
                sys.exit(1)
        inner = raw_val[1:-1]
        unescaped = inner.replace(r"\"", '"').replace("\\\\", "\\")
        return unescaped, True

    if '"' in raw_val:
        print_error(f"Error: Invalid filter format: '{raw_arg}'. Malformed quoted value.")
        sys.exit(1)

    lower = raw_val.lower()
    if lower == "true":
        return True, False
    if lower == "false":
        return False, False
    if raw_val.isdigit():
        return int(raw_val), False
    return raw_val, False


def _parse_single_clause(clause: str, raw_arg: str) -> tuple[str, str, str | bool | int, bool]:
    """Parse ``key<op>value`` into ``(key, op, parsed_value, was_quoted)``."""
    if not clause:
        print_error(f"Error: Invalid filter format: '{raw_arg}'. Empty filter clause.")
        sys.exit(1)

    found = _find_operator(clause)
    if found is None:
        print_error(
            f"Error: Invalid filter format: '{raw_arg}'. "
            "Use 'key=value' or comparison operators (<, <=, >, >=, ==, !=, =~)."
        )
        sys.exit(1)

    op_idx, op = found
    key = clause[:op_idx]
    raw_val = clause[op_idx + len(op) :]

    if not key or not key.strip() or not raw_val or not raw_val.strip():
        print_error(f"Error: Invalid filter format: '{raw_arg}'. Key and value cannot be empty.")
        sys.exit(1)

    try:
        validate_filter_key(key)
    except ValueError as exc:
        print_error(f"Error: Invalid filter format: '{raw_arg}'. {exc}")
        sys.exit(1)

    if not _VALID_KEY_PATTERN.match(key):
        print_error(f"Error: Invalid filter format: '{raw_arg}'. Invalid filter attribute '{key}'.")
        sys.exit(1)

    if raw_val.startswith(("=", "<", ">", "~", "!")):
        print_error(f"Error: Invalid filter format: '{raw_arg}'. Invalid filter operator.")
        sys.exit(1)

    if op != "=":
        try:
            validate_filter_operator(op)
        except ValueError as exc:
            print_error(f"Error: Invalid filter format: '{raw_arg}'. {exc}")
            sys.exit(1)

    parsed_val, was_quoted = _parse_scalar_value(raw_val, raw_arg)
    return key, op, parsed_val, was_quoted


def _format_clause(key: str, op: str, val: str | bool | int, was_quoted: bool) -> str:
    """Format a parsed clause as a valid iMednet server filter segment."""
    api_op = "==" if op == "=" else op
    camel_key = _snake_to_camel(key)
    if was_quoted and isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace('"', r"\"")
        return f'{camel_key}{api_op}"{escaped}"'
    return f"{camel_key}{api_op}{_format_filter_value(val)}"


def parse_filter_args(filter_args: list[str] | None) -> dict[str, Any] | None:
    """Parse repeatable CLI ``--filter`` strings into SDK filter kwargs.

    Supports legacy ``key=value`` equality, all seven iMednet comparison
    operators (``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``, ``=~``), and
    compound expressions joined exclusively by ``;`` (AND) or ``,`` (OR).
    Exits with status code ``1`` on ``stderr`` if syntax or connector
    exclusivity rules are violated.
    """
    if not filter_args:
        return None

    for arg in filter_args:
        if not isinstance(arg, str) or not arg or not arg.strip():
            print_error(f"Error: Invalid filter format: '{arg}'. Filter cannot be empty.")
            sys.exit(1)
        try:
            validate_filter_syntax(arg)
        except ValueError as exc:
            print_error(f"Error: Invalid filter format: '{arg}'. {exc}")
            sys.exit(1)

    combined_raw = ";".join(filter_args)
    try:
        validate_connector_exclusivity(combined_raw)
    except ValueError as exc:
        print_error(f"Error: Invalid filter format: '{combined_raw}'. {exc}")
        sys.exit(1)

    has_comma = any(_has_unquoted_char(arg, ",") for arg in filter_args)
    if has_comma:
        raw_arg = filter_args[0]
        clauses = _split_outside_quotes(raw_arg, ",")
        parsed_clauses = [_parse_single_clause(clause, raw_arg) for clause in clauses]
        formatted = ",".join(_format_clause(*pc) for pc in parsed_clauses)
        try:
            validate_filter_syntax(formatted)
        except ValueError as exc:
            print_error(f"Error: Invalid filter format: '{raw_arg}'. {exc}")
            sys.exit(1)
        return {"filter": formatted}

    parsed_and_clauses: list[tuple[str, str, str | bool | int, bool]] = []
    for arg in filter_args:
        for clause in _split_outside_quotes(arg, ";"):
            parsed_and_clauses.append(_parse_single_clause(clause, arg))

    # Detect duplicate target keys (e.g. age>=18;age<=65) so no condition is overwritten.
    seen_camel_keys: set[str] = set()
    has_duplicate_key = False
    for key, _, _, _ in parsed_and_clauses:
        ck = _snake_to_camel(key)
        if ck in seen_camel_keys:
            has_duplicate_key = True
            break
        seen_camel_keys.add(ck)

    if has_duplicate_key:
        formatted = ";".join(_format_clause(*pc) for pc in parsed_and_clauses)
        try:
            validate_filter_syntax(formatted)
        except ValueError as exc:
            print_error(f"Error: Invalid filter format: '{combined_raw}'. {exc}")
            sys.exit(1)
        return {"filter": formatted}

    filter_dict: dict[str, Any] = {}
    for key, op, val, _ in parsed_and_clauses:
        if op == "=":
            filter_dict[key] = val
        else:
            filter_dict[key] = (op, val)
    return filter_dict


def add_output_format(parser: argparse.ArgumentParser) -> None:
    """Add the common opt-in machine response flag to a leaf read command."""
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("table", "json"),
        default="table",
        help="Response format: human table (default) or versioned JSON on stdout.",
    )


def add_list_query_args(
    parser: argparse.ArgumentParser,
    *,
    include_filter: bool = True,
    filter_dest: str = "filter_args",
    filter_help: str | None = None,
    include_record_data_filter: bool = False,
) -> None:
    """Attach server-side filter, sort, limit, and unbounded flags to a list subparser."""
    if include_filter:
        parser.add_argument(
            "-f",
            "--filter",
            action="append",
            dest=filter_dest,
            default=None,
            help=filter_help
            or (
                "Server filter expression using =, ==, !=, <, <=, >, >=, =~ and "
                "';' (AND) or ',' (OR) connectors. Repeat for multiple AND filters."
            ),
        )
    if include_record_data_filter:
        parser.add_argument(
            "--record-data-filter",
            dest="record_data_filter",
            default=None,
            help=(
                "Server recordDataFilter expression for Form Variable values "
                "(e.g., 'AESER==Serious;AGE>=18')."
            ),
        )
    parser.add_argument(
        "--sort",
        dest="sort",
        default=None,
        help="Server sort specification (e.g., 'dateCreated,desc' or 'recordId,asc').",
    )
    parser.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=None,
        help="Maximum number of items to fetch before stopping.",
    )
    parser.add_argument(
        "--all",
        dest="fetch_all",
        action="store_true",
        default=False,
        help="Fetch all matching pages even in interactive TTY sessions.",
    )


def prepare_list_query(
    *,
    filter_args: list[str] | None = None,
    record_data_filter: str | None = None,
    sort: str | None = None,
    limit: int | None = None,
    fetch_all: bool = False,
    output_format: str = "table",
    has_output_file: bool = False,
) -> tuple[dict[str, Any], int | None]:
    """Validate CLI query options and resolve SDK ``list()`` kwargs and item bound.

    Returns:
        Tuple of ``(request_kwargs, effective_limit)``.
    """
    if limit is not None and fetch_all:
        print_error("Error: Cannot combine --limit and --all.")
        sys.exit(1)

    if limit is not None and limit <= 0:
        print_error("Error: --limit must be a positive integer.")
        sys.exit(1)

    if sort is not None and not sort.strip():
        print_error("Error: --sort cannot be empty.")
        sys.exit(1)

    request_kwargs: dict[str, Any] = {}
    parsed_filters = parse_filter_args(filter_args)
    if parsed_filters:
        request_kwargs.update(parsed_filters)

    if record_data_filter is not None:
        if not record_data_filter.strip():
            print_error("Error: Invalid record data filter: cannot be empty.")
            sys.exit(1)
        try:
            validate_record_filter(record_data_filter)
        except ValueError as exc:
            print_error(f"Error: Invalid record data filter: {exc}")
            sys.exit(1)
        request_kwargs["record_data_filter"] = record_data_filter

    if sort is not None:
        request_kwargs["sort"] = sort.strip()

    effective_limit: int | None = None
    if limit is not None:
        effective_limit = limit
    elif not fetch_all and output_format == "table" and not has_output_file:
        try:
            from .grid import is_interactive_terminal

            is_tty = is_interactive_terminal()
        except ImportError:
            is_tty = False
        if is_tty:
            effective_limit = DEFAULT_INTERACTIVE_LIMIT

    if effective_limit is not None:
        request_kwargs["size"] = min(effective_limit, MAX_PAGE_SIZE)

    return request_kwargs, effective_limit


def collect_list_items(
    source: Iterable[Any],
    effective_limit: int | None,
) -> tuple[list[Any], bool]:
    """Materialize items from an SDK ``list()`` iterable up to ``effective_limit``.

    Detects server-side truncation without issuing an extra page request when
    backed by ``_PaginatedIterator``.

    Returns:
        Tuple of ``(items, truncated)``.
    """
    if effective_limit is None:
        return list(source), False

    if isinstance(source, Sequence):
        truncated = len(source) > effective_limit
        return list(source[:effective_limit]), truncated

    if hasattr(source, "has_more"):
        items = list(itertools.islice(source, effective_limit))
        return items, bool(getattr(source, "has_more", False))

    iterator = iter(source)
    batch = list(itertools.islice(iterator, effective_limit + 1))
    truncated = len(batch) > effective_limit
    return batch[:effective_limit], truncated
