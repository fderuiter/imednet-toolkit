"""CLI argument parsing utilities."""

from __future__ import annotations

import argparse
import itertools
import sys
from collections.abc import Iterable, Sequence
from typing import Any

from imednet.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from imednet.utils.filters import parse_filter_expressions, validate_record_filter

from .output import print_error

STUDY_KEY_ARG = "The key identifying the study."
DEFAULT_INTERACTIVE_LIMIT = DEFAULT_PAGE_SIZE


def parse_filter_args(
    filter_args: list[str] | None,
    *,
    model: type[Any] | None = None,
) -> dict[str, Any] | None:
    """Parse repeatable CLI ``--filter`` strings via the shared SPI filter parser.

    Supports legacy ``key=value`` equality, all seven iMednet comparison
    operators (``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``, ``=~``), compound
    expressions joined exclusively by ``;`` (AND) or ``,`` (OR), and optional
    Pydantic ``model`` attribute validation. Exits with status code ``1`` on
    ``stderr`` if validation fails.
    """
    if not filter_args:
        return None
    try:
        return parse_filter_expressions(filter_args, model=model)
    except ValueError as exc:
        print_error(f"Error: {exc}")
        sys.exit(1)


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
    model: type[Any] | None = None,
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
    parsed_filters = parse_filter_args(filter_args, model=model)
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
