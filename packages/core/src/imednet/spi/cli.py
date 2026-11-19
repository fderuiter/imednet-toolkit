"""Service Provider Interface (SPI) for CLI extensions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.cli.decorators import with_sdk
from imednet.cli.utils.args import (
    STUDY_KEY_ARG,
    add_no_interactive,
    add_output_format,
    parse_filter_args,
)
from imednet.cli.utils.context import fetching_status
from imednet.cli.utils.output import (
    Console,
    Status,
    clear_line,
    console,
    display_list,
    emit_json,
    format_job_progress,
    print_error,
)
from imednet.cli.utils.terminal import (
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
    from imednet.cli.utils.grid import is_interactive_terminal, run_interactive_grid

__all__ = [
    "NON_INTERACTIVE_ENV",
    "NO_COLOR_ENV",
    "OUTPUT_FORMAT_ENV",
    "STUDY_KEY_ARG",
    "Console",
    "Status",
    "add_no_interactive",
    "add_output_format",
    "clear_line",
    "console",
    "display_list",
    "emit_json",
    "fetching_status",
    "format_job_progress",
    "is_dumb_terminal",
    "is_interactive_terminal",
    "is_json_output_mode",
    "is_non_interactive",
    "is_tty",
    "parse_filter_args",
    "print_error",
    "run_interactive_grid",
    "supports_ansi_color",
    "supports_fullscreen_ui",
    "supports_interactive_stdin",
    "supports_live_status",
    "with_sdk",
]


def __getattr__(name: str) -> Any:
    """Lazily load interactive grid symbols so headless startup avoids importing curses."""
    if name in ("is_interactive_terminal", "run_interactive_grid"):
        from imednet.cli import utils as _cli_utils

        return getattr(_cli_utils, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return module attributes including lazily loaded symbols."""
    return sorted(list(globals().keys()) + __all__)
