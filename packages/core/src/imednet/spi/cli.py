"""Service Provider Interface (SPI) for CLI extensions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imednet.cli.decorators import with_sdk
from imednet.cli.utils.args import STUDY_KEY_ARG, add_output_format, parse_filter_args
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

if TYPE_CHECKING:
    from imednet.cli.utils.grid import is_interactive_terminal, run_interactive_grid

__all__ = [
    "STUDY_KEY_ARG",
    "Console",
    "Status",
    "add_output_format",
    "clear_line",
    "console",
    "display_list",
    "emit_json",
    "fetching_status",
    "format_job_progress",
    "is_interactive_terminal",
    "parse_filter_args",
    "print_error",
    "run_interactive_grid",
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
