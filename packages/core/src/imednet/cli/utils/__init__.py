"""CLI utility functions and classes."""

from .args import STUDY_KEY_ARG, parse_filter_args
from .commands import register_list_command
from .context import fetching_status, get_sdk
from .grid import InteractiveDataGrid, is_interactive_terminal, run_interactive_grid
from .output import (
    _STATUS_COLOR_MAP,
    _format_cell_value,
    _truncate,
    console,
    display_list,
    print_error,
)

__all__ = [
    "STUDY_KEY_ARG",
    "_STATUS_COLOR_MAP",
    "InteractiveDataGrid",
    "_format_cell_value",
    "_truncate",
    "console",
    "display_list",
    "fetching_status",
    "get_sdk",
    "is_interactive_terminal",
    "parse_filter_args",
    "print_error",
    "register_list_command",
    "run_interactive_grid",
]
