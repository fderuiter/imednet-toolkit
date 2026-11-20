"""CLI argument parsing utilities."""

from __future__ import annotations

import argparse
import itertools
import os
import sys
import weakref
from collections.abc import Callable, Collection, Iterable, Sequence
from typing import Any

from imednet.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from imednet.utils.filters import (
    _parse_clause_operator,
    _split_filter_clauses,
    extract_filter_attributes,
    parse_filter_expressions,
    validate_record_filter,
)

from .output import print_error
from .terminal import OUTPUT_FORMAT_ENV

STUDY_KEY_ARG = "The key identifying the study."
DEFAULT_INTERACTIVE_LIMIT = DEFAULT_PAGE_SIZE


_STUDY_KEY_FILTER_ATTRS: frozenset[str] = frozenset({"study_key", "studyKey"})


_ENDPOINT_FILTER_ATTRS_CACHE: weakref.WeakKeyDictionary[type[Any], frozenset[str] | None] = (
    weakref.WeakKeyDictionary()
)


def resolve_endpoint_filter_attributes(model: type[Any] | None) -> frozenset[str] | None:
    """Resolve documented endpoint ``FILTER_ATTRIBUTES`` for a registered SDK model."""
    if model is None:
        return None
    target_model: type[Any] = model
    if target_model in _ENDPOINT_FILTER_ATTRS_CACHE:
        return _ENDPOINT_FILTER_ATTRS_CACHE[target_model]
    from imednet.endpoints.registry import ENDPOINT_REGISTRY

    resolved: frozenset[str] | None = None
    for endpoint_cls in ENDPOINT_REGISTRY.values():
        if getattr(endpoint_cls, "MODEL", None) is target_model:
            attrs = getattr(endpoint_cls, "FILTER_ATTRIBUTES", None)
            if isinstance(attrs, frozenset):
                resolved = attrs
                break
    _ENDPOINT_FILTER_ATTRS_CACHE[target_model] = resolved
    return resolved


def _contains_study_key_filter(parsed: dict[str, Any]) -> bool:
    """Return True if parsed filters include ``study_key`` or ``studyKey``."""
    if _STUDY_KEY_FILTER_ATTRS.intersection(parsed):
        return True
    raw_filter = parsed.get("filter")
    if isinstance(raw_filter, str):
        return bool(_STUDY_KEY_FILTER_ATTRS.intersection(extract_filter_attributes(raw_filter)))
    return False


def parse_filter_args(
    filter_args: list[str] | None,
    *,
    model: type[Any] | None = None,
    allowed_attributes: Collection[str] | None = None,
    disallow_study_key: bool = False,
) -> dict[str, Any] | None:
    """Parse repeatable CLI ``--filter`` strings via the shared SPI filter parser.

    Supports legacy ``key=value`` equality, Table 2 iMednet comparison
    operators (``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``), compound
    expressions joined exclusively by ``;`` (AND) or ``,`` (OR), endpoint
    ``allowed_attributes`` allowlists, and Pydantic ``model`` scalar coercion.
    Exits with status code ``1`` on ``stderr`` if validation fails.
    """
    if not filter_args:
        return None

    effective_allowed: Collection[str] | None = (
        allowed_attributes
        if allowed_attributes is not None
        else resolve_endpoint_filter_attributes(model)
    )
    if disallow_study_key and effective_allowed is not None:
        effective_allowed = frozenset(
            attr for attr in effective_allowed if attr not in _STUDY_KEY_FILTER_ATTRS
        )

    try:
        parsed = parse_filter_expressions(
            filter_args,
            model=model,
            allowed_attributes=effective_allowed,
            allow_regex=False,
        )
    except ValueError as exc:
        print_error(f"Error: {exc}")
        sys.exit(1)

    if disallow_study_key and _contains_study_key_filter(parsed):
        print_error(
            "Error: Invalid filter: 'study_key' and 'studyKey' cannot be used in "
            "--filter when positional study_key is provided."
        )
        sys.exit(1)
    return parsed


def parse_record_data_filter(record_data_filter: str | None) -> str | None:
    """Validate a CLI ``--record-data-filter`` string via ``validate_record_filter``.

    Supports comparison operators including ``=~``, exclusive ``;`` (AND) or
    ``,`` (OR) connectors, and literal ``none`` (case-insensitive) bypass.
    Exits with status code ``1`` on ``stderr`` if validation fails.
    """
    if record_data_filter is None:
        return None
    stripped = record_data_filter.strip()
    if not stripped:
        print_error("Error: Invalid record data filter: cannot be empty.")
        sys.exit(1)
    try:
        validate_record_filter(record_data_filter)
        if stripped.lower() != "none":
            clauses, _ = _split_filter_clauses(stripped)
            for clause in clauses:
                if _parse_clause_operator(clause) is None:
                    raise ValueError(
                        f"Malformed filter clause '{clause}': missing comparison operator"
                    )
    except ValueError as exc:
        print_error(f"Error: Invalid record data filter: {exc}")
        sys.exit(1)
    return stripped


