"""Service Provider Interface (SPI) for CLI extensions."""

from typing import TYPE_CHECKING, Any

from imednet.cli.decorators import with_sdk
from imednet.cli.utils import (
    NO_COLOR_ENV,
    NON_INTERACTIVE_ENV,
    OUTPUT_FORMAT_ENV,
    STUDY_KEY_ARG,
    Console,
    Status,
    add_no_interactive,
    clear_line,
    console,
    fetching_status,
    format_job_progress,
    is_dumb_terminal,
    is_json_output_mode,
    is_non_interactive,
    is_tty,
    parse_filter_args,
    print_error,
    supports_ansi_color,
    supports_fullscreen_ui,
    supports_interactive_stdin,
    supports_live_status,
)

if TYPE_CHECKING:
    from imednet.cli.utils import is_interactive_terminal

__all__ = [
    "NON_INTERACTIVE_ENV",
    "NO_COLOR_ENV",
    "OUTPUT_FORMAT_ENV",
    "STUDY_KEY_ARG",
    "Console",
    "Status",
    "add_no_interactive",
    "clear_line",
    "console",
    "fetching_status",
    "format_job_progress",
    "is_dumb_terminal",
    "is_interactive_terminal",
    "is_json_output_mode",
    "is_non_interactive",
    "is_tty",
    "parse_filter_args",
    "print_error",
    "supports_ansi_color",
    "supports_fullscreen_ui",
    "supports_interactive_stdin",
    "supports_live_status",
    "with_sdk",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve interactive terminal helpers from ``imednet.cli.utils``."""
    if name == "is_interactive_terminal":
        from imednet.cli.utils import is_interactive_terminal

        return is_interactive_terminal
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return exported module attributes including lazy SPI helpers."""
    return sorted(set(globals()) | set(__all__))
