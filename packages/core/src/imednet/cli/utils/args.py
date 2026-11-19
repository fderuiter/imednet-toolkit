"""CLI argument parsing utilities."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .output import print_error

STUDY_KEY_ARG = "The key identifying the study."


def parse_filter_args(
    filter_args: list[str] | None,
    *,
    model: type[Any] | None = None,
) -> dict[str, Any] | None:
    """Parse CLI filter strings into a dictionary via the shared SPI filter parser."""
    if not filter_args:
        return None
    from imednet.spi.utils import parse_filter_expressions

    try:
        return parse_filter_expressions(filter_args, model=model)
    except ValueError as exc:
        print_error(f"Error: {exc}")
        sys.exit(1)


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
        default="table",
        help="Response format: human table (default) or versioned JSON on stdout.",
    )
    add_no_interactive(parser)


def add_filter_arg(
    parser: argparse.ArgumentParser,
    *,
    dest: str = "filters",
    example: str = "status=Open",
) -> None:
    """Add repeatable ``-f`` / ``--filter`` argument to a command parser."""
    parser.add_argument(
        "-f",
        "--filter",
        action="append",
        dest=dest,
        default=None,
        help=f"Filter criteria (e.g., '{example}'). Repeat for multiple filters.",
    )


def add_sort_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--sort`` query argument to a command parser."""
    parser.add_argument(
        "--sort",
        default=None,
        help="Sort expression (e.g., 'dateCreated,desc').",
    )


def add_record_data_filter_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--record-data-filter`` query argument to a record/export parser."""
    parser.add_argument(
        "--record-data-filter",
        dest="record_data_filter",
        default=None,
        help="Filter expression on clinical record data variables (e.g., 'AGE>18').",
    )


def add_list_query_args(
    parser: argparse.ArgumentParser,
    *,
    include_filter: bool = True,
    include_sort: bool = True,
    include_record_data_filter: bool = False,
    filter_dest: str = "filters",
    filter_example: str = "status=Open",
) -> None:
    """Attach standard ``--filter``, ``--record-data-filter``, and ``--sort`` flags."""
    if include_filter:
        add_filter_arg(parser, dest=filter_dest, example=filter_example)
    if include_record_data_filter:
        add_record_data_filter_arg(parser)
    if include_sort:
        add_sort_arg(parser)


def build_list_query_kwargs(
    *,
    filters: list[str] | None = None,
    sort: str | None = None,
    record_data_filter: str | None = None,
    model: type[Any] | None = None,
) -> dict[str, Any]:
    """Build validated endpoint keyword arguments from CLI filter/sort options."""
    query_kwargs: dict[str, Any] = {}
    parsed_filters = parse_filter_args(filters, model=model)
    if parsed_filters:
        query_kwargs.update(parsed_filters)

    if record_data_filter is not None:
        from imednet.utils.filters import validate_record_filter

        stripped_rdf = record_data_filter.strip()
        if not stripped_rdf:
            print_error("Error: --record-data-filter cannot be empty.")
            sys.exit(1)
        try:
            validate_record_filter(stripped_rdf)
        except ValueError as exc:
            print_error(f"Error: {exc}")
            sys.exit(1)
        query_kwargs["record_data_filter"] = stripped_rdf

    if sort is not None:
        stripped_sort = sort.strip()
        if not stripped_sort or any(ch.isspace() for ch in stripped_sort):
            print_error(
                f"Error: Invalid --sort value '{sort}'. "
                "Use 'property' or 'property,asc|desc' without whitespace."
            )
            sys.exit(1)
        query_kwargs["sort"] = stripped_sort

    return query_kwargs
