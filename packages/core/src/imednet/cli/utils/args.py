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


def add_output_format(parser: argparse.ArgumentParser) -> None:
    """Add the common opt-in machine response flag to a leaf read command."""
    parser.add_argument(
        "--format",
        dest="output_format",
        choices=("table", "json"),
        default="table",
        help="Response format: human table (default) or versioned JSON on stdout.",
    )
