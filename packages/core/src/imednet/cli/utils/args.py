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
