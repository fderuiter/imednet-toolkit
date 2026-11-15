"""CLI argument parsing utilities."""

from __future__ import annotations

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
