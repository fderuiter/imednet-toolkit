"""CLI argument parsing utilities."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .output import print_error

STUDY_KEY_ARG = "The key identifying the study."


def parse_filter_args(filter_args: list[str] | None) -> dict[str, Any] | None:
    """Parse a list of ``key=value`` strings into a dictionary."""
    if not filter_args:
        return None
    filter_dict: dict[str, str | bool | int] = {}
    for arg in filter_args:
        if "=" not in arg or arg.startswith("=") or arg.endswith("="):
            print_error(
                f"Error: Invalid filter format: '{arg}'. Use 'key=value' with non-empty key and value."
            )
            sys.exit(1)
        key, value = arg.split("=", 1)
        if not key.strip() or not value.strip():
            print_error(f"Error: Invalid filter format: '{arg}'. Key and value cannot be empty.")
            sys.exit(1)

        if value.lower() == "true":
            filter_dict[key.strip()] = True
        elif value.lower() == "false":
            filter_dict[key.strip()] = False
        elif value.isdigit():
            filter_dict[key.strip()] = int(value)
        else:
            filter_dict[key.strip()] = value
    return filter_dict


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