def add_no_interactive(parser: argparse.ArgumentParser, *, suppress_default: bool = True) -> None:
    """Add ``--no-interactive`` option to disable interactive UI, spinners, and prompts."""
    if "--no-interactive" in parser._option_string_actions:
        return
    kwargs: dict[str, Any] = {
        "action": "store_true",
        "help": "Disable interactive terminal UI, live spinners, and stdin prompts.",
    }
    if suppress_default:
        kwargs["default"] = argparse.SUPPRESS
    parser.add_argument("--no-interactive", **kwargs)


def add_output_format(parser: argparse.ArgumentParser) -> None:
    """Add common machine response and non-interactive flags to a leaf read command."""
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("table", "json"),
        default=None,
        help="Response format: human table (default) or versioned JSON on stdout.",
    )
    add_no_interactive(parser)


def resolve_output_format(
    output_format: str | None = None,
    *,
    default: str = "table",
) -> str:
    """Resolve CLI ``--format`` option against ``IMEDNET_CLI_OUTPUT_FORMAT``."""
    if output_format is not None:
        return output_format
    if os.environ.get(OUTPUT_FORMAT_ENV, "").strip().lower() == "json":
        return "json"
    return default


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
                "Server filter expression using =, ==, !=, <, <=, >, >= and "
                "';' (AND) or ',' (OR) connectors. Repeat for multiple AND filters."
            ),
        )
    if include_record_data_filter:
        parser.add_argument(
            "--record-data-filter",
            dest="record_data_filter",
            default=None,
            help=(
                "Server recordDataFilter expression for Form Variable values using "
                "==, !=, <, <=, >, >=, =~ (e.g., 'AESER==Serious;AGE>=18' or 'AETERM=~^Head')."
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
    output_format: str | None = None,
    has_output_file: bool = False,
    no_interactive: bool = False,
    model: type[Any] | None = None,
    allowed_attributes: Collection[str] | None = None,
    disallow_study_key: bool = False,
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
    parsed_filters = parse_filter_args(
        filter_args,
        model=model,
        allowed_attributes=allowed_attributes,
        disallow_study_key=disallow_study_key,
    )
    if parsed_filters:
        request_kwargs.update(parsed_filters)

    parsed_record_data_filter = parse_record_data_filter(record_data_filter)
    if parsed_record_data_filter is not None:
        request_kwargs["record_data_filter"] = parsed_record_data_filter

    if sort is not None:
        request_kwargs["sort"] = sort.strip()

    resolved_format = resolve_output_format(output_format)
    effective_limit: int | None = None
    if limit is not None:
        effective_limit = limit
    elif (
        not fetch_all and not no_interactive and resolved_format == "table" and not has_output_file
    ):
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


def collect_list_items_with_remainder(
    source: Iterable[Any],
    effective_limit: int | None,
) -> tuple[list[Any], bool, Callable[[], list[Any]] | None]:
    """Materialize items up to ``effective_limit`` and return a remainder loader when truncated.

    Preserves the live ``_PaginatedIterator`` cursor so subsequent ``[a]`` loads drain
    remaining pages starting at page 1 without re-fetching page 0.

    Returns:
        Tuple of ``(items, truncated, load_remaining)``.
    """
    if effective_limit is None:
        return list(source), False, None

    if isinstance(source, Sequence):
        truncated = len(source) > effective_limit
        initial = list(source[:effective_limit])
        if not truncated:
            return initial, False, None
        remaining = list(source[effective_limit:])

        def _drain_sequence() -> list[Any]:
            rest = list(remaining)
            remaining.clear()
            return rest

        return initial, True, _drain_sequence

    if hasattr(source, "has_more"):
        iterator = iter(source)
        items = list(itertools.islice(iterator, effective_limit))
        truncated = bool(getattr(source, "has_more", False))
        if not truncated:
            return items, False, None

        def _drain_paginated() -> list[Any]:
            return list(iterator)

        return items, True, _drain_paginated

    iterator = iter(source)
    batch = list(itertools.islice(iterator, effective_limit + 1))
    truncated = len(batch) > effective_limit
    initial = batch[:effective_limit]
    if not truncated:
        return initial, False, None
    overflow = batch[effective_limit:]

    def _drain_iterable() -> list[Any]:
        rest = [*overflow, *iterator]
        overflow.clear()
        return rest

    return initial, True, _drain_iterable


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
    items, truncated, _ = collect_list_items_with_remainder(source, effective_limit)
    return items, truncated
