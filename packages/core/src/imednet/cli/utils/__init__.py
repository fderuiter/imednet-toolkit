"""CLI utility functions and classes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .args import STUDY_KEY_ARG, add_no_interactive, add_output_format, parse_filter_args
from .commands import register_list_command
from .context import fetching_status, get_sdk
from .output import (
    _HC_STATUS_COLOR_MAP,
    _STATUS_COLOR_MAP,
    Console,
    Status,
    _format_cell_value,
    _truncate,
    clear_line,
    console,
    display_list,
    emit_json,
    format_job_progress,
    print_error,
)
from .terminal import (
    NO_COLOR_ENV,
    NON_INTERACTIVE_ENV,
    OUTPUT_FORMAT_ENV,
    is_dumb_terminal,
    is_json_output_mode,
    is_non_interactive,
    is_tty,
    supports_ansi_color,
    supports_fullscreen_ui,
    supports_interactive_stdin,
    supports_live_status,
)

if TYPE_CHECKING:
    from .grid import InteractiveDataGrid, is_interactive_terminal, run_interactive_grid

__all__ = [
    "NON_INTERACTIVE_ENV",
    "NO_COLOR_ENV",
    "OUTPUT_FORMAT_ENV",
    "STUDY_KEY_ARG",
    "_HC_STATUS_COLOR_MAP",
    "_STATUS_COLOR_MAP",
    "Console",
    "InteractiveDataGrid",
    "Status",
    "_format_cell_value",
    "_truncate",
    "add_no_interactive",
    "add_output_format",
    "clear_line",
    "console",
    "display_list",
    "emit_json",
    "fetching_status",
    "format_job_progress",
    "get_sdk",
    "is_dumb_terminal",
    "is_interactive_terminal",
    "is_json_output_mode",
    "is_non_interactive",
    "is_tty",
    "parse_filter_args",
    "print_error",
    "register_list_command",
    "run_interactive_grid",
    "supports_ansi_color",
    "supports_fullscreen_ui",
    "supports_interactive_stdin",
    "supports_live_status",
]


def __getattr__(name: str) -> Any:
    """Lazily load interactive grid symbols so standard CLI startup avoids importing curses."""
    if name in ("InteractiveDataGrid", "is_interactive_terminal", "run_interactive_grid"):
        try:
            import importlib

            grid_mod = importlib.import_module("imednet.cli.utils.grid")
            return getattr(grid_mod, name)
        except (ImportError, AttributeError):
            if name == "is_interactive_terminal":
                return lambda: False
            if name == "run_interactive_grid":
                return lambda *args, **kwargs: False
            raise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
